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


def _box_stl(x: float, y: float, z: float) -> bytes:
    p = lambda i: (x if i & 1 else 0.0, y if i & 2 else 0.0, z if i & 4 else 0.0)
    quads = [[0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4], [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5]]
    tris = [t for a, b, c, d in quads for t in ([a, b, c], [a, c, d])]
    out = bytearray(80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for v in t:
            out += struct.pack("<3f", *p(v))
        out += b"\0\0"
    return bytes(out)


def _slice(tmp_path: Path, stl: bytes, scale: float) -> tuple[dict, str]:
    job = tmp_path / "job"
    job.mkdir()
    (job / "part.stl").write_bytes(stl)
    (job / "job.json").write_text(json.dumps({"mesh": "part.stl", "model": "A1 Mini", "scale": scale,
                                              "filament": {"type": "PLA", "colorHex": "#FFFFFF", "trayInfoIdx": None},
                                              "fixes": {"orient": False, "brim": False, "supports": False}}))
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
