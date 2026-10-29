"""Strict selected-file admission for the Moonraker queue door; never rewrites slices."""
from __future__ import annotations

import math
from pathlib import PurePosixPath
import re
import stat
import struct
import zipfile
import xml.etree.ElementTree as ET

from .gcode_meta import GCODE_SUFFIXES, read_gcode_stream
from .threemf_objects import check_plate_entry, gcode_plates, plate_named
from ..vprinter.capture import MAX_SLICE_INFO_BYTES, _vp_submit_filament, parse_required_filaments

# Exact profile/fleet identities. Bambu files must retain the native model ID so
# the unchanged final dispatch guard independently verifies the same identity.
POOLS = {
    "sv08-max": ("Sovol SV08 MAX", {"Sovol SV08 MAX", "PS SV08 MAX"}, None),
    "giga": ("Elegoo OrangeStorm Giga", {"Elegoo OrangeStorm Giga", "PS Giga"}, None),
    "p2s": ("P2S", {"Bambu Lab P2S", "PS P2S"}, "N7"),
    "a1-mini": ("A1 Mini", {"Bambu Lab A1 mini", "PS A1 mini"}, "N1"),
    "x1c": ("X1C", {"Bambu Lab X1 Carbon", "PS X1 Carbon"}, "BL-P001"),
}
MAX_ZIP_ENTRIES = 1024
MAX_ZIP_DIRECTORY_BYTES = 2 * 1024**2
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_ZIP_RATIO = 200


def _check_zip_directory(path):
    # ZipFile allocates the directory in its constructor, before infolist() can bound it.
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 65557))  # fixed end record plus maximum ZIP comment
        tail = stream.read(65557)
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        raise ValueError("Missing ZIP end record")
    _, disk, directory_disk, disk_entries, entries, directory_size, offset, comment = struct.unpack_from(
        "<4s4H2IH", tail, end)
    if (disk or directory_disk or disk_entries != entries or entries > MAX_ZIP_ENTRIES
            or directory_size > MAX_ZIP_DIRECTORY_BYTES or offset + directory_size > size - len(tail) + end
            or end + 22 + comment != len(tail)
            or (end >= 20 and tail[end - 20:end - 16] == b"PK\x06\x07")):
        raise ValueError("Oversized, multipart or ZIP64 directory is not supported")


def _check_zip(archive):
    entries = archive.infolist()
    names = [i.filename for i in entries]
    if len(entries) > MAX_ZIP_ENTRIES or len(names) != len(set(names)):
        raise ValueError("3MF has too many or duplicate entries")
    if sum(i.file_size for i in entries) > MAX_EXPANDED_BYTES:
        raise ValueError("3MF expanded size exceeds the limit")
    for i in entries:
        if (i.flag_bits & 1 or i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or i.file_size > max(1, i.compress_size) * MAX_ZIP_RATIO
                or stat.S_ISLNK(i.external_attr >> 16)
                or i.filename.startswith("/") or "\\" in i.filename
                or ".." in PurePosixPath(i.filename).parts):
            raise ValueError("3MF contains an unsafe or oversized entry")
        if i.filename.endswith((".config", ".json", ".xml")) and i.file_size > MAX_SLICE_INFO_BYTES:
            raise ValueError("3MF metadata exceeds the limit")


def read_file_facts(path, pool: str, plateindex=None) -> dict:
    model, aliases, model_id = POOLS[pool]
    plate = plate_named(plateindex)
    if plateindex is not None and plate is None:
        raise ValueError("plateindex must be a number from 1 to 64")
    if path.suffix.lower() in GCODE_SUFFIXES:
        if model_id or plateindex is not None:
            raise ValueError("This model needs a sliced 3MF; raw G-code cannot name a plate")
        with path.open("rb") as stream:
            meta, config = read_gcode_stream(stream)
        plate = 1
        filaments = meta["requiredFilaments"]
    elif path.suffix.lower() == ".3mf":
        if model_id is None:
            raise ValueError("Use G-code (use_3mf=0) for the shop's Klipper presets")
        _check_zip_directory(path)
        with zipfile.ZipFile(path) as archive:
            _check_zip(archive)
            held = gcode_plates(path)
            if not held or (plate is None and len(held) != 1) or (plate is not None and plate not in held):
                raise ValueError("Choose a sliced plate that this 3MF actually contains")
            plate = plate or held[0]
            check_plate_entry(path, plate)
            raw = archive.read("Metadata/slice_info.config")
            if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise ValueError("3MF metadata contains XML entities")
            root = ET.fromstring(raw)
            plates = [p for p in root.iter("plate") if any(
                m.get("key") == "index" and m.get("value") == str(plate) for m in p.findall("metadata"))]
            if len(plates) != 1:
                raise ValueError("3MF must describe the selected plate exactly once")
            declared = plates[0].findall("filament")
            slots = [f.get("id") for f in declared]
            if (not slots or any(s is None for s in slots) or len(slots) != len(set(slots))
                    or any(f.get("group_id", "0") != "0" for f in declared)):
                raise ValueError("Selected plate must name each filament once, on a single extruder")
            ids = {m.get("value", "").strip() for m in root.iter("metadata") if m.get("key") == "printer_model_id"}
            if ids != {model_id}:
                raise ValueError("3MF model ID is missing, unknown or conflicts with the model URL")
            with archive.open(f"Metadata/plate_{plate}.gcode") as stream:
                meta, config = read_gcode_stream(stream)
        filaments = [_vp_submit_filament(f) for f in parse_required_filaments(path, plate)]
        # Do not silently prefer either producer when the two descriptions conflict.
        gc_filaments = {f["slot"]: f for f in meta["requiredFilaments"]}
        if set(gc_filaments) != {f.get("slot") for f in filaments}:
            raise ValueError("Selected plate's filament slots disagree with its G-code")
        if any("nozzleDiameterMm" not in f for f in gc_filaments.values()):
            raise ValueError("Selected G-code must identify a single nozzle")
        for f in filaments:
            g = gc_filaments[f["slot"]]
            for key in ("type", "color", "nozzleDiameterMm", "usedG"):
                if key in f and key in g and f[key] != g[key]:
                    raise ValueError("Selected plate's filament facts disagree with its G-code")
            for key in ("nozzleDiameterMm", "usedG"):
                if key not in f and key in g:
                    f[key] = g[key]
    else:
        raise ValueError("Upload a sliced .gcode or .3mf file")

    if meta["printerModel"] not in aliases:
        raise ValueError("File's printer_model does not match this model URL; select the matching PS preset")
    if not filaments or len(filaments) > 16 or (model_id is None and len(filaments) != 1):
        raise ValueError("This target cannot run the file's filament arrangement; Klipper needs one filament")
    for f in filaments:
        if (type(f.get("slot")) is not int or not 1 <= f["slot"] <= 64
                or not isinstance(f.get("type"), str) or not f["type"].strip()
                or not re.fullmatch(r"[0-9A-F]{8}", f.get("color", ""))
                or type(f.get("nozzleDiameterMm")) not in (int, float)
                or not 0 < f["nozzleDiameterMm"] <= 2):
            raise ValueError("File must identify each used filament's material, colour and nozzle diameter")
        if "extruderId" in f and f["extruderId"] != 0:
            raise ValueError("This model URL does not support a second extruder")
        if pool == "a1-mini" and not f["type"].upper().startswith("PLA"):
            raise ValueError("The shop's A1 minis print PLA only")
    # Missing weight stays unknown, including a partial measurement.
    weights = [f.get("usedG") for f in filaments]
    grams = math.ceil(round(sum(weights), 2)) if all(type(g) in (int, float) and math.isfinite(g) and g >= 0 for g in weights) else None
    if grams is not None and grams > 100_000:
        raise ValueError("Selected plate exceeds the weight limit")
    bed = config.get("curr_bed_type", "").strip().strip('"') or None
    if bed is not None and len(bed) > 80:
        raise ValueError("Invalid bed type")
    return {"version": 1, "modelPool": pool, "printerModel": model,
            "sourcePrinterModel": meta["printerModel"], "printerModelId": model_id,
            "plate": plate, "bedType": bed, "requiredFilaments": filaments, "estGrams": grams}
