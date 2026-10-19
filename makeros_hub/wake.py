"""Check in the moment the site has new work for this box (v0.73, 2026-10-18).

Operator: "i would rather have what feels like live updates on that stuff and have it be accurate". The heartbeat stays
every 30 s; between beats this thread holds one long request open on GET /api/print/hub/wait?since=<seq> (the site
answers within 25 s, at once when a job, a ready file, a cleared plate, a Pause or Stop, or a paper print appears for this
box) and sets `wake`, which ends the heartbeat loop's sleep early. A site without the route (404) is left alone for ten
minutes; any other failure backs off ten seconds. It never sends anything itself: the heartbeat does the work.
v0.75: the answer also carries the site's live views (`cameraLive`), handed to `on_live` at once, so a live view's first
frame does not wait for the heartbeat's camera beat.
"""
from __future__ import annotations

import logging
import threading
import time

from .http import TransportError, get_json

log = logging.getLogger("makeros_hub.wake")


def watch(url: str, bearer: str, wake: threading.Event, stop: threading.Event, *, get=get_json, pause=None, on_live=None) -> None:
    pause = pause or stop.wait
    seq = -1
    while not stop.is_set():
        try:
            resp = get(f"{url}?since={seq}", bearer=bearer, timeout=40.0)
            body = resp.body if isinstance(resp.body, dict) else {}
            if isinstance(body.get("seq"), int):
                seq = body["seq"]
            if body.get("work") is True:
                wake.set()
            if on_live is not None and isinstance(body.get("cameraLive"), list):
                try:
                    on_live(body["cameraLive"])
                except Exception as e:  # noqa: BLE001 - a live-view hiccup must never stop the wake lane
                    log.warning("wake watch: live view update failed: %s", e.__class__.__name__)
            if resp.status == 404:
                pause(600)
            elif resp.status != 200:
                pause(10)
        except TransportError as e:
            log.debug("wake watch: %s", e)
            pause(10)


def sleep_until(stop: threading.Event, wake: threading.Event, seconds: float, clock=time.monotonic) -> bool:
    """The heartbeat loop's wait: True when woken early for new work, False at the normal interval or on stop."""
    end = clock() + seconds
    while not stop.is_set():
        left = end - clock()
        if left <= 0:
            return False
        if wake.wait(min(left, 1.0)):
            wake.clear()
            return True
    return False


def start(cfg, bearer: str, wake: threading.Event, stop: threading.Event, on_live=None) -> threading.Thread:
    t = threading.Thread(target=watch, args=(cfg.wait_url, bearer, wake, stop), kwargs={"on_live": on_live}, name="wake-watch", daemon=True)
    t.start()
    return t
