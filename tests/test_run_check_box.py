"""v0.81: the REAL runner on a box: /opt/ps-checker/run-check through sudo as the hub's user, the way the hub calls it, with the installed
ps-checker image (ChatGPT's review of the shop-box move: the runner's own path, its directory races and its cleanup). Runs only as root
on a machine set up like the shop box (oes-pool-deploy scripts/install-checker.py ran there) and only when asked:
PS_CHECKER_BOX_TESTS=1 python3 -m pytest tests/test_run_check_box.py. Skipped everywhere else."""

import json
import os
import pwd
import shutil
import signal
import struct
import subprocess
import time
import uuid
from pathlib import Path

import pytest

RUN_CHECK = "/opt/ps-checker/run-check"
SPOOL = Path("/var/lib/makeros-hub/spool")
STAGE = Path("/var/lib/ps-checker/run")
HUB = "makeros-hub"


def _on_a_box() -> bool:
    if os.environ.get("PS_CHECKER_BOX_TESTS") != "1" or os.geteuid() != 0 or not os.path.exists(RUN_CHECK):
        return False
    try:
        pwd.getpwnam(HUB)
    except KeyError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _on_a_box(), reason="not a box set up by install-checker.py, or PS_CHECKER_BOX_TESTS is not 1")
CUBE = bytearray(80) + struct.pack("<I", 12)
for _a, _b, _c in [((0, 0, 0), (0, 20, 0), (20, 20, 0)), ((0, 0, 0), (20, 20, 0), (20, 0, 0)), ((0, 0, 20), (20, 0, 20), (20, 20, 20)),
                   ((0, 0, 20), (20, 20, 20), (0, 20, 20)), ((0, 0, 0), (20, 0, 0), (20, 0, 20)), ((0, 0, 0), (20, 0, 20), (0, 0, 20)),
                   ((0, 20, 0), (0, 20, 20), (20, 20, 20)), ((0, 20, 0), (20, 20, 20), (20, 20, 0)), ((0, 0, 0), (0, 0, 20), (0, 20, 20)),
                   ((0, 0, 0), (0, 20, 20), (0, 20, 0)), ((20, 0, 0), (20, 20, 0), (20, 20, 20)), ((20, 0, 0), (20, 20, 20), (20, 0, 20))]:
    CUBE += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in (_a, _b, _c)) + b"\0\0"
JOB = {"name": "model.stl", "units": "file", "scale": 1, "models": ["A1 Mini"], "minWall": 0.84, "picture": True, "stl": False}


def as_hub(*cmd, **kw):
    return subprocess.run(["setpriv", f"--reuid={HUB}", f"--regid={HUB}", "--init-groups", "--", *cmd], **kw)


def job(job=JOB, model=bytes(CUBE)) -> tuple[str, Path]:
    """A job folder as the hub writes one: <spool>/<id>/.check owned by the hub, its job.json and model."""
    uid = uuid.uuid4().hex
    work = SPOOL / uid / ".check"
    as_hub("mkdir", "-p", "-m", "0700", str(work), check=True)
    as_hub("sh", "-c", 'cat > "$1"', "sh", str(work / "job.json"), input=json.dumps(job).encode(), check=True)
    if model is not None:
        as_hub("sh", "-c", 'cat > "$1"', "sh", str(work / "model.stl"), input=model, check=True)
    return uid, work


def through_sudo(uid: str, seconds: int = 60, env: dict | None = None) -> int:
    """run-check exactly as the hub runs it: the hub's user, sudo -n."""
    pre = ["env", *[f"{k}={v}" for k, v in (env or {}).items()]] if env else []
    return as_hub(*pre, "sudo", "-n", RUN_CHECK, uid, str(seconds), capture_output=True, timeout=seconds + 40).returncode


def nothing_left(uid: str) -> None:
    assert list(STAGE.iterdir()) == [], list(STAGE.iterdir())
    names = subprocess.run(["docker", "ps", "-a", "--filter", f"name=ps-check-{uid}", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert names.strip() == "", names


@pytest.fixture(autouse=True)
def _clean():
    yield
    for p in SPOOL.iterdir():
        shutil.rmtree(p, ignore_errors=True)


def test_a_check_through_sudo_as_the_hub_comes_back_and_leaves_nothing():
    uid, work = job()
    assert through_sudo(uid) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    assert report["ok"] is True and report["report"]["parts"]["count"] == 1
    assert (work / "out" / "picture.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert os.stat(work / "out" / "report.json").st_uid == pwd.getpwnam(HUB).pw_uid   # written back as the hub
    nothing_left(uid)


def test_a_job_file_linked_to_a_file_only_root_may_read_is_refused():
    for name in ("model.stl", "job.json"):
        uid, work = job(model=None if name == "model.stl" else bytes(CUBE))
        target = work / name
        if target.exists():
            target.unlink()
        as_hub("ln", "-s", "/etc/shadow", str(target), check=True)
        assert through_sudo(uid) == 2
        out = work / "out"
        assert not out.exists() or all(b"root:" not in p.read_bytes() for p in out.iterdir())
        nothing_left(uid)


def test_a_job_folder_swapped_for_a_link_reaches_nothing_the_hub_cannot_read():
    secret = Path("/root/ps-check-secret")
    secret.mkdir(mode=0o700, exist_ok=True)
    (secret / "job.json").write_text(json.dumps(JOB))
    (secret / "model.stl").write_bytes(bytes(CUBE))
    uid = uuid.uuid4().hex
    as_hub("mkdir", "-p", str(SPOOL / uid), check=True)
    as_hub("ln", "-s", str(secret), str(SPOOL / uid / ".check"), check=True)
    try:
        assert through_sudo(uid) == 2
        assert not (secret / "out").exists()
        nothing_left(uid)
    finally:
        shutil.rmtree(secret)


def test_outputs_go_back_only_where_the_hub_itself_may_write():
    target = Path("/root/ps-check-target")
    target.mkdir(mode=0o755, exist_ok=True)
    uid, work = job()
    as_hub("ln", "-s", str(target), str(work / "out"), check=True)   # the hub points its out/ at a folder only root may write
    try:
        through_sudo(uid)
        assert list(target.iterdir()) == []
        nothing_left(uid)
    finally:
        shutil.rmtree(target)


def test_the_hub_cannot_choose_the_image():
    uid, work = job()
    assert through_sudo(uid, env={"PS_CHECKER_IMAGE": "ps-checker:test-stubborn"}) == 0   # sudo drops it; the installed image runs
    assert json.loads((work / "out" / "report.json").read_text())["ok"] is True
    nothing_left(uid)


@pytest.fixture(scope="module")
def stubborn():
    """An image that ignores the stop signal, as root alone may name one (PS_CHECKER_IMAGE outside sudo)."""
    d = Path("/tmp/ps-check-stubborn")
    d.mkdir(exist_ok=True)
    (d / "Dockerfile").write_text("FROM node:22.20.0-alpine3.22\nENTRYPOINT [\"sh\", \"-c\", \"trap '' TERM INT; sleep 600\"]\n")
    subprocess.run(["docker", "build", "-q", "-t", "ps-checker:test-stubborn", str(d)], check=True, capture_output=True)
    yield
    subprocess.run(["docker", "image", "rm", "-f", "ps-checker:test-stubborn"], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_a_container_that_ignores_the_stop_is_killed_and_removed_at_the_deadline(stubborn):
    uid, _work = job()
    started = time.monotonic()
    rc = subprocess.run([RUN_CHECK, uid, "5"], env={**os.environ, "PS_CHECKER_IMAGE": "ps-checker:test-stubborn"}, capture_output=True, timeout=60).returncode
    assert rc == 124 and time.monotonic() - started < 20
    nothing_left(uid)


def test_an_interrupted_run_removes_its_container_and_its_staged_job(stubborn):
    uid, _work = job()
    p = subprocess.Popen([RUN_CHECK, uid, "60"], env={**os.environ, "PS_CHECKER_IMAGE": "ps-checker:test-stubborn"})
    for _ in range(100):
        if subprocess.run(["docker", "ps", "--filter", f"name=ps-check-{uid}", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.strip():
            break
        time.sleep(0.2)
    else:
        p.kill()
        pytest.fail("the container never started")
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 143
    nothing_left(uid)
