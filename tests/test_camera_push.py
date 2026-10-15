"""Camera frames are pushed OUT-OF-BAND to /api/print/hub/camera as raw JPEG bytes (not base64'd into the heartbeat,
whose body the cloud caps at 512KB). Verifies push_camera_frames' URL + body shape and best-effort behavior."""

import base64

import makeros_hub.agent as agent
from makeros_hub import http
from makeros_hub.config import Config


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, *a):
        self.warnings.append(a)


def test_camera_url_builder():
    c = Config(cloud_url="https://procrastinationstation.net/")
    assert c.camera_url == "https://procrastinationstation.net/api/print/hub/camera"


def test_push_frames_posts_raw_bytes_and_signals_failures_with_their_reason(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "post_bytes", lambda url, data, **kw: calls.append((url, bytes(data), kw.get("content_type"))) or 200)

    raw = b"\xff\xd8\xffHELLO"
    frames = [
        {"printerId": "p1", "jpegBase64": base64.b64encode(raw).decode()},
        {"printerId": None, "jpegBase64": "x"},  # skipped — no id
        {"printerId": "p3"},                      # skipped — no frame
    ]
    failures = [{"printerId": "p2", "reason": "timeout", "sourceId": "b" * 64}, "p4"]  # dict + legacy-str shapes

    pushed = agent.push_camera_frames("http://c/api/print/hub/camera", "tok", frames, failures, _Log())

    urls = [u for (u, _b, _ct) in calls]
    assert ("http://c/api/print/hub/camera?printerId=p1", raw, "image/jpeg") in calls  # decoded raw bytes
    assert f"http://c/api/print/hub/camera?printerId=p2&failed=1&sourceId={'b' * 64}&reason=timeout" in urls
    assert "http://c/api/print/hub/camera?printerId=p4&failed=1" in urls  # legacy-str failure
    assert len(calls) == 3  # two malformed frames skipped
    assert pushed == {"p1": 200}


def test_push_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(agent, "post_bytes", boom)
    log = _Log()
    frames = [{"printerId": "p1", "jpegBase64": base64.b64encode(b"\xff\xd8\xffx").decode()}]
    # must NOT raise — a failed frame push can never sink the heartbeat loop
    assert agent.push_camera_frames("http://c/api/print/hub/camera", "tok", frames, None, log) == {}
    assert log.warnings  # logged the failure


def test_source_identity_survives_upload_and_failure_errors_are_visible(monkeypatch):
    calls = []
    source = "a" * 64
    monkeypatch.setattr(agent, "post_bytes", lambda url, *args, **kwargs: calls.append(url) or 200)
    agent.push_camera_frames("http://c/camera", "tok", [{"printerId": "p1", "sourceId": source, "jpegBase64": base64.b64encode(b"jpeg").decode()}], [{"printerId": "p2", "sourceId": source}], _Log())
    assert calls == [f"http://c/camera?printerId=p1&sourceId={source}", f"http://c/camera?printerId=p2&failed=1&sourceId={source}"]
    def fail(*args, **kwargs):
        raise RuntimeError("connection failed")
    monkeypatch.setattr(agent, "post_bytes", fail)
    log = _Log()
    agent.push_camera_frames("http://c/camera", "tok", [], [{"printerId": "p2"}], log)
    assert log.warnings


def _frames(*pids):
    return [{"printerId": p, "jpegBase64": base64.b64encode(b"\xff\xd8\xff" + p.encode()).decode(), "sourceId": "a" * 64,
             "sha256": "%064x" % i} for i, p in enumerate(pids, 1)]


def test_pushes_stop_at_the_first_5xx_or_network_error_and_failures_are_not_posted_after(monkeypatch):
    for answer, mark in ((503, 503), (http.TransportError("down"), "net")):
        calls = []

        def post(url, _data, answer=answer, **_kw):
            calls.append(url)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(agent, "post_bytes", post)
        out = agent.push_camera_frames("http://c/camera", "tok", _frames("p1", "p2"), [{"printerId": "p3", "reason": "timeout"}], _Log())
        assert out == {"p1": mark}
        assert len(calls) == 1   # p2 and the p3 failure wait for a site that answers


def test_only_a_site_200_marks_a_frame_captured():
    from makeros_hub.printers.camera import CameraScheduler
    frames = _frames("ok", "big", "type", "bad", "gone", "moved", "down", "net", "unsent")
    answers = {"ok": 200, "big": 413, "type": 415, "bad": 422, "gone": 404, "moved": 409, "down": 503, "net": "net"}
    s = CameraScheduler()
    s.plan([{"printerId": f["printerId"], "vendor": "klipper", "moonrakerUrl": "http://k"} for f in frames], {}, 0.0)
    agent._apply_camera_push(s, frames, answers, 0.0)
    agent._apply_camera_push(s, frames, answers, 1.0)   # twice: the second refusal opens the breaker
    assert s._cams["ok"].last_ok == 1.0 and s.stats["ok"] == 2
    assert {p for p, c in s._cams.items() if c.retry_at > 1.0} == {"big", "type", "bad"}
    assert {p for p, c in s._cams.items() if c.streak} == {"big", "type", "bad"}
    assert {p for p, c in s._cams.items() if c.last_ok is None and not c.streak} == {"gone", "moved", "down", "net", "unsent"}
    assert s.stats["push"] == {"200": 2, "413": 2, "415": 2, "422": 2, "404": 2, "409": 2, "503": 2, "net": 2, "unsent": 2}
