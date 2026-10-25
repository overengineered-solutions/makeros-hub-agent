"""Send to PS step 2 (review): real slices through the shop's ps-slicer image, with this branch's ps_slice.py mounted over the
image's, measuring the printed part from the G-code against the size the site asked for. The site sends one factor (the file's
units and the member's scale together); the hub must apply it exactly once. Runs where docker and the image are present
(PS_SLICER_IMAGE, default ps-slicer:2.4.2-1); skipped elsewhere."""

import json
import os
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest

IMAGE = os.environ.get("PS_SLICER_IMAGE", "ps-slicer:2.4.2-1")
WRAPPER = Path(__file__).resolve().parents[1] / "slicer" / "ps_slice.py"


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_present(), reason=f"docker image {IMAGE} not present")


def _boxes_stl(*boxes: tuple[float, float, float, float, float, float]) -> bytes:
    """Boxes (x0, y0, z0, x1, y1, z1) as one binary STL with each facet's normal worked out from its winding, as the site's
    checked mesh carries them (pstation printability.ts toBinaryStl)."""
    quads = [[0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4], [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5]]
    tris = []
    for x0, y0, z0, x1, y1, z1 in boxes:
        p = lambda i: (x1 if i & 1 else x0, y1 if i & 2 else y0, z1 if i & 4 else z0)
        tris += [(p(a), p(b), p(c)) for q in quads for a, b, c in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))]
    out = bytearray(80) + struct.pack("<I", len(tris))
    for a, b, c in tris:
        u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        length = sum(k * k for k in n) ** 0.5 or 1.0
        out += struct.pack("<3f", *(k / length for k in n)) + b"".join(struct.pack("<3f", *t) for t in (a, b, c)) + b"\0\0"
    return bytes(out)


def _box_stl(x: float, y: float, z: float) -> bytes:
    return _boxes_stl((0.0, 0.0, 0.0, x, y, z))


def _slice(tmp_path: Path, stl: bytes, scale: float = 1.0, model: str = "A1 Mini", nozzle: str | None = None,
           colours: dict | None = None,
           **fixes: bool) -> tuple[dict, str]:
    job = tmp_path / "job"
    job.mkdir()
    (job / "part.stl").write_bytes(stl)
    (job / "job.json").write_text(json.dumps({"nozzleDiameterMm": 0.4, "mesh": "part.stl", "model": model, "scale": scale,
                                              "filament": colours["filaments"][0] if colours else {"type": "PLA", "colorHex": "#FFFFFF", "trayInfoIdx": None},
                                              "fixes": {"orient": False, "brim": False, "supports": False, **fixes},
                                              **({"colours": colours} if colours else {}),
                                              **({"nozzle": nozzle} if nozzle else {})}))
    os.chmod(job, 0o777)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:size=1g,mode=1777",
                    "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{job}:/job",
                    "-v", f"{WRAPPER}:/opt/ps-slice/ps_slice.py:ro", IMAGE], capture_output=True, timeout=600)
    result = json.loads((job / "out" / "result.json").read_text())
    gcode = ""
    if result.get("ok"):
        with zipfile.ZipFile(job / "out" / "sliced.3mf") as z:
            gcode = z.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
    return result, gcode


NOT_THE_PART = re.compile(r"skirt|brim|prime|purge|custom|wipe", re.I)


def _extents(gcode: str) -> tuple[float, float, float]:
    """The part's printed X and Y span (outer wall centre lines) and its top layer's height, from the moves that extrude
    inside the part's own features (Orca's '; FEATURE:' markers), so the start purge line and any skirt are left out."""
    x = y = z = 0.0
    feature = None
    xs, ys, zs = [], [], []
    for line in gcode.splitlines():
        if line.startswith("; FEATURE:"):
            feature = line.split(":", 1)[1].strip()
            continue
        if not line.startswith(("G1 ", "G0 ", "G2 ", "G3 ")):
            continue
        words = dict((w[0], w[1:]) for w in line.split(";")[0].split()[1:] if len(w) > 1)
        try:
            x, y, z = float(words.get("X", x)), float(words.get("Y", y)), float(words.get("Z", z))
            e = float(words.get("E", 0))
        except ValueError:
            continue
        if e > 0 and feature and not NOT_THE_PART.search(feature):
            xs.append(x), ys.append(y), zs.append(z)
    assert xs, "no extrusion inside the part"
    return max(xs) - min(xs), max(ys) - min(ys), max(zs)


LINE = 0.42   # the A1 mini process's outer wall line: the centre lines sit half a line inside each face


@pytest.mark.parametrize("name, file_size, scale, printed", [
    # a 20 x 10 x 6 m building drawn in millimetres, at 1:150 and at 1:1000
    ("1:150", (20000, 10000, 6000), 1 / 150, (133.33, 66.67, 40.0)),
    ("1:1000", (20000, 10000, 6000), 1 / 1000, (20.0, 10.0, 6.0)),
    # drawn in metres (24 x 12 x 6) and named so on the site at 1:200: the site sends 1000 / 200 = 5 on the file as read
    ("metres at 1:200", (24, 12, 6), 1000 / 200, (120.0, 60.0, 30.0)),
])
def test_the_part_prints_at_the_size_asked(tmp_path, name, file_size, scale, printed):
    result, gcode = _slice(tmp_path, _box_stl(*file_size), scale)
    assert result["ok"], result
    assert (result["scaleMin"], result["scaleMax"]) == (0.001, 1000.0) and result["lineWidth"]["A1 Mini"] == LINE
    assert result["settings"]["sparse_infill_pattern"] == "rectilinear"
    sx, sy, top = _extents(gcode)
    assert sorted([sx, sy]) == pytest.approx(sorted([printed[0] - LINE, printed[1] - LINE]), abs=0.3), (name, sx, sy)
    assert top == pytest.approx(printed[2], abs=0.21), (name, top)


def test_a_scale_past_the_range_is_refused_with_its_sentence(tmp_path):
    result, _ = _slice(tmp_path, _box_stl(10, 10, 10), 0.0005)
    assert result["ok"] is False and "size change" in result["error"] and result["scaleMin"] == 0.001


def test_the_bed_is_judged_at_the_printed_size(tmp_path):
    # the 20 m building at 1:100 is 200 mm long, past the A1 mini's 180; at 1:150 (above) it fit
    result, _ = _slice(tmp_path, _box_stl(20000, 10000, 6000), 1 / 100)
    assert result["ok"] is False and "does not fit on this printer's bed" in result["error"]


# board 72f424f2: an unticked brim is no brim and a ticked one is one, in the G-code itself (OrcaSlicer's own default is auto_brim,
# which the Bambu presets leave in place)
@pytest.mark.parametrize("brim", [False, True])
def test_the_brim_is_in_the_gcode_only_when_ticked(tmp_path, brim):
    result, gcode = _slice(tmp_path, _box_stl(20, 20, 10), brim=brim)
    assert result["ok"], result
    assert ("; FEATURE: Brim" in gcode) is brim


def _laid(gcode: str) -> dict[str, list[tuple[float, float]]]:
    """Where each feature extrudes (the end of each extruding G1), by Orca's '; FEATURE:' name."""
    x = y = 0.0
    feature, out = "", {}
    for line in gcode.splitlines():
        if line.startswith("; FEATURE:"):
            feature = line.split(":", 1)[1].strip()
        elif line.startswith("G1 "):
            words = dict((w[0], w[1:]) for w in line.split(";")[0].split()[1:] if len(w) > 1)
            x, y = float(words.get("X", x)), float(words.get("Y", y))
            if float(words.get("E", 0)) > 0:
                out.setdefault(feature, []).append((x, y))
    return out


# supports ticked are reported only where the slice lays them (v0.82): a cube gets none; a 30 mm bridge 10 mm up gets them under it,
# while a 15 mm one beside it gets none, as the site's check says (printability.ts BRIDGE_MM 20), with Bambu's max_bridge_length 0
def test_supports_are_reported_only_where_the_slice_lays_them(tmp_path):
    (tmp_path / "cube").mkdir()
    cube, _ = _slice(tmp_path / "cube", _box_stl(20, 20, 10), supports=True)
    assert cube["ok"] and cube["supportUsed"] is False, cube
    (tmp_path / "bridges").mkdir()
    bridges, gcode = _slice(tmp_path / "bridges", _boxes_stl(
        (0, 0, 0, 8, 8, 10), (23, 0, 0, 31, 8, 10), (0, 0, 10, 31, 8, 12),                        # a 15 mm bridge
        (0, 20, 0, 8, 28, 10), (38, 20, 0, 46, 28, 10), (0, 20, 10, 46, 28, 12)), supports=True)   # a 30 mm one
    assert bridges["ok"] and bridges["supportUsed"] is True, bridges
    laid = _laid(gcode)
    x0, y0 = min(p[0] for p in laid["Outer wall"]), min(p[1] for p in laid["Outer wall"])
    support = [p for f, ps in laid.items() if f.startswith("Support") for p in ps]
    under = lambda ya, yb: sum(1 for px, py in support if 9 <= px - x0 <= 22 and ya <= py - y0 <= yb)
    assert under(0, 8) <= 2 < under(20, 28)


# board 459044ef: the H2D slices the member's filament on the nozzle the spec names, and hub 0.77's dispatch reads that nozzle off
# the plate (slice_info group_id and filament_maps, project_settings physical_extruder_map)
@pytest.mark.parametrize("nozzle, extruder", [("left", 1), ("right", 0)])
def test_the_h2d_slices_on_the_nozzle_the_spec_names_and_dispatch_reads_it(tmp_path, nozzle, extruder):
    from makeros_hub.vprinter.capture import parse_required_filaments
    result, _ = _slice(tmp_path, _box_stl(20, 20, 10), model="H2D", nozzle=nozzle)
    assert result["ok"] and result["modelId"] == "O1D" and result["lineWidth"]["H2D"] == LINE, result
    (filament,) = parse_required_filaments(tmp_path / "job" / "out" / "sliced.3mf", 1)
    assert filament["extruderId"] == extruder and filament["nozzleDiameterMm"] == 0.4


@pytest.mark.parametrize("model", ["A1 Mini", "P2S", "X1C", "H2D"])
def test_layer_colours_keep_both_spools_presets_and_h2d_nozzles(tmp_path, model):
    from makeros_hub.vprinter.capture import parse_required_filaments
    filaments = [dict(type="PLA", material="PLA", colorHex=colour, trayInfoIdx=tray, nozzle=nozzle)
                 for colour, tray, nozzle in [("#FF0000", "GFA00", "right"), ("#0000FF", None, "left")]]
    colours = {"filaments": filaments, "parts": [{"first": 0, "count": 12, "filament": 0}, {"first": 12, "count": 12, "filament": 1}]}
    result, _ = _slice(tmp_path, _boxes_stl((0, 0, 0, 20, 20, 10), (20, 0, 0, 40, 20, 10)),
                       scale=0.5, model=model, nozzle="right" if model == "H2D" else None, colours=colours)
    assert result["ok"] and result["plates"] == 1, result
    assert result["prepareVersion"] == 2 and result["colours"] is True
    assert "Bambu PLA Basic" in result["filament"] and "Generic PLA" in result["filament"]
    used = parse_required_filaments(tmp_path / "job/out/sliced.3mf", 1)
    assert len(used) == 2
    for actual, expected in zip(sorted(used, key=lambda f: f["slot"]), filaments):
        assert actual["color"] == expected["colorHex"][1:] + "FF"
        assert actual["material"] == "PLA"
        if model == "H2D":
            assert actual["extruderId"] == (0 if expected["nozzle"] == "right" else 1)
            assert actual["nozzleDiameterMm"] == 0.4


# The edges the site's check takes from this slicer (pstation printability.ts PLATES, 2026-10-23): about 1.25 mm clear of every edge,
# the X1C's 250 mm height and the corner it keeps clear (a part beside it, turned round on the bed if need be, its short side held to
# 234 because the arrange refused a 235 square and 235 x 245 yet took 235.5 x 253.5), the 300 mm of the H2D both nozzles reach, which
# turns nothing round. A part the check says fits as it sits must slice.
@pytest.mark.parametrize("model, nozzle, size, fits", [
    ("A1 Mini", None, (177.5, 177.5, 1), True), ("A1 Mini", None, (178, 178, 1), False),
    ("X1C", None, (10, 10, 249), True), ("X1C", None, (10, 10, 252), False),
    ("X1C", None, (234, 234, 1), True), ("X1C", None, (234, 253.5, 1), True), ("X1C", None, (253.5, 234, 1), True),
    ("X1C", None, (250, 230, 10), True), ("X1C", None, (235, 235, 1), False), ("X1C", None, (236, 236, 1), False),
    ("H2D", "left", (297.5, 317.5, 1), True), ("H2D", "right", (298, 318, 1), False), ("H2D", "left", (300, 297, 1), False),
])
def test_the_bed_edges_the_sites_check_uses(tmp_path, model, nozzle, size, fits):
    result, _ = _slice(tmp_path, _box_stl(*size), model=model, nozzle=nozzle)
    assert result["ok"] is fits, result
    if not fits:
        assert "does not fit on this printer's bed" in result["error"]
