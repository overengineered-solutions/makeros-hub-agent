"""PSTP v1 from the shop's slice. Geometry tables stay on disk, including during simplification."""
from __future__ import annotations

import gzip
import hashlib
import math
import re
import struct
import tempfile
import time
import zipfile

MAX_LINE = 16 * 1024
MAX_INPUT = 1024 * 1024 * 1024
MAX_SOURCE_POINTS = 32_000_000
MAX_PATHS = MAX_SOURCE_POINTS // 2
MAX_LAYERS = 100_000
TARGET_POINTS = 5_000_000
MAX_POINTS = 8_000_000
MAX_BYTES = 64 * 1024 * 1024
# Includes parsing, reduction and gzip; the parent also kills the low-priority process at this deadline.
DEADLINE_SECONDS = 180
POINT = struct.Struct("<fff")
PATH = struct.Struct("<BBHII")
LAYER = struct.Struct("<ffII")
HEADER = struct.Struct("<4sHH6f5IfB7x")
WORD = re.compile(r"([A-Za-z])([+-]?(?:\d+(?:\.\d*)?|\.\d+))")


class ToolpathError(ValueError):
    def __init__(self, reason, line=0):
        self.reason, self.line = reason, line
        super().__init__(f"{reason} at line {line}")


class Budget:
    def __init__(self, seconds=DEADLINE_SECONDS):
        self.end = time.monotonic() + seconds
        self.line = 0

    def check(self):
        if time.monotonic() > self.end:
            raise ToolpathError("deadline exceeded", self.line)


def sliced_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Geometry:
    def __init__(self, directory=None, budget=None):
        self.directory, self.budget = directory, budget or Budget()
        self.points = tempfile.TemporaryFile(dir=directory)
        self.paths = tempfile.TemporaryFile(dir=directory)
        self.layers = []
        self.features = ["Travel"]
        self.point_count = self.path_count = self.source_segments = 0
        self.extrude_length = self.travel_length = self.filament_length = 0.0
        self.minimum, self.maximum = [math.inf] * 3, [-math.inf] * 3
        self.active = None
        self.last = None

    def close(self):
        self.points.close()
        self.paths.close()

    def fail(self, reason):
        raise ToolpathError(reason, self.budget.line)

    def layer(self, z=0.0, height=0.0):
        self.end_path()
        if len(self.layers) >= MAX_LAYERS:
            self.fail("too_large")
        self.layers.append([z, height, self.path_count, 0])

    def end_path(self):
        if self.active is not None:
            feature, tool, first = self.active
            self.paths.write(PATH.pack(feature, tool, 0, first, self.point_count - first))
            self.path_count += 1
            self.layers[-1][3] += 1
            self.active = None
            if self.path_count > MAX_PATHS:
                self.fail("too_large")

    def point(self, p):
        if self.point_count >= MAX_SOURCE_POINTS:
            self.fail("too_large")
        self.points.write(POINT.pack(*p))
        self.point_count += 1
        for axis in range(3):
            self.minimum[axis] = min(self.minimum[axis], p[axis])
            self.maximum[axis] = max(self.maximum[axis], p[axis])
        self.last = p
        if self.point_count % 4096 == 0:
            self.budget.check()

    def segment(self, start, end, feature, tool):
        if self.active is None or self.active[:2] != (feature, tool) or self.last != start:
            self.end_path()
            self.active = (feature, tool, self.point_count)
            self.point(start)
        self.point(end)

    def path_records(self):
        self.paths.seek(0)
        while raw := self.paths.read(PATH.size * 4096):
            yield from PATH.iter_unpack(raw)

    def path_points(self, first, count):
        self.points.seek(first * POINT.size)
        while count:
            take = min(count, 4096)
            yield from POINT.iter_unpack(self.points.read(take * POINT.size))
            count -= take
            self.budget.check()


def number(raw):
    value = float(raw)
    if not math.isfinite(value) or abs(value) > 1e7:
        raise ValueError("invalid number")
    return value


def words(raw, allowed):
    result = {}
    pos = 0
    for match in WORD.finditer(raw):
        if raw[pos:match.start()].strip():
            raise ValueError("malformed motion")
        key = match[1].upper()
        if key not in allowed or key in result:
            raise ValueError("unsupported motion parameter")
        result[key] = number(match[2])
        pos = match.end()
    if raw[pos:].strip():
        raise ValueError("malformed motion")
    return result


def arc_points(start, end, values, clockwise):
    """XY arcs: <=1 mm chords and <=0.01 mm sagitta, including full circles and helical Z."""
    if "R" in values:
        if "I" in values or "J" in values:
            raise ValueError("mixed arc centres")
        dx, dy = end[0] - start[0], end[1] - start[1]
        chord = math.hypot(dx, dy)
        radius = abs(values["R"])
        if not chord or radius < chord / 2:
            raise ValueError("invalid arc radius")
        side = (-1 if clockwise else 1) * (-1 if values["R"] < 0 else 1)
        offset = side * math.sqrt(max(0, radius * radius - chord * chord / 4)) / chord
        cx, cy = (start[0] + end[0]) / 2 - dy * offset, (start[1] + end[1]) / 2 + dx * offset
    else:
        if not ("I" in values or "J" in values):
            raise ValueError("arc without centre")
        cx, cy = start[0] + values.get("I", 0), start[1] + values.get("J", 0)
        radius = math.hypot(start[0] - cx, start[1] - cy)
    if radius == 0 or abs(math.hypot(end[0] - cx, end[1] - cy) - radius) > max(0.02, radius * 0.001):
        raise ValueError("inconsistent arc centre")
    angle = math.atan2(start[1] - cy, start[0] - cx)
    sweep = (math.atan2(end[1] - cy, end[0] - cx) - angle) % math.tau
    if clockwise:
        sweep = sweep - math.tau if sweep else -math.tau
    elif not sweep:
        sweep = math.tau
    step = min(1 / radius, 2 * math.acos(max(-1, 1 - 0.01 / radius)))
    count = max(3, math.ceil(abs(sweep) / step))
    if count > MAX_SOURCE_POINTS:
        raise ValueError("arc has too many points")
    for i in range(1, count):
        theta = angle + sweep * i / count
        yield (cx + radius * math.cos(theta), cy + radius * math.sin(theta), start[2] + (end[2] - start[2]) * i / count)
    yield end


def parse(source, *, directory=None, budget=None):
    """Read bounded binary lines; start geometry at the first slicer layer, keeping preamble coordinate/mode state."""
    result = Geometry(directory, budget)
    position, offset = [0.0] * 3, [0.0] * 3
    e, relative, relative_e = 0.0, False, None
    tool, feature, wipe, retracted = 0, 0, False, False
    debts = {}
    total = 0
    try:
        while raw := source.readline(MAX_LINE + 1):
            result.budget.line += 1
            result.budget.check()
            total += len(raw)
            if len(raw) > MAX_LINE or total > MAX_INPUT:
                result.fail("input limit exceeded")
            line = raw.decode("utf-8").strip()
            if line.startswith(";"):
                comment = line[1:].strip()
                if comment in ("LAYER_CHANGE", "CHANGE_LAYER"):
                    result.layer(position[2])
                elif comment.startswith(("Z:", "Z_HEIGHT:", "HEIGHT:", "LAYER_HEIGHT:")):
                    if not result.layers:
                        result.fail("layer metadata before layer")
                    key, value = comment.split(":", 1)
                    result.layers[-1][0 if key in ("Z", "Z_HEIGHT") else 1] = number(value)
                elif comment.startswith(("TYPE:", "FEATURE:")):
                    name = comment.split(":", 1)[1].strip()
                    if not name or len(name.encode("utf-8")) > 40:
                        result.fail("invalid feature name")
                    if name not in result.features:
                        if len(result.features) == 255:
                            result.fail("too many features")
                        result.features.append(name)
                    feature = result.features.index(name)
                    result.end_path()
                elif comment in ("WIPE_START", "WIPE_END"):
                    wipe = comment == "WIPE_START"
                    result.end_path()
                continue
            line = line.split(";", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"([GMT]\d+(?:\.\d+)?)", line, re.I)
            if not match:
                if line.startswith("EXCLUDE_OBJECT"):
                    result.end_path()
                elif re.match(r"[XYZEF](?:[+-]?(?:\d|\.)|nan\b|inf\b)|N\d", line, re.I):
                    result.fail("motion without command")
                continue
            command = match[1].upper()
            params = line[match.end():].strip()
            if command in ("G90", "G91"):
                relative = command == "G91"
                continue
            if command in ("M82", "M83"):
                relative_e = command == "M83"
                continue
            if command.startswith("T"):
                if params:
                    result.fail("malformed tool change")
                selected = number(command[1:])
                # Bambu's end G-code unloads with T65535; it is not a physical tool index.
                if selected == 65535:
                    retracted = True
                elif selected != int(selected) or not 0 <= selected <= 255:
                    result.fail("tool index outside PSTP")
                else:
                    tool, retracted = int(selected), False
                    debts[tool] = 0.0
                result.end_path()
                continue
            if command in ("G10", "G11"):
                if params and (command != "G10" or words(params, "S") not in ({"S": 0}, {"S": 1})):
                    result.fail("unsupported firmware retraction")
                retracted = command == "G10"
                result.end_path()
                continue
            if command in ("G20", "G18", "G19", "G90.1", "G5", "G6", "G50", "G51", "G52", "G53", "G54", "G55", "G56", "G57", "G58", "G59", "G68", "G69") or command.startswith(("G38.", "G5.", "G92.")):
                result.fail("unsupported motion mode")
            if command == "G28":
                if result.layers:
                    result.fail("homing inside toolpath")
                position, offset = [0.0] * 3, [0.0] * 3
                continue
            if command not in ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G92"):
                continue
            values = words(params, "XYZE" if command == "G92" else "XYZEFIJR" if command in ("G2", "G02", "G3", "G03") else "XYZEF")
            if command == "G92":
                if not values:
                    result.fail("coordinate reset without axes")
                for axis, key in enumerate("XYZ"):
                    if key in values:
                        offset[axis] = position[axis] - values[key]
                e = values.get("E", e)
                result.end_path()
                continue
            end = tuple(position[i] + values.get(key, 0) if relative else values[key] + offset[i] if key in values else position[i] for i, key in enumerate("XYZ"))
            if any(abs(v) > 1e7 for v in end):
                result.fail("coordinate limit exceeded")
            ne = e + values.get("E", 0) if (relative if relative_e is None else relative_e) else values.get("E", e)
            delta = ne - e
            deposited = max(0, delta - debts.get(tool, 0))
            debts[tool] = max(0, debts.get(tool, 0) - delta)
            extruding = deposited > 1e-9 and not wipe and not retracted
            kind = feature if extruding else 0
            if extruding and feature == 0:
                if "Unknown" not in result.features:
                    if len(result.features) == 255:
                        result.fail("too many features")
                    result.features.append("Unknown")
                kind = result.features.index("Unknown")
            points = arc_points(position, end, values, command in ("G2", "G02")) if command in ("G2", "G02", "G3", "G03") else (end,)
            moved = False
            for point in points:
                length = math.dist(position, point)
                if length and result.layers:
                    result.segment(tuple(position), point, kind, tool)
                    if extruding:
                        result.extrude_length += length
                    else:
                        result.travel_length += length
                    moved = True
                position = point
            if moved:
                result.source_segments += 1
                if extruding:
                    result.filament_length += deposited
            if delta < 0:
                result.end_path()
            e = ne
        result.end_path()
        if not result.layers or not result.extrude_length:
            result.fail("no printable layers")
        return result
    except Exception as exc:
        result.close()
        if isinstance(exc, ToolpathError):
            raise
        raise ToolpathError("malformed G-code", result.budget.line) from exc


def simplify(points, tolerance):
    """Linear-time sleeve: monotone projections within tolerance/2 of a fixed ray bound chord error by tolerance."""
    anchor = next(points)
    yield anchor
    last = next(points)
    vector = tuple(last[i] - anchor[i] for i in range(3))
    norm = sum(v * v for v in vector)
    projection = norm
    for point in points:
        delta = tuple(point[i] - anchor[i] for i in range(3))
        dot = sum(delta[i] * vector[i] for i in range(3))
        perpendicular = sum(v * v for v in delta) - dot * dot / norm if norm else math.inf
        if dot < projection or perpendicular > (tolerance / 2) ** 2:
            yield last
            anchor = last
            vector = tuple(point[i] - anchor[i] for i in range(3))
            norm = sum(v * v for v in vector)
            dot = norm
        projection, last = dot, point
    yield last


def reduce_geometry(source, *, travel=True, tolerance=0.0, alternate=False):
    out = Geometry(source.directory, source.budget)
    out.features = source.features
    try:
        records = source.path_records()
        for index, (z, height, first, count) in enumerate(source.layers):
            keep = not alternate or index % 2 == 0
            if keep:
                out.layer(z, height)
            for _ in range(count):
                feature, tool, _, first, size = next(records)
                if not keep or not travel and feature == 0:
                    continue
                points = source.path_points(first, size)
                if tolerance:
                    points = simplify(points, tolerance)
                out.active = (feature, tool, out.point_count)
                for point in points:
                    out.point(point)
                out.end_path()
            source.budget.check()
        return out
    except Exception:
        out.close()
        raise


def write_pstp(source, destination):
    """Apply §4 in order, preserving source counts. Owns and closes source, also on refusal."""
    original = source
    original_layers, original_segments = len(source.layers), source.source_segments
    flags, tolerance = 0, 0.0
    try:
        for bit, options in ((4, {"travel": False}), (1, {"tolerance": 0.05}), (2, {"alternate": True}), (1, {"tolerance": 0.2})):
            if source.point_count <= TARGET_POINTS:
                break
            # Rebuild 0.2 from the original so two simplifications cannot accumulate more than 0.2 mm error.
            reduced = (reduce_geometry(original, travel=False, tolerance=0.2, alternate=True) if options.get("tolerance") == 0.2
                       else reduce_geometry(source, **options))
            if source is not original:
                source.close()
            source = reduced
            flags |= bit
            tolerance = options.get("tolerance", tolerance)
        features = b"".join(bytes([len(name.encode('utf-8'))]) + name.encode('utf-8') for name in source.features)
        size = 64 + len(features) + len(source.layers) * 16 + source.path_count * 12 + source.point_count * 6
        if source.point_count > MAX_POINTS or size > MAX_BYTES or not source.point_count:
            source.fail("too_large")
        with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0, compresslevel=6) as zipped:
            zipped.write(HEADER.pack(b"PSTP", 1, flags, *source.minimum, *source.maximum, len(source.layers), source.path_count,
                                     source.point_count, original_layers, original_segments, tolerance, len(source.features)))
            zipped.write(features)
            for layer in source.layers:
                zipped.write(LAYER.pack(*layer))
            source.paths.seek(0)
            while chunk := source.paths.read(64 * 1024):
                zipped.write(chunk)
                source.budget.check()
            scale = [65535 / (hi - lo) if hi > lo else 0 for lo, hi in zip(source.minimum, source.maximum)]
            packed = bytearray()
            for point in source.path_points(0, source.point_count):
                packed.extend(struct.pack("<HHH", *(max(0, min(65535, round((point[i] - source.minimum[i]) * scale[i]))) for i in range(3))))
                if len(packed) >= 24 * 1024:
                    zipped.write(packed)
                    packed.clear()
            zipped.write(packed)
        source.budget.check()
        return {"layers": len(source.layers), "paths": source.path_count, "points": source.point_count, "flags": flags, "bytes": size}
    finally:
        source.close()
        if source is not original:
            original.close()


def build(sliced, plate, destination):
    budget = Budget()
    with zipfile.ZipFile(sliced) as archive:
        name = f"Metadata/plate_{plate}.gcode"
        entries = [entry for entry in archive.infolist() if entry.filename == name]
        if len(entries) != 1 or entries[0].file_size > MAX_INPUT:
            raise ToolpathError("missing, duplicate or oversized plate")
        with archive.open(entries[0]) as source:
            geometry = parse(source, directory=destination.parent, budget=budget)
    with destination.open("wb") as output:
        return write_pstp(geometry, output)
