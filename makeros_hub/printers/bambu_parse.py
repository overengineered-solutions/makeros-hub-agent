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
import re
from typing import Any

# A Bambu tray_color is 8 hex chars (RRGGBBAA), no leading '#'. Anything else is
# garbage we omit so the cloud only ever stores a renderable swatch value.
_HEX8_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_RAW_AMS_UNIT_MAX_BYTES = 8 * 1024
# Long, unambiguous tokens matched as substrings; short/ambiguous ones (sn, ip,
# mac, pass, key, ...) matched ONLY as whole key-segments, so benign AMS keys like
# "snapshot"/"recipe"/"ipcam" aren't collaterally dropped from the raw passthrough.
_SECRETISH_SUBSTRINGS = (
    "serial", "password", "passwd", "secret", "accesscode", "access_code", "apikey", "api_key", "token"
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
    "FAILED": "error",
    "OFFLINE": "offline",
    "UNKNOWN": "idle",
}


def map_activity_state(gcode_state: Any) -> str:
    if not isinstance(gcode_state, str):
        return "idle"
    return _GCODE_STATE.get(gcode_state.upper(), "idle")


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
    return {
        k: tray_out[k]
        for k in ("material", "productName", "colorHex", "remainPct", "tagUid")
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
    for unit_idx, unit in enumerate(units_raw):
        if not isinstance(unit, dict):
            continue

        unit_id = _to_int(unit.get("id"))
        unit_out: dict[str, Any] = {"unit": unit_idx if unit_id is None else unit_id, "trays": []}

        humidity = _num(unit.get("humidity"))
        if humidity is not None:
            # Bound to 0-100 (the cloud DTO mirrors this). NOTE: some Bambu AMS
            # report humidity as a 1-5 dryness LEVEL rather than a %; the v0.8.0
            # cloud renders "{n}%" — confirm against the live AMS 2 Pro value and
            # adjust the label if it's a level (tracked).
            unit_out["humidity"] = max(0.0, min(100.0, float(humidity)))

        temp = _num(unit.get("temp"))
        if temp is not None:
            unit_out["temp"] = float(temp)

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
    if tray_now is None or tray_now < 0 or tray_now > 63 or tray_now in (254, 255):
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
    error_reason: str | None = None,
) -> dict:
    """Build the wire DTO the cloud heartbeat expects (PrinterStatusDTO).

    `merged` is the deep-merged report state (telemetry under merged['print']).
    Only set keys are included — the cloud DTO is strict() and rejects unknown
    keys AND rejects an explicit null for an optional number, so omit-when-absent
    is required. NEVER include the serial / IP / access code (telemetry only).
    """
    out: dict[str, Any] = {"printerId": printer_id, "connectionState": connection_state}
    if connection_state == "error" and error_reason:
        out["errorReason"] = error_reason

    print_obj = merged.get("print") if isinstance(merged.get("print"), dict) else {}

    # Activity state only meaningful once we're actually connected.
    if connection_state == "connected":
        out["state"] = map_activity_state(print_obj.get("gcode_state"))

    pct = _num(print_obj.get("mc_percent"))
    if pct is not None:
        out["progressPct"] = max(0.0, min(100.0, pct))

    nozzle = _num(print_obj.get("nozzle_temper"))
    if nozzle is not None:
        out["nozzleTempC"] = nozzle
    bed = _num(print_obj.get("bed_temper"))
    if bed is not None:
        out["bedTempC"] = bed

    name = print_obj.get("subtask_name") or print_obj.get("gcode_file")
    if isinstance(name, str) and name.strip():
        out["jobName"] = name.strip()[:300]

    eta = _num(print_obj.get("mc_remaining_time"))  # MINUTES
    if eta is not None:
        out["etaMinutes"] = int(eta)

    if connection_state == "connected":
        ams = build_ams(print_obj)
        if ams:
            out["ams"] = ams
        vt_tray = build_vt_tray(print_obj)
        if vt_tray:
            out["vtTray"] = vt_tray
        active = build_active_tray(print_obj)
        if active is not None:
            out["amsActiveTray"] = active
        hms = build_hms(print_obj)
        if hms:
            out["hms"] = hms
        pe = _to_int(print_obj.get("print_error"))
        if pe:
            out["printError"] = pe

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
