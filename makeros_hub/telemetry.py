"""v0.70: the telemetry push — coalesced per-printer status deltas every few seconds, beside the unchanged 30 s
heartbeat.

Why. The Floor saw what the hub knew up to 30 s late, because the heartbeat was the only path to the site and it is a
heavy transaction there (hub row lock, one upsert per printer, dispatch, cameras, OTA), so it cannot simply run faster.
The operator, 2026-10-15: "these waits are killer ... so we dont have to wait on hub heart beats i am tired of it and it
is confusing to everyone". The accepted design (pstation reports/Live printer status design.md) is not a faster
heartbeat, not a broker and not WebSockets: a thin stdlib POST of rounded deltas on one persistent HTTPS connection,
with a full snapshot as the self-healing floor. MQTT stays the upgrade path once there are two hubs or two site
processes.

Contract: docs/telemetry-v1.md (the site is built against that file). Everything here is best-effort: any failure is
logged, the next send is a full snapshot, and nothing can reach the heartbeat loop — the push runs on its own daemon
thread and never raises out of run().
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

log = logging.getLogger("makeros-hub.telemetry")

CONTRACT_VERSION = 1
# How often manager.live_statuses() is read into the latest-wins dict (cheap: each adapter's status() under its lock).
SAMPLE_SEC = 1.0
# The site's nextMs is clamped to this range; so is the configured cadence.
MIN_INTERVAL_MS = 2_000
MAX_INTERVAL_MS = 60_000
# A full snapshot at least this often, so a delta the site missed (restart, dropped request) heals within a minute.
FULL_EVERY_SEC = 60.0
# 404 means the site has no route yet (the hub can ship before the site does): say so once, ask again every minute.
NOT_FOUND_RETRY_SEC = 60.0
TIMEOUT_SEC = 5.0
MAX_RESPONSE_BYTES = 64 * 1024

# Status-dict key -> wire key. The heartbeat keeps nozzleTempC/bedTempC (the site's heartbeat ingest reads those
# names); the contract calls them nozzleC/bedC. Every other key is spelled the same on both.
_WIRE_KEYS = {
    "state": "state",
    "gcodeState": "gcodeState",
    "stageCode": "stageCode",
    "stageSecondsLeft": "stageSecondsLeft",
    "displayMessage": "displayMessage",
    "preparing": "preparing",
    "progressPct": "progressPct",
    "etaMinutes": "etaMinutes",
    "printDurationSec": "printDurationSec",
    "elapsedSec": "elapsedSec",
    "layer": "layer",
    "nozzleTempC": "nozzleC",
    "nozzleTargetC": "nozzleTargetC",
    "bedTempC": "bedC",
    "bedTargetC": "bedTargetC",
    "chamberC": "chamberC",
    "jobName": "jobName",
}
_HALF_DEGREE_KEYS = ("nozzleC", "nozzleTargetC", "bedC", "bedTargetC", "chamberC")
_WHOLE_SECOND_KEYS = ("printDurationSec", "elapsedSec")
_IDENTITY_KEYS = ("printerId", "sampledAt")
# A printer that is not printing sends a delta only when one of these moved: its temperature drift is noise
# ("idle printers silent"), but the moment it starts, stops, finishes (gcodeState FINISH) or changes file must go out.
_TRANSITION_KEYS = ("connected", "state", "gcodeState", "preparing", "jobName")
_ACTIVE_STATES = frozenset({"printing", "paused"})


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clamp_ms(ms: float) -> int:
    return max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, int(ms)))


def project(status: dict) -> Optional[dict]:
    """One heartbeat status dict -> its contract view, rounded BEFORE any diff (temperatures to 0.5 C, progress to
    0.1 %, whole minutes and seconds), so a nozzle wobbling 215.1 / 215.3 sends nothing. None for a row without an id."""
    pid = status.get("printerId")
    if not isinstance(pid, str) or not pid:
        return None
    connected = status.get("connectionState") == "connected"
    out: dict[str, Any] = {"printerId": pid, "connected": connected}
    sampled = status.get("sampledAt")
    if not _is_number(sampled) or sampled <= 0:
        return None  # Never observed: the heartbeat reports connectivity without inventing a sample time.
    out["sampledAt"] = int(sampled)
    for key, wire in _WIRE_KEYS.items():
        if key in status:
            out[wire] = status[key]
    if not connected:
        # The adapters give no activity state while disconnected; the contract's vocabulary has words for it.
        out["state"] = "error" if status.get("connectionState") == "error" else "offline"
    for key in _HALF_DEGREE_KEYS:
        if _is_number(out.get(key)):
            out[key] = round(out[key] * 2) / 2
    if _is_number(out.get("progressPct")):
        out["progressPct"] = round(float(out["progressPct"]), 1)
    if _is_number(out.get("etaMinutes")):
        out["etaMinutes"] = int(out["etaMinutes"])
    for key in _WHOLE_SECOND_KEYS:
        if _is_number(out.get(key)):
            out[key] = int(out[key])
    return out


def delta(prev: Optional[dict], cur: dict) -> Optional[dict]:
    """What a delta carries for one printer: everything when it was never sent; otherwise printerId, sampledAt and the
    keys whose rounded value changed since the last accepted send (a key that is gone is sent as null, so the site can
    clear it); nothing at all for an idle or offline printer whose state did not move."""
    if prev is None:
        return dict(cur)
    changed = {k: v for k, v in cur.items() if k not in _IDENTITY_KEYS and (k not in prev or prev[k] != v)}
    changed.update({k: None for k in prev if k not in cur and k not in _IDENTITY_KEYS})
    if not changed:
        return None
    active = any(d.get("state") in _ACTIVE_STATES or d.get("preparing") is True for d in (prev, cur))
    if not active and not any(k in changed for k in _TRANSITION_KEYS):
        return None
    out: dict[str, Any] = {"printerId": cur["printerId"]}
    if "sampledAt" in cur:
        out["sampledAt"] = cur["sampledAt"]
    out.update(changed)
    return out


class TelemetryPusher:
    """Latest-wins dict per printer, fed from `statuses` every second; a flush every `interval_sec` of coalesced
    deltas, a full snapshot at start, after any failure and every FULL_EVERY_SEC; one persistent http.client
    connection, re-created on error. `connect`, `clock` and `wall` are injectable for tests."""

    def __init__(
        self,
        url: str,
        bearer: str,
        statuses: Callable[[], list[dict]],
        *,
        interval_sec: float = 5.0,
        connect: Optional[Callable[[], http.client.HTTPConnection]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        parts = urlsplit(url)
        self._path = parts.path or "/"
        if connect is None:
            # HTTPS to the cloud; plain HTTP only for the loopback URLs config.validate_cloud_url allows (tests).
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            host, port = parts.hostname or "", parts.port
            connect = lambda: cls(host, port, timeout=TIMEOUT_SEC)  # noqa: E731
        self._connect = connect
        self._bearer = bearer
        self._statuses = statuses
        self._clock = clock
        self._wall = wall
        self._interval = _clamp_ms(interval_sec * 1000) / 1000.0
        self._latest: dict[str, dict] = {}   # printerId -> projected view, latest wins
        self._sent: dict[str, dict] = {}     # printerId -> the view the site last accepted
        self._conn: Optional[http.client.HTTPConnection] = None
        self._want_full = True                # start: the site knows nothing of us yet
        self._last_full_at: Optional[float] = None
        self._next_at = clock()
        self._route_missing = False
        self._failing = False

    # --- sampling ----------------------------------------------------------
    def sample(self) -> None:
        latest: dict[str, dict] = {}
        for status in self._statuses():
            view = project(status) if isinstance(status, dict) else None
            if view is not None:
                latest[view["printerId"]] = view
        self._latest = latest
        for pid in [p for p in self._sent if p not in latest]:
            del self._sent[pid]   # a printer removed from config-down is forgotten; it reappears as new

    def due(self) -> bool:
        return self._clock() >= self._next_at

    # --- sending -----------------------------------------------------------
    def flush(self) -> None:
        now = self._clock()
        full = self._want_full or self._last_full_at is None or now - self._last_full_at >= FULL_EVERY_SEC
        if full:
            printers = [dict(view) for view in self._latest.values() if view["connected"]]
        else:
            printers = [d for d in (delta(self._sent.get(pid), view) for pid, view in self._latest.items()) if d]
            if not printers:
                self._next_at = now + self._interval
                return   # nothing moved: silence; the periodic full is the keepalive
        body = {"v": CONTRACT_VERSION, "sentAt": int(self._wall() * 1000), "full": full, "printers": printers}
        reply = self._post(body)
        if reply is None:
            self._want_full = True
            self._next_at = now + (NOT_FOUND_RETRY_SEC if self._route_missing else self._interval)
            return
        for sent in printers:
            self._sent[sent["printerId"]] = dict(self._latest[sent["printerId"]])
        if full:
            self._last_full_at = now
        self._want_full = reply.get("wantFull") is True
        next_ms = reply.get("nextMs")
        if _is_number(next_ms):
            self._interval = _clamp_ms(next_ms) / 1000.0
        self._next_at = now + self._interval

    def _post(self, body: dict) -> Optional[dict]:
        """One POST on the kept connection. The site's reply dict when it accepted the send; None on anything else
        (the caller then sends a full next time)."""
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        # Same bearer and headers as the heartbeat (http.post_json), on a connection that stays open between sends.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bearer}",
            "User-Agent": "makeros-hub-agent",
        }
        try:
            if self._conn is None:
                self._conn = self._connect()
            self._conn.request("POST", self._path, body=data, headers=headers)
            resp = self._conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
        except Exception as exc:  # noqa: BLE001 - a dead socket is replaced on the next send
            self._drop_connection()
            self._note_failure(f"transport error: {exc}")
            return None
        if status == 404:
            self._drop_connection()
            if not self._route_missing:
                log.info("telemetry: the site has no %s route yet (404); retrying every %.0f s", self._path, NOT_FOUND_RETRY_SEC)
            self._route_missing = True
            return None
        was_missing, self._route_missing = self._route_missing, False
        if not 200 <= status < 300 or len(raw) > MAX_RESPONSE_BYTES:
            self._drop_connection()
            self._note_failure(f"HTTP {status}")
            return None
        try:
            reply = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            reply = None
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            self._drop_connection()
            # An empty or broken 200 (a proxy's, say) is not the site accepting the send — the heartbeat's rule too.
            self._note_failure("reply is not {ok: true}")
            return None
        if self._failing or was_missing:
            log.info("telemetry: sends accepted again")
        self._failing = False
        return reply

    def _note_failure(self, detail: str) -> None:
        # The first failure of a run is WARN (it lands in the diag ring); repeats stay DEBUG so an outage does not
        # drown the journal at one line per send — the klipper poller's rule.
        if self._failing:
            log.debug("telemetry send failed: %s", detail)
        else:
            log.warning("telemetry send failed (next send is a full snapshot): %s", detail)
        self._failing = True

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # --- the thread --------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        """The thread body: sample every second, flush when due. Nothing escapes: a bad tick is logged, the next send
        is a full snapshot, and the heartbeat never notices."""
        while not stop.is_set():
            try:
                self.sample()
                if self.due():
                    self.flush()
            except Exception as exc:  # noqa: BLE001
                self._want_full = True
                self._note_failure(f"tick failed: {exc}")
            if stop.wait(SAMPLE_SEC):
                break
        self._drop_connection()


def start_telemetry(cfg, bearer: str, statuses: Callable[[], list[dict]], stop: threading.Event) -> Optional[threading.Thread]:
    """Start the push thread; None when telemetry_sec is 0 (MAKEROS_HUB_TELEMETRY_SEC=0), and the heartbeat carries
    status alone, as before 0.70."""
    if cfg.telemetry_sec <= 0:
        log.info("telemetry push disabled (telemetry_sec=0); the heartbeat carries status alone")
        return None
    pusher = TelemetryPusher(cfg.telemetry_url, bearer, statuses, interval_sec=cfg.telemetry_sec)
    thread = threading.Thread(target=pusher.run, args=(stop,), name="telemetry", daemon=True)
    thread.start()
    log.info("telemetry push every %ss to %s (full snapshot every %.0f s)", cfg.telemetry_sec, cfg.telemetry_url, FULL_EVERY_SEC)
    return thread
