"""The member sees the shop's actual moves; reduction is labelled and a broken parse is never partial."""
import gzip
import io
import math
from pathlib import Path
import struct
import zipfile

import pytest

from makeros_hub import toolpath as tp

FIXTURE = Path(__file__).parent / "fixtures/twisted-tower.gcode.gz"
PREFIX = b"G90\nM83\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\n"


def parsed(code):
    return tp.parse(io.BytesIO(code))


def decode(raw):
    data = gzip.decompress(raw)
    magic, version, flags = struct.unpack_from("<4sHH", data)
    assert magic == b"PSTP" and version == 1 and flags <= 7
    bounds = struct.unpack_from("<6f", data, 8)
    layers, paths, points, source_layers, segments = struct.unpack_from("<5I", data, 32)
    tolerance, feature_count = struct.unpack_from("<fB", data, 52)
    assert all(math.isfinite(v) for v in (*bounds, tolerance))
    assert points <= 8_000_000 and len(data) <= 64 * 1024 * 1024 and data[57:64] == bytes(7)
    features, offset = [], 64
    for _ in range(feature_count):
        size = data[offset]
        assert 0 < size <= 40
        features.append(data[offset + 1:offset + 1 + size].decode("utf-8"))
        offset += 1 + size
    assert features[0] == "Travel"
    layer_table = list(struct.iter_unpack("<ffII", data[offset:offset + layers * 16]))
    offset += layers * 16
    path_table = list(struct.iter_unpack("<BBHII", data[offset:offset + paths * 12]))
    offset += paths * 12
    assert len(data) == offset + points * 6
    for z, height, first, count in layer_table:
        assert math.isfinite(z) and math.isfinite(height) and first + count <= paths
    for feature, tool, reserved, first, count in path_table:
        assert feature < feature_count and reserved == 0 and first + count <= points and count >= 2
    def coordinate(index):
        quantized = struct.unpack_from("<HHH", data, offset + index * 6)
        return tuple(bounds[i] + quantized[i] / 65535 * (bounds[i + 3] - bounds[i]) for i in range(3))
    return {"flags": flags, "layers": layer_table, "paths": path_table, "points": points, "features": features,
            "source_layers": source_layers, "segments": segments, "tolerance": tolerance, "coordinate": coordinate, "bytes": len(data)}


def encoded(code):
    output = io.BytesIO()
    tp.write_pstp(parsed(code), output)
    return decode(output.getvalue())


def test_real_orca_slice_golden_counts_and_lengths():
    with gzip.open(FIXTURE, "rb") as source:
        geometry = tp.parse(source)
    assert len(geometry.layers) == 450
    assert (geometry.source_segments, geometry.path_count, geometry.point_count) == (98177, 4116, 102293)
    assert geometry.extrude_length == pytest.approx(459149.15522673645, abs=0.001)
    assert geometry.travel_length == pytest.approx(32824.21488483297, abs=0.001)
    assert geometry.filament_length == pytest.approx(15073.27565, abs=0.00001)
    output = io.BytesIO()
    tp.write_pstp(geometry, output)
    result = decode(output.getvalue())
    assert (result["source_layers"], result["segments"], result["points"], result["flags"], result["bytes"]) == (450, 98177, 102293, 0, 670529)
    assert result["features"] == ["Travel", "Custom", "Inner wall", "Outer wall", "Bottom surface", "Internal solid infill",
                                   "Sparse infill", "Internal Bridge", "Top surface"]
    assert result["layers"][0][:2] == pytest.approx((0.2, 0.2))
    assert result["layers"][-1][:2] == pytest.approx((90, 0.199997))


def test_modes_g92_and_tool_changes_keep_bed_coordinates():
    result = encoded(PREFIX + b"G1X10Y10Z0.2E1\nG91\nG1 X2 E1\nG92 X0 E0\nG90\nM82\nG1 X3 E1\nT1\nG1 X4 E2\nM83\nG1 X5 E1\n")
    assert result["segments"] == 5
    assert [p[1] for p in result["paths"]] == [0, 0, 1]
    assert result["coordinate"](result["points"] - 1) == pytest.approx((17, 10, 0.2), abs=0.001)


def test_xyz_mode_applies_to_extrusion_until_an_explicit_extruder_mode():
    result = encoded(b"G91\n;LAYER_CHANGE\n;TYPE:Brim\nG1 X1 E1\nG1 X1 E1\nM82\nG1 X1 E2\nM83\nG90\nG1 X4 E1\n")
    assert [p[0] for p in result["paths"]] == [1, 0, 1]
    assert result["coordinate"](result["points"] - 1)[0] == pytest.approx(4)


def test_retraction_wipes_and_firmware_retraction_are_travel():
    result = encoded(PREFIX + b"G1 X1 E1\nG1 E-1\nG1 X2 E0.5\nG1 X3 E0.5\nG1 X4 E1\n"
                     b";WIPE_START\nG1 X5 E1\n;WIPE_END\nG10\nG1 X6 E1\nG11\nG1 X7 E1\nG10 S1\nG1 X8\nG11\nG1 X9 E1\n")
    assert [p[0] for p in result["paths"]] == [1, 0, 1, 0, 0, 1, 0, 1]
    assert result["segments"] == 9


def test_features_are_kept_verbatim_and_object_markers_split_paths():
    result = encoded(PREFIX + b";TYPE:Brim\nG1 X1 E1\n; FEATURE: Support\nG1 X2 E1\n;TYPE:Support interface\nG1 X3 E1\n"
                     b";TYPE:New slicer feature\nG1 X4 E1\nEXCLUDE_OBJECT_START NAME=cube\nG1 X5 E1\nEXCLUDE_OBJECT_END NAME=cube\nG1 X6 E1\nM999\nFIRMWARE_EVENT X999\n")
    assert result["features"] == ["Travel", "Outer wall", "Brim", "Support", "Support interface", "New slicer feature"]
    assert [p[0] for p in result["paths"]] == [2, 3, 4, 5, 5, 5]


def test_extrusion_without_feature_has_a_label_and_unload_is_travel():
    result = encoded(b"M83\n;LAYER_CHANGE\nG1 X1 E1\nT65535\nG1 X2 E1\n")
    assert result["features"] == ["Travel", "Unknown"]
    assert [p[0] for p in result["paths"]] == [1, 0]


@pytest.mark.parametrize("command,finish,length", [
    (b"G3 X0 Y10 I-10 J0 E1", (0, 10, 0), math.pi * 5),
    (b"G2 X0 Y-10 I-10 J0 E1", (0, -10, 0), math.pi * 5),
    (b"G3 X0 Y10 R10 E1", (0, 10, 0), math.pi * 5),
    (b"G3 X0 Y10 R-10 E1", (0, 10, 0), math.pi * 15),
    (b"G2 X0 Y-10 R-10 E1", (0, -10, 0), math.pi * 15),
    (b"G2 I-10 J0 E1", (10, 0, 0), math.pi * 20),
    (b"G3 X0 Y10 Z2 I-10 J0 E1", (0, 10, 2), math.hypot(math.pi * 5, 2)),
])
def test_arcs_have_short_chords_and_correct_length(command, finish, length):
    geometry = parsed(b"G1 X10\n" + PREFIX + command + b"\n")
    try:
        assert geometry.source_segments == 1
        assert geometry.extrude_length == pytest.approx(length, rel=0.001)
        points = list(geometry.path_points(0, geometry.point_count))
        assert points[-1] == pytest.approx(finish)
        assert all(math.dist(a, b) < 1.01 for a, b in zip(points, points[1:]))
    finally:
        geometry.close()


def test_tiny_full_circle_cannot_collapse_to_its_identical_endpoints():
    geometry = parsed(b"G1 X0.001\n" + PREFIX + b"G3 I-0.001 E1\n")
    try:
        assert geometry.source_segments == 1 and geometry.point_count >= 4 and geometry.extrude_length > 0
    finally:
        geometry.close()


@pytest.mark.parametrize("bad", [b"G1 Xnan", b"G1 Xinf", b"G1 X1.2.3", b"G1 X1 X2", b"G1 X1 A2", b"G1 X100000000",
                                 b"G1broken", b"G2 X1 E1", b"G2 X1 I1 J1 R1", b"G3 X100 I1 E1", b"G3 X1 R0.1",
                                 b"G20", b"G18", b"G5 X1", b"G5.2 X1", b"G52 X1", b"G68 X1", b"G28", b"G10 L2 P1", b"T256", b"T1oops", b"X2 Y1",
                                 b"N123 G1 X1 E1", b"G92.1", b"G92",
                                 b";TYPE:" + b"x" * 41, b";Z:nan", b"G1 X1\xff"])
def test_bad_motion_refuses_the_whole_preview_with_line_number(bad):
    with pytest.raises(tp.ToolpathError) as error:
        parsed(PREFIX + b"G1 X1 E1\n" + bad + b"\nG1 X2 E1\n")
    assert error.value.line == 8


def test_limits_and_deadline(monkeypatch):
    with pytest.raises(tp.ToolpathError, match="input limit"):
        parsed(PREFIX + b";" + b"x" * tp.MAX_LINE)
    with pytest.raises(tp.ToolpathError, match="deadline"):
        tp.parse(io.BytesIO(PREFIX), budget=tp.Budget(-1))
    for name, value in (("MAX_INPUT", 1), ("MAX_SOURCE_POINTS", 1), ("MAX_PATHS", 0), ("MAX_LAYERS", 0)):
        with monkeypatch.context() as patch:
            patch.setattr(tp, name, value)
            with pytest.raises(tp.ToolpathError):
                parsed(PREFIX + b"G1 X1 E1\n")
    with pytest.raises(tp.ToolpathError, match="no printable"):
        parsed(PREFIX + b"G1 X1\n")
    with pytest.raises(tp.ToolpathError, match="too many features"):
        parsed(PREFIX + b"".join(f";TYPE:Kind {i}\n".encode() for i in range(255)))


def test_plate_is_streamed_from_zip_not_read_whole(tmp_path, monkeypatch):
    path, output = tmp_path / "sliced.3mf", tmp_path / "preview.gz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_2.gcode", PREFIX + b"G1 X1 E1\n")
        archive.writestr("Metadata/plate_1.gcode", b"bad")
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda *a: pytest.fail("whole ZIP entry read"))
    assert tp.build(path, 2, output)["points"] == 2
    assert decode(output.read_bytes())["segments"] == 1
    with pytest.raises(tp.ToolpathError):
        tp.build(path, 3, output)


def test_simplification_stays_within_tolerance_and_keeps_endpoints():
    points = [(i / 100, math.sin(i / 100) / 10, i / 1000) for i in range(1000)]
    kept = list(tp.simplify(iter(points), 0.05))
    assert kept[0] == points[0] and kept[-1] == points[-1] and len(kept) < len(points) / 10
    for a, b in zip(kept, kept[1:]):
        for p in points[points.index(a):points.index(b) + 1]:
            direction = [b[i] - a[i] for i in range(3)]
            t = max(0, min(1, sum((p[i] - a[i]) * direction[i] for i in range(3)) / sum(v*v for v in direction)))
            assert math.dist(p, [a[i] + t * direction[i] for i in range(3)]) <= 0.05


def test_size_stages_and_final_eight_million_limit(monkeypatch):
    monkeypatch.setattr(tp, "TARGET_POINTS", 5)
    monkeypatch.setattr(tp, "MAX_POINTS", 8)
    # Two seven-point zigzags: neither tolerance can erase a turn; alternate layers leaves seven, allowed after 0.2.
    layer = b";LAYER_CHANGE\nG1 X1 E1\nG1 X0 E1\nG1 X1 E1\nG1 X0 E1\nG1 X1 E1\nG1 X0 E1\n"
    result = encoded(b"M83\n;TYPE:Brim\n" + layer * 2)
    assert (result["flags"], result["points"], result["source_layers"], result["segments"]) == (7, 7, 2, 12)
    assert result["tolerance"] == pytest.approx(0.2)
    with pytest.raises(tp.ToolpathError, match="too_large"):
        encoded(b"M83\n;TYPE:Brim\n" + layer * 4)
    monkeypatch.setattr(tp, "MAX_BYTES", 64)
    with pytest.raises(tp.ToolpathError, match="too_large"):
        encoded(PREFIX + b"G1 X1 E1\n")


def test_travel_is_dropped_first_and_only_the_needed_flags_are_set(monkeypatch):
    monkeypatch.setattr(tp, "TARGET_POINTS", 5)
    result = encoded(PREFIX + b"G1 X1 E1\nG0 X2\nG0 X3\nG0 X4\nG0 X5\n")
    assert result["flags"] == 4 and result["points"] == 2 and result["segments"] == 5 and result["tolerance"] == 0
    result = encoded(PREFIX + b"".join(f"G1 X{i} E1\n".encode() for i in range(1, 7)))
    assert result["flags"] == 5 and result["points"] == 2 and result["tolerance"] == pytest.approx(0.05)


def test_size_rule_on_a_six_million_point_file(tmp_path):
    path = tmp_path / "six-million.gcode"
    with path.open("wb") as source:
        source.write(b"G90\nM83\nG1 X1 Y1\n;TYPE:Outer wall\n")
        for layer in range(4):
            source.write(f";LAYER_CHANGE\n;Z:{layer / 5}\n;HEIGHT:0.2\n".encode())
            first, second = (b"G1 X0 Y0 E1\n", b"G1 X1 Y1 E1\n") if layer % 2 == 0 else (b"G1 X1 Y1 E1\n", b"G1 X0 Y0 E1\n")
            source.write((first + second) * 749_999 + first)
    with path.open("rb") as source:
        geometry = tp.parse(source, directory=tmp_path)
    assert geometry.point_count == 6_000_000
    output = io.BytesIO()
    tp.write_pstp(geometry, output)
    result = decode(output.getvalue())
    assert result["flags"] == 7 and result["tolerance"] == pytest.approx(0.05)
    assert result["points"] == 3_000_000 and result["source_layers"] == 4 and result["segments"] == 5_999_996
    assert [layer[0] for layer in result["layers"]] == pytest.approx([0, 0.4])
