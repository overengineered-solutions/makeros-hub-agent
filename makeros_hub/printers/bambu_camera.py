"""Grab a single JPEG frame from a Bambu P1/A1-series LAN camera.

The P1 and A1 series (incl. the A1 mini — the PS pilot) expose their camera over
a proprietary TCP protocol on port 6000 (TLS), NOT the X1's RTSP :322. SimplyPrint
uses this same LAN path, which is how the operator already gets a reduced-fps feed.
Stdlib only (socket + ssl + struct) — no ffmpeg/opencv.

Protocol (reverse-engineered; matches mattcar15/bambu-connect + ha-bambulab):
  1. TCP connect <ip>:6000, wrap in TLS. The printer presents a self-signed cert,
     so verification is disabled (safe: traffic is shop-LAN / Tailscale only and
     the access code is the real auth).
  2. Send an 80-byte auth packet: little-endian u32 [0x40, 0x3000, 0, 0], then the
     username "bblp" and the access code, each ASCII and null-padded to 32 bytes.
  3. The printer streams MJPEG (~1 fps). Read until one full JPEG is framed by its
     SOI (FF D8 FF E0) and EOI (FF D9) markers; return those bytes.

Returns the JPEG bytes, or None on any failure (no camera / wrong port / timeout /
bad code) — the caller treats None as "no frame this beat", never an error.
The X1 series (RTSP :322) is intentionally NOT handled here and yields None.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Optional

CAMERA_PORT = 6000
# Bounded detail string surfaced alongside the categorized reason (no secret
# risk — :6000 exceptions are connection errors, they don't echo the code).
_DETAIL_TAIL = 200
_AUTH_HEADER = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
# Generic JPEG start: SOI (FF D8) + the first marker byte (FF). Matches APP0/JFIF
# (FF D8 FF E0) AND Exif/quant-table variants (FF E1 / DB) so a non-JFIF frame
# isn't silently dropped.
_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"  # JPEG end-of-image
# v0.69: the WHOLE capture (connect + TLS + one frame). It was 4 s per blocking step with no total, so a peer that
# trickled bytes held a capture thread for as long as it liked.
_DEFAULT_TIMEOUT = 5.0
# A single 1280x720 MJPEG frame is ~100 KB; cap the read so a misbehaving/non-Bambu
# peer on :6000 can't stream unboundedly into agent memory.
_MAX_FRAME_BYTES = 2 * 1024 * 1024


def _auth_packet(access_code: str) -> bytes:
    username = b"bblp".ljust(32, b"\x00")
    code = access_code.encode("ascii", "ignore")[:32].ljust(32, b"\x00")
    return _AUTH_HEADER + username + code


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one :6000 capture. Mirrors rtsp_camera.CaptureResult's shape +
    reason vocabulary so the cloud's no_frame copy map handles both paths
    identically. On success: jpeg set, reason None. On failure: jpeg None,
    reason categorized ('unreachable' / 'timeout' / 'tls-error' / 'liveview-off'
    / 'bad-jpeg' / 'unknown'), detail a bounded non-secret excerpt."""

    jpeg: Optional[bytes]
    reason: Optional[str]
    detail: str


def capture_frame_with_reason(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> CaptureResult:
    """Capture one :6000 frame, categorizing the failure on the exception/outcome
    (there's no ffmpeg stderr here — the socket layer is the signal). Brings the
    A1/P1 path to diagnostic parity with the RTSP path so an A1 failure (wrong
    code / LAN-mode off / asleep) is actionable instead of a silent 'unknown'."""
    if not host or not access_code:
        return CaptureResult(None, "unreachable", "")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    deadline = time.monotonic() + timeout

    def left() -> float:
        return max(0.05, deadline - time.monotonic())

    phase = "connect"
    try:
        with _connect(host, deadline) as raw:
            phase = "tls"
            raw.settimeout(left())
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                phase = "stream"
                tls.sendall(_auth_packet(access_code))
                jpeg = _read_one_jpeg(tls, max_bytes, deadline)
    except ssl.SSLError as exc:
        return CaptureResult(None, "tls-error", str(exc)[:_DETAIL_TAIL])
    except socket.timeout:
        # connect -> the port is not answering; tls -> the handshake is stuck; stream -> authed, then no picture (the
        # signature an A1 shows after a power blip: the camera wedges while MQTT keeps reporting)
        return CaptureResult(None, {"connect": "unreachable", "tls": "tls-error"}.get(phase, "timeout"), f"timed out in {phase}")
    except ConnectionRefusedError:
        return CaptureResult(None, "unreachable", "connection refused")
    except (OSError, ValueError) as exc:
        # OSError covers EHOSTUNREACH/ENETUNREACH/reset; ValueError an odd host.
        return CaptureResult(None, "unreachable", str(exc)[:_DETAIL_TAIL])

    if jpeg:
        return CaptureResult(jpeg, None, "")
    # Connected + authed but no JPEG framed before max_bytes / clean close. The
    # dominant causes are LAN-Mode Liveview off or a wrong access code (the
    # printer accepts the socket then closes without streaming). We can't tell
    # them apart on :6000, so report the more-common operator cause and let the
    # detail string carry the ambiguity.
    return CaptureResult(
        None, "liveview-off", "no frame (LAN-Mode Liveview off, or wrong access code)"
    )


def _connect(host: str, deadline: float) -> socket.socket:
    """socket.create_connection, but every resolved address shares the capture's one deadline (create_connection gives
    each address the whole timeout: two dead addresses took 10 s). ponytail: the name lookup itself is not
    deadline-bound; printers are configured by IP."""
    err: OSError = socket.timeout("capture deadline")
    for *_, addr in socket.getaddrinfo(host, CAMERA_PORT, type=socket.SOCK_STREAM):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            return socket.create_connection(addr[:2], timeout=left)
        except OSError as exc:
            err = exc
    raise err


def capture_frame(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> Optional[bytes]:
    """Return one JPEG frame from the printer's :6000 camera, or None on failure.
    Back-compat shim around capture_frame_with_reason."""
    return capture_frame_with_reason(
        host, access_code, timeout=timeout, max_bytes=max_bytes
    ).jpeg


def _read_one_jpeg(sock: ssl.SSLSocket, max_bytes: int, deadline: Optional[float] = None) -> Optional[bytes]:
    """Accumulate 4 KB chunks until one complete SOI..EOI JPEG is buffered.
    Bounded by TOTAL bytes read — not just current buffer size — so a peer that
    never sends an SOI (or never an EOI) and just trickles bytes can't keep a
    capture thread alive indefinitely; it's cut off once it has sent max_bytes.
    With `deadline` (monotonic, v0.69) it is also cut off at that instant: each recv waits only for the time left, so a
    1-byte-a-second peer can no longer stretch a capture past the overall budget."""
    keep = len(_SOI) - 1
    buf = bytearray()
    total = 0
    while total < max_bytes:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("capture deadline")
            sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            # Let a stalled read surface as 'timeout' (Codex MEDIUM) rather than
            # collapsing into the generic no-frame → 'liveview-off' bucket.
            raise
        except (OSError, ssl.SSLError):
            return None
        if not chunk:
            return None  # peer closed before a full frame
        total += len(chunk)
        buf += chunk
        start = buf.find(_SOI)
        if start == -1:
            # No SOI yet — keep only a short tail in case the marker straddles
            # chunks (total still counts the discarded bytes, so the cap holds).
            if len(buf) > keep:
                del buf[: len(buf) - keep]
            continue
        end = buf.find(_EOI, start + len(_SOI))
        if end != -1:
            return bytes(buf[start : end + len(_EOI)])
    return None


# Run as a probe ON THE PI to confirm the camera path before relying on it:
#   python3 -m makeros_hub.printers.bambu_camera <printer-ip> <access-code>
# Prints the captured frame size (PASS) or a clear FAIL — the "test it live"
# answer to whether the A1 serves on :6000.
if __name__ == "__main__":  # pragma: no cover - manual on-device probe
    import sys

    if len(sys.argv) != 3:
        print("usage: python3 -m makeros_hub.printers.bambu_camera <ip> <access_code>")
        raise SystemExit(2)
    frame = capture_frame(sys.argv[1], sys.argv[2])
    if frame:
        print(f"PASS :6000 — captured {len(frame)} bytes (JPEG {frame[:4].hex()}..{frame[-2:].hex()})")
    else:
        print("FAIL :6000 — no frame (wrong port/code, or this model uses RTSP :322)")
        raise SystemExit(1)
