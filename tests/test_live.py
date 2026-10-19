"""Live view (v0.75): the worker loop with injected frames/post/clock, the snapshot pacing, and which printers the hub
starts a worker for when the site lists them (cameraLive)."""
import threading
import time

from makeros_hub import live
from makeros_hub.http import TransportError

JPEG = b"\xff\xd8\xff\xe0x\xff\xd9"


def _view(until=10_000.0):
    return live.View("p1", "src", until)


def test_posts_each_frame_until_until():
    clock = {"ms": 0.0}
    posted = []

    def frames():
        while True:
            clock["ms"] += 1000
            yield JPEG

    v = _view(until=5_000)
    live.run_view(v, frames, lambda view, j: posted.append(j) or 200, now_ms=lambda: clock["ms"], wait=lambda s: None)
    assert v.ended == "until" and v.sent == 4 and len(posted) == 4 and v.failed == 0


def test_the_site_saying_over_stops_it():
    v = _view()
    statuses = iter([200, 200, 410, 200])
    live.run_view(v, lambda: iter([JPEG] * 10), lambda view, j: next(statuses), now_ms=lambda: 0, wait=lambda s: None)
    assert v.ended == "site-410" and v.sent == 2 and v.failed == 1


def test_network_errors_wait_a_second_and_carry_on_and_bad_frames_count():
    v = _view()
    waits, calls = [], {"n": 0}

    def post(view, j):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TransportError("down")
        if calls["n"] == 4:
            view.stop.set()
        return 200

    live.run_view(v, lambda: iter([JPEG, JPEG, None, JPEG, JPEG, JPEG]), post, now_ms=lambda: 0, wait=waits.append)
    assert waits == [1.0] and v.sent == 3 and v.failed == 2 and v.ended == "stopped"


def test_a_broken_stream_is_reopened_after_a_second():
    v = _view()
    opened, waits = [], []

    def frames():
        opened.append(1)
        if len(opened) == 1:
            raise OSError("gateway restarted")
        if len(opened) == 3:
            v.stop.set()
        yield JPEG

    live.run_view(v, frames, lambda view, j: 200, now_ms=lambda: 0, wait=waits.append)
    assert len(opened) == 3 and waits == [1.0, 1.0] and v.sent == 1 and v.ended == "stopped"


def test_snapshot_cameras_are_asked_about_once_a_second():
    t = {"now": 0.0}
    waits = []
    shots = iter([(JPEG, None, ""), (None, "timeout", "")])

    def snapshot(url):
        t["now"] += 0.3
        return next(shots)

    it = live.snapshot_frames("http://pi:8080/snapshot", snapshot=snapshot, clock=lambda: t["now"], wait=waits.append)
    assert next(it) == JPEG and next(it) is None
    assert waits == [0.7]


class FakeGateway:
    def __init__(self, serves=True):
        self._serves = serves
        self.block = threading.Event()

    def serves(self, name):
        return self._serves

    def mjpeg(self, pid):
        while not self.block.wait(0.01):
            yield JPEG


def _targets():
    return {
        "x1": {"printerId": "x1", "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c", "cameraEnabled": True},
        "a1": {"printerId": "a1", "vendor": "bambu", "model": "A1 mini", "host": "h", "accessCode": "c", "cameraEnabled": True},
        "a1pi": {"printerId": "a1pi", "vendor": "bambu", "model": "A1 mini", "host": "h", "accessCode": "c",
                 "cameraSnapshotUrl": "http://pi:8080/snapshot", "cameraEnabled": True},
        "sv08": {"printerId": "sv08", "vendor": "klipper", "moonrakerUrl": "http://192.168.1.3", "cameraEnabled": True},
    }


def _wait_for(cond, s=2.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_which_printers_go_live():
    posts: list[str] = []
    gwy = FakeGateway()
    views = live.LiveViews(targets=_targets, post=lambda v, j: posts.append(v.printer_id) or 200, gateway=gwy,
                           snapshot=lambda url: (JPEG, None, ""), now_ms=lambda: 0)
    until = time.time() * 1000 + 60_000
    views.update([{"printerId": p, "until": until} for p in ("x1", "a1", "a1pi", "sv08")])
    assert _wait_for(lambda: {"x1", "a1pi", "sv08"} <= set(posts)), posts
    st = {s["printerId"]: s for s in views.stats()}
    assert st["a1"] == {"printerId": "a1", "error": "no-live-camera"}, "an A1 mini's own camera is never live"
    assert st["x1"]["sent"] >= 1 and "error" not in st["x1"]
    views.update([{"printerId": "x1", "until": until + 300_000}])   # renewed; the others left the list
    assert views._views["x1"][0].until_ms == until + 300_000
    assert _wait_for(lambda: all(not th.is_alive() for pid, (v, th) in views._views.items() if pid != "x1"))
    views.update(None)   # an answer without the key leaves the workers alone
    assert views._views["x1"][1].is_alive()
    views.stop_all()
    gwy.block.set()


def test_rtsp_without_the_gateway_is_reported_not_started():
    views = live.LiveViews(targets=_targets, post=lambda v, j: 200, gateway=FakeGateway(serves=False), now_ms=lambda: 0)
    views.update([{"printerId": "x1", "until": time.time() * 1000 + 60_000}, {"printerId": "gone", "until": 1e15}])
    assert {s["printerId"]: s.get("error") for s in views.stats()} == {"x1": "no-gateway", "gone": "no-live-camera"}
    assert views._views == {}


def test_the_hub_runs_at_most_four():
    gwy = FakeGateway()
    targets = {f"p{i}": {"printerId": f"p{i}", "vendor": "klipper", "moonrakerUrl": "http://k", "cameraEnabled": True} for i in range(6)}
    views = live.LiveViews(targets=lambda: targets, post=lambda v, j: 200, gateway=gwy,
                           snapshot=lambda url: (time.sleep(0.05), (JPEG, None, ""))[1], now_ms=lambda: 0)
    views.update([{"printerId": p, "until": time.time() * 1000 + 60_000} for p in targets])
    errors = [s for s in views.stats() if s.get("error") == "hub-cap"]
    assert len(views._views) == 4 and len(errors) == 2
    views.stop_all()


def test_a_view_the_site_ended_restarts_only_for_a_new_view():
    gwy = FakeGateway()
    views = live.LiveViews(targets=_targets, post=lambda v, j: 410, gateway=gwy, snapshot=lambda url: (JPEG, None, ""), now_ms=lambda: 0)
    until = time.time() * 1000 + 60_000
    views.update([{"printerId": "sv08", "until": until}])
    assert _wait_for(lambda: views._views["sv08"][0].ended == "site-410")
    first = views._views["sv08"][0]
    views.update([{"printerId": "sv08", "until": until}])   # a heartbeat that raced the end
    assert views._views["sv08"][0] is first
    views.update([{"printerId": "sv08", "until": until + 5}])   # a new view on the same printer
    assert views._views["sv08"][0] is not first
    views.stop_all()
