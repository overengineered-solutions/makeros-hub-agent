"""Grab one JPEG frame from a Bambu X1 / H2 / P2S LAN camera (RTSPS :322, H.264).

Unlike the A1/P1 (`:6000` raw-JPEG — see bambu_camera.py), the X1 / X1C / X1E /
X2D / H2* / P2S stream H.264 over RTSP-over-TLS on :322 — verified vs
greghesp/ha-bambulab `models.py` CAMERA_RTSP + Doridian OpenBambuAPI `video.md`.
H.264 can't be decoded with the stdlib, so we shell out to ffmpeg for a single
keyframe → JPEG. ffmpeg is OPTIONAL: if it isn't installed, capture returns a
no-ffmpeg result (the caller degrades to no-frame) and a one-time warning is
logged. Requires "LAN Mode Liveview" ON on the printer (gates :322) + the LAN
access code (same code as MQTT/FTPS). Stdlib only (subprocess + select + shutil).

Credential exposure (accepted): ffmpeg takes the RTSP URL — which embeds the LAN
access code — in its argv, so the code is briefly visible in `ps`/`/proc` for
the ≤timeout life of the process. Accepted for this threat model: the hub is a
single-tenant shop appliance, the value is a LAN-local printer access code (not
an API key) that already lives on the box for MQTT/FTPS, and the process is
short-lived. We never log/echo it (redacted from any stderr before it leaves).

Socket timeout (v0.42.0): the prior v0.41.0 hard-coded `-stimeout`, which the
Pi's ffmpeg (Bookworm 5.1) rejects at argv-parse time ("Error splitting the
argument list: Option not found") — that broke ALL RTSP cameras the moment
v0.41.0 deployed. `-stimeout` was renamed to `-timeout` on the RTSP demuxer in
ffmpeg 5.0 and the alias dropped. We now (a) never hard-code an unprobed flag —
`_supported_timeout_flag()` runs ffmpeg ONCE at first use to discover which
socket-timeout flag this build actually accepts (preferring the portable
AVIO-level `-rw_timeout`), and (b) degrade safely to the Python `select()`
deadline if none is supported. An unrecognized flag can therefore never again
silently break capture.

Observability (v0.41.0+): on failure, capture_frame_with_reason returns a
CaptureResult with a categorized `reason` and a bounded redacted `stderr_tail`
so the cloud's `nativeprint.camera.no_frame` event names the cause. Reason
strings are a stable contract mirrored by the cloud copy map at
apps/web/app/(admin)/admin/3dprinting/hubs/printers-section.tsx (NoFrameHint).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

log = logging.getLogger("makeros-hub.printers")

RTSP_PORT = 322
# Per-capture wall-clock deadline. MUST stay below collect_camera_frames'
# overall_timeout (8s) so a single wedged capture is bounded by ITS OWN
# categorizable deadline rather than being abandoned (uncategorized) by the
# pool. Layering: socket flag (4s) < per-capture deadline (5.5s) < pool (8s).
_DEFAULT_TIMEOUT = 5.5
# ffmpeg socket I/O timeout (µs) spliced before -i when the build supports a
# socket-timeout flag. 4s < the 5.5s python deadline so ffmpeg self-exits
# (cleanly, categorizable) before python has to SIGKILL it.
_SOCKET_TIMEOUT_USEC = 4_000_000
# A 720p H.264 keyframe → JPEG is ~50-300 KB; cap so a misbehaving/hostile peer
# on :322 can't stream unbounded bytes into agent memory.
_MAX_FRAME_BYTES = 4 * 1024 * 1024
# Bounded stderr captured for the cloud's no_frame payload. ffmpeg with
# -loglevel error emits sub-1KB; 4KB is generous. We keep DRAINING the pipe
# past the cap (discarding) so a chatty child can't backpressure-stall us.
_MAX_STDERR_BYTES = 4 * 1024
STDERR_TAIL = 400
_SOI = b"\xff\xd8\xff"  # JPEG start-of-image
_EOI = b"\xff\xd9"  # JPEG end-of-image
_warned_no_ffmpeg = False

# ffmpeg argv-parse rejection signatures. Broad on purpose (Codex HIGH): ffmpeg
# phrases an unknown option as "Option <name> not found." with the flag NAME in
# the middle, so a literal "option not found" substring check would MISS it and
# false-accept a rejected flag — re-shipping the very regression this guards. The
# regex matches the real forms. Used BOTH to reject a probe candidate and to
# categorize a capture failure as 'ffmpeg-arg' (our bug, not the printer's).
_ARG_ERR_RE = re.compile(
    r"option\s.*\bnot found|could not find option|unrecognized option|"
    r"error splitting the argument list|trailing option",
)


# Categorized reason for the cloud's no_frame event payload. STABLE CONTRACT —
# the cloud admin UI maps each to operator-facing copy. Adding one here means
# adding its copy on the cloud side (printers-section.tsx NoFrameHint).
#   no-ffmpeg     — ffmpeg not installed on the hub (agent/host issue)
#   ffmpeg-arg    — ffmpeg rejected our argv (AGENT BUG, never the operator's)
#   liveview-off  — printer's LAN-Mode Liveview is off (RTSP 404 / stream gone)
#   auth-fail     — LAN access code rejected (401/403)
#   unreachable   — no route / refused / DNS / connection timed out
#   tls-error     — TLS handshake/cert failure
#   timeout       — our deadline or ffmpeg's socket timeout fired
#   bad-jpeg      — ffmpeg returned 0 but the bytes aren't a valid JPEG
#   unknown       — uncategorized (stderr_tail carries the raw cause)
NoFrameReason = str


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one capture attempt. On success: jpeg set, reason None,
    stderr_tail "". On failure: jpeg None, reason categorized, stderr_tail a
    redacted ≤STDERR_TAIL-char excerpt (or "" when ffmpeg never ran)."""

    jpeg: Optional[bytes]
    reason: Optional[NoFrameReason]
    stderr_tail: str


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@functools.lru_cache(maxsize=1)
def _supported_timeout_flag() -> tuple[str, ...]:
    """Discover ONCE (cached for the process) which socket-timeout flag this
    ffmpeg build accepts, by actually running ffmpeg with the candidate against
    a trivial lavfi input and checking for an argv-parse rejection. Returns the
    argv fragment to splice before -i (e.g. ('-rw_timeout','4000000')), or ()
    if none are accepted — in which case we rely on the Python deadline alone.

    Preference order: `-rw_timeout` (AVIO-level, one consistent meaning, present
    since ffmpeg 3.x and on 5.1) → `-timeout` (RTSP-demuxer socket timeout on
    ffmpeg ≥5, but overloaded/position-sensitive) → `-stimeout` (legacy, removed
    on 5.x — last resort for very old builds). Directly RUN-probing (not grepping
    `-h`) is what prevents a repeat of the v0.41.0 regression: we only ship a
    flag this exact binary has proven it accepts."""
    if not ffmpeg_available():
        return ()
    for flag in ("-rw_timeout", "-timeout", "-stimeout"):
        argv = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            flag,
            str(_SOCKET_TIMEOUT_USEC),
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=16x16:d=0",
            "-frames:v",
            "0",
            "-f",
            "null",
            "-",
        ]
        try:
            # 2s per candidate (≤6s total, once per process, ideally warmed at
            # startup) so a cold first capture can't blow the 8s pool budget.
            r = subprocess.run(argv, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return ()  # can't probe → degrade to python-deadline-only
        err = (r.stderr or "").lower()
        # Accept the flag ONLY if ffmpeg did not reject it as unknown. The regex
        # catches "Option <name> not found." etc. — a literal substring check
        # here would false-accept and re-ship the regression (Codex HIGH).
        if not _ARG_ERR_RE.search(err):
            log.info("rtsp camera: using socket-timeout flag %s", flag)
            return (flag, str(_SOCKET_TIMEOUT_USEC))
    log.warning(
        "rtsp camera: no ffmpeg socket-timeout flag accepted; "
        "relying on the per-capture deadline only"
    )
    return ()


def rtsp_url(host: str, access_code: str) -> str:
    """`rtsps://bblp:<code>@<host>:322/streaming/live/1` — quote the code so a
    stray character can't break the URL (no shell is involved; argv only)."""
    return f"rtsps://bblp:{quote(access_code, safe='')}@{host}:{RTSP_PORT}/streaming/live/1"


def ffmpeg_argv(host: str, access_code: str) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        # RTSP over TCP (more reliable than UDP through the shop LAN); ffmpeg
        # accepts the self-signed printer cert by default (no -tls_verify 1).
        "-rtsp_transport",
        "tcp",
        # Socket I/O timeout — only the flag this build PROVED it accepts (or
        # nothing). Must precede -i. Empty tuple = degrade to the python deadline.
        *_supported_timeout_flag(),
        "-i",
        rtsp_url(host, access_code),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]


def _redact_access_code(text: str, access_code: str) -> str:
    """Strip the LAN access code from ffmpeg stderr before it leaves this module
    — ffmpeg echoes the dial URL (which embeds the code) into its error output."""
    if not text or not access_code:
        return text
    return text.replace(access_code, "***").replace(quote(access_code, safe=""), "***")


def _categorize_stderr(stderr: str, returncode: Optional[int], timed_out: bool) -> NoFrameReason:
    """Map ffmpeg stderr + returncode + an explicit deadline flag → a stable
    reason. `timed_out` is the runner's authoritative signal that OUR deadline
    fired (not re-derived from wall-clock, which is brittle on a loaded Pi)."""
    blob = stderr.lower() if stderr else ""
    # OUR bug first — an unrecognized/garbled flag. Nothing else looks like this,
    # and it must never masquerade as an operator-fixable reason (this is exactly
    # the v0.41.0 -stimeout outage, which showed as 'unknown × 7').
    if _ARG_ERR_RE.search(blob):
        return "ffmpeg-arg"
    if timed_out:
        return "timeout"
    # Network reachability (incl. ffmpeg's own "Connection timed out" on connect).
    if any(
        m in blob
        for m in (
            "no route to host",
            "network is unreachable",
            "connection refused",
            "name or service not known",
            "failed to resolve",
            "host is down",
            "connection timed out",
        )
    ):
        return "unreachable"
    if "401 unauthorized" in blob or "403 forbidden" in blob:
        return "auth-fail"
    # Liveview-off: Bambu :322 closes the stream / 404s the DESCRIBE when LAN
    # Liveview is off. (Note: some firmwares simply refuse the TCP connect, which
    # lands in 'unreachable' above — validate against a real OFF printer via the
    # camera-test probe.) Require a 404/stream-gone signal, not a bare DESCRIBE
    # failure (a DESCRIBE 500 is a different problem).
    if (
        "404 not found" in blob
        or "rtsp/1.0 404" in blob
        or "stream not found" in blob
        or ("describe" in blob and "404" in blob)
    ):
        return "liveview-off"
    if ("tls" in blob or "ssl" in blob) and ("handshake" in blob or "error" in blob):
        return "tls-error"
    # ffmpeg's own socket-timeout (rw_timeout) firing, distinct from connect.
    if "operation timed out" in blob or "timed out" in blob:
        return "timeout"
    if returncode is None:
        return "timeout"  # killed (deadline/cap) with no actionable stderr
    return "unknown"


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group so a wedged ffmpeg (and any child
    it spawned) dies immediately instead of being orphaned past the deadline.
    Falls back to killing just the process if the group call fails."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_ffmpeg(
    cmd: list[str], timeout: float, max_bytes: int
) -> tuple[bytes, Optional[int], bytes, bool]:
    """Run ffmpeg, reading stdout INCREMENTALLY (select + read1) so memory is
    bounded to max_bytes+1, and time is bounded by `timeout`. stderr is captured
    to a bounded buffer AND kept draining past the cap (so a chatty child can't
    backpressure-stall stdout). Started in its own session so a wedged process
    group can be SIGKILLed rather than orphaned. Returns
    (stdout_bytes, returncode, stderr_bytes, timed_out); returncode is None when
    we had to kill it (deadline or over-cap). Never raises for I/O."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        # Surface the OSError as pseudo-stderr so the caller can categorize.
        return b"", None, str(exc).encode("utf-8", errors="replace"), False
    buf = bytearray()
    stderr_buf = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    over_cap = False
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        while len(buf) <= max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], remaining)
            if not ready:
                timed_out = True
                break
            saw_data = False
            if proc.stdout in ready:
                chunk = proc.stdout.read1(65536)
                if chunk:
                    buf += chunk
                    saw_data = True
            if proc.stderr in ready:
                err_chunk = proc.stderr.read1(4096)
                if err_chunk:
                    saw_data = True
                    # Append up to the cap; beyond it KEEP READING (discard) so
                    # the OS pipe never fills and backpressure-stalls the child.
                    room = _MAX_STDERR_BYTES - len(stderr_buf)
                    if room > 0:
                        stderr_buf += err_chunk[:room]
            if not saw_data and proc.poll() is not None:
                break
        else:
            over_cap = True  # exited the while via the byte cap — untrusted bytes
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rc = None
        if over_cap or timed_out:
            rc = None  # contract: 0 clean, >0 ffmpeg error, None killed
        try:
            # NON-BLOCKING final drain (Codex MEDIUM): a blocking read() could
            # stall past the deadline if a process-group child kept the stderr
            # write-end open. select(0) + read1 only consumes what's already
            # buffered and returns immediately.
            room = _MAX_STDERR_BYTES - len(stderr_buf)
            if room > 0 and proc.stderr is not None:
                ready, _, _ = select.select([proc.stderr], [], [], 0)
                if ready:
                    tail = proc.stderr.read1(room)
                    if tail:
                        stderr_buf += tail
        except Exception:  # noqa: BLE001 - final drain must never raise
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    return bytes(buf), rc, bytes(stderr_buf), timed_out


def capture_frame_with_reason(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
    runner=_run_ffmpeg,
) -> CaptureResult:
    """Capture one frame, returning a categorized CaptureResult so callers that
    surface the failure (the cloud no_frame event, the camera-test probe) can
    show a concrete next step. `capture_frame` is the back-compat bytes-only
    shim around this."""
    global _warned_no_ffmpeg
    if not host or not access_code:
        return CaptureResult(jpeg=None, reason="unreachable", stderr_tail="")
    if not ffmpeg_available():
        if not _warned_no_ffmpeg:
            log.warning(
                "ffmpeg not found — X1/H2/P2S camera capture disabled; "
                "install it on the hub (sudo apt-get install -y ffmpeg)"
            )
            _warned_no_ffmpeg = True
        return CaptureResult(jpeg=None, reason="no-ffmpeg", stderr_tail="")

    out, returncode, stderr_bytes, timed_out = runner(
        ffmpeg_argv(host, access_code), timeout, max_bytes
    )
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    redacted = _redact_access_code(stderr_text, access_code).strip()
    lines = redacted.splitlines()
    stderr_tail = lines[-1] if lines else ""
    if len(stderr_tail) > STDERR_TAIL:
        stderr_tail = stderr_tail[-STDERR_TAIL:]

    if returncode != 0:
        return CaptureResult(
            jpeg=None,
            reason=_categorize_stderr(redacted, returncode, timed_out),
            stderr_tail=stderr_tail,
        )
    if not (0 < len(out) <= max_bytes) or out[:3] != _SOI or out[-2:] != _EOI:
        return CaptureResult(jpeg=None, reason="bad-jpeg", stderr_tail=stderr_tail)
    return CaptureResult(jpeg=out, reason=None, stderr_tail="")


def capture_frame(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
    runner=_run_ffmpeg,
) -> Optional[bytes]:
    """Return one JPEG frame from the printer's :322 RTSPS stream, or None on any
    failure. Back-compat shim around capture_frame_with_reason."""
    return capture_frame_with_reason(
        host, access_code, timeout=timeout, max_bytes=max_bytes, runner=runner
    ).jpeg


# Manual on-device probe:  python3 -m makeros_hub.printers.rtsp_camera <ip> <code>
if __name__ == "__main__":  # pragma: no cover - manual on-device probe
    import sys

    if len(sys.argv) != 3:
        print("usage: python3 -m makeros_hub.printers.rtsp_camera <ip> <access_code>")
        raise SystemExit(2)
    print(f"socket-timeout flag: {_supported_timeout_flag() or '(none — python deadline only)'}")
    result = capture_frame_with_reason(sys.argv[1], sys.argv[2])
    if result.jpeg:
        print(
            f"PASS :322 — captured {len(result.jpeg)} bytes "
            f"(JPEG {result.jpeg[:4].hex()}..{result.jpeg[-2:].hex()})"
        )
    else:
        print(f"FAIL :322 — reason={result.reason} stderr_tail={result.stderr_tail!r}")
        raise SystemExit(1)
