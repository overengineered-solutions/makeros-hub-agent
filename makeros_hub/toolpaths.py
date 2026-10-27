"""Durable optional previews: accepted fetched reports feed a worker, never the printer/heartbeat lane."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .http import TransportError, post_bytes
from .printers.queue_progress import write_json_durably
from .toolpath import DEADLINE_SECONDS, MAX_BYTES, sliced_sha256

log = logging.getLogger("makeros-hub.toolpaths")
RETRY_SECONDS = 3600
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
STATES = {"pending", "stored", "stopped", "failed", "too_large"}


def save(path, state):
    if not write_json_durably(path, json.dumps(state)):
        raise OSError("toolpath state could not be persisted")


def read(path):
    with path.open("rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("oversized toolpath state")
    state = json.loads(raw)
    if (not isinstance(state, dict) or type(state.get("v")) is not int or state["v"] != 1
            or not isinstance(state.get("jobId"), str) or not JOB_ID.fullmatch(state["jobId"])
            or not isinstance(state.get("sha256"), str) or not SHA.fullmatch(state["sha256"])
            or not isinstance(state.get("file"), str) or not state["file"] or len(state["file"]) > 255
            or state["file"] in (".", "..") or "/" in state["file"] or "\\" in state["file"]
            or type(state.get("plate")) is not int or not 1 <= state["plate"] <= 64
            or type(state.get("assignmentSeq")) is not int or not 0 <= state["assignmentSeq"] <= 2 ** 31 - 1
            or type(state.get("accepted")) is not bool or state.get("state") not in STATES
            or type(state.get("attempt")) is not int or not 0 <= state["attempt"] <= 1000
            or any(type(state.get(k)) not in (int, float) or not math.isfinite(state[k]) or state[k] < 0 for k in ("expires", "next"))):
        raise ValueError("invalid toolpath state")
    if path.name != f".toolpath-{state['jobId']}-{state['sha256']}-{state['plate']}-{state['assignmentSeq']}.json":
        raise ValueError("toolpath identity changed")
    return state


def prepare(job_id, sliced, plate, sha, *, assignment_seq=None):
    """Before reporting, leave a dormant request; only an accepted report arms it."""
    try:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or not SHA.fullmatch(sha):
            raise ValueError("invalid toolpath identity")
        seq = assignment_seq if type(assignment_seq) is int and 0 <= assignment_seq <= 2 ** 31 - 1 else 0
        path = sliced.parent / f".toolpath-{job_id}-{sha}-{plate}-{seq}.json"
        if not path.exists():
            save(path, {"v": 1, "jobId": job_id, "file": sliced.name, "sha256": sha, "plate": plate, "assignmentSeq": seq,
                        "accepted": False, "state": "pending", "expires": 0, "next": 0, "attempt": 0})
        return path
    except Exception as exc:  # an optional preview must never change the fetched report's outcome
        log.warning("toolpath failed: could not queue preview (%s); use the slice pictures", type(exc).__name__)
        return None


def accept(path, *, now=None):
    if path is None:
        return
    try:
        state = read(path)
        if not state["accepted"]:
            state.update(accepted=True, expires=(time.time() if now is None else now) + RETRY_SECONDS)
            save(path, state)
    except Exception as exc:
        log.warning("toolpath failed: could not arm preview (%s); use the slice pictures", type(exc).__name__)


def generate(sliced, plate, sha, output):
    """One child at a time, niceness 19, capped address space; no CPU-heavy Python on the heartbeat's interpreter."""
    result = subprocess.run([sys.executable, "-m", "makeros_hub.toolpaths", str(sliced), str(plate), sha, str(output)],
                            capture_output=True, timeout=DEADLINE_SECONDS + 5)
    if result.returncode:
        # Only our bounded JSON result, never stderr, G-code, filenames or member data, enters diagnostics.
        try:
            detail = json.loads(result.stdout[:1024])
        except (ValueError, UnicodeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        reason = "too_large" if detail.get("reason") == "too_large" else "failed"
        line = detail.get("line") if type(detail.get("line")) is int else 0
        raise PreviewFailed(reason, line)


class PreviewFailed(Exception):
    def __init__(self, reason, line=0):
        self.reason, self.line = reason, line


def discard_bytes(path):
    for artifact in (path.with_suffix(".gz"), path.with_suffix(".tmp")):
        artifact.unlink(missing_ok=True)


def process(path, url_for, bearer, *, now=None, poster=post_bytes, builder=generate):
    now = time.time() if now is None else now
    started = time.monotonic()
    state = read(path)
    sliced = path.parent / state["file"]
    if not sliced.is_file() or sliced.is_symlink():
        discard_bytes(path)
        path.unlink(missing_ok=True)
        return
    if state["state"] != "pending" or not state["accepted"] or state["next"] > now:
        return

    def finish(status, reason=None, line=0):
        state["state"] = status
        if reason:
            log.warning("toolpath %s: %s at line %d; the 3D preview is unavailable, use the slice pictures",
                        state["jobId"], reason, line)
        discard_bytes(path)
        if sliced.exists() and path.exists():
            save(path, state)

    if now >= state["expires"]:
        finish("failed", "upload retry expired")
        return
    output, partial = path.with_suffix(".gz"), path.with_suffix(".tmp")
    try:
        if sliced_sha256(sliced) != state["sha256"]:
            finish("stopped")
            return
        if not output.exists():
            partial.unlink(missing_ok=True)
            builder(sliced, state["plate"], state["sha256"], partial)
            with partial.open("rb") as data:
                os.fsync(data.fileno())
            os.replace(partial, output)
        if not sliced.exists() or not path.exists():
            discard_bytes(path)
            return
        if sliced_sha256(sliced) != state["sha256"]:
            finish("stopped")
            return
        if output.stat().st_size > MAX_BYTES:
            finish("too_large", "too_large")
            return
        remaining = state["expires"] - now - (time.monotonic() - started)
        if remaining <= 0:
            finish("failed", "upload retry expired")
            return
        # Reserve the retry BEFORE posting, so a restart cannot restart the backoff or the one-hour window.
        state["attempt"] += 1
        state["next"] = now + min(300, 10 * 2 ** min(state["attempt"] - 1, 5))
        save(path, state)
        with output.open("rb") as data:
            response = poster(url_for(state["jobId"]), data, bearer=bearer, timeout=min(30.0, remaining),
                              content_type="application/vnd.ps.toolpath", content_encoding="gzip", content_length=output.stat().st_size,
                              extra_headers={"X-PS-Sliced-Sha256": state["sha256"], "X-PS-Plate": str(state["plate"])}, response_body=True)
        if response.status == 204:
            finish("stored")
        elif response.status == 404 or response.status == 409 and response.body.get("error") == "slice_mismatch":
            finish("stopped")
        elif response.status == 409 and response.body.get("error") == "not_ready":
            pass
        elif response.status >= 500 or response.status == 429:
            log.warning("toolpath %s: site unavailable (%d), retrying", state["jobId"], response.status)
        else:
            finish("failed", f"upload refused ({response.status})")
    except TransportError:
        log.warning("toolpath %s: upload connection failed, retrying", state["jobId"])
    except PreviewFailed as exc:
        finish(exc.reason, exc.reason, exc.line)
    except subprocess.TimeoutExpired:
        finish("failed", "preview deadline exceeded")
    except Exception as exc:
        finish("failed", f"preview failed ({type(exc).__name__})")


def sweep(spool, url_for, bearer, **kwargs):
    for path in Path(spool).glob("*/.toolpath-*.json"):
        try:
            process(path, url_for, bearer, **kwargs)
        except Exception as exc:
            log.warning("toolpath failed: unreadable pending preview (%s); use the slice pictures", type(exc).__name__)


def start(cfg, bearer, stop, spool):
    def run():
        while not stop.is_set():
            try:
                sweep(spool, lambda job: cfg.file_url(f"/api/print/hub/toolpath/{job}"), bearer)
            except Exception as exc:
                log.warning("toolpath worker failed (%s); use the slice pictures", type(exc).__name__)
            stop.wait(10)
    worker = threading.Thread(target=run, name="toolpath-upload", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        log.warning("toolpath worker could not start; use the slice pictures")
        return None
    return worker


def main():
    import resource
    from .toolpath import ToolpathError, build
    try:
        os.nice(19)
        # The service shares a 600 MiB cgroup with cameras and printer control; tables are disk-backed.
        resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024))
        sliced, plate, sha, output = sys.argv[1:]
        with open(sliced, "rb") as source:
            import hashlib
            if hashlib.file_digest(source, "sha256").hexdigest() != sha:
                raise ToolpathError("slice changed")
            source.seek(0)
            build(source, int(plate), Path(output))
        return 0
    except Exception as exc:
        print(json.dumps({"reason": "too_large" if isinstance(exc, ToolpathError) and exc.reason == "too_large" else "failed",
                          "line": exc.line if isinstance(exc, ToolpathError) else 0}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
