"""Derive the Virtual Printer's filament pool from the agent's OWN live read of
the real printers' AMS — the same per-heartbeat status the agent already builds
(bambu_parse.build_ams). This lets the VP mirror reality continuously, with no
cloud round-trip and no config re-pull: whenever a real spool changes, the next
heartbeat recomputes the pool and the runtime hot-applies it (bumping ams.version
so OrcaSlicer's Device tab — the send-time source of truth — re-reads).

Mirrors the cloud's deriveVpPool/trayForAmsTray so the VP behaves identically
whether the pool arrives via config-down or this local fast-path; they read the
same source and converge.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def _norm_color(value: Any) -> str:
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
    once. Keyed-sort = deterministic (a stable pool means no needless churn).
    Capped to the VP's slot count. Returns trays in build_push_status's shape."""
    capacity = max(1, int(units) * int(trays))
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for status in statuses or []:
        for unit in status.get("ams") or []:
            for tray in unit.get("trays") or []:
                material = str(tray.get("material") or "").strip()
                if not material:  # empty slot
                    continue
                key = (
                    material.lower(),
                    str(tray.get("filamentId") or ""),
                    _norm_color(tray.get("colorHex")),
                )
                deduped.setdefault(key, tray)

    pool: list[dict[str, Any]] = []
    for key in sorted(deduped):
        t = deduped[key]
        color = _norm_color(t.get("colorHex"))
        cols = t.get("colors") if isinstance(t.get("colors"), list) and t.get("colors") else [color]
        nozzle_min = t.get("nozzleTempMin")
        nozzle_max = t.get("nozzleTempMax")
        pool.append(
            {
                "tray_type": str(t.get("material") or "").strip(),
                "tray_info_idx": str(t.get("filamentId") or ""),
                "tray_sub_brands": str(t.get("productName") or ""),
                "tray_color": color,
                "cols": cols,
                "nozzle_temp_min": str(nozzle_min if nozzle_min is not None else 190),
                "nozzle_temp_max": str(nozzle_max if nozzle_max is not None else 230),
                "remain": t.get("remainPct") if t.get("remainPct") is not None else -1,
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
