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
from typing import Optional

CAMERA_PORT = 6000
_AUTH_HEADER = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
_SOI = b"\xff\xd8\xff\xe0"  # JPEG start-of-image + APP0 (JFIF)
_EOI = b"\xff\xd9"  # JPEG end-of-image
_DEFAULT_TIMEOUT = 4.0
# A single 1280x720 MJPEG frame is ~100 KB; cap the read so a misbehaving/non-Bambu
# peer on :6000 can't stream unboundedly into agent memory.
_MAX_FRAME_BYTES = 2 * 1024 * 1024


def _auth_packet(access_code: str) -> bytes:
    username = b"bblp".ljust(32, b"\x00")
    code = access_code.encode("ascii", "ignore")[:32].ljust(32, b"\x00")
    return _AUTH_HEADER + username + code


def capture_frame(
    host: str,
    access_code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> Optional[bytes]:
    """Return one JPEG frame from the printer's :6000 camera, or None on failure."""
    if not host or not access_code:
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((host, CAMERA_PORT), timeout=timeout) as raw:
            raw.settimeout(timeout)
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(_auth_packet(access_code))
                return _read_one_jpeg(tls, max_bytes)
    except (OSError, ssl.SSLError, ValueError):
        # ValueError guards an empty/odd host; OSError covers connect/timeout/reset.
        return None


def _read_one_jpeg(sock: ssl.SSLSocket, max_bytes: int) -> Optional[bytes]:
    """Accumulate 4 KB chunks until one complete SOI..EOI JPEG is buffered."""
    buf = bytearray()
    while len(buf) < max_bytes:
        try:
            chunk = sock.recv(4096)
        except (OSError, ssl.SSLError):
            return None
        if not chunk:
            return None  # peer closed before a full frame
        buf += chunk
        start = buf.find(_SOI)
        if start == -1:
            # No SOI yet — keep only a tail in case the marker straddles chunks.
            if len(buf) > 3:
                del buf[: len(buf) - 3]
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
