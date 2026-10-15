"""Pure Bambu LAN report parsing — NO paho, NO I/O, so it is fully unit-testable
on a dev box without a printer.

Bambu printers in LAN/Developer mode stream their state on the MQTT topic
`device/<serial>/report`. Messages are PARTIAL DELTAS, not full snapshots: after
the initial `pushall` snapshot the printer sends small objects with only changed
keys. We keep a merged state dict and deep-merge each frame into it, then read
the handful of telemetry fields we care about out of the merged `print` object.

We deliberately own this parse (rather than depend on pybambu/bambulabs_api) so a
firmware field rename degrades to a missing-optional instead of crashing the
agent, and so we can emit the repo's shape_observed / iterated observability.

Field references (verified against ha-bambulab/pybambu + bambulabs_api):
  print.gcode_state      job lifecycle: IDLE/PREPARE/RUNNING/PAUSE/FINISH/FAILED
  print.mc_percent       0-100 progress (the reliable signal; never bill off grams)
  print.mc_remaining_time MINUTES remaining (NOT seconds)
  print.subtask_name     current job/plate name; fallback print.gcode_file
  print.nozzle_temper    nozzle temp C ; print.bed_temper  bed temp C
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

# A Bambu tray_color is 8 hex chars (RRGGBBAA), no leading '#'. Anything else is
# garbage we omit so the cloud only ever stores a renderable swatch value.
_HEX8_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_RAW_AMS_UNIT_MAX_BYTES = 8 * 1024
# Long, unambiguous tokens matched as substrings; short/ambiguous ones (sn, ip,
# mac, pass, key, ...) matched ONLY as whole key-segments, so benign AMS keys like
# "snapshot"/"recipe"/"ipcam" aren't collaterally dropped from the raw passthrough.
_SECRETISH_SUBSTRINGS = (
    "serial", "password", "passwd", "secret", "accesscode", "access_code", "apikey", "api_key", "token",
    # AMS/chip hardware ids leak into the raw passthrough otherwise (low-stakes,
    # but the doctrine is no hardware serials in stored telemetry).
    "ams_id", "chip_id",
)
_SECRETISH_SEGMENTS = frozenset({"sn", "ip", "mac", "pass", "key", "access", "auth", "cert"})

# Bambu gcode_state -> our normalized printer activity state. The cloud column
# enum is idle|printing|paused|error|offline.
_GCODE_STATE = {
    "RUNNING": "printing",
    "PAUSE": "paused",
    "PREPARE": "idle",
    "SLICING": "idle",
    "INIT": "idle",
    "IDLE": "idle",
    "FINISH": "idle",  # completed OK — printer is now idle/awaiting clear
    # A failed/cancelled job leaves the printer FREE again, same as FINISH — a
    # failed *job* is not a printer *fault* (real faults surface via HMS, not
    # gcode_state). Parking it in "error" mislabels a ready printer as broken
    # (this is why SimplyPrint showed it ready while we showed it errored). The
    # failure is recorded as a job event; bed-occupancy safety for auto-routing
    # is gated by the operator bed-clear ack in the cloud, not here.
    "FAILED": "idle",
    "OFFLINE": "offline",
    "UNKNOWN": "idle",
}


def map_activity_state(gcode_state: Any) -> str:
    if not isinstance(gcode_state, str):
        return "idle"
    return _GCODE_STATE.get(gcode_state.upper(), "idle")


# v0.70 (docs/telemetry-v1.md): the stage, layers, targets and elapsed time that were in the merged report all along
# and never read; the operator, 2026-10-15: "is there a way to give more granular information, like maybe its heating
# up etc. Maybe we show current temp vs target temp".
# A print is under way in these gcode_states (the same set as manager.BUSY_GCODE_STATES; not imported, this module
# stays pure). stg_cur 0 means "printing" and is only a stage while one of them holds: at boot the printer reports 0
# where it should say idle (ha-bambulab remaps it), and after a print it keeps the last value.
_ACTIVE_GCODE_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE"})
# X1 reports -1 when idle, P1/A1 report 255 (ha-bambulab const.py).
_STAGE_IDLE_SENTINELS = frozenset({-1, 255})
_STAGE_CODE_MAX = 1000
# A packed device.*.info.temp word above this is not a temperature (65535 is the firmware's "none" in sibling fields).
_TEMP_SANE_MAX_C = 500.0


def _packed_temp(v: Any) -> tuple[float, float] | None:
    """P2S/H2D firmware packs device.bed.info.temp, device.ctc.info.temp and device.extruder.info[].temp as one int:
    current in the low 16 bits, target in the high 16 (ha-bambulab models.py: 6553700 -> 100 / 100). The H2D fixture's
    `temp: 27` is 27 / 0 under the same rule."""
    n = _to_int(v)
    if n is None or n < 0 or n > 0xFFFFFFFF:
        return None
    current, target = float(n & 0xFFFF), float(n >> 16)
    if current > _TEMP_SANE_MAX_C or target > _TEMP_SANE_MAX_C:
        return None
    return current, target


def _packed_device_temp(device: dict, part: str) -> tuple[float, float] | None:
    block = device.get(part)
    info = block.get("info") if isinstance(block, dict) else None
    return _packed_temp(info.get("temp")) if isinstance(info, dict) else None


def _packed_extruder_temp(device: dict) -> tuple[float, float] | None:
    """The active extruder's packed temp (device.extruder.state: low 4 bits the count, the next 4 the active one); the
    first valid entry when the active one is not listed. One nozzle on the wire, as the flat nozzle_temper is."""
    extruders = device.get("extruder")
    if not isinstance(extruders, dict) or not isinstance(extruders.get("info"), list):
        return None
    state = _to_int(extruders.get("state"))
    active = (state >> 4) & 15 if state is not None and state >= 0 else 0
    fallback: tuple[float, float] | None = None
    for ext in extruders["info"]:
        if not isinstance(ext, dict):
            continue
        packed = _packed_temp(ext.get("temp"))
        if packed is None:
            continue
        if _to_int(ext.get("id")) == active:
            return packed
        fallback = fallback or packed
    return fallback


def build_temperatures(print_obj: dict) -> dict[str, Any]:
    """nozzleTempC/nozzleTargetC, bedTempC/bedTargetC, chamberC from the flat floats (nozzle_temper,
    nozzle_target_temper, bed_temper, bed_target_temper, chamber_temper: X1C/P1/A1 and older firmware), each falling
    back to the packed device.*.info.temp layout (P2S/H2D) when the flat field is absent. A firmware update can flip a
    machine from one layout to the other, so both are read field by field."""
    nozzle, nozzle_target = _num(print_obj.get("nozzle_temper")), _num(print_obj.get("nozzle_target_temper"))
    bed, bed_target = _num(print_obj.get("bed_temper")), _num(print_obj.get("bed_target_temper"))
    chamber = _num(print_obj.get("chamber_temper"))
    device = print_obj.get("device")
    if isinstance(device, dict):
        packed_nozzle = _packed_extruder_temp(device)
        if packed_nozzle is not None:
            nozzle = packed_nozzle[0] if nozzle is None else nozzle
            nozzle_target = packed_nozzle[1] if nozzle_target is None else nozzle_target
        packed_bed = _packed_device_temp(device, "bed")
        if packed_bed is not None:
            bed = packed_bed[0] if bed is None else bed
            bed_target = packed_bed[1] if bed_target is None else bed_target
        packed_chamber = _packed_device_temp(device, "ctc")
        if packed_chamber is not None and chamber is None:
            chamber = packed_chamber[0]   # the high word is the chamber target; the v1 contract has no key for it
    out: dict[str, Any] = {}
    for key, value in (("nozzleTempC", nozzle), ("nozzleTargetC", nozzle_target), ("bedTempC", bed),
                       ("bedTargetC", bed_target), ("chamberC", chamber)):
        if value is not None:
            out[key] = value
    return out


def build_stage(print_obj: dict) -> dict[str, Any]:
    """stageCode (stg_cur) and stageSecondsLeft (stg_cd, the countdown of that stage). The idle sentinels are omitted,
    and 0 ("printing") only counts while a print is under way; the site maps codes to words (7 heating nozzle, 2 heatbed
    preheating, 13 homing, ...) and shows unknown ones as working."""
    out: dict[str, Any] = {}
    code = _to_int(print_obj.get("stg_cur"))
    gcode_state = print_obj.get("gcode_state")
    active = isinstance(gcode_state, str) and gcode_state.upper() in _ACTIVE_GCODE_STATES
    if code is None or code in _STAGE_IDLE_SENTINELS or not 0 <= code <= _STAGE_CODE_MAX or (code == 0 and not active):
        return out
    out["stageCode"] = code
    seconds_left = _to_int(print_obj.get("stg_cd"))
    if seconds_left is not None and seconds_left >= 0:
        out["stageSecondsLeft"] = seconds_left
    return out


def build_layer(print_obj: dict) -> list[int] | None:
    """[layer_num, total_layer_num] when both are whole numbers and a total is known (0/0 before a print has one)."""
    current, total = _to_int(print_obj.get("layer_num")), _to_int(print_obj.get("total_layer_num"))
    if current is None or total is None or total <= 0 or current < 0:
        return None
    return [current, total]


def build_elapsed_sec(print_obj: dict, now: float) -> int | None:
    """Seconds since gcode_start_time (unix seconds, a string on the wire) while a print is under way; None when the
    printer gave none or a time in the future."""
    gcode_state = print_obj.get("gcode_state")
    if not (isinstance(gcode_state, str) and gcode_state.upper() in _ACTIVE_GCODE_STATES):
        return None
    start = _to_int(print_obj.get("gcode_start_time"))
    if start is None or start <= 0 or start > now + 60:
        return None
    return max(0, int(now - start))


def merge_report(state: dict, delta: dict) -> dict:
    """Deep-merge a report delta into the running state dict, in place.

    Recursive on nested dicts (so a partial `print` delta updates only its
    changed keys); scalar/list values overwrite. Returns `state` for chaining.
    """
    for k, v in delta.items():
        if isinstance(v, dict) and isinstance(state.get(k), dict):
            merge_report(state[k], v)
        else:
            state[k] = v
    return state


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # Only a whole-number float is a real int; "1.9"-as-float, inf and nan
        # are not (is_integer() is False for inf/nan).
        return int(v) if v.is_integer() else None
    if isinstance(v, str):
        # Strict integer parse: int("1.9")/int("1e3") raise, so malformed
        # numerics never silently truncate into a valid-looking id.
        try:
            return int(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def _num_to_int(v: Any) -> int | None:
    num = _num(v)
    if num is None:
        return None
    try:
        return int(num)
    except (OverflowError, ValueError):
        return None


def _is_secretish_key(key: Any) -> bool:
    key_s = (key if isinstance(key, str) else str(key)).lower()
    if any(tok in key_s for tok in _SECRETISH_SUBSTRINGS):
        return True
    return any(seg in _SECRETISH_SEGMENTS for seg in re.split(r"[^a-z0-9]+", key_s))


def _drop_secretish_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_secretish_keys(v) for k, v in value.items() if not _is_secretish_key(k)}
    if isinstance(value, list):
        return [_drop_secretish_keys(v) for v in value]
    return value


def _raw_ams_unit(unit: dict) -> dict | None:
    raw = _drop_secretish_keys(unit)
    try:
        encoded = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(encoded) > _RAW_AMS_UNIT_MAX_BYTES:
        return None
    return raw


def _add_tray_identity_fields(tray_out: dict[str, Any], tray: dict) -> None:
    material = tray.get("tray_type")
    if isinstance(material, str) and material.strip():
        tray_out["material"] = material.strip()[:64]

    product_name = tray.get("tray_sub_brands")
    if isinstance(product_name, str) and product_name.strip():
        tray_out["productName"] = product_name.strip()[:64]

    filament_id = tray.get("tray_info_idx")
    if isinstance(filament_id, str) and filament_id.strip():
        tray_out["filamentId"] = filament_id.strip()[:32]

    color = tray.get("tray_color")
    if isinstance(color, str):
        hex_color = color.strip().lstrip("#")
        if _HEX8_RE.match(hex_color):
            tray_out["colorHex"] = hex_color.upper()

    colors_raw = tray.get("cols")
    if isinstance(colors_raw, list):
        colors: list[str] = []
        for item in colors_raw:
            if not isinstance(item, str):
                continue
            hex_color = item.strip().lstrip("#")
            if _HEX8_RE.match(hex_color):
                colors.append(hex_color.upper())
                if len(colors) >= 8:
                    break
        if colors:
            tray_out["colors"] = colors

    remain = _num(tray.get("remain"))
    if remain is not None:
        tray_out["remainPct"] = max(0.0, min(100.0, remain))

    tag_uid = tray.get("tag_uid")
    if isinstance(tag_uid, str) and tag_uid.strip():
        tray_out["tagUid"] = tag_uid.strip()[:64]

    nozzle_temp_min = _num_to_int(tray.get("nozzle_temp_min"))
    if nozzle_temp_min is not None:
        tray_out["nozzleTempMin"] = nozzle_temp_min

    nozzle_temp_max = _num_to_int(tray.get("nozzle_temp_max"))
    if nozzle_temp_max is not None:
        tray_out["nozzleTempMax"] = nozzle_temp_max


def _build_ams_tray(tray: dict, slot: int) -> dict[str, Any]:
    tray_out: dict[str, Any] = {"slot": slot}
    _add_tray_identity_fields(tray_out, tray)
    return tray_out


def build_vt_tray(print_obj: dict) -> dict | None:
    """External/A1-direct spool state from print.vt_tray, or None when empty."""
    if not isinstance(print_obj, dict):
        return None
    vt_tray = print_obj.get("vt_tray")
    if not isinstance(vt_tray, dict) or not vt_tray:
        return None

    tray_out: dict[str, Any] = {}
    _add_tray_identity_fields(tray_out, vt_tray)
    # filamentId included (v0.50): the VP pool dedupes spools by material|filamentId|color, so the
    # same Generic PLA on an AMS and on an external holder must key identically.
    return {
        k: tray_out[k]
        for k in ("material", "productName", "filamentId", "colorHex", "remainPct", "tagUid")
        if k in tray_out
    } or None


def build_ams(print_obj: dict) -> list[dict] | None:
    """Per-AMS-unit filament state for the cloud DTO. None when no AMS present.
    Empty {} trays are skipped. Omit keys that are absent, like
    normalize_status. Unit raw passthrough is scrubbed for secret-ish keys."""
    if not isinstance(print_obj, dict):
        return None
    ams_obj = print_obj.get("ams")
    if not isinstance(ams_obj, dict):
        return None
    units_raw = ams_obj.get("ams")
    if not isinstance(units_raw, list):
        return None

    units: list[dict] = []
    emitted_unit_idx = 0
    for unit in units_raw:
        if not isinstance(unit, dict):
            continue

        # Use a CONTIGUOUS enumeration index (0..n-1 over ACCEPTED units), NOT
        # the raw Bambu unit id and NOT the raw array position. Standard AMS units
        # are 0-3, but the H2D's second unit (and the AMS-HT) report id 128+,
        # which exceeds the cloud DTO's unit cap (0-15) and would drop the WHOLE
        # report → the printer stuck `pending`. Counting only accepted (dict)
        # units keeps the index gap-free even if a malformed report carries
        # leading garbage, so the downstream `unit*4+slot` mapping stays sane. The
        # raw id stays in `raw`. (Per-nozzle AMS→nozzle routing for the H2D is
        # handled by the dual-pool VP work, not this telemetry index.)
        unit_out: dict[str, Any] = {"unit": emitted_unit_idx, "trays": []}
        emitted_unit_idx += 1
        # BambuStudio DevFilaSystem::ParseAmsInfo: info is HEX, not decimal.
        # Missing/unknown bindings must not make a dual-extruder spool look reachable.
        info = unit.get("info")
        if isinstance(info, str) and re.fullmatch(r"[0-9a-fA-F]{1,16}", info):
            bits = int(info, 16)
            ams_type = {1: "ams", 2: "ams_lite", 3: "ams_2_pro", 4: "ams_ht"}.get(bits & 15)
            if ams_type:
                unit_out["amsType"] = ams_type
            extruder = (bits >> 8) & 15
            if extruder in (0, 1):  # 0xE needs switcher topology; never guess it is the right nozzle.
                unit_out["extruderId"] = extruder

        # `humidity` is a 1-5 dryness LEVEL (verified live on the AMS 2 Pro);
        # `humidity_raw` is the actual percentage (e.g. "44"). Capture both — the
        # cloud prefers humidityRaw for the "% " readout and keeps the level too.
        humidity = _num(unit.get("humidity"))
        if humidity is not None:
            unit_out["humidity"] = max(0.0, min(5.0, float(humidity)))

        humidity_raw = _num(unit.get("humidity_raw"))
        if humidity_raw is not None:
            unit_out["humidityRaw"] = max(0.0, min(100.0, float(humidity_raw)))

        temp = _num(unit.get("temp"))
        if temp is not None:
            unit_out["temp"] = float(temp)

        # Drying timer: 0 = not drying, >0 = actively drying (minutes remaining).
        dry_time = _num(unit.get("dry_time"))
        if dry_time is not None:
            unit_out["dryTime"] = max(0, int(dry_time))

        trays_raw = unit.get("tray")
        if isinstance(trays_raw, list):
            for slot_idx, tray in enumerate(trays_raw):
                # A physical AMS unit has 4 trays (slots 0-3). The downstream
                # ams_mapping is unit*4+slot, so never emit a slot outside 0-3
                # even if a malformed report carries a longer tray array.
                if slot_idx > 3:
                    break
                if not isinstance(tray, dict) or not tray:
                    continue
                unit_out["trays"].append(_build_ams_tray(tray, slot_idx))

        raw = _raw_ams_unit(unit)
        if raw is not None:
            unit_out["raw"] = raw

        units.append(unit_out)

    return units or None


def build_nozzle_hardware(print_obj: dict) -> dict[str, Any]:
    """Selected observed device fields only; physical extruder 0=right, 1=left.

    BambuStudio DeviceCore/DevExtruderSystem.cpp ParseV2_0 and DevNozzleSystem.cpp ParseV2_0.
    A missing nozzle/binding is unknown, never a synthetic 0.4mm or a default right nozzle.
    """
    device = print_obj.get("device")
    if not isinstance(device, dict):
        return {}
    out: dict[str, Any] = {}
    extruders = device.get("extruder")
    bindings: dict[int, set[int]] = {}
    if isinstance(extruders, dict):
        state = _to_int(extruders.get("state"))
        if state is not None and state >= 0 and 1 <= (state & 15) <= 2:
            out["extruderCount"] = state & 15
        for ext in extruders.get("info", []) if isinstance(extruders.get("info"), list) else []:
            if not isinstance(ext, dict):
                continue
            eid, nid = _to_int(ext.get("id")), _to_int(ext.get("hnow"))
            if eid in (0, 1) and nid is not None and 0 <= nid <= 15:
                bindings.setdefault(nid, set()).add(eid)
    nozzle = device.get("nozzle")
    if not isinstance(nozzle, dict) or not isinstance(nozzle.get("info"), list):
        return out
    nozzles = []
    seen: set[int] = set()
    exist = _to_int(nozzle.get("exist"))
    for item in nozzle["info"]:
        if not isinstance(item, dict):
            continue
        nid, diameter, kind = _to_int(item.get("id")), _num(item.get("diameter")), item.get("type")
        if nid is None or not 0 <= nid <= 15 or nid in seen:
            continue
        if exist is not None and (exist < 0 or not exist & (1 << nid)):
            continue
        if diameter is None or not math.isfinite(diameter) or not 0 < diameter <= 2:
            continue
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", kind):
            continue
        seen.add(nid)
        entry: dict[str, Any] = {"id": nid, "diameterMm": diameter, "type": kind}
        if len(bindings.get(nid, set())) == 1:
            entry["extruderId"] = next(iter(bindings[nid]))
        nozzles.append(entry)
    if nozzles:
        out["nozzles"] = nozzles
    return out


def _has_material(tray: dict) -> bool:
    material = tray.get("tray_type")
    return isinstance(material, str) and bool(material.strip())


def _tray_present(tray: dict, *, empty_when_unflagged: bool = False) -> bool:
    """Is a spool physically in this tray? Current firmware reports a `state` bitfield; the rule is
    pybambu utils.ams_tray_spool_loaded verbatim (SPOOL=0x01 must be set; legacy values <=3 count as
    loaded only at 3; otherwise STEADY=0x08 must be set). Without `state`: an EMPTY AMS slot is a bare
    {"id"} (OpenBambuAPI pushall sample), so any richer object holds a spool — but an empty EXTERNAL
    holder reports a full field set (tray_type "", color 00000000, ...), so vt_tray without `state`
    is never called present (`empty_when_unflagged`)."""
    state = _to_int(tray.get("state"))
    if state is None:
        return False if empty_when_unflagged else any(k != "id" for k in tray)
    if not state & 0x01:
        return False
    if state <= 3:
        return state == 3
    return bool(state & 0x08)


def build_unidentified_spools(print_obj: dict) -> list[dict] | None:
    """Trays holding a spool the printer could not identify (no tray_type: a non-RFID spool nobody has
    typed on the screen/Handy yet) — invisible to the VP pool, so surface them for The Floor. Same
    unit/slot enumeration as build_ams (CONTIGUOUS unit index, slots 0-3); the external spool is
    slot 254 (Bambu's own id for vt_tray). None when there are none."""
    if not isinstance(print_obj, dict):
        return None
    out: list[dict] = []
    ams_obj = print_obj.get("ams")
    units_raw = ams_obj.get("ams") if isinstance(ams_obj, dict) else None
    unit_idx = 0
    for unit in units_raw if isinstance(units_raw, list) else []:
        if not isinstance(unit, dict):
            continue
        trays_raw = unit.get("tray")
        for slot_idx, tray in enumerate(trays_raw if isinstance(trays_raw, list) else []):
            if slot_idx > 3:
                break
            if isinstance(tray, dict) and tray and _tray_present(tray) and not _has_material(tray):
                out.append({"unit": unit_idx, "slot": slot_idx})
        unit_idx += 1
    vt_tray = print_obj.get("vt_tray")
    if isinstance(vt_tray, dict) and _tray_present(vt_tray, empty_when_unflagged=True) and not _has_material(vt_tray):
        out.append({"slot": 254})
    return out or None


def build_active_tray(print_obj: dict) -> int | None:
    """The currently-selected AMS global tray index, or None for external(254)/
    none(255)/absent."""
    if not isinstance(print_obj, dict):
        return None
    ams_obj = print_obj.get("ams")
    if not isinstance(ams_obj, dict):
        return None
    tray_now = _to_int(ams_obj.get("tray_now"))
    # Sentinels: 254 = external spool, 255 = none. Bound to a sane global index
    # (16 AMS units * 4 slots) so a garbage value like -1 or 999 never escapes.
    # ams_id*4+slot: standard units 0..15, the AMS-HT / H2D second port (raw id 128+) → 512+ (audit 2026-08-30); 254/255 = external
    if tray_now is None or tray_now < 0 or tray_now > 1023 or tray_now in (254, 255):
        return None
    return tray_now


def build_hms(print_obj: dict) -> list[dict] | None:
    """List of {"attr": int, "code": int} from print.hms, or None when
    empty/absent."""
    if not isinstance(print_obj, dict):
        return None
    hms_raw = print_obj.get("hms")
    if not isinstance(hms_raw, list):
        return None
    hms: list[dict] = []
    for item in hms_raw:
        if not isinstance(item, dict):
            continue
        attr = _to_int(item.get("attr"))
        code = _to_int(item.get("code"))
        if attr is not None and code is not None:
            hms.append({"attr": attr, "code": code})
    return hms or None


def normalize_status(
    printer_id: str,
    merged: dict,
    *,
    connection_state: str,
    model: str | None,
    error_reason: str | None = None,
    now: float | None = None,
) -> dict:
    """Build the wire DTO the cloud heartbeat expects (PrinterStatusDTO).

    `model` is the config-down machine model (e.g. "A1 Mini") — the VP live-mirror scopes each
    Virtual Printer's filament pool by it (live_pool.scoped_statuses), so it must ride EVERY status:
    a REQUIRED keyword (None only when config-down carried no model; the key is then omitted).
    Additive keys (model, gcodeState, unidentifiedSpools, and the v0.70 stage/target/layer keys) are safe
    for pstation, whose heartbeat ingest hand-picks fields and ignores the rest.

    `merged` is the deep-merged report state (telemetry under merged['print']).
    Only set keys are included — the cloud DTO rejects an explicit null for an optional number, so
    omit-when-absent is required. NEVER include the serial / IP / access code (telemetry only).
    `now` (wall seconds) is only for elapsedSec; tests pass it, the adapter leaves it to the clock.
    """
    out: dict[str, Any] = {"printerId": printer_id, "connectionState": connection_state}
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()[:80]
    if connection_state == "error" and error_reason:
        out["errorReason"] = error_reason

    print_obj = merged.get("print") if isinstance(merged.get("print"), dict) else {}

    # Telemetry only while we're actually connected. v0.70: progress, temperatures, jobName and the ETA used to sit
    # outside this check, so the cached values of a printer that had gone away kept going out as if fresh.
    if connection_state == "connected":
        out["state"] = map_activity_state(print_obj.get("gcode_state"))
        # The RAW state too: `state` collapses FINISH/FAILED into idle, but a plate-clear gate needs
        # to tell IDLE (dismissed at the printer) from FINISH (part still on the bed).
        gcode_state = print_obj.get("gcode_state")
        if isinstance(gcode_state, str) and gcode_state.strip():
            out["gcodeState"] = gcode_state.strip()[:32]
        # PREPARE maps to idle above (the printer is between the member's send and its first move: download, heat); the
        # flag says so without changing what `state` has always meant to the site.
        out["preparing"] = isinstance(gcode_state, str) and gcode_state.strip().upper() == "PREPARE"

        pct = _num(print_obj.get("mc_percent"))
        if pct is not None:
            out["progressPct"] = max(0.0, min(100.0, pct))

        out.update(build_temperatures(print_obj))
        out.update(build_stage(print_obj))
        layer = build_layer(print_obj)
        if layer is not None:
            out["layer"] = layer
        elapsed = build_elapsed_sec(print_obj, time.time() if now is None else now)
        if elapsed is not None:
            out["elapsedSec"] = elapsed

        name = print_obj.get("subtask_name") or print_obj.get("gcode_file")
        if isinstance(name, str) and name.strip():
            out["jobName"] = name.strip()[:300]

        eta = _num(print_obj.get("mc_remaining_time"))  # MINUTES
        if eta is not None:
            out["etaMinutes"] = int(eta)

        out.update(build_nozzle_hardware(print_obj))
        ams = build_ams(print_obj)
        if ams:
            out["ams"] = ams
        vt_tray = build_vt_tray(print_obj)
        if vt_tray:
            out["vtTray"] = vt_tray
        active = build_active_tray(print_obj)
        if active is not None:
            out["amsActiveTray"] = active
        unidentified = build_unidentified_spools(print_obj)
        if unidentified:
            out["unidentifiedSpools"] = unidentified
        hms = build_hms(print_obj)
        if hms:
            out["hms"] = hms
        pe = _to_int(print_obj.get("print_error"))
        if pe:
            out["printError"] = pe

        # s_obj = the identify_ids the printer has been told to skip in the
        # current print (BambuStudio reads this same field back). Surface it so
        # the web UI can mark already-skipped objects; cap to the 64 skip-window
        # ceiling, ints only (the cloud scopes display to an active print).
        s_obj = print_obj.get("s_obj")
        if isinstance(s_obj, list):
            skipped = [o for o in s_obj if isinstance(o, int) and not isinstance(o, bool)]
            if skipped:
                out["skippedObjects"] = skipped[:64]

    return out


def summarize_shape(merged: dict) -> dict:
    """Redacted shape summary for the one-time `bambu.shape_observed` log — key
    names + array lengths only, never values (no serial/IP/material leak)."""
    print_obj = merged.get("print") if isinstance(merged.get("print"), dict) else {}
    array_lengths = {k: len(v) for k, v in print_obj.items() if isinstance(v, list)}
    ams_obj = print_obj.get("ams")
    units = ams_obj.get("ams") if isinstance(ams_obj, dict) else None
    if isinstance(units, list):
        array_lengths["ams.units"] = len(units)
        trays_total = 0
        trays_loaded = 0
        tray_keys: set[str] = set()
        for unit in units:
            if not isinstance(unit, dict) or not isinstance(unit.get("tray"), list):
                continue
            trays_total += len(unit["tray"])
            for tray in unit["tray"]:
                if not isinstance(tray, dict):
                    continue
                tray_keys.update(str(k) for k in tray.keys())
                material = tray.get("tray_type")
                if isinstance(material, str) and material.strip():
                    trays_loaded += 1
        array_lengths["ams.trays_total"] = trays_total
        array_lengths["ams.trays_loaded"] = trays_loaded
    else:
        tray_keys = set()
    out = {
        "topLevelKeys": sorted(merged.keys()),
        "printKeys": sorted(print_obj.keys()),
        "arrayLengths": array_lengths,
        "values": "[redacted]",
    }
    if tray_keys:
        out["amsTrayKeys"] = sorted(tray_keys)
    return out
