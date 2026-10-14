"""What an OrcaSlicer G-code says about itself: the printer it was sliced for and the filaments it uses.

Checked against OrcaSlicer's GCode.cpp / GCodeProcessor.cpp:
- every line we read is "; key = value";
- for a non-Bambu printer (the SV08) the "; CONFIG_BLOCK_START" .. "; CONFIG_BLOCK_END" block and the filament stats sit at
  the END of the file; for a Bambu printer the config block follows HEADER_BLOCK_END at the TOP (the stats stay at the end);
- per-filament stats are ", "-joined and zero-filled for an unused filament ("; filament used [mm] = 0.00, 1520.31");
  "; filament used [g] = " is only written when a filament density is set;
- config lists are ";"-joined ("filament_type = PLA;PETG"); an element with a space is double-quoted;
- print_host / printhost_apikey are banned from the config block, so a member's token is never in the file.

Pure and stdlib-only (the capture helpers it borrows are too); the shim's ingest and Klipper start_print both call it.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..vprinter.capture import _vp_submit_filament, normalize_color

GCODE_SUFFIXES = frozenset({".gcode", ".gco", ".g"})
# ponytail: 1 MiB windows; widen if a config block ever exceeds them.
WINDOW_BYTES = 1 << 20
MODEL_MAX_CHARS = 80
MAX_EST_GRAMS = 100_000   # the cloud's accepted range for estGrams; beyond it the field is dropped, not the upload
_BLOCK_START = "; CONFIG_BLOCK_START"
_BLOCK_END = "; CONFIG_BLOCK_END"


def _pairs(text: str) -> dict[str, str]:
    """First "; key = value" per key (the tail window comes first, so the end-of-file block wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("; ") and "=" in line:
            key, _, value = line[2:].partition("=")
            out.setdefault(key.strip(), value.strip())
    return out


def _config_block(text: str) -> dict[str, str] | None:
    start = text.find(_BLOCK_START)
    if start < 0:
        return None
    end = text.find(_BLOCK_END, start)
    return _pairs(text[start : end if end >= 0 else len(text)])


def _list(value: str | None) -> list[str]:
    return [v.strip().strip('"').strip() for v in value.split(";")] if value else []


def _numbers(value: str | None) -> list[float | None]:
    out: list[float | None] = []
    for part in (value or "").split(","):
        try:
            n = float(part)
        except ValueError:
            n = None
        out.append(n if n is not None and math.isfinite(n) else None)
    return out if value else []


def read_gcode_meta(path) -> dict[str, Any] | None:
    """{printerModel, estGrams, requiredFilaments} for a G-code file, None for any other suffix (a .3mf keeps today's
    path). printerModel is None when the file carries no config block; estGrams is None without a "[g]" line."""
    p = Path(path)
    if p.suffix.lower() not in GCODE_SUFFIXES:
        return None
    with p.open("rb") as fh:
        size = fh.seek(0, 2)
        tail_at = max(0, size - WINDOW_BYTES)
        fh.seek(tail_at)
        tail = fh.read().decode("utf-8", errors="replace")
        fh.seek(0)
        head = fh.read(min(WINDOW_BYTES, tail_at)).decode("utf-8", errors="replace")   # only what the tail did not cover

    config = _config_block(tail)
    if config is None:
        config = _config_block(head) or {}
    stats = _pairs(tail)
    for key, value in _pairs(head).items():
        stats.setdefault(key, value)

    model = config.get("printer_model", "").strip().strip('"').strip()[:MODEL_MAX_CHARS] or None
    types = _list(config.get("filament_type"))
    colours = _list(config.get("filament_colour"))
    used_mm = _numbers(stats.get("filament used [mm]"))
    used_g = _numbers(stats.get("filament used [g]"))   # exact key: "total filament used [g]" is a different key

    slots = [i for i, mm in enumerate(used_mm, 1) if mm and mm > 0] if used_mm else list(range(1, len(types) + 1))
    filaments: list[dict[str, Any]] = []
    grams: list[float] = []
    for slot in slots:
        item: dict[str, Any] = {"slot": slot}
        if slot <= len(types) and types[slot - 1]:
            item["type"] = types[slot - 1]
        if slot <= len(colours) and normalize_color(colours[slot - 1]):
            item["color"] = normalize_color(colours[slot - 1])
        if slot <= len(used_g) and used_g[slot - 1] is not None and used_g[slot - 1] >= 0:
            item["usedG"] = round(used_g[slot - 1], 2)
            grams.append(used_g[slot - 1])
        filaments.append(_vp_submit_filament(item))

    total = math.ceil(sum(grams)) if grams else 0
    return {
        "printerModel": model,
        "estGrams": total if 0 < total <= MAX_EST_GRAMS else None,
        "requiredFilaments": filaments,
    }
