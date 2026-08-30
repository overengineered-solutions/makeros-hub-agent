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
    cands = [t for t in trays if t["material"] == material and t["color"] == color]
    if not cands:
        return None
    if fid:
        exact = [t for t in cands if t["filament_id"] == fid]
        if exact:
            cands = exact
        elif any(t["filament_id"] for t in cands):
            raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} ({fid}) — a different {describe(req)} is loaded")
    return min(cands, key=lambda t: (t["tray_id"] in EXTERNAL_TRAY_IDS, t["tray_id"]))


def _by_filament_index(required: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Project filament index (0-based, = the ams_mapping position) → requirement. The capture records Bambu's
    `<filament id>` which is 1-based; the array-metadata fallback is 0-based — a list whose smallest slot is ≥1 is
    1-based. ponytail: heuristic; real Bambu 3MFs always take the 1-based path."""
    slots = [r["slot"] for r in required if isinstance(r, dict) and isinstance(r.get("slot"), int) and not isinstance(r.get("slot"), bool)]
    if not slots:
        return {}
    base = 1 if min(slots) >= 1 else 0
    return {r["slot"] - base: r for r in required if isinstance(r, dict) and isinstance(r.get("slot"), int) and not isinstance(r.get("slot"), bool)}


def translate_mapping(mapping: Any, required: list[dict[str, Any]], trays: list[dict[str, Any]], *,
                      fill_required: bool = True, skip_fill: frozenset[int] = frozenset()) -> list[int]:
    """Rewrite one ams_mapping list: position k (the k-th project filament) → the physical tray holding required
    filament k. -1 and 254/255 pass through for positions the plate does not use; a mapped position with no requirement
    record becomes -1 rather than a guessed tray. With `fill_required` (the PRIMARY map) every required filament ends
    up mapped: a list that is truncated, or carries -1 where the plate needs a spool, is extended/filled from the
    requirements (codex v0.56 r4 — a short list must never start a print with a required spool unmapped) — except the
    positions in `skip_fill` (filaments the H2D's second-nozzle map already covers). Without it (`ams_mapping2`) -1
    keeps its meaning: "this filament is not on this nozzle". Raises TrayTranslationError('spool_mismatch') when a
    required spool is not loaded on this printer."""
    by_index = _by_filament_index(required or [])
    mapping = mapping if isinstance(mapping, list) else []
    if not by_index and any(isinstance(v, int) and not isinstance(v, bool) and v >= 0 and v not in EXTERNAL_TRAY_IDS for v in mapping):
        # codex v0.56 r1: no requirements + AMS trays mapped = nothing to translate FROM; blanking them would print
        raise TrayTranslationError("spool_mismatch", "no filament requirements recorded for this job (re-send it from OrcaSlicer)")
    length = max([len(mapping)] + ([k + 1 for k in by_index if k not in skip_fill] if fill_required else []))
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
        if req is None or (v < 0 and (not fill_required or k in skip_fill)):
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


def translate_print_trays(print_cmd: dict[str, Any], required: list[dict[str, Any]] | None, ams_units: Any, vt_tray: Any = None,
                          ) -> dict[str, Any]:
    """Return a copy of a built `print` command with ams_mapping (and ams_mapping2 when present — the H2D's second
    nozzle uses the same tray-id space) translated to this printer's physical trays. use_ams=false (external spool)
    is left untouched. An AMS print WITHOUT a mapping (a web upload, or a malformed replay coerced to []) gets one built
    from the requirements — never sent bare (codex v0.56 r3). Raises TrayTranslationError."""
    out = dict(print_cmd)
    trays = live_trays(ams_units, vt_tray)
    if out.get("use_ams") is not True:
        # a direct-spool print (use_ams false): the external holder must carry EVERY required filament — nothing to
        # translate, but the identity gate still applies (audit 2026-08-30). No requirements known = nothing to check.
        ext = [x for x in trays if x["tray_id"] in EXTERNAL_TRAY_IDS]
        for req in _by_filament_index(required or []).values():
            if _pick(req, ext) is None:
                raise TrayTranslationError("spool_mismatch", f"needs {describe(req)} on the external spool (not loaded there)")
        return out
    if not trays and unaddressable_units(ams_units):
        raise TrayTranslationError("spool_mismatch", f"{unaddressable_units(ams_units)} AMS unit(s) report no id — cannot address trays")
    mapping2 = out.get("ams_mapping2")
    # H2D: a filament the second-nozzle map covers is deliberately -1 in the primary map — never "fill" it there
    on_second = frozenset(k for k, v in enumerate(mapping2 if isinstance(mapping2, list) else [])
                          if isinstance(v, int) and not isinstance(v, bool) and v >= 0)
    mapping = out.get("ams_mapping")
    if isinstance(mapping, list) and mapping:
        out["ams_mapping"] = translate_mapping(mapping, required or [], trays, skip_fill=on_second)
    elif on_second:
        out["ams_mapping"] = translate_mapping([], required or [], trays, skip_fill=on_second)
    else:
        out["ams_mapping"] = mapping_from_requirements(required or [], trays)
    if isinstance(mapping2, list) and mapping2:
        out["ams_mapping2"] = translate_mapping(mapping2, required or [], trays, fill_required=False)
    return out
