"""Members' file checks on the shop box (v0.81; pstation Send to PS step 4, operator 2026-10-22: "Let's go ahead and do the work to get it
on the shop box instead of raising the linode cap").

The site hands this hub one check at a time in the heartbeat (`checks`), the way it hands slice jobs (`fetches`), each with an attempt
token. The hub fetches the member's file with its hub token, writes <spool>/<id>/.check/job.json and model.<ext>, runs ONE root-owned
command, /opt/ps-checker/run-check <id> (oes-pool-deploy scripts/install-checker.py installs it; checker/ in this repo is the container
and the site's own check code, bundled by pstation scripts/build-checker.mjs), sends Print it's slicer copy when asked and posts the
report. The site takes it only for this hub's run with its token: a check the member cancelled, or one the site took back, is refused
there and simply dropped here. The heartbeat says what the installed checker is (`checker`: its format and version, from a probe of the
image itself), and the site hands checks only to a box whose format is at least its own.

Stdlib only, like the rest of the agent.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

log = logging.getLogger("makeros_hub.checks")

CHECK_RUNNER = os.environ.get("MAKEROS_HUB_CHECK_RUNNER", "/opt/ps-checker/run-check")
CHECK_TIMEOUT_SEC = 180          # run-check stops the container at 150 s; this is the sudo call's own outer bound
MAX_CHECK_BYTES = 32 * 1024 * 1024   # the site's check budget (pstation printability.ts MAX_FILE_BYTES)
MAX_IMAGE_BYTES = 2_000_000
# the site holds one member file in memory at a time and answers 503 while another upload has it: wait 2 s a try, up to a minute for
# the fetch and a minute for the slicer's copy, inside the check's 5-minute lease (pstation scripts/send-to-ps.mjs waits the same way)
BUSY_TRIES = 30
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME = re.compile(r"^model\.(stl|obj|3mf)$")
_VERSION = re.compile(r"^[0-9A-Za-z._-]{1,40}$")
UNITS = ("file", "millimeter", "centimeter", "meter", "inch", "foot", "micron")


def run_check_job(uid: str) -> int:
    """The runner's exit code: 0 when the checker ran (its report says how the check went), 124 at the time limit, 137 or 134 when it
    ran out of memory."""
    return subprocess.run(["sudo", "-n", CHECK_RUNNER, uid], capture_output=True, timeout=CHECK_TIMEOUT_SEC).returncode


def checker_installed() -> bool:
    return os.path.isfile(CHECK_RUNNER) and os.access(CHECK_RUNNER, os.X_OK)


def parse_checker(report) -> dict | None:
    """What a checker's report.json says it is, strictly: {format: 0..1000, version: 1-40 letters, digits, . _ -}; else unknown."""
    if not isinstance(report, dict):
        return None
    fmt, version = report.get("format"), report.get("version")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or not 0 <= fmt <= 1000 or not isinstance(version, str) or not _VERSION.match(version):
        return None
    return {"format": fmt, "version": version}


class CheckerInfo:
    """What the installed ps-checker image says it is, for the heartbeat (`checker`): read from the image itself (a probe job, and every
    check's report), never from the agent's version, since install-checker.py installs the image by its own box tap. Refreshed every
    REFRESH_SEC; None = not known, and the heartbeat then says nothing, so the site hands this box no checks."""
    REFRESH_SEC = 15 * 60

    def __init__(self) -> None:
        self.value: dict | None = None
        self.checked_at: float | None = None

    def seen(self, report) -> None:
        self.value = parse_checker(report)
        self.checked_at = time.monotonic()

    def stale(self) -> bool:
        return self.checked_at is None or time.monotonic() - self.checked_at > self.REFRESH_SEC


CHECKER = CheckerInfo()


def probe_checker(spool_dir, runner=run_check_job) -> dict | None:
    """Ask the installed image what it is: a job of {"probe": true} through the runner a check uses, in the spool like one. Leaves
    nothing behind; an image that cannot answer is unknown."""
    uid = uuid.uuid4().hex
    root = Path(spool_dir) / uid
    try:
        work = root / ".check"
        work.mkdir(parents=True, mode=0o700)
        (work / "job.json").write_text(json.dumps({"probe": True}), encoding="utf-8")
        runner(uid)
        return json.loads((work / "out" / "report.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run says nothing; the checker is then unknown
        log.info("checker probe: no answer (%s)", exc)
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def job_of(check) -> dict | None:
    """A check from the heartbeat, read strictly: only what the checker reads goes into its job file, and only a path to this check's own
    address on the site is ever fetched. None for anything else (skipped, never half-run)."""
    if not isinstance(check, dict):
        return None
    cid, attempt, name, size, path = (check.get(k) for k in ("checkId", "attempt", "fileName", "sizeBytes", "path"))
    units, scale, models, min_wall = (check.get(k) for k in ("units", "scale", "models", "minWall"))
    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")
    if not (isinstance(cid, str) and _UUID.match(cid) and isinstance(attempt, str) and _UUID.match(attempt)
            and isinstance(name, str) and _NAME.match(name) and isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_CHECK_BYTES
            and path == f"/api/print/hub/check/{cid}" and units in UNITS and num(scale) and scale > 0 and num(min_wall) and 0.1 <= min_wall <= 10
            and isinstance(models, list) and len(models) <= 8 and all(isinstance(m, str) and 0 < len(m) <= 40 for m in models)
            and isinstance(check.get("picture"), bool) and isinstance(check.get("stl"), bool)):
        return None
    return {"checkId": cid, "attempt": attempt, "path": path, "sizeBytes": size,
            "job": {"name": name, "units": units, "scale": float(scale), "models": list(models), "minWall": float(min_wall),
                    "picture": check["picture"], "stl": check["stl"]}}


def outcome_of(report, rc: int, out: Path) -> dict:
    """What to tell the site: the checker's own report when it wrote one, else what its exit code says (the time limit, memory)."""
    if isinstance(report, dict) and report.get("ok") is True and isinstance(report.get("report"), dict) and isinstance(report.get("sized"), dict):
        picture = out / "picture.png"
        image = None
        if picture.is_file() and picture.stat().st_size <= MAX_IMAGE_BYTES:
            image = base64.b64encode(picture.read_bytes()).decode("ascii")
        return {"ok": True, "report": report["report"], "sized": report["sized"], "image": image}
    if isinstance(report, dict) and report.get("ok") is False and isinstance(report.get("error"), str):
        return {"ok": False, "error": report["error"][:300], "plain": report.get("plain") is True}
    return {"ok": False, "kind": "timeout" if rc == 124 else "memory" if rc in (134, 137) else "broke"}


def run_checks(checks, spool_dir, *, getter, post_json, post_bytes, runner=run_check_job, sleep=time.sleep) -> int:
    """Run the first valid check the site handed this beat (one at a time, on the agent's own worker thread) and post its report.
    `getter(path, dest) -> (sha256, size)`, `post_json(path, body) -> Response`, `post_bytes(path, data) -> status`, paths relative to
    the site. Returns 1 when the site took a report, else 0. Never leaves the job folder behind."""
    for raw in (checks if isinstance(checks, list) else []):
        c = job_of(raw)
        if c is None:
            log.warning("skipping a malformed check: %s", {k: raw.get(k) for k in ("checkId", "fileName", "path")} if isinstance(raw, dict) else type(raw).__name__)
            continue
        uid = c["checkId"].replace("-", "")
        root = Path(spool_dir) / uid
        work = root / ".check"
        try:
            if root.is_symlink():
                root.unlink()
            shutil.rmtree(root, ignore_errors=True)
            work.mkdir(parents=True, mode=0o700)
            model = work / c["job"]["name"]
            for tries in range(BUSY_TRIES, 0, -1):
                try:
                    _sha, size = getter(f"{c['path']}?attempt={c['attempt']}", model)
                    break
                except Exception as exc:  # noqa: BLE001 - cancelled, taken back or the site is down: the site's lease takes it back
                    # ponytail: get_to_file says the status only in its message
                    if tries > 1 and str(exc).endswith("HTTP 503"):
                        sleep(2)
                        continue
                    log.info("check %s: not fetched (%s)", c["checkId"], exc)
                    return 0
            if size != c["sizeBytes"]:
                log.warning("check %s: fetched %d bytes, the site said %d", c["checkId"], size, c["sizeBytes"])
                return 0
            (work / "job.json").write_text(json.dumps(c["job"]), encoding="utf-8")
            started = time.monotonic()
            try:
                rc = runner(uid)
            except Exception as exc:  # noqa: BLE001 - a runner that cannot start is a check that broke
                log.warning("check %s: runner failed to start: %s", c["checkId"], exc)
                rc = -1
            report = None
            try:
                report = json.loads((work / "out" / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = None
            checker = parse_checker(report) or CHECKER.value or {"format": 0, "version": "unknown"}
            if parse_checker(report):
                CHECKER.seen(report)   # every check says what the image that ran it is: a rolled-back image shows at once
            outcome = outcome_of(report, rc, work / "out")
            if outcome.get("ok") and c["job"]["stl"]:
                stl = work / "out" / "model.stl"
                data = stl.read_bytes() if stl.is_file() else None
                status = 0
                for tries in range(BUSY_TRIES, 0, -1):
                    status = post_bytes(f"{c['path']}/stl?attempt={c['attempt']}", data) if data is not None else 0
                    if status != 503 or tries == 1:
                        break
                    sleep(2)
                if status != 200:
                    log.warning("check %s: the site did not take the slicer's copy (%s)", c["checkId"], status)
            resp = post_json(f"{c['path']}/result", {"attempt": c["attempt"], "checker": checker, "outcome": outcome})
            ok = getattr(resp, "status", None) == 200 and isinstance(getattr(resp, "body", None), dict) and resp.body.get("ok") is True
            log.info("check %s: %s in %.1f s (rc %s), site %s", c["checkId"], "checked" if outcome.get("ok") else "not checked",
                     time.monotonic() - started, rc, "took it" if ok else f"refused it: {getattr(resp, 'body', resp)}")
            return 1 if ok else 0
        except Exception as exc:  # noqa: BLE001 - a failed check must never sink the loop; the site's lease takes it back
            log.warning("check %s: failed: %s", c["checkId"], exc)
            return 0
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return 0
