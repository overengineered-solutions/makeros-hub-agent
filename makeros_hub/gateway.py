"""The shop box's camera gateway (v0.75): go2rtc on a private unix socket, installed by oes-pool-deploy's
scripts/install-camera-gateway.py (operator, 2026-10-19: "lets currently max out at 1 frame a second").

Each RTSPS printer (X1C, P2S, H2D) is two streams on the gateway:
  <printerId>        rtspx://bblp:<code>@<host>:322/streaming/live/1 (RTSPS without a certificate check: the printer's own
                     certificate is self-signed)
  <printerId>-live   ffmpeg reading that stream back at one frame a second, 1280 wide. The rate lives in the gateway's
                     config as the `live1fps` template: go2rtc refuses a source that contains spaces.
A still is GET /api/frame.jpeg?src=<printerId> (the next keyframe as one JPEG); a live view reads
/api/stream.mjpeg?src=<printerId>-live. The decode runs in the gateway's service and cgroup, not in this process, and only
while someone reads: go2rtc stops a stream's producers when its last reader leaves.

Never logs or reports an access code: errors carry stream names, status codes and exception class names only.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("makeros_hub.gateway")

SOCKET_PATH = "/run/camera-gateway/api.sock"
CHECK_EVERY_S = 180.0    # GET /api/streams at most this often while nothing changed (never every beat)
ANSWERED_WITHIN_S = 600.0   # "cameraGateway: true" = the gateway answered this recently
FRAME_TIMEOUT_S = 5.0    # a still: under rtsp_camera's 5.5 s capture deadline, so a stuck gateway reads as 'timeout' in a beat
STREAM_READ_TIMEOUT_S = 10.0   # a live stream with no frame for this long is reopened by the live worker
API_MAX_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 1_500_000   # = camera.SITE_MAX_FRAME_BYTES (the site's cap); a bigger part ends the stream


class GatewayError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


def stream_sources(printer: dict[str, Any]) -> dict[str, str]:
    """The two gateway streams for one RTSPS printer target ({} without its id, host or access code)."""
    pid, host, code = printer.get("printerId"), printer.get("host"), printer.get("accessCode")
    if not (isinstance(pid, str) and pid and isinstance(host, str) and host.strip() and isinstance(code, str) and code):
        return {}
    return {pid: f"rtspx://bblp:{urllib.parse.quote(code, safe='')}@{host.strip()}:322/streaming/live/1",
            f"{pid}-live": f"ffmpeg:{pid}#video=mjpeg#raw=live1fps#width=1280"}


def read_jpeg_parts(fp, max_bytes: int = MAX_FRAME_BYTES) -> Iterator[bytes]:
    """Each JPEG of a multipart/x-mixed-replace body (go2rtc writes `--frame` / Content-Type / Content-Length / blank line /
    the bytes / CRLF). Ends quietly when the body ends; raises GatewayError on a malformed or oversized part."""
    while True:
        line = fp.readline(256)
        if not line:
            return
        if not line.strip():
            continue
        if not line.startswith(b"--"):
            raise GatewayError("not a multipart boundary")
        length = None
        for _ in range(16):
            h = fp.readline(1024)
            if not h:
                return
            if not h.strip():
                break
            k, _, v = h.partition(b":")
            if k.strip().lower() == b"content-length":
                try:
                    length = int(v.strip())
                except ValueError:
                    raise GatewayError("bad Content-Length") from None
        else:
            raise GatewayError("too many part headers")
        if length is None or not 0 < length <= max_bytes:
            raise GatewayError(f"part size {length}")
        data = fp.read(length)
        if len(data) < length:
            return
        yield data


class Gateway:
    """Talks to go2rtc over its socket. ensure_streams runs on the heartbeat thread; frame and mjpeg on capture and live
    workers. `registered` maps a stream name to the digest of the source this process PUT (the gateway masks credentials
    in what it lists, so it cannot be compared); it is only ever replaced or popped whole-key, so readers on other threads
    see a consistent value."""

    def __init__(self, path: str = SOCKET_PATH, clock: Callable[[], float] = time.monotonic):
        self.path, self.clock = path, clock
        self.registered: dict[str, str] = {}
        self.answered_at: Optional[float] = None
        self._want: Optional[str] = None
        self._checked_at = float("-inf")
        self._lock = threading.Lock()

    def _request(self, method: str, target: str, timeout: float = 5.0, max_bytes: int = API_MAX_BYTES) -> tuple[int, bytes]:
        conn = _UnixConnection(self.path, timeout)
        try:
            conn.request(method, target)
            resp = conn.getresponse()
            body = resp.read(max_bytes + 1)
            self.answered_at = self.clock()
            if len(body) > max_bytes:
                raise GatewayError(f"answer over {max_bytes} bytes")
            return resp.status, body
        finally:
            conn.close()

    def available(self) -> bool:
        return self.answered_at is not None and self.clock() - self.answered_at < ANSWERED_WITHIN_S

    def serves(self, printer_id: Any) -> bool:
        """This printer's streams are on the gateway and the gateway is answering."""
        return isinstance(printer_id, str) and printer_id in self.registered and self.available()

    def ensure_streams(self, targets: list[dict[str, Any]]) -> None:
        """Make the gateway hold exactly the streams for `targets` (the camera-enabled RTSPS printers). Checks with one GET
        when the target set changed or CHECK_EVERY_S passed; PUTs what is missing or changed, DELETEs what is not wanted
        (the gateway is this hub's alone). A gateway that is not installed or not answering leaves nothing registered,
        so stills take the direct path and live view is not offered for these printers. Never raises."""
        want: dict[str, str] = {}
        for t in targets:
            want.update(stream_sources(t))
        digest = hashlib.sha256(json.dumps(sorted(want.items())).encode()).hexdigest()
        now = self.clock()
        with self._lock:
            if digest == self._want and now - self._checked_at < CHECK_EVERY_S:
                return
            self._want, self._checked_at = digest, now
            try:
                status, body = self._request("GET", "/api/streams")
                listed = json.loads(body) if status == 200 else None
                if not isinstance(listed, dict):
                    raise GatewayError(f"stream list answered HTTP {status}")
                for name, src in want.items():
                    h = hashlib.sha256(src.encode()).hexdigest()
                    if name in listed and self.registered.get(name) == h:
                        continue
                    q = urllib.parse.urlencode({"name": name, "src": src})
                    status, _ = self._request("PUT", f"/api/streams?{q}")
                    if status == 200:
                        self.registered[name] = h
                    else:
                        self.registered.pop(name, None)
                        log.warning("camera gateway refused stream %s (HTTP %s)", name, status)
                for name in [n for n in listed if n not in want]:
                    status, _ = self._request("DELETE", f"/api/streams?{urllib.parse.urlencode({'src': name})}")
                    log.info("camera gateway: removed stream %s (HTTP %s)", name, status)
                for name in [n for n in self.registered if n not in want]:
                    self.registered.pop(name, None)
            except (OSError, GatewayError, ValueError, http.client.HTTPException) as e:
                if self.registered:
                    log.warning("camera gateway not answering (%s); stills go direct", e.__class__.__name__)
                self.registered.clear()

    def forget(self, name: str) -> None:
        """The gateway said it has no such stream (restarted with a fresh config): re-check on the next beat."""
        self.registered.pop(name, None)
        self._want = None

    def frame(self, printer_id: str, max_bytes: int = MAX_FRAME_BYTES) -> tuple[Optional[bytes], Optional[str], str]:
        """One still as (jpeg, None, "") or (None, reason, detail), reasons from the camera vocabulary: timeout, unreachable,
        bad-jpeg, too-large, unknown, or no-camera-source when the stream is not registered."""
        if printer_id not in self.registered:
            return None, "no-camera-source", "not on the camera gateway"
        try:
            status, body = self._request("GET", f"/api/frame.jpeg?{urllib.parse.urlencode({'src': printer_id})}",
                                         timeout=FRAME_TIMEOUT_S, max_bytes=max_bytes)
        except TimeoutError:
            return None, "timeout", "via the camera gateway"
        except GatewayError:
            return None, "too-large", "via the camera gateway"
        except (OSError, http.client.HTTPException) as e:
            return None, "unreachable", f"camera gateway: {e.__class__.__name__}"
        if status == 404:
            self.forget(printer_id)
            return None, "no-camera-source", "camera gateway lost the stream"
        if status != 200:
            return None, "unknown", f"camera gateway HTTP {status}"
        # go2rtc answers an empty 200 when the printer refused the stream; the direct path then names why
        if not body.startswith(b"\xff\xd8"):
            return None, "bad-jpeg", "via the camera gateway"
        return body, None, ""

    def mjpeg(self, printer_id: str, max_bytes: int = MAX_FRAME_BYTES) -> Iterator[bytes]:
        """The printer's 1 fps live stream, one JPEG at a time, until the gateway ends it. Raises GatewayError on a refusal
        and OSError/TimeoutError on a dead socket (a stall over STREAM_READ_TIMEOUT_S included)."""
        name = f"{printer_id}-live"
        if name not in self.registered:
            raise GatewayError("live stream not on the camera gateway")
        conn = _UnixConnection(self.path, STREAM_READ_TIMEOUT_S)
        try:
            conn.request("GET", f"/api/stream.mjpeg?{urllib.parse.urlencode({'src': name})}")
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read(4096)
                if resp.status == 404:
                    self.forget(name)
                raise GatewayError(f"live stream answered HTTP {resp.status}")
            self.answered_at = self.clock()
            for part in read_jpeg_parts(resp, max_bytes):
                self.answered_at = self.clock()
                yield part
        finally:
            conn.close()


GATEWAY = Gateway()
