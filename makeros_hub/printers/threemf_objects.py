"""Parse the per-object list (identify_id + name) from a sliced Bambu 3MF.

The per-object-skip feature needs the `identify_id` integers the printer's
`skip_objects` MQTT command takes. BambuStudio writes those ids alongside the
object name into `Metadata/slice_info.config` — verified verbatim against its
own writer (bbs_3mf.cpp: `<object identify_id="K" name="..." skipped=".."/>`
under `<plate>`, plus a `label_object_enabled` plate metadata) and its own
parser (SkipPartCanvas.cpp `ModelSettingHelper`). Pure stdlib (zipfile + xml)
so it's unit-testable without a printer; never raises.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
import zipfile

log = logging.getLogger("makeros-hub.printers")

_SLICE_INFO = "Metadata/slice_info.config"
# Bambu disables "Skip objects" outside this window: 1 object = nothing to skip,
# >64 = unsupported. Mirror it so we never surface an unusable object list.
_MIN_OBJECTS = 2
_MAX_OBJECTS = 64
# A label can't bloat a report; the printer's own names are short.
_MAX_NAME = 120
# Bound the slice_info read before it reaches the XML parser — mirrors the VP
# capture parser's guard (makeros_hub/vprinter/capture.py MAX_SLICE_INFO_BYTES)
# so a crafted/huge or zip-bombed 3MF can't exhaust the Pi. ZipInfo.file_size is
# the UNCOMPRESSED size, so this also caps a high-ratio zip bomb.
_MAX_SLICE_INFO_BYTES = 10 * 1024 * 1024


def parse_plate_objects(threemf_path, plate: int = 1) -> list[dict]:
    """Return ``[{"id": int, "name": str}]`` for the given 1-based plate.

    Returns ``[]`` (never raises — a broad guard, since this runs on the dispatch
    path and must never block a print) when the file is unreadable, oversized,
    the plate isn't found, per-object labelling is disabled, or the object count
    is outside Bambu's 2..64 skip window — any case where skip can't be used.
    """
    try:
        with zipfile.ZipFile(threemf_path) as zf:
            info = zf.getinfo(_SLICE_INFO)
            if info.file_size > _MAX_SLICE_INFO_BYTES:
                log.info(
                    "3mf slice_info too large (%s bytes) — skipping objects", info.file_size
                )
                return []
            raw = zf.read(info)
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 — a malformed 3MF must never block dispatch
        log.info("3mf object parse skipped (%s): %s", threemf_path, exc)
        return []

    for plate_el in root.iter("plate"):
        index = None
        label_enabled = False
        objects: list[dict] = []
        for child in plate_el:
            if child.tag == "metadata":
                key = child.get("key")
                if key == "index":
                    try:
                        index = int(child.get("value", ""))
                    except (TypeError, ValueError):
                        index = None
                elif key == "label_object_enabled":
                    label_enabled = child.get("value") == "true"
            elif child.tag == "object":
                try:
                    oid = int(child.get("identify_id"))
                except (TypeError, ValueError):
                    continue
                name = (child.get("name") or "").strip()[:_MAX_NAME]
                objects.append({"id": oid, "name": name})

        if index != plate:
            continue
        if not label_enabled:
            return []
        # Dedup by id, preserve slice order.
        seen: set[int] = set()
        deduped: list[dict] = []
        for obj in objects:
            if obj["id"] in seen:
                continue
            seen.add(obj["id"])
            deduped.append(obj)
        if not (_MIN_OBJECTS <= len(deduped) <= _MAX_OBJECTS):
            return []
        return deduped
    return []
