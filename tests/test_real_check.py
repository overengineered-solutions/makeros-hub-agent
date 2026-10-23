"""v0.81: real checks through the shop's ps-checker image (checker/Dockerfile and the vendored checker/check.mjs), run with run-check's own
container flags on a job folder the way the hub writes one: a probe says the format and version, a part left in the air is found, Print
it's slicer copy comes back, and a job without network or a writable root still checks. Runs where docker and the image are present
(PS_CHECKER_IMAGE, default ps-checker:current); skipped elsewhere."""

import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

IMAGE = os.environ.get("PS_CHECKER_IMAGE", "ps-checker:current")
RUN_CHECK = Path(__file__).resolve().parents[1] / "checker" / "run-check"


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_present(), reason=f"docker image {IMAGE} not present")


def _boxes_stl(*boxes) -> bytes:
    tris = []
    for x0, y0, z0, x1, y1, z1 in boxes:
        p = lambda i: (x1 if i & 1 else x0, y1 if i & 2 else y0, z1 if i & 4 else z0)
        for a, b, c, d in [[0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4], [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5]]:
            tris += [[p(a), p(b), p(c)], [p(a), p(c), p(d)]]
    out = bytearray(80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for v in t:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    return bytes(out)


def _flags() -> list[str]:
    """run-check's docker run flags, read from the script itself, so this runs the container exactly as the box will."""
    line = re.search(r"docker run (.*?)\"\$IMAGE\"", RUN_CHECK.read_text().replace("\\\n", " "), re.S).group(1)
    return line.replace('"$uid:$gid"', f"{os.getuid()}:{os.getgid()}").split()


def _run(tmp_path, job: dict, model: bytes | None = None) -> tuple[dict, Path]:
    work = tmp_path / "job"
    work.mkdir()
    (work / "job.json").write_text(json.dumps(job))
    if model is not None:
        (work / "model.stl").write_bytes(model)
    cmd = ["docker", "run", *[f.replace('"$real"', str(work)) for f in _flags()], IMAGE]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return json.loads((work / "out" / "report.json").read_text()), work / "out"


def test_probe_says_its_format_version_and_limits(tmp_path):
    report, _ = _run(tmp_path, {"probe": True})
    assert report["probe"] is True and isinstance(report["format"], int) and report["format"] >= 1
    assert re.fullmatch(r"[0-9A-Za-z._-]{1,40}", report["version"])
    assert report["maxTriangles"] == 300000


def test_checks_a_part_left_in_the_air_and_draws_the_picture(tmp_path):
    job = {"name": "model.stl", "units": "file", "scale": 1, "models": ["P2S"], "minWall": 0.84, "picture": True, "stl": False}
    report, out = _run(tmp_path, job, _boxes_stl((0, 0, 0, 20, 20, 20), (30, 0, 3.4, 40, 10, 8.4)))
    assert report["ok"] is True
    assert report["report"]["parts"]["count"] == 2
    air = [f for f in report["report"]["findings"] if f["id"] == "in-the-air"]
    assert air and air[0]["detail"].startswith("A part 10 × 10 × 5 mm starts 3.4 mm above the bed.")
    assert (out / "picture.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_print_it_gets_its_slicer_copy(tmp_path):
    job = {"name": "model.stl", "units": "file", "scale": 0.5, "models": ["P2S"], "minWall": 0.84, "picture": False, "stl": True}
    report, out = _run(tmp_path, job, _boxes_stl((0, 0, 0, 20, 20, 20)))
    assert report["ok"] is True and report["sized"]["factor"] == 0.5
    stl = (out / "model.stl").read_bytes()
    assert len(stl) == 84 + 12 * 50 and struct.unpack_from("<I", stl, 80)[0] == 12
    assert not (out / "picture.png").exists()
