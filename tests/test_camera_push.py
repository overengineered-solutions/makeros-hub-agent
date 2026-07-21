"""Camera frames are pushed OUT-OF-BAND to /api/print/hub/camera as raw JPEG bytes (not base64'd into the heartbeat,
whose body the cloud caps at 512KB). Verifies push_camera_frames' URL + body shape and best-effort behavior."""

import base64

import makeros_hub.agent as agent
from makeros_hub.config import Config


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, *a):
        self.warnings.append(a)


def test_camera_url_builder():
    c = Config(cloud_url="https://procrastinationstation.net/")
    assert c.camera_url == "https://procrastinationstation.net/api/print/hub/camera"


def test_push_frames_posts_raw_bytes_and_signals_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "post_bytes", lambda url, data, **kw: calls.append((url, bytes(data), kw.get("content_type"))) or 200)

    raw = b"\xff\xd8\xffHELLO"
    frames = [
        {"printerId": "p1", "jpegBase64": base64.b64encode(raw).decode()},
        {"printerId": None, "jpegBase64": "x"},  # skipped — no id
        {"printerId": "p3"},                      # skipped — no frame
    ]
    failures = [{"printerId": "p2", "reason": "timeout"}, "p4"]  # dict + legacy-str shapes

    agent.push_camera_frames("http://c/api/print/hub/camera", "tok", frames, failures, _Log())

    urls = [u for (u, _b, _ct) in calls]
    assert ("http://c/api/print/hub/camera?printerId=p1", raw, "image/jpeg") in calls  # decoded raw bytes
    assert "http://c/api/print/hub/camera?printerId=p2&failed=1" in urls
    assert "http://c/api/print/hub/camera?printerId=p4&failed=1" in urls  # legacy-str failure
    assert len(calls) == 3  # two malformed frames skipped


def test_push_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(agent, "post_bytes", boom)
    log = _Log()
    frames = [{"printerId": "p1", "jpegBase64": base64.b64encode(b"\xff\xd8\xffx").decode()}]
    # must NOT raise — a failed frame push can never sink the heartbeat loop
    agent.push_camera_frames("http://c/api/print/hub/camera", "tok", frames, None, log)
    assert log.warnings  # logged the failure
