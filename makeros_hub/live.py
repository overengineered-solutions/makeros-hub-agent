"""Live view (v0.75): one frame a second from one printer's camera to the site while a member watches it.

Operator, 2026-10-19: "if we arent at a cap lets let someone request a live view form the page" and "i want every camera on
it if available". The site decides who watches and enforces the cap (3 printers live at once, 5 minutes each, renewable);
its heartbeat answer, and its wake answer so the first frame comes within seconds, carry `cameraLive: [{printerId, until}]`
(until = epoch ms). This keeps one worker thread per listed printer and stops it when the printer leaves the list, at
`until`, or when the site answers 410/404/401 for a frame.

Per camera kind (camera.camera_source_kind):
  bambu-rtsp     the gateway's 1 fps MJPEG stream (gateway.py); no gateway = no live view for that printer, reported
  http-snapshot  the snapshot URL about once a second (the camera Pis on the A1 minis, the SV08's Moonraker webcam)
  bambu-lan      never (the A1 minis' own port-6000 cameras)
Each frame is POSTed as raw JPEG to /api/print/hub/camera/live?printerId=&sourceId= with the hub bearer. A network error
waits a second and carries on. Nothing is stored here; the site keeps only the newest frame, in memory.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from .http import TransportError
from .printers.camera import _http_snapshot_with_reason, _snapshot_url, camera_source_id, camera_source_kind

log = logging.getLogger("makeros_hub.live")

MAX_WORKERS = 4        # ponytail: defensive; the site's own cap (3) is the real one
STOP_STATUSES = (401, 404, 410)
SNAPSHOT_EVERY_S = 1.0


@dataclass
class View:
    printer_id: str
    source_id: str
    until_ms: float
    stop: threading.Event = field(default_factory=threading.Event)
    sent: int = 0
    failed: int = 0
    post_ms: int = 0
    ended: str = ""          # why the worker stopped: until, stopped, site-<status>, error


def snapshot_frames(url: str, *, snapshot=_http_snapshot_with_reason, clock=time.monotonic,
                    wait: Callable[[float], Any] = time.sleep) -> Iterator[Optional[bytes]]:
    """A snapshot camera as a stream: one GET about every second; a failed GET yields None (counted, never fatal)."""
    while True:
        t0 = clock()
        jpeg, _reason, _detail = snapshot(url)
        yield jpeg
        wait(max(0.0, SNAPSHOT_EVERY_S - (clock() - t0)))


def run_view(view: View, frames: Callable[[], Iterator[Optional[bytes]]], post: Callable[[View, bytes], int], *,
             now_ms: Callable[[], float] = lambda: time.time() * 1000, wait: Callable[[float], Any] = time.sleep) -> None:
    """The worker loop. Opens `frames()`, posts each JPEG, and reopens it a second after it ends or breaks, until the view is
    stopped, `until` passes, or the site says the view is over."""
    while True:
        try:
            for jpeg in frames():
                if view.stop.is_set():
                    view.ended = view.ended or "stopped"
                    return
                if now_ms() >= view.until_ms:
                    view.ended = "until"
                    return
                if not jpeg:
                    view.failed += 1
                    continue
                t0 = time.monotonic()
                try:
                    status = post(view, jpeg)
                except TransportError:
                    view.failed += 1
                    wait(1.0)
                    continue
                view.post_ms += int((time.monotonic() - t0) * 1000)
                if status == 200:
                    view.sent += 1
                    continue
                view.failed += 1
                if status in STOP_STATUSES:
                    view.ended = f"site-{status}"
                    return
        except Exception as e:  # noqa: BLE001 - a broken stream is reopened, never raised into the hub
            view.failed += 1
            log.info("live view %s: stream broke (%s); reopening", view.printer_id, e.__class__.__name__)
        if view.stop.is_set() or now_ms() >= view.until_ms:
            view.ended = view.ended or ("stopped" if view.stop.is_set() else "until")
            return
        wait(1.0)


class LiveViews:
    """The workers. `update` is called from the heartbeat thread and the wake thread with the site's whole list."""

    def __init__(self, *, targets: Callable[[], dict[str, dict]], post: Callable[[View, bytes], int], gateway,
                 snapshot=_http_snapshot_with_reason, now_ms: Callable[[], float] = lambda: time.time() * 1000):
        self._targets, self._post, self._gateway, self._snapshot, self._now_ms = targets, post, gateway, snapshot, now_ms
        self._views: dict[str, tuple[View, threading.Thread]] = {}
        self._refused: dict[str, str] = {}
        self._lock = threading.Lock()

    def _frames(self, kind: str, target: dict) -> Optional[Callable[[], Iterator[Optional[bytes]]]]:
        pid = target["printerId"]
        if kind == "bambu-rtsp":
            return (lambda: self._gateway.mjpeg(pid)) if self._gateway.serves(f"{pid}-live") else None
        if kind == "http-snapshot":
            url = _snapshot_url(target) or ""
            return lambda: snapshot_frames(url, snapshot=self._snapshot)
        return None

    def update(self, entries: Any) -> None:
        if not isinstance(entries, list):
            return   # an older site, or an answer without the key: leave the workers alone
        want: dict[str, float] = {}
        for e in entries[:8]:
            if isinstance(e, dict) and isinstance(e.get("printerId"), str) and isinstance(e.get("until"), (int, float)):
                want[e["printerId"]] = float(e["until"])
        now = self._now_ms()
        with self._lock:
            for pid, (view, thread) in list(self._views.items()):
                if pid not in want:
                    view.stop.set()
                    if not thread.is_alive():
                        del self._views[pid]
            self._refused = {pid: why for pid, why in self._refused.items() if pid in want}
            targets = self._targets()
            for pid, until in want.items():
                cur = self._views.get(pid)
                if cur and cur[1].is_alive() and not cur[0].stop.is_set():
                    cur[0].until_ms = until   # a renewal
                    continue
                if until <= now or (cur and cur[0].ended.startswith("site-") and until <= cur[0].until_ms):
                    continue   # over, or the site already ended this one (a new view carries a new until)
                t = targets.get(pid)
                kind = camera_source_kind(t) if t else None
                frames = self._frames(kind, t) if kind else None
                if frames is None:
                    self._refused[pid] = "no-gateway" if kind == "bambu-rtsp" else "no-live-camera"
                    continue
                if sum(th.is_alive() for _, th in self._views.values()) >= MAX_WORKERS:
                    self._refused[pid] = "hub-cap"
                    continue
                view = View(pid, camera_source_id(t), until)
                th = threading.Thread(target=run_view, args=(view, frames, self._post), name=f"live-{pid[:8]}", daemon=True)
                self._views[pid] = (view, th)
                self._refused.pop(pid, None)
                th.start()
                log.info("live view started for %s (%s)", pid, kind)

    def stats(self) -> list[dict]:
        """Per view for the heartbeat (cameraLiveStats): frames sent, failed, average post ms, and why one could not run."""
        with self._lock:
            out = [{"printerId": v.printer_id, "sent": v.sent, "failed": v.failed,
                    "postMs": round(v.post_ms / v.sent) if v.sent else None, **({"ended": v.ended} if v.ended else {})}
                   for v, _ in self._views.values()]
            out += [{"printerId": pid, "error": why} for pid, why in self._refused.items()]
        return out[:8]

    def stop_all(self) -> None:
        with self._lock:
            for view, _ in self._views.values():
                view.stop.set()
