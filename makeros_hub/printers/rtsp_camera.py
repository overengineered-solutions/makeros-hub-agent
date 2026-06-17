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
the ≤timeout life of the process. This is an accepted exposure for this threat
model: the hub is a single-tenant shop appliance (only the operator + this
agent), the value is a LAN-local printer access code (not an API key/token) that
already lives on the box for the MQTT/FTPS paths, and the process is short-lived.
We do NOT log/echo it (R2.10 still holds for our own logging). A localhost RTSP
auth-proxy would remove even the argv exposure but is out of scope here.

Observability (v0.41.0): on failure, capture_frame_with_reason returns a
CaptureResult with a categorized `reason` ('no-ffmpeg' / 'liveview-off' /
'auth-fail' / 'unreachable' / 'timeout' / 'bad-jpeg') and a bounded redacted
`stderr_tail` (≤400 chars) so the cloud's `nativeprint.camera.no_frame` event
can name the cause instead of just the printerId. Categorization is keyword-
based against ffmpeg's stderr — see _categorize_stderr — and matches the
patterns we've seen in practice on Bambu :322. The access code is redacted from
stderr before it leaves this module.
"""

from __future__ import annotations

import logging
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

log = logging.getLogger("makeros-hub.printers")

RTSP_PORT = 322
_DEFAULT_TIMEOUT = 10.0
# A 720p H.264 keyframe → JPEG is ~50-300 KB; cap so a misbehaving/hostile peer
# on :322 can't stream unbounded bytes into agent memory.
_MAX_FRAME_BYTES = 4 * 1024 * 1024
# Bounded stderr we capture for the cloud's no_frame event payload. ffmpeg's
# error messages are tiny (sub-1KB) so 4KB is generous; we trim to STDERR_TAIL
# chars on the way out so the audit log stays cheap to store.
_MAX_STDERR_BYTES = 4 * 1024
STDERR_TAIL = 400
# Socket I/O timeout passed to ffmpeg's `-stimeout` (microseconds). 5s catches
# half-open TCP that the python-side select-deadline currently has to clean up
# via SIGKILL; lower than _DEFAULT_TIMEOUT so the python timeout still wins on
# a wedged ffmpeg process. Source: ffmpeg RTSP protocol docs + the workflow
# diagnose recommendation 2026-06-17.
_STIMEOUT_USEC = 5_000_000
_SOI = b"\xff\xd8\xff"  # JPEG start-of-image
_EOI = b"\xff\xd9"  # JPEG end-of-image
_warned_no_ffmpeg = False


# Categorized reason for the cloud's no_frame event payload. Keep these stable
# — the cloud admin UI maps them to operator-facing copy ("Toggle LAN-Mode
# Liveview ON on the printer LCD", etc.). Adding a new reason here means
# adding its copy on the cloud side.
NoFrameReason = str  # one of: no-ffmpeg / liveview-off / auth-fail / unreachable / timeout / bad-jpeg / unknown


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one capture attempt.

    On success: jpeg is the JPEG bytes, reason is None, stderr_tail is "".
    On failure: jpeg is None, reason is a categorized string, stderr_tail
    is a redacted ≤STDERR_TAIL-char excerpt of ffmpeg's stderr (or "" if
    we never invoked ffmpeg — e.g. no-ffmpeg / no-host).
    """

    jpeg: Optional[bytes]
    reason: Optional[NoFrameReason]
    stderr_tail: str


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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
        # Socket I/O timeout (µs). Catches half-open TCP at the socket layer
        # so the python-side select-deadline doesn't have to SIGKILL a wedged
        # process. Must come BEFORE -i.
        "-stimeout",
        str(_STIMEOUT_USEC),
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
    """Strip the LAN access code from ffmpeg stderr before it leaves this module.
    ffmpeg always echoes the URL it tried to dial back into its error output, so
    the credential would otherwise land in system_events.payload."""
    if not text or not access_code:
        return text
    quoted = quote(access_code, safe="")
    return text.replace(access_code, "***").replace(quoted, "***")


def _categorize_stderr(stderr: str, returncode: Optional[int], deadline_hit: bool) -> NoFrameReason:
    """Map ffmpeg stderr + returncode → one of the categorized reasons. The
    operator-facing copy on the cloud side keys off this string, so the list
    is a stable contract. Add a new reason only by also extending the cloud
    map at apps/web/lib/print/camera-no-frame-copy.ts (mirrored)."""
    if deadline_hit:
        return "timeout"
    blob = stderr.lower() if stderr else ""
    # Network-layer failures first — these eclipse any application semantics.
    if any(
        marker in blob
        for marker in (
            "no route to host",
            "network is unreachable",
            "connection refused",
            "name or service not known",
            "host is down",
        )
    ):
        return "unreachable"
    # Authentication failure — the access code on file doesn't match what the
    # printer has stored.
    if "401" in blob or "unauthorized" in blob or "authentication" in blob:
        return "auth-fail"
    # Liveview-off / RTSP server-not-listening — most common operator
    # misconfiguration. Bambu's :322 closes the TCP socket cleanly when
    # Liveview is OFF, ffmpeg sees this as connection-failed/EOF without
    # a 401. The most reliable signature is "Server returned 404" /
    # "rtsp/1.0 404" or "End of file" on read.
    if any(
        marker in blob
        for marker in (
            "404 not found",
            "rtsp/1.0 404",
            "stream not found",
            "could not find rtsp option",
            "describe failed",
        )
    ):
        return "liveview-off"
    # Common ffmpeg complaint when TLS handshake fails. Often correlates with
    # Liveview-off on Bambu too, but we surface as its own reason so the cloud
    # can show a different hint if it gets noisy.
    if "tls" in blob and ("handshake" in blob or "ssl" in blob):
        return "liveview-off"
    if returncode is None:
        # Deadline wasn't hit (already handled) but no rc either → killed.
        return "timeout"
    return "unknown"


def _run_ffmpeg(
    cmd: list[str], timeout: float, max_bytes: int
) -> tuple[bytes, Optional[int], bytes]:
    """Run ffmpeg, reading stdout INCREMENTALLY (select + read1) so memory is
    bounded to max_bytes+1 regardless of how much the peer streams, and time is
    bounded by `timeout`. stderr is captured to a bounded buffer (~_MAX_STDERR_BYTES)
    so the caller can categorize the failure reason. Returns
    (stdout_bytes, returncode, stderr_bytes); returncode is None if we had to
    kill it (over the byte cap or the deadline). Never raises for I/O —
    returns (b'', None, b'')."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        # Bubble the OSError message as fake stderr so the caller can
        # categorize (e.g. ffmpeg missing mid-flight); the wrapper has
        # already gated on ffmpeg_available() so this is rare.
        return b"", None, str(exc).encode("utf-8", errors="replace")
    buf = bytearray()
    stderr_buf = bytearray()
    deadline = time.monotonic() + timeout
    over_cap = False
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        while len(buf) <= max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline — kill below, no further reads
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], remaining)
            if not ready:
                break  # timed out waiting for more output
            saw_data = False
            if proc.stdout in ready:
                chunk = proc.stdout.read1(65536)
                if chunk:
                    buf += chunk
                    saw_data = True
            if proc.stderr in ready:
                err_chunk = proc.stderr.read1(4096)
                if err_chunk:
                    # Bounded stderr buffer — stop appending once we hit the
                    # cap so a misbehaving peer can't OOM us via stderr either.
                    room = _MAX_STDERR_BYTES - len(stderr_buf)
                    if room > 0:
                        stderr_buf += err_chunk[:room]
                    saw_data = True
            # No more data AND child exited → done.
            if not saw_data and proc.poll() is not None:
                break
        else:
            # Loop exited via the cap (len > max_bytes) — over-limit peer.
            # Treat as "killed for cap" so callers never trust the bytes.
            over_cap = True
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rc = None
        if over_cap:
            # Force rc=None so the caller's contract holds: 0 = clean, >0 = ffmpeg
            # error, None = killed (over cap / deadline). A SIGKILL exit code
            # (-9) would otherwise be reported here on the cap path, which is
            # the same operational outcome but breaks the `rc is None` check.
            rc = None
        # Drain any final stderr now that ffmpeg has stopped writing.
        try:
            tail = proc.stderr.read(_MAX_STDERR_BYTES - len(stderr_buf))
            if tail:
                stderr_buf += tail
        except Exception:  # noqa: BLE001 - stderr drain must never raise
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    return bytes(buf), rc, bytes(stderr_buf)


def capture_frame_with_reason(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
    runner=_run_ffmpeg,
) -> CaptureResult:
    """Like `capture_frame` but returns a categorized CaptureResult so callers
    that surface the failure (the cloud no_frame event, the on-demand camera
    test probe) can show the operator a concrete next step instead of "no
    frame this beat". The same wrapper is used by both — capture_frame
    discards the reason for back-compat."""
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

    start = time.monotonic()
    out, returncode, stderr_bytes = runner(ffmpeg_argv(host, access_code), timeout, max_bytes)
    elapsed = time.monotonic() - start
    deadline_hit = elapsed >= timeout - 0.05  # 50ms slop — select() polling jitter
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    redacted_stderr = _redact_access_code(stderr_text, access_code)
    stderr_tail = redacted_stderr.strip().splitlines()[-1:][0] if redacted_stderr.strip() else ""
    if len(stderr_tail) > STDERR_TAIL:
        stderr_tail = stderr_tail[-STDERR_TAIL:]

    # A nonzero/none exit (Liveview off, auth fail, timeout, killed-over-cap)
    # never yields a trusted frame even if stdout happens to look JPEG-shaped.
    if returncode != 0:
        return CaptureResult(
            jpeg=None,
            reason=_categorize_stderr(redacted_stderr, returncode, deadline_hit),
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
    failure (no ffmpeg / Liveview off / unreachable / timeout / nonzero exit /
    not-a-JPEG / over the byte cap). The caller treats None as 'no frame this
    beat', never an error. `runner` is injectable for tests.

    Back-compat shim around capture_frame_with_reason — preserved so external
    callers (and the manual probe below) keep working unchanged."""
    result = capture_frame_with_reason(
        host, access_code, timeout=timeout, max_bytes=max_bytes, runner=runner
    )
    return result.jpeg


# Manual on-device probe to confirm the path before relying on it:
#   python3 -m makeros_hub.printers.rtsp_camera <printer-ip> <access-code>
if __name__ == "__main__":  # pragma: no cover - manual on-device probe
    import sys

    if len(sys.argv) != 3:
        print("usage: python3 -m makeros_hub.printers.rtsp_camera <ip> <access_code>")
        raise SystemExit(2)
    result = capture_frame_with_reason(sys.argv[1], sys.argv[2])
    if result.jpeg:
        print(
            f"PASS :322 — captured {len(result.jpeg)} bytes (JPEG {result.jpeg[:4].hex()}..{result.jpeg[-2:].hex()})"
        )
    else:
        print(f"FAIL :322 — reason={result.reason} stderr_tail={result.stderr_tail!r}")
        raise SystemExit(1)
