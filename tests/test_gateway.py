"""The camera gateway client (v0.75) against a fake go2rtc on a unix socket: stream registration, stills, the live MJPEG
stream and its multipart parser, and what happens when the gateway is missing."""
import io
import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import pytest

from makeros_hub import gateway as gw

JPEG = b"\xff\xd8\xff\xe0fake-frame\xff\xd9"
PID = "11111111-2222-3333-4444-555555555555"


class FakeGo2rtc(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.streams: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.frame_mode = "ok"      # ok | empty | slow | 500
        self.mjpeg_frames = 3
        super().__init__(path, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def address_string(self):
        return "unix"

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        srv: FakeGo2rtc = self.server
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        srv.calls.append((method, u.path))
        if u.path == "/api/streams":
            if method == "GET":
                return self._send(200, json.dumps({n: {"producers": [{"url": "masked"}]} for n in srv.streams}).encode())
            if method == "PUT":
                src = q["src"][0]
                if " " in src:   # the real gateway: "streams: source with spaces may be insecure"
                    return self._send(400, b"spaces")
                srv.streams[q["name"][0]] = src
                return self._send(200)
            if method == "DELETE":
                srv.streams.pop(q["src"][0], None)
                return self._send(200)
        if u.path == "/api/frame.jpeg" and method == "GET":
            if q["src"][0] not in srv.streams:
                return self._send(404, b"stream not found")
            if srv.frame_mode == "slow":
                time.sleep(1.0)
            if srv.frame_mode == "empty":
                return self._send(200, b"")   # go2rtc when the printer refused the stream
            if srv.frame_mode == "500":
                return self._send(500, b"ffmpeg failed")
            return self._send(200, JPEG, "image/jpeg")
        if u.path == "/api/stream.mjpeg" and method == "GET":
            if q["src"][0] not in srv.streams:
                return self._send(404, b"stream not found")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Connection", "close")
            self.end_headers()
            for i in range(srv.mjpeg_frames):
                part = JPEG + bytes([i])
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(part) + part + b"\r\n")
            return None
        return self._send(404, b"")

    def do_GET(self):
        self._route("GET")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture()
def fake():
    d = tempfile.mkdtemp(prefix="gw")   # short: a unix socket path is capped at 108 bytes
    path = os.path.join(d, "api.sock")
    srv = FakeGo2rtc(path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, path
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(d, ignore_errors=True)


def _rtsp(pid=PID, host="192.168.1.50", code="Ab/cd 12"):
    return {"printerId": pid, "vendor": "bambu", "model": "X1C", "host": host, "accessCode": code}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_stream_sources_quote_the_code_and_name_the_1fps_template():
    s = gw.stream_sources(_rtsp())
    assert s[PID] == "rtspx://bblp:Ab%2Fcd%2012@192.168.1.50:322/streaming/live/1"
    assert s[f"{PID}-live"] == f"ffmpeg:{PID}#video=mjpeg#raw=live1fps#width=1280"
    assert " " not in "".join(s.values())   # the gateway refuses a source with spaces
    assert gw.stream_sources({"printerId": PID, "host": "h"}) == {}


def test_registers_checks_rarely_reregisters_on_change_and_removes_the_rest(fake):
    srv, path = fake
    srv.streams["stale-printer"] = "rtspx://x"
    clock = Clock()
    g = gw.Gateway(path, clock=clock)
    g.ensure_streams([_rtsp()])
    assert set(srv.streams) == {PID, f"{PID}-live"}
    assert srv.streams[PID].startswith("rtspx://bblp:Ab%2Fcd%2012@")
    assert g.serves(PID) and g.serves(f"{PID}-live") and g.available()
    n = len(srv.calls)
    clock.t += 60
    g.ensure_streams([_rtsp()])
    assert len(srv.calls) == n, "no GET every beat while nothing changed"
    clock.t += gw.CHECK_EVERY_S
    g.ensure_streams([_rtsp()])
    assert srv.calls[n:] == [("GET", "/api/streams")], "a periodic check finds both present and PUTs nothing"
    g.ensure_streams([_rtsp(host="192.168.1.51")])   # a changed host: checked at once, the source re-PUT
    assert "@192.168.1.51:" in srv.streams[PID]
    g.ensure_streams([])
    assert srv.streams == {} and not g.serves(PID)


def test_a_gateway_restarted_with_a_fresh_config_is_refilled_on_the_next_check(fake):
    srv, path = fake
    clock = Clock()
    g = gw.Gateway(path, clock=clock)
    g.ensure_streams([_rtsp()])
    srv.streams.clear()   # the installer rewrote the config
    clock.t += gw.CHECK_EVERY_S
    g.ensure_streams([_rtsp()])
    assert set(srv.streams) == {PID, f"{PID}-live"}


def test_no_gateway_means_nothing_registered_and_not_available(tmp_path):
    g = gw.Gateway(str(tmp_path / "missing.sock"))
    g.ensure_streams([_rtsp()])
    assert not g.available() and not g.serves(PID) and g.registered == {}
    assert g.frame(PID)[1] == "no-camera-source"


def test_stills(fake, monkeypatch):
    srv, path = fake
    g = gw.Gateway(path)
    g.ensure_streams([_rtsp()])
    assert g.frame(PID) == (JPEG, None, "")
    srv.frame_mode = "empty"
    assert g.frame(PID)[1] == "bad-jpeg"
    srv.frame_mode = "500"
    assert g.frame(PID)[1:] == ("unknown", "camera gateway HTTP 500")
    srv.frame_mode = "slow"
    monkeypatch.setattr(gw, "FRAME_TIMEOUT_S", 0.2)
    assert g.frame(PID)[1] == "timeout"
    srv.frame_mode = "ok"
    assert g.frame(PID, max_bytes=4)[1] == "too-large"
    srv.streams.pop(PID)
    assert g.frame(PID)[1] == "no-camera-source" and PID not in g.registered, "a lost stream is forgotten and re-checked"


def test_live_stream_yields_each_jpeg(fake):
    srv, path = fake
    g = gw.Gateway(path)
    g.ensure_streams([_rtsp()])
    assert list(g.mjpeg(PID)) == [JPEG + b"\x00", JPEG + b"\x01", JPEG + b"\x02"]
    srv.streams.pop(f"{PID}-live")
    with pytest.raises(gw.GatewayError):
        list(g.mjpeg(PID))
    assert not g.serves(f"{PID}-live")


def test_multipart_parser():
    part = lambda b, extra=b"": b"--frame\r\nContent-Type: image/jpeg\r\n" + extra + b"Content-Length: %d\r\n\r\n" % len(b) + b + b"\r\n"
    assert list(gw.read_jpeg_parts(io.BytesIO(part(JPEG) + part(b"x" * 10)))) == [JPEG, b"x" * 10]
    assert list(gw.read_jpeg_parts(io.BytesIO(part(JPEG)[:-8]))) == [], "a truncated last part ends the stream quietly"
    with pytest.raises(gw.GatewayError):
        list(gw.read_jpeg_parts(io.BytesIO(part(b"x" * 100)), max_bytes=50))
    with pytest.raises(gw.GatewayError):
        list(gw.read_jpeg_parts(io.BytesIO(b"garbage\r\n")))
    with pytest.raises(gw.GatewayError):
        list(gw.read_jpeg_parts(io.BytesIO(b"--frame\r\nContent-Type: image/jpeg\r\n\r\nxx")))   # no length
