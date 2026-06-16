"""Klipper / Moonraker adapter — read-only MVP.

One adapter per Klipper printer. Polls Moonraker over HTTP every few seconds
in a background thread, normalizes the response to the same wire DTO the
BambuAdapter produces (so `agent.py` doesn't need to branch). Stdlib only —
no requests/httpx, paho not involved. The agent's heartbeat reads via
`status()` like every other adapter.

Scope of this MVP (operator deadline: tomorrow morning):
- Connection state (connecting / connected / offline / error)
- Activity state (idle / printing / paused / error)
- Progress %, nozzle temp, bed temp, current filename, ETA minutes
- NO control commands yet (pause/resume/start_print → KeyError on dispatch)
- NO job ingest (terminal print_jobs land in a follow-up)

Auth: trusted LAN — Moonraker is typically wide open on the makerspace LAN.
A future PR can add the standard `X-Api-Key` header (operator pastes the
Moonraker API key per-printer). For now we send no auth and the operator
needs Moonraker bound to its LAN interface only.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

# Poll cadence. 5s gives 6 samples per heartbeat (30s) — enough to feel live
# without hammering Moonraker. The bambu adapter is event-driven, but for
# Klipper a quiet poll keeps the code stdlib-only.
POLL_INTERVAL_SEC = 5.0
HTTP_TIMEOUT_SEC = 4.0
# How long without a successful poll before we mark the printer offline.
STALE_SEC = 30.0
# How long to give the FIRST poll before declaring 'unreachable'.
CONNECT_TIMEOUT_SEC = 20.0

# The exact Moonraker query we issue every tick. Asking for only the fields
# we use keeps response size small (< 2 KB) and means we never have to parse
# a giant query object.
_QUERY_OBJECTS = (
    "print_stats=state,filename,print_duration,total_duration"
    "&extruder=temperature,target"
    "&heater_bed=temperature,target"
    "&virtual_sdcard=progress"
    "&display_status=progress,message"
)

# Mapping from Moonraker `print_stats.state` to the wire DTO's activity state.
# Moonraker's state machine: 'standby' | 'printing' | 'paused' | 'complete' |
# 'cancelled' | 'error'. We collapse complete/cancelled/standby into 'idle'
# because the cloud's activity state is what's happening RIGHT NOW.
_STATE_MAP = {
    "standby": "idle",
    "printing": "printing",
    "paused": "paused",
    "complete": "idle",
    "cancelled": "idle",
    "error": "error",
}


class KlipperAdapter:
    """Owns one Klipper printer's Moonraker connection. Thread-safe reads via
    `status()`; a background polling thread keeps the cached state fresh."""

    def __init__(self, printer_id: str, moonraker_url: str):
        self.printer_id = printer_id
        self.moonraker_url = _normalize_base(moonraker_url)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._last_poll_ok_at: Optional[float] = None
        self._last_error_reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started: float = 0.0

    # Bambu compat — every adapter exposes these. The model field is only
    # set by Bambu so the agent's status pipeline can omit it.
    @property
    def model(self) -> None:
        return None

    @property
    def host(self) -> str:
        # Used by camera dispatch's "moonrakerUrl or host" fallback.
        return self.moonraker_url

    def start(self) -> None:
        self._started = time.monotonic()
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"klipper-{self.printer_id[:8]}", daemon=True
        )
        self._thread.start()
        log.info("klipper adapter %s polling %s", self.printer_id, self.moonraker_url)

    def stop(self) -> None:
        self._stop.set()
        # Don't join — the thread is daemon, polls are 4s timeouts max.

    # --- status read (any thread) -----------------------------------------
    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            last = self._last_poll_ok_at
            err = self._last_error_reason
            data = self._data
            started = self._started

        if last is not None:
            if (now - last) <= STALE_SEC:
                return _build_status(self.printer_id, data, "connected")
            return _build_status(self.printer_id, data, "offline")

        # No successful poll yet. If we have an error reason AND we've been
        # running for the connect timeout, surface it. Else we're still
        # connecting.
        if err and (now - started) > CONNECT_TIMEOUT_SEC:
            return _build_status(
                self.printer_id, {}, "error", error_reason=err
            )
        return _build_status(self.printer_id, {}, "connecting")

    def pending_jobs(self) -> list[dict]:
        """No job ingest in the read-only MVP."""
        return []

    def ack_jobs(self, job_keys: list[str]) -> None:
        return None

    def send_command(self, command: str, params: dict | None = None) -> dict:
        """Control commands are deferred to a follow-up. Return a structured
        rejection the manager can re-report so the cloud sees it."""
        return {
            "ok": False,
            "errorReason": "klipper_control_not_implemented",
            "command": command,
        }

    # --- polling loop -----------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._fetch_once()
            except Exception as e:  # noqa: BLE001 - never let the thread die
                reason = _classify_error(e)
                with self._lock:
                    self._last_error_reason = reason
                log.debug("klipper %s poll failed: %s", self.printer_id, reason)
            else:
                with self._lock:
                    self._data = data
                    self._last_poll_ok_at = time.monotonic()
                    self._last_error_reason = None
            # `wait` returns True if .set() fires mid-sleep — exit immediately.
            if self._stop.wait(POLL_INTERVAL_SEC):
                return

    def _fetch_once(self) -> dict[str, Any]:
        url = f"{self.moonraker_url}/printer/objects/query?{_QUERY_OBJECTS}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        # Moonraker shape: { result: { status: { print_stats: {...}, ... } } }
        result = parsed.get("result")
        if not isinstance(result, dict):
            raise ValueError("missing result")
        status = result.get("status")
        if not isinstance(status, dict):
            raise ValueError("missing status")
        return status


# ---------------------------------------------------------------------------
# Pure helpers (testable without HTTP)
# ---------------------------------------------------------------------------


def _normalize_base(url: str) -> str:
    """Trim trailing slash + add http:// if the operator forgot the scheme."""
    u = url.strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = f"http://{u}"
    return u


def _classify_error(err: BaseException) -> str:
    """Short machine-readable error reason for the wire DTO."""
    if isinstance(err, urllib.error.HTTPError):
        return f"http_{err.code}"
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", "url_error")
        if isinstance(reason, TimeoutError):
            return "timeout"
        # `[Errno 111] Connection refused` → 'unreachable'
        if hasattr(reason, "errno") and getattr(reason, "errno", None) == 111:
            return "unreachable"
        return "unreachable"
    if isinstance(err, TimeoutError):
        return "timeout"
    if isinstance(err, (ValueError, json.JSONDecodeError)):
        return "shape_mismatch"
    return "unknown"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _build_status(
    printer_id: str,
    status: dict[str, Any],
    connection_state: str,
    error_reason: Optional[str] = None,
) -> dict:
    """Pure: Moonraker status dict → the cloud's PrinterStatusDTO shape.

    Same key set + casing as `bambu_parse.normalize_status` so the cloud
    ingest doesn't branch on vendor. Omit-when-absent: the cloud DTO is
    strict() and rejects an explicit null for an optional number.
    """
    out: dict[str, Any] = {"printerId": printer_id, "connectionState": connection_state}
    if connection_state == "error" and error_reason:
        out["errorReason"] = error_reason

    if connection_state == "connected":
        print_stats = status.get("print_stats")
        if isinstance(print_stats, dict):
            state_raw = print_stats.get("state")
            if isinstance(state_raw, str):
                mapped = _STATE_MAP.get(state_raw, "idle")
                out["state"] = mapped
            fn = print_stats.get("filename")
            if isinstance(fn, str) and fn.strip():
                out["jobName"] = fn.strip()[:300]
            # Print duration vs total duration: we don't have an ETA from
            # Moonraker directly, but `print_stats.print_duration` × (1/progress)
            # − print_duration gives an estimate. Cheap to compute; null when
            # progress is 0 or unknown.
            print_dur = _num(print_stats.get("print_duration"))
            vs = status.get("virtual_sdcard")
            progress = _num(vs.get("progress")) if isinstance(vs, dict) else None
            if progress is not None:
                pct = max(0.0, min(100.0, progress * 100.0))
                out["progressPct"] = pct
                if print_dur and progress > 0.01:
                    eta_sec = print_dur * (1.0 - progress) / progress
                    if eta_sec > 0:
                        out["etaMinutes"] = int(eta_sec / 60)

        extruder = status.get("extruder")
        if isinstance(extruder, dict):
            t = _num(extruder.get("temperature"))
            if t is not None:
                out["nozzleTempC"] = t
        bed = status.get("heater_bed")
        if isinstance(bed, dict):
            t = _num(bed.get("temperature"))
            if t is not None:
                out["bedTempC"] = t

        # If print_stats said nothing, default to idle so the cloud doesn't
        # render this connection as 'offline' (no state) by accident.
        if "state" not in out:
            out["state"] = "idle"

    return out
