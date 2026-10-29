"""Parse the per-object list (identify_id + name) from a sliced Bambu 3MF.

The per-object-skip feature needs the `identify_id` integers the printer's
`skip_objects` MQTT command takes. BambuStudio writes those ids alongside the
object name into `Metadata/slice_info.config` — verified verbatim against its
own writer (bbs_3mf.cpp: `<object identify_id="K" name="..." skipped=".."/>`
under `<plate>`, plus a `label_object_enabled` plate metadata) and its own
parser (SkipPartCanvas.cpp `ModelSettingHelper`). Pure stdlib (zipfile + xml)
so it's unit-testable without a printer; parse_plate_objects never raises. The
plate checks below (v0.80.1) raise Unreadable3mf; the dispatch pass holds that
one assignment.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

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
            # Metadata alone enables billing credits for parts the printer cannot skip.
            with zf.open(f"Metadata/plate_{plate}.gcode") as gcode:
                remaining = 2 * 1024**3
                marked = False
                while remaining > 0:
                    line = gcode.readline(min(remaining, 64 * 1024))
                    if not line:
                        break
                    remaining -= len(line)
                    if re.match(rb"^[ \t]*M624[ \t]+[^;\s]", line):
                        marked = True
                        break
                    if len(line) == 64 * 1024 and not line.endswith(b'\n'):
                        return []  # An oversized line cannot safely be split into executable commands.
                if not marked:
                    return []
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


# The plate's picture and its objects' first-layer boxes, for the site (operator, 2026-10-26: "sending a sliced image back tot he user
# showing our recommended settings"; the site's skip-objects tab outlines each object by its box). Read from the sliced 3MF the hub
# holds (a Print it slice, an OrcaSlicer send); both best-effort and bounded, never raising: a file without them sends neither, as
# before. The site checks both again (a PNG it decodes; boxes it matches to the plate's objects by name).
PLATE_PICTURE_MAX = 192 * 1024        # Bambu's plate_N.png is 512 x 512, usually 40-150 KB; anything larger is not sent
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_PLATE_JSON = 1024 * 1024
_MAX_BOXES = 256


def plate_picture(threemf_path, plate: int = 1) -> bytes | None:
    """The plate's picture for the member: the shop slicer's four views, Metadata/ps_views_N.png (2026-10-27; operator: "i think it might
    make sense to just send a few views"), else the plate's own Metadata/plate_N.png (else plate_N_small.png), as an OrcaSlicer send
    has: a PNG of at most PLATE_PICTURE_MAX bytes, or None."""
    try:
        with zipfile.ZipFile(threemf_path) as zf:
            for name in (f"Metadata/ps_views_{int(plate)}.png", f"Metadata/plate_{int(plate)}.png", f"Metadata/plate_{int(plate)}_small.png"):
                try:
                    info = zf.getinfo(name)
                except KeyError:
                    continue
                if info.file_size > PLATE_PICTURE_MAX:
                    continue
                data = zf.read(info)
                if data.startswith(_PNG) and len(data) <= PLATE_PICTURE_MAX:
                    return data
    except Exception as exc:  # noqa: BLE001 - a picture never stops a print or its report
        log.info("3mf plate picture skipped (%s): %s", threemf_path, exc)
    return None


def plate_boxes(threemf_path, plate: int = 1) -> list[dict]:
    """Metadata/plate_N.json's bbox_objects, as OrcaSlicer writes them: ``[{"name": str, "bbox": [x0, y0, x1, y1]}]`` (each object's
    first layer, bed millimetres), names cut to 120, finite boxes with x1 > x0 and y1 > y0, at most 256; ``[]`` otherwise."""
    try:
        with zipfile.ZipFile(threemf_path) as zf:
            info = zf.getinfo(f"Metadata/plate_{int(plate)}.json")
            if info.file_size > _MAX_PLATE_JSON:
                return []
            raw = json.loads(zf.read(info).decode("utf-8"))
        boxes = raw.get("bbox_objects", []) if isinstance(raw, dict) else []
        if not isinstance(boxes, list):
            raise ValueError("bbox_objects must be a list")
        out: list[dict] = []
        for b in boxes:
            box = b.get("bbox") if isinstance(b, dict) else None
            name = b.get("name") if isinstance(b, dict) else None
            if not isinstance(name, str) or not isinstance(box, list) or len(box) != 4:
                continue
            if not all(type(v) in (int, float) for v in box):
                continue
            box = [float(v) for v in box]
            if not all(math.isfinite(v) for v in box) or not (box[2] > box[0] and box[3] > box[1]):
                continue
            out.append({"name": name.strip()[:_MAX_NAME], "bbox": [round(v, 3) for v in box]})
            if len(out) >= _MAX_BOXES:
                break
        return out
    except Exception as exc:  # noqa: BLE001 - boxes never stop a print or its report
        log.info("3mf plate boxes skipped (%s): %s", threemf_path, exc)
        return []


# v0.80.1 (2026-10-22): OrcaSlicer 2.4.2's Export plate sliced file keeps the plate's own number, so plate 2 of a project exports
# as a file holding only plate_2.gcode; told to print plate 1, an X1C stopped with 0500-4003 ("unable to parse the file") and
# the hub saw only start_not_observed, six times for one member. The printer is asked for exactly Metadata/plate_N.gcode, so
# only that canonical name counts (N 1 to 64, no padding: plate_02.gcode is not plate 2), and a number is never parsed past
# two digits (a 5,000-digit name once raised out of int()).
_CANONICAL_PLATE = r"[1-9]|[1-5][0-9]|6[0-4]"
_PLATE_GCODE = re.compile(rf"Metadata/plate_({_CANONICAL_PLATE})\.gcode")
_PLATE_VALUE = re.compile(_CANONICAL_PLATE)


class Unreadable3mf(Exception):
    """A sliced 3MF the hub must not send: a damaged zip, an entry listed twice, or a plate entry no printer could read."""


def plate_named(value) -> int | None:
    """The plate a message names: an int 1..64, or that number as a canonical digit string; anything else names none."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 64 else None
    if isinstance(value, str) and _PLATE_VALUE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def gcode_plates(threemf_path) -> list[int] | None:
    """The plates whose G-code a sliced 3MF holds, ascending: the plates a printer can be told to start from it. ``None`` when
    the file is not a zip at all (a Klipper .gcode: no plates, not this check's business). Raises Unreadable3mf for a damaged
    zip or one that lists a name twice (two readers could read two different files under one name)."""
    with open(threemf_path, "rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            return None
    try:
        with zipfile.ZipFile(threemf_path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise Unreadable3mf("the file is a damaged zip") from exc
    if len(names) != len(set(names)):
        raise Unreadable3mf("the file lists an entry twice")
    return sorted(int(m.group(1)) for m in map(_PLATE_GCODE.fullmatch, names) if m)


def check_plate_entry(threemf_path, plate: int) -> None:
    """The plate's G-code entry is one a printer can read: stored or deflated, its local header names it with the directory's
    compression, its data lies inside the file, and its stream starts. Raises Unreadable3mf."""
    name = f"Metadata/plate_{plate}.gcode"
    damaged = Unreadable3mf(f"plate {plate}'s entry in the file is damaged")
    try:
        size = os.path.getsize(threemf_path)
        with zipfile.ZipFile(threemf_path) as zf, open(threemf_path, "rb") as fh:
            info = zf.getinfo(name)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Unreadable3mf(f"plate {plate} uses a compression the printer does not read")
            fh.seek(info.header_offset)
            head = fh.read(30)
            if len(head) != 30 or head[:4] != b"PK\x03\x04":
                raise damaged
            (method,) = struct.unpack_from("<H", head, 8)
            nlen, xlen = struct.unpack_from("<HH", head, 26)
            if method != info.compress_type or fh.read(nlen) != name.encode() or info.header_offset + 30 + nlen + xlen + info.compress_size > size:
                raise damaged
            with zf.open(info) as stream:
                stream.read(1)
    except Unreadable3mf:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, zlib.error, EOFError, ValueError, NotImplementedError) as exc:
        raise damaged from exc
