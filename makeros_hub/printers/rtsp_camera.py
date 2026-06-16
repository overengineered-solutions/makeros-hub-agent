"""Grab one JPEG frame from a Bambu X1 / H2 / P2S LAN camera (RTSPS :322, H.264).

Unlike the A1/P1 (`:6000` raw-JPEG — see bambu_camera.py), the X1 / X1C / X1E /
X2D / H2* / P2S stream H.264 over RTSP-over-TLS on :322 — verified vs
greghesp/ha-bambulab `models.py` CAMERA_RTSP + Doridian OpenBambuAPI `video.md`.
H.264 can't be decoded with the stdlib, so we shell out to ffmpeg for a single
keyframe → JPEG. ffmpeg is OPTIONAL: if it isn't installed, capture returns None
(the caller degrades to no-frame) and a one-time warning is logged. Requires
"LAN Mode Liveview" ON on the printer (gates :322) + the LAN access code (same
code as MQTT/FTPS). Stdlib only (subprocess + select + shutil).

Credential exposure (accepted): ffmpeg takes the RTSP URL — which embeds the LAN
access code — in its argv, so the code is briefly visible in `ps`/`/proc` for
the ≤timeout life of the process. This is an accepted exposure for this threat
model: the hub is a single-tenant shop appliance (only the operator + this
agent), the value is a LAN-local printer access code (not an API key/token) that
already lives on the box for the MQTT/FTPS paths, and the process is short-lived.
We do NOT log/echo it (R2.10 still holds for our own logging). A localhost RTSP
auth-proxy would remove even the argv exposure but is out of scope here.
"""

from __future__ import annotations

import logging
import select
import shutil
import subprocess
import time
from typing import Optional
from urllib.parse import quote

log = logging.getLogger("makeros-hub.printers")

RTSP_PORT = 322
_DEFAULT_TIMEOUT = 10.0
# A 720p H.264 keyframe → JPEG is ~50-300 KB; cap so a misbehaving/hostile peer
# on :322 can't stream unbounded bytes into agent memory.
_MAX_FRAME_BYTES = 4 * 1024 * 1024
_SOI = b"\xff\xd8\xff"  # JPEG start-of-image
_EOI = b"\xff\xd9"  # JPEG end-of-image
_warned_no_ffmpeg = False


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


def _run_ffmpeg(cmd: list[str], timeout: float, max_bytes: int) -> tuple[bytes, Optional[int]]:
    """Run ffmpeg, reading stdout INCREMENTALLY (select + read1) so memory is
    bounded to max_bytes+1 regardless of how much the peer streams, and time is
    bounded by `timeout`. stderr is discarded (not buffered). Returns
    (stdout_bytes, returncode); returncode is None if we had to kill it (over
    the byte cap or the deadline). Never raises for I/O — returns (b'', None)."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        return b"", None
    buf = bytearray()
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    try:
        while len(buf) <= max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bytes(buf), None  # deadline — caller rejects (rc None)
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                return bytes(buf), None  # timed out waiting for more output
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break  # EOF — ffmpeg finished writing
            buf += chunk
        else:
            # Loop exited via the cap (len > max_bytes) — over-limit peer.
            return bytes(buf), None
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rc = None
        if proc.stdout is not None:
            proc.stdout.close()
    return bytes(buf), rc


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
    beat', never an error. `runner` is injectable for tests."""
    global _warned_no_ffmpeg
    if not host or not access_code:
        return None
    if not ffmpeg_available():
        if not _warned_no_ffmpeg:
            log.warning(
                "ffmpeg not found — X1/H2/P2S camera capture disabled; "
                "install it on the hub (sudo apt-get install -y ffmpeg)"
            )
            _warned_no_ffmpeg = True
        return None

    out, returncode = runner(ffmpeg_argv(host, access_code), timeout, max_bytes)
    # A nonzero/none exit (Liveview off, auth fail, timeout, killed-over-cap)
    # never yields a trusted frame even if stdout happens to look JPEG-shaped.
    if returncode != 0:
        return None
    if not (0 < len(out) <= max_bytes) or out[:3] != _SOI or out[-2:] != _EOI:
        return None
    return out


# Manual on-device probe to confirm the path before relying on it:
#   python3 -m makeros_hub.printers.rtsp_camera <printer-ip> <access-code>
if __name__ == "__main__":  # pragma: no cover - manual on-device probe
    import sys

    if len(sys.argv) != 3:
        print("usage: python3 -m makeros_hub.printers.rtsp_camera <ip> <access_code>")
        raise SystemExit(2)
    frame = capture_frame(sys.argv[1], sys.argv[2])
    if frame:
        print(f"PASS :322 — captured {len(frame)} bytes (JPEG {frame[:4].hex()}..{frame[-2:].hex()})")
    else:
        print("FAIL :322 — no frame (ffmpeg missing? Liveview off? wrong code/IP?)")
        raise SystemExit(1)
