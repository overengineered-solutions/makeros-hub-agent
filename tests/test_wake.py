"""v0.73: the hub checks in the moment the site has new work (makeros_hub/wake.py)."""
import threading

from makeros_hub import wake
from makeros_hub.http import TransportError


class R:
    def __init__(self, status, body):
        self.status, self.body = status, body


def test_watch_sets_wake_on_work_and_carries_the_sequence():
    stop, ev, asked = threading.Event(), threading.Event(), []
    answers = [R(200, {"work": False, "seq": 3}), R(200, {"work": True, "seq": 4})]

    def get(url, bearer, timeout):
        asked.append(url)
        if not answers:
            stop.set()
            return R(200, {"work": False, "seq": 4})
        return answers.pop(0)
    wake.watch("https://site/api/print/hub/wait", "tok", ev, stop, get=get, pause=lambda s: None)
    assert ev.is_set()
    assert asked[0].endswith("?since=-1") and asked[1].endswith("?since=3") and asked[2].endswith("?since=4")


def test_watch_backs_off_on_a_missing_route_and_on_errors():
    stop, ev, pauses = threading.Event(), threading.Event(), []
    seq = [R(404, {}), TransportError("down"), R(500, {})]

    def get(url, bearer, timeout):
        if not seq:
            stop.set()
            return R(200, {"work": False, "seq": 0})
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    wake.watch("u", "t", ev, stop, get=get, pause=pauses.append)
    assert pauses == [600, 10, 10] and not ev.is_set()


def test_sleep_until_wakes_early_and_otherwise_waits_out_the_interval():
    stop, ev = threading.Event(), threading.Event()
    ev.set()
    assert wake.sleep_until(stop, ev, 30) is True and not ev.is_set()   # woken: cleared for next time
    t = [0.0]
    clock = lambda: t[0]
    real_wait = ev.wait
    def fake_wait(timeout):
        t[0] += timeout
        return False
    ev.wait = fake_wait
    assert wake.sleep_until(stop, ev, 5, clock=clock) is False and t[0] >= 5
    ev.wait = real_wait
    stop.set()
    assert wake.sleep_until(stop, ev, 30) is False
