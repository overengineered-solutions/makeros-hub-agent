"""Virtual → physical AMS tray translation (design B3, built 2026-08-30, hub v0.56).

The Virtual Printer shows a member a RANKED, DEDUPED pool of the model group's spools (vprinter/live_pool.py: virtual
slot i is the i-th key in sorted/ranked order), so the `ams_mapping` OrcaSlicer sends names VIRTUAL positions, never
physical trays. Replaying it verbatim (the v0.51 "identity") printed from whatever tray happened to sit at that index —
right only by coincidence, even on a single printer. The id a printer wants is `ams_id * 4 + slot` for EVERY unit
(BambuStudio DeviceManager.cpp `get_tray_id_by_ams_id_and_slot_id`: an AMS-HT with id 128 is 512+slot) and 254/255
for the external spools.

We translate by what the member actually chose — material + colour (+ Bambu filament id when it disambiguates) per
project filament — against the printer's LIVE trays, and REFUSE (never guess) when a required spool is not loaded:
the cloud puts the job back in the queue with the reason. Pure functions; the adapter feeds them its own DTO.
"""

from __future__ import annotations

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
        return s[:6]
    return None


def _norm_fid(value: Any) -> str | None:
    s = str(value or "").strip().upper()
    return s or None


def live_trays(ams_units: Any, vt_tray: Any = None) -> list[dict[str, Any]]:
    """Addressable spools: [{tray_id, material, color, filament_id}] from the adapter DTO (bambu_parse.build_ams units,
    which keep the printer's raw unit id under `raw.id`). A unit whose raw id is missing cannot be addressed, so its
    trays are not offered (they show up in the refusal detail instead). The external spool rides as 254."""
    out: list[dict[str, Any]] = []
    for unit in ams_units or []:
        if not isinstance(unit, dict):
            continue
        raw = unit.get("raw") if isinstance(unit.get("raw"), dict) else {}
        try:
            ams_id = int(raw.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if ams_id < 0:
            continue
        for tray in unit.get("trays") or []:
            if not isinstance(tray, dict):
                continue
            slot = tray.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 3:
                continue
            material = _norm_material(tray.get("material"))
            if not material:
                continue
            out.append({"tray_id": ams_id * 4 + slot, "material": material, "color": _norm_color(tray.get("colorHex")),
                        "filament_id": _norm_fid(tray.get("filamentId"))})
    if isinstance(vt_tray, dict) and _norm_material(vt_tray.get("material")):
        out.append({"tray_id": 254, "material": _norm_material(vt_tray.get("material")),
                    "color": _norm_color(vt_tray.get("colorHex")), "filament_id": _norm_fid(vt_tray.get("filamentId"))})
    return out


def unaddressable_units(ams_units: Any) -> int:
    n = 0
    for unit in ams_units or []:
        if isinstance(unit, dict):
            raw = unit.get("raw") if isinstance(unit.get("raw"), dict) else {}
            try:
                int(raw.get("id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                n += 1
    return n


def describe(req: dict[str, Any]) -> str:
    material = _norm_material(req.get("type") or req.get("material")) or "any material"
    color = _norm_color(req.get("color"))
    return f"{material} #{color}" if color else material


def _pick(req: dict[str, Any], trays: list[dict[str, Any]]) -> dict[str, Any] | None:
    material = _norm_material(req.get("type") or req.get("material"))
    color = _norm_color(req.get("color"))
    fid = _norm_fid(req.get("idx") or req.get("trayInfoIdx"))
    cands = [t for t in trays if t["tray_id"] not in EXTERNAL_TRAY_IDS
             and (not material or t["material"] == material) and (color is None or t["color"] == color)]
    if not cands:
        return None
    if fid:
        exact = [t for t in cands if t["filament_id"] == fid]
        if exact:
            cands = exact
    return min(cands, key=lambda t: t["tray_id"])


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
    filament k. -1 and 254/255 pass through; a mapped position with no requirement record (a filament this plate
    does not use) becomes -1 rather than a guessed tray. Raises TrayTranslationError('spool_mismatch') when a
    required spool is not loaded on this printer."""
    by_index = _by_filament_index(required)
    out: list[int] = []
    for k, v in enumerate(mapping if isinstance(mapping, list) else []):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v in EXTERNAL_TRAY_IDS:
            out.append(v if isinstance(v, int) and not isinstance(v, bool) else -1)
            continue
        req = by_index.get(k)
        if req is None:
            out.append(-1)
            continue
        tray = _pick(req, trays)
        if tray is None:
            raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} (not loaded on this printer)")
        out.append(tray["tray_id"])
    return out


def translate_print_trays(print_cmd: dict[str, Any], required: list[dict[str, Any]], ams_units: Any, vt_tray: Any = None,
                          ) -> dict[str, Any]:
    """Return a copy of a built `print` command with ams_mapping (and ams_mapping2 when present — the H2D's second
    nozzle uses the same tray-id space) translated to this printer's physical trays. use_ams=false (external spool)
    is left untouched. Raises TrayTranslationError."""
    out = dict(print_cmd)
    if out.get("use_ams") is not True:
        return out
    trays = live_trays(ams_units, vt_tray)
    if not trays and unaddressable_units(ams_units):
        raise TrayTranslationError("spool_mismatch", f"{unaddressable_units(ams_units)} AMS unit(s) report no id — cannot address trays")
    for key in ("ams_mapping", "ams_mapping2"):
        if key in out and isinstance(out[key], list) and out[key]:
            out[key] = translate_mapping(out[key], required, trays)
    return out
