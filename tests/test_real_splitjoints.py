"""Real Orca slices of the proposed set and a selected approved piece; no printing or network."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_real_slice import IMAGE, _image_present, _boxes_stl

pytestmark = pytest.mark.skipif(not _image_present(), reason=f"docker image {IMAGE} not present")


@pytest.mark.parametrize("mode", ["fit", "floors"])
def test_slices_every_piece_then_only_the_exact_approved_piece(tmp_path, mode):
    job = tmp_path / "job"
    job.mkdir()
    layers = []
    if mode == "fit":
        data = _boxes_stl((0, 0, 0, 300, 20, 20))
    else:
        data = _boxes_stl((0, 0, 0, 30, 30, 10), (0, 0, 10, 30, 30, 20))
        layers = [dict(name="Floor 1", first=0, count=12), dict(name="Roof", first=12, count=12)]
    (job / "part.stl").write_bytes(data)
    spec = dict(mesh="part.stl", model="A1 Mini", scale=1, filament=dict(type="PLA", colorHex="#FFFFFF", trayInfoIdx=None),
                fixes=dict(orient=True, brim=False, supports=False),
                prepare=dict(version=1, thicken=False, moveLargeGaps=False, split=dict(mode=mode, bed=[177.5, 177.5, 180], clearance=.2, layers=layers)))
    slicer = Path(__file__).resolve().parents[1] / "slicer"
    command = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:size=1g,mode=1777",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{job}:/job", "-v", f"{slicer}:/opt/ps-slice:ro"]
    # An existing image can exercise the new wrapper with the locally cached, pinned geometry packages.
    if os.environ.get("PS_PREPARE_PACKAGES"):
        command += ["-v", f"{os.environ['PS_PREPARE_PACKAGES']}:/geometry:ro", "-e", "PYTHONPATH=/geometry"]
    command += [IMAGE]
    def run():
        (job / "job.json").write_text(json.dumps(spec))
        result = subprocess.run(command, capture_output=True, timeout=600)
        answer = json.loads((job / "out/result.json").read_text())
        assert result.returncode == 0 and answer.get("ok"), (answer, result.stderr.decode()[-1000:])
        return answer
    group = run()
    assert len(group["pieces"]) == len(group["prepare"]["pieces"]) == 3
    for i, piece in enumerate(group["pieces"]):
        assert piece["grams"] > 0 and piece["plates"] == 1
        assert (job / f"out/piece-{i}/sliced.3mf").exists()
    spec["piece"] = {k: group["prepare"]["pieces"][1][k] for k in ("index", "sha256")}
    chosen = run()
    assert "pieces" not in chosen and chosen["index"] == 1
    assert chosen["prepare"] == group["prepare"]
    assert chosen["grams"] == group["pieces"][1]["grams"]
