"""Derive the Virtual Printer's filament pool from the agent's OWN live read of
the real printers' AMS — the same per-heartbeat status the agent already builds
(bambu_parse.build_ams). This lets the VP mirror reality continuously, with no
cloud round-trip and no config re-pull: whenever a real spool changes, the next
heartbeat recomputes the pool and the runtime hot-applies it (bumping ams.version
so OrcaSlicer's Device tab — the send-time source of truth — re-reads).

Field-for-field parity with the cloud's deriveVpPool/trayForAmsTray
(apps/web/lib/print/virtual-printers.ts): same material-key normalization,
same color normalization, same built-in filament catalog fallback, same dedupe
key + ordering. Because both paths read the SAME source and map it the SAME way,
the live-mirror's display identity equals the cloud config-down's display
identity for a stable shop — so right after a config-down the live-mirror no-ops
(no extra ams.version bump), and it only ever fires when a real spool changed
faster than the cloud round-trip. Keep these two in lockstep when either changes.
"""

from __future__ import annotations

import dataclasses
from typing import Any

# Mirror of the cloud FILAMENT_CATALOG (virtual-printers.ts). A material with no
# Bambu filament id on the tray falls back to the catalog's info idx + temps,
# exactly like the cloud, so identity matches instead of diverging to "".
_FILAMENT_CATALOG: dict[str, dict[str, Any]] = {
    "PLA": {"infoIdx": "GFL99", "tempMin": 190, "tempMax": 240},
    "PETG": {"infoIdx": "GFG99", "tempMin": 230, "tempMax": 260},
    "ABS": {"infoIdx": "GFB99", "tempMin": 240, "tempMax": 270},
    "TPU": {"infoIdx": "GFU99", "tempMin": 200, "tempMax": 240},
    "PLA-CF": {"infoIdx": "GFL99", "tempMin": 200, "tempMax": 250},
    "PA": {"infoIdx": "GFN99", "tempMin": 250, "tempMax": 290},
}
_FALLBACK_FILAMENT = _FILAMENT_CATALOG["PLA"]

# OrcaSlicer's Device tab can't resolve Bambu's GENERIC filament ids (e.g. Generic
# PLA "GFL99") for the VP's model — even though the tray is otherwise IDENTICAL to
# a real printer's (verified field-for-field 2026-06-15) — so it falls back to the
# first type in the list, alphabetically ABS (the operator's "2 ABS" = 2 Generic
# PLA spools). RECOGNIZED Bambu ids (GFA01 "PLA Matte" etc.) resolve fine, so remap
# a generic id to its recognized Basic counterpart + matching sub-brand so the TYPE
# renders correctly. The real spool is a PLA, so profile/temps stay right.
_GENERIC_IDX_REMAP: dict[str, tuple[str, str]] = {
    "GFL99": ("GFA00", "PLA Basic"),  # Generic PLA -> Bambu PLA Basic
}


def _resolve_idx(info_idx: str, product_name: str | None) -> tuple[str, str]:
    """Return (tray_info_idx, tray_sub_brands): a recognized id (+ its sub-brand)
    for a generic id OrcaSlicer can't resolve, else the id + the real product
    name."""
    remapped = _GENERIC_IDX_REMAP.get(info_idx)
    if remapped is not None:
        return remapped
    return info_idx, product_name or ""


def _clean_optional(value: Any) -> str | None:
    """cleanOptionalText: trimmed string, or None when blank."""
    s = str(value).strip() if value is not None else ""
    return s or None


def _norm_material(value: Any) -> str:
    """normalizeMaterialKey: trim + UPPERCASE, defaulting to PLA. Uppercase is
    load-bearing — the cloud uppercases, so the VP's tray_type must too or the
    display identity flip-flops with config-down on any lowercase/mixed input."""
    return (str(value).strip().upper() if value is not None else "") or "PLA"


def _norm_color(value: Any) -> str:
    """normalizeColor: strip '#', uppercase, pad 6-hex with FF, pass 8-hex,
    else opaque white."""
    s = str(value or "").strip().lstrip("#").upper()
    if len(s) == 6 and all(c in "0123456789ABCDEF" for c in s):
        return s + "FF"
    if len(s) == 8 and all(c in "0123456789ABCDEF" for c in s):
        return s
    return "FFFFFFFF"


def vp_pool_from_statuses(
    statuses: list[dict[str, Any]],
    units: int,
    trays: int,
) -> list[dict[str, Any]]:
    """Pool = the filaments ACTUALLY loaded across the hub's printers, deduped by
    (material, Bambu filament id, color) so the same spool in two printers shows
    once. Mirrors the cloud deriveVpPool: statuses are visited in a stable
    printerId order (deterministic first-wins tie-break), the dedupe key is the
    same `material|filamentId|color` string, and the result is key-sorted then
    capped to the VP's slot count. Returns trays in build_push_status's shape."""
    capacity = max(1, int(units) * int(trays))
    # Stable visit order so the "first wins" dedupe is deterministic across
    # heartbeats (mirrors the cloud's orderBy(asc(hubPrinters.id))).
    ordered = sorted(
        statuses or [],
        key=lambda s: str(s.get("printerId") or "") if isinstance(s, dict) else "",
    )
    deduped: dict[str, dict[str, Any]] = {}
    for status in ordered:
        if not isinstance(status, dict):
            continue
        for unit in status.get("ams") or []:
            if not isinstance(unit, dict):
                continue
            for tray in unit.get("trays") or []:
                if not isinstance(tray, dict):
                    continue
                if not _clean_optional(tray.get("material")):
                    continue  # empty slot
                key = "|".join(
                    (
                        _norm_material(tray.get("material")),
                        _clean_optional(tray.get("filamentId")) or "",
                        _norm_color(tray.get("colorHex")),
                    )
                )
                deduped.setdefault(key, tray)

    pool: list[dict[str, Any]] = []
    for key in sorted(deduped):
        t = deduped[key]
        material = _norm_material(t.get("material"))
        color = _norm_color(t.get("colorHex"))
        catalog = _FILAMENT_CATALOG.get(material, _FALLBACK_FILAMENT)
        raw_cols = t.get("colors")
        cols = (
            [_norm_color(c) for c in raw_cols]
            if isinstance(raw_cols, list) and raw_cols
            else [color]
        )
        nozzle_min = t.get("nozzleTempMin")
        nozzle_max = t.get("nozzleTempMax")
        remain = t.get("remainPct")
        info_idx, sub_brands = _resolve_idx(
            _clean_optional(t.get("filamentId")) or catalog["infoIdx"],
            _clean_optional(t.get("productName")),
        )
        pool.append(
            {
                "tray_type": material,
                "tray_info_idx": info_idx,
                "tray_sub_brands": sub_brands,
                "tray_color": color,
                "cols": cols,
                "nozzle_temp_min": str(
                    nozzle_min if isinstance(nozzle_min, (int, float)) else catalog["tempMin"]
                ),
                "nozzle_temp_max": str(
                    nozzle_max if isinstance(nozzle_max, (int, float)) else catalog["tempMax"]
                ),
                "remain": max(0, min(100, round(remain)))
                if isinstance(remain, (int, float))
                else -1,
            }
        )
        if len(pool) >= capacity:
            break
    return pool


# The display-identity fields OrcaSlicer renders (and that the runtime's
# ams.version gates on). Volatile fields (remain%, temps) are excluded so a
# spool ticking down doesn't churn the version.
_IDENTITY_KEYS = ("tray_type", "tray_info_idx", "tray_sub_brands", "tray_color", "cols")


def _pool_identity(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: t.get(k) for k in _IDENTITY_KEYS if k in t} for t in pool]


def updated_config_if_pool_changed(current_config: Any, statuses: list[dict[str, Any]]) -> Any:
    """If the live AMS (derived from the agent's own printer `statuses`) differs
    from `current_config`'s pool by DISPLAY IDENTITY, return a new frozen config
    carrying the live pool — the caller reconciles it so the running VP hot-applies
    (ams.version bump + push, so OrcaSlicer's Device tab re-reads). Returns None
    when nothing display-relevant changed, so a stable shop never churns.

    `current_config` is a VirtualPrinterConfig (frozen dataclass) or None.
    """
    if current_config is None:
        return None
    live = vp_pool_from_statuses(statuses, current_config.units, current_config.trays)
    cap = max(1, int(current_config.units) * int(current_config.trays))
    if _pool_identity(live) == _pool_identity(list(current_config.pool)[:cap]):
        return None
    return dataclasses.replace(current_config, pool=tuple(live))
