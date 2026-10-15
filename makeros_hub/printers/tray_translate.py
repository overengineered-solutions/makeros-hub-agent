"""Virtual → physical AMS tray translation (design B3, built 2026-08-30, hub v0.56).

The Virtual Printer shows a member a RANKED, DEDUPED pool of the model group's spools (vprinter/live_pool.py: virtual
slot i is the i-th key in sorted/ranked order), so the `ams_mapping` OrcaSlicer sends names VIRTUAL positions, never
physical trays. Replaying it verbatim (the v0.51 "identity") printed from whatever tray happened to sit at that index —
right only by coincidence, even on a single printer. BambuStudio DevMapping.cpp uses `ams_id * 4 + slot`
for four-slot AMS units and `ams_id + slot` for AMS HT in flat COMMAND mappings. The parallel ams_mapping2
uses explicit {ams_id, slot_id} objects. This is distinct from legacy telemetry tray-number helpers.

We translate by what the member actually chose — material + colour (+ Bambu filament id when it disambiguates) per
project filament — against the printer's LIVE trays, and REFUSE (never guess) when a required spool is not loaded:
the cloud puts the job back in the queue with the reason. Pure functions; the adapter feeds them its own DTO.
"""

from __future__ import annotations

import json
import re
from typing import Any

EXTERNAL_TRAY_IDS = frozenset({254, 255})


class TrayTranslationError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _norm_material(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_color(value: Any) -> str | None:
    s = str(value or "").strip().lstrip("#").upper()
    if len(s) in (6, 8) and all(c in "0123456789ABCDEF" for c in s):
        return s[:6] if len(s) == 8 and s.endswith("FF") else s
    return None


def cleared_tray(tray: dict[str, Any]) -> bool:
    """Alpha 00 is a transparent spool only when a real RFID tag confirms it is present."""
    color = _norm_color(tray.get("colorHex"))
    tag = tray.get("tagUid")
    tagged = isinstance(tag, str) and re.fullmatch(r"[0-9a-fA-F]{16}", tag) and any(c != "0" for c in tag)
    return bool(color and len(color) == 8 and color.endswith("00") and not tagged)


def _norm_fid(value: Any) -> str | None:
    s = str(value or "").strip().upper()
    return s or None


def _ams_id(value: Any) -> int | None:
    if isinstance(value, str):
        value = value.strip()
        if not 1 <= len(value) <= 3 or not value.isascii() or not value.isdigit():
            return None
        value = int(value)
    return value if type(value) is int and 0 <= value <= 253 else None


def live_trays(ams_units: Any, vt_tray: Any = None) -> list[dict[str, Any]]:
    """Addressable spools: [{tray_id, material, color, filament_id}] from the adapter DTO (bambu_parse.build_ams units,
    which keep the printer's raw unit id under `raw.id`). A unit whose raw id is missing cannot be addressed, so its
    trays are not offered (they show up in the refusal detail instead). The external spool rides as 254."""
    out: list[dict[str, Any]] = []
    for unit in ams_units or []:
        if not isinstance(unit, dict):
            continue
        raw = unit.get("raw") if isinstance(unit.get("raw"), dict) else {}
        ams_id = _ams_id(raw.get("id"))
        if ams_id is None:
            continue
        for tray in unit.get("trays") or []:
            if not isinstance(tray, dict):
                continue
            slot = tray.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 3:
                continue
            if unit.get("amsType") == "ams_ht" and slot != 0:
                continue
            material = _norm_material(tray.get("material"))
            if not material or cleared_tray(tray):
                continue
            # BambuStudio DevMapping.cpp: N3S/AMS HT uses ams_id+slot in the
            # legacy flat COMMAND map; mapping2 carries the explicit pair.
            if ams_id >= 128 and unit.get("amsType") != "ams_ht":
                continue  # old/unknown hardware cannot establish this address format
            entry = {"tray_id": ams_id + slot if unit.get("amsType") == "ams_ht" else ams_id * 4 + slot,
                     "ams_id": ams_id, "slot_id": slot,
                     "material": material, "color": _norm_color(tray.get("colorHex")),
                     "filament_id": _norm_fid(tray.get("filamentId"))}
            if unit.get("extruderId") in (0, 1) and not isinstance(unit.get("extruderId"), bool):
                entry["extruderId"] = unit["extruderId"]
            remain = _norm_remain(tray.get("remainPct"))
            if remain is not None:        # only when the AMS reports one (RFID spool) — the dict shape is pinned by tests
                entry["remain"] = remain
            out.append(entry)
    if isinstance(vt_tray, dict) and _norm_material(vt_tray.get("material")) and not cleared_tray(vt_tray):
        ext = {"tray_id": 254, "ams_id": 255, "slot_id": 0, "material": _norm_material(vt_tray.get("material")),
               "color": _norm_color(vt_tray.get("colorHex")), "filament_id": _norm_fid(vt_tray.get("filamentId"))}
        remain = _norm_remain(vt_tray.get("remainPct"))
        if remain is not None:
            ext["remain"] = remain
        out.append(ext)
    return out


def _norm_remain(value: Any) -> float | None:
    """Remaining filament %, when the AMS reports one (RFID spools; a generic spool reports nothing). None = unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def unaddressable_units(ams_units: Any) -> int:
    n = 0
    for unit in ams_units or []:
        if isinstance(unit, dict):
            raw = unit.get("raw") if isinstance(unit.get("raw"), dict) else {}
            if _ams_id(raw.get("id")) is None:
                n += 1
    return n


def describe(req: dict[str, Any]) -> str:
    material = _norm_material(req.get("type") or req.get("material")) or "any material"
    color = _norm_color(req.get("color"))
    label = f"{material} #{color}" if color else material
    if "extruderId" in req:
        label += " on the right nozzle" if req["extruderId"] == 0 else " on the left nozzle"
    return label


def _pick(req: dict[str, Any], trays: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The physical tray for one requirement: same material + colour; when the member chose a specific Bambu filament
    (idx) and the loaded candidates carry ids, ONLY an exact id match will do (codex v0.56 r1: a different white PLA is
    the wrong spool, not a near miss) — the brand-less fallback applies only when no candidate reports an id. AMS trays
    win over the external spool (254/255), which is a valid mapping target when it is the only match."""
    material = _norm_material(req.get("type") or req.get("material"))
    color = _norm_color(req.get("color"))
    fid = _norm_fid(req.get("idx") or req.get("trayInfoIdx"))
    if not material or color is None:
        # codex v0.56 r3: a half identity is not a wildcard — "PLA, any colour" would map to black when the member chose
        # white. The cloud may PARK on partial identity; the hub never PRINTS on it.
        raise TrayTranslationError("spool_mismatch", f"filament identity incomplete ({describe(req)}) — re-send it from OrcaSlicer")
    cands = [t for t in trays if t["material"] == material and t["color"] == color
             and ("extruderId" not in req or t.get("extruderId") == req["extruderId"])]
    if not cands:
        return None
    if fid:
        exact = [t for t in cands if t["filament_id"] == fid]
        if exact:
            cands = exact
        elif any(t["filament_id"] for t in cands):
            raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} ({fid}) — a different {describe(req)} is loaded")
    # Among identical spools, the FULLEST first (shop log F3, 2026-09-02): the old tie-break was the lowest tray id,
    # which is a coin the member did not flip — three of the same black PLA at 84/6/3 % must not start a print on the
    # 3 % one. A spool with no reported remaining (generic, no RFID) sorts after every reported one.
    return min(cands, key=lambda t: (t["tray_id"] in EXTERNAL_TRAY_IDS, -(t.get("remain") if t.get("remain") is not None else -1.0), t["tray_id"]))


def _by_filament_index(required: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Project filament index (0-based, = the ams_mapping position) → requirement. The capture records Bambu's
    `<filament id>` which is 1-based; the array-metadata fallback is 0-based — a list whose smallest slot is ≥1 is
    1-based. ponytail: heuristic; real Bambu 3MFs always take the 1-based path."""
    slots = [r["slot"] for r in required if isinstance(r, dict) and isinstance(r.get("slot"), int) and not isinstance(r.get("slot"), bool)]
    if not slots:
        return {}
    base = 1 if min(slots) >= 1 else 0
    return {r["slot"] - base: r for r in required if isinstance(r, dict) and isinstance(r.get("slot"), int) and not isinstance(r.get("slot"), bool)}


def translate_mapping(mapping: Any, required: list[dict[str, Any]], trays: list[dict[str, Any]]) -> list[int]:
    """Rewrite one ams_mapping list: position k (the k-th project filament) → the physical tray holding required
    filament k. -1 and 254/255 pass through for positions the plate does not use; a mapped position with no requirement
    record becomes -1 rather than a guessed tray. Every required filament is mapped: truncated or missing
    entries are filled from requirements. Nozzle-specific requirements constrain the same shared picker.
    Raises TrayTranslationError('spool_mismatch') when a required spool is not reachable."""
    by_index = _by_filament_index(required or [])
    mapping = mapping if isinstance(mapping, list) else []
    if not by_index and any(isinstance(v, int) and not isinstance(v, bool) and v >= 0 and v not in EXTERNAL_TRAY_IDS for v in mapping):
        # codex v0.56 r1: no requirements + AMS trays mapped = nothing to translate FROM; blanking them would print
        raise TrayTranslationError("spool_mismatch", "no filament requirements recorded for this job (re-send it from OrcaSlicer)")
    length = max([len(mapping)] + [k + 1 for k in by_index])
    out: list[int] = []
    for k in range(length):
        v = mapping[k] if k < len(mapping) else -1
        v = v if isinstance(v, int) and not isinstance(v, bool) else -1
        req = by_index.get(k)
        if v in EXTERNAL_TRAY_IDS:
            # a REQUIRED filament routed to the external holder must be what the holder reports (audit 2026-08-30): the
            # same material + colour (+ id) rule as an AMS tray — never "whatever is on the spool holder"
            if req is not None:
                if v == 255:
                    # the H2D's second external holder: we mirror only ONE vt_tray, so it cannot be verified — refuse rather
                    # than send a required filament to a holder whose contents we do not know (codex v0.58 r1)
                    raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} on the second external holder (not verifiable yet)")
                ext = [x for x in trays if x["tray_id"] in EXTERNAL_TRAY_IDS]
                if _pick(req, ext) is None:
                    raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} on the external spool (not loaded there)")
            out.append(v)
            continue
        if req is None:
            out.append(-1)
            continue
        tray = _pick(req, trays)
        if tray is None:
            raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} (not loaded on this printer)")
        out.append(tray["tray_id"])
    return out


def mapping_from_requirements(required: list[dict[str, Any]], trays: list[dict[str, Any]]) -> list[int]:
    """A web upload has no member mapping (no captured print command): build one from the plate's requirements alone —
    position k = the physical tray for project filament k, -1 for positions the plate does not use."""
    by_index = _by_filament_index(required or [])
    if not by_index:
        raise TrayTranslationError("spool_mismatch", "no filament requirements recorded for this job (re-send it from OrcaSlicer)")
    out = [-1] * (max(by_index) + 1)
    for k, req in by_index.items():
        tray = _pick(req, trays)
        if tray is None:
            raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} (not loaded on this printer)")
        out[k] = tray["tray_id"]
    return out


def _mapping2(value: Any) -> list[dict[str, int]]:
    if not isinstance(value, list) or len(value) > 256:
        raise TrayTranslationError("unsupported_mapping", "ams_mapping2 must contain AMS/slot objects; re-send from OrcaSlicer")
    for item in value:
        if not isinstance(item, dict) or set(item) != {"ams_id", "slot_id"} or any(
            isinstance(item[k], bool) or not isinstance(item[k], int) or not 0 <= item[k] <= 255
            for k in ("ams_id", "slot_id")
        ):
            raise TrayTranslationError("unsupported_mapping", "ams_mapping2 must contain integer ams_id/slot_id pairs")
    return value


def _json_array(value: Any) -> list:
    if isinstance(value, str) and len(value) <= 16_384:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return []
    return value if isinstance(value, list) and len(value) <= 256 else []


def _dual_nozzle_requirements(command: dict, required: list[dict], nozzles: Any) -> list[dict]:
    # Task nozzleId (0=right, 1=left) is explicit in SelectMachine.cpp's
    # ams_mapping_info. ams_mapping2 says NOTHING about the selected nozzle.
    # Automatic nozzle_mapping uses another protocol/version; do not infer it.
    if command.get("nozzle_mapping") not in (None, [], "", "[]"):
        raise TrayTranslationError("unsupported_nozzle_mapping", "automatic nozzle mapping needs a captured Orca command before H2D dispatch")
    mapping = _json_array(command.get("ams_mapping_info"))
    expected = _json_array(command.get("nozzles_info"))
    observed = nozzles if isinstance(nozzles, list) else []
    by_index = _by_filament_index(required)
    if not by_index or len(by_index) != len(required) or any(not 0 <= k < 256 for k in by_index):
        raise TrayTranslationError("unsupported_nozzle_mapping", "H2D filament indices are missing or ambiguous; re-send from OrcaSlicer")
    result = []
    for index, req in by_index.items():
        entry = mapping[index] if 0 <= index < len(mapping) else None
        eid = entry.get("nozzleId") if isinstance(entry, dict) else None
        if type(eid) is not int or eid not in (0, 1):
            raise TrayTranslationError("unsupported_nozzle_mapping", "H2D filament nozzle is unknown; capture Orca ams_mapping_info/nozzles_info before dispatch")
        actual = [n for n in observed if isinstance(n, dict) and n.get("extruderId") == eid]
        wanted = [n for n in expected if isinstance(n, dict) and type(n.get("id")) is int and n.get("id") == eid]
        if len(actual) != 1 or len(wanted) != 1 or isinstance(wanted[0].get("diameter"), bool) or wanted[0].get("diameter") != actual[0].get("diameterMm"):
            raise TrayTranslationError("unsupported_nozzle_mapping", f"H2D nozzle {eid} diameter is missing or differs from the sliced job")
        result.append(dict(req, extruderId=eid))
    return result


def translate_print_trays(print_cmd: dict[str, Any], required: list[dict[str, Any]] | None, ams_units: Any, vt_tray: Any = None,
                          *, extruder_count: int | None = None, nozzles: Any = None,
                          ) -> dict[str, Any]:
    """Translate one filament selection into both physical address representations.

    Dual-extruder jobs require proven per-filament nozzle metadata; no array name or missing hardware
    implies a nozzle. Single-extruder web uploads can still derive mappings from filament requirements.
    """
    out = dict(print_cmd)
    trays = live_trays(ams_units, vt_tray)
    mapping2 = _mapping2(out["ams_mapping2"]) if "ams_mapping2" in out else None
    if extruder_count == 2:
        required = _dual_nozzle_requirements(out, required or [], nozzles)
        if out.get("use_ams") is not True or any(m["ams_id"] in (254, 255) and m["slot_id"] != 255 for m in mapping2 or []):
            raise TrayTranslationError("unsupported_nozzle_mapping", "H2D external holders are not yet observed per nozzle; use a verified AMS path")
    if out.get("use_ams") is not True:
        # a direct-spool print (use_ams false): the external holder must carry EVERY required filament — nothing to
        # translate, but the identity gate still applies (audit 2026-08-30). No requirements known = nothing to check.
        ext = [x for x in trays if x["tray_id"] in EXTERNAL_TRAY_IDS]
        by_index = _by_filament_index(required or [])
        mapping = out.get("ams_mapping") if isinstance(out.get("ams_mapping"), list) else []
        for k, req in by_index.items():
            if mapping2 is not None and (k >= len(mapping2) or mapping2[k] != {"ams_id": 255, "slot_id": 0}):
                raise TrayTranslationError("unsupported_mapping", "direct-spool command does not name the observed external holder")
            if k < len(mapping) and mapping[k] == 255:
                raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} on the second external holder (not verifiable yet)")
            if _pick(req, ext) is None:
                raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} on the external spool (not loaded there)")
        return out
    if not trays and unaddressable_units(ams_units):
        raise TrayTranslationError("spool_mismatch", f"{unaddressable_units(ams_units)} AMS unit(s) report no id — cannot address trays")
    mapping = out.get("ams_mapping")
    if mapping2 is not None:
        # A mapped external holder is represented only in mapping2; the v0 map
        # carries -1. Keep the old external identity check for single extruders.
        mapping = list(mapping) if isinstance(mapping, list) else []
        for k, pair in enumerate(mapping2):
            if pair["ams_id"] in (254, 255) and pair["slot_id"] != 255:
                if pair != {"ams_id": 255, "slot_id": 0}:
                    raise TrayTranslationError("unsupported_mapping", "external AMS/slot pair is not supported")
                mapping.extend([-1] * max(0, k + 1 - len(mapping)))
                mapping[k] = 254
    if isinstance(mapping, list) and mapping:
        out["ams_mapping"] = translate_mapping(mapping, required or [], trays)
    else:
        out["ams_mapping"] = mapping_from_requirements(required or [], trays)
    if "ams_mapping_info" in out:
        info = _json_array(out["ams_mapping_info"])
        info = [dict(item, ams=out["ams_mapping"][i]) if isinstance(item, dict) and i < len(out["ams_mapping"]) else item
                for i, item in enumerate(info)]
        out["ams_mapping_info"] = json.dumps(info, separators=(",", ":")) if isinstance(out["ams_mapping_info"], str) else info
    if mapping2 is not None or extruder_count == 2:
        physical = {t["tray_id"]: t for t in trays}
        out["ams_mapping2"] = [
            {"ams_id": physical[v]["ams_id"], "slot_id": physical[v]["slot_id"]}
            if v in physical else {"ams_id": 255, "slot_id": 255}
            for v in out["ams_mapping"]
        ]
        out["ams_mapping"] = [-1 if v in EXTERNAL_TRAY_IDS else v for v in out["ams_mapping"]]
    return out
