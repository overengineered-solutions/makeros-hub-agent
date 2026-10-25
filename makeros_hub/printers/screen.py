"""Shop facts and screen taps, off both the heartbeat and the printer polling threads.

Moonraker keeps each tap until the site acknowledges it. A lost reply or hub restart replays the same id; the site
dedupes it. This lane never sends motion, including for a screen's audit of a control it already ran locally.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
REFRESH_SEC = 60.0  # screen state.LINK_LOST_SEC (180) / 3
SITE_STALE_SEC = 90.0
TIMEOUT_SEC = 2.0
POLL_SEC = 5.0
EVENT_ID = re.compile(r"\d{1,16}-[a-f0-9]{6}\Z")
GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
ACTIONS = {"plate_clear", "report_problem", "control", "paused_long", "power_loss"}


def _number(x):
    return type(x) in (int, float) and math.isfinite(x)


def facts_of(value):
    if not isinstance(value, dict):
        raise ValueError("screen facts must be an object")
    facts = {}
    for key, size in (("who", 40), ("job", 80), ("job_id", 64), ("next", 80)):
        v = value.get(key)
        if not isinstance(v, str) or len(v) > size:
            raise ValueError("invalid screen text")
        facts[key] = v
    warnings, zones = value.get("warnings"), value.get("next_zones")
    if not isinstance(warnings, list) or len(warnings) > 10 or any(not isinstance(w, str) or len(w) > 200 for w in warnings):
        raise ValueError("invalid screen warnings")
    if not isinstance(zones, list) or len(zones) > 4 or any(type(z) is not int or not 1 <= z <= 4 for z in zones):
        raise ValueError("invalid screen zones")
    finish = value.get("finish_at")
    if finish is not None and (not _number(finish) or not 0 < finish < 1e11):
        raise ValueError("invalid screen finish time")
    # No site unlock path exists yet. Never turn an accidental field into staff access.
    return dict(facts, warnings=list(warnings), next_zones=sorted(set(zones)), finish_at=finish, staff_until=None, v=1)


def event_of(key, value):
    if not isinstance(key, str) or not EVENT_ID.fullmatch(key) or not isinstance(value, dict):
        raise ValueError("malformed screen event")
    if value.get("id") != key or type(value.get("v")) is not int or value["v"] != 1 or value.get("action") not in ACTIONS:
        raise ValueError("unknown or malformed screen event")
    if not _number(value.get("at")) or not 0 < value["at"] <= 1e11:
        raise ValueError("invalid screen event time")
    out = {k: value[k] for k in ("id", "v", "at", "action")}
    job = value.get("job_id", "")
    if not isinstance(job, str) or (job and not GUID.fullmatch(job)):
        raise ValueError("invalid screen job id")
    out["job_id"] = job
    for k, pattern in (("control", r"[a-z0-9_:.-]{1,64}"), ("result", r"[a-z_]{1,32}"), ("decision", r"[a-z_]{1,32}")):
        if k in value:
            if not isinstance(value[k], str) or not re.fullmatch(pattern, value[k]):
                raise ValueError("invalid screen event detail")
            out[k] = value[k]
    if "minutes" in value:
        if type(value["minutes"]) is not int or not 0 <= value["minutes"] <= 100000:
            raise ValueError("invalid screen pause time")
        out["minutes"] = value["minutes"]
    if value.get("by") in ("staff", "member"):
        out["by"] = value["by"]
    # Do not forward filenames, free text or a site's named power-loss actor into staff diagnostics.
    return out


class ScreenLink:
    def __init__(self, printer_id, base):
        self.printer_id, self.base = printer_id, base
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._facts, self._offered = None, 0.0
        self._written, self._written_at = None, 0.0
        self._pending, self._acked = {}, set()
        self._outage = False

    def offer(self, value):
        facts = facts_of(value) if value is not None else None
        with self._lock:
            self._facts, self._offered = facts, time.monotonic()
        if facts is not None and self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"screen-{self.printer_id[:8]}", daemon=True)
            self._thread.start()
        self._wake.set()

    def pending(self):
        with self._lock:
            return [dict(e, printerId=self.printer_id) for k, e in self._pending.items() if k not in self._acked]

    def ack(self, ids):
        with self._lock:
            self._acked.update(k for k in ids if k in self._pending)
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _request(self, method, key, value=None):
        args = {"namespace": "pstation", "key": key}
        url = self.base + "/server/database/item"
        data = None
        if method == "GET":
            url += "?" + urllib.parse.urlencode(args)
        else:
            if method == "POST":
                args["value"] = value
            data = json.dumps(args, allow_nan=False).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as response:
                body = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as e:
            if e.code == 404 and method in ("GET", "DELETE"):
                return {}  # namespace/key not written yet, or this exact event already deleted
            raise
        if len(body) > 1024 * 1024:
            raise ValueError("screen database response too large")
        parsed = json.loads(body)
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(result, dict) or "value" not in result:
            raise ValueError("screen database response missing value")
        returned = result.get("key")
        path = key if isinstance(key, list) else key.split(".")
        if result.get("namespace") != "pstation" or (returned.split(".") if isinstance(returned, str) else returned) != path:
            raise ValueError("screen database response names another key")
        if method == "POST" and result["value"] != value:
            raise ValueError("screen database write did not match")
        return result["value"]

    def _sync(self):
        now = time.monotonic()
        with self._lock:
            facts, offered, acked = self._facts, self._offered, set(self._acked)
        if facts is not None and now - offered <= SITE_STALE_SEC and (facts != self._written or now - self._written_at >= REFRESH_SEC):
            self._request("POST", "screen", dict(facts, updated=time.time()))
            self._written, self._written_at = facts, now
        events = self._request("GET", "screen_events")
        if not isinstance(events, dict):
            raise ValueError("screen event collection must be an object")
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if k in events}
            self._acked.intersection_update(events)
        processed = 0
        for key, value in list(events.items())[:64]:
            if self._stop.is_set() or time.monotonic() - now >= POLL_SEC:
                break
            discard = key in acked
            if not discard:
                try:
                    event = event_of(key, value)
                except (ValueError, TypeError):
                    log.warning("screen %s: unknown or malformed event discarded", self.printer_id)
                    discard = True
                else:
                    with self._lock:
                        self._pending[key] = event
            if discard:
                self._request("DELETE", ["screen_events", key])
                with self._lock:
                    self._pending.pop(key, None)
                    self._acked.discard(key)
            processed += 1
        if events:
            log.info("screen %s events processed=%d, expected=%d", self.printer_id, processed, len(events))

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._sync()
            except Exception as e:  # a printer's database must never stop the print/status loops
                if not self._outage:
                    log.warning("screen %s: link unavailable (%s); will retry", self.printer_id, type(e).__name__)
                self._outage = True
            else:
                if self._outage:
                    log.info("screen %s: link recovered", self.printer_id)
                self._outage = False
            self._wake.wait(POLL_SEC)
