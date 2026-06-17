"""Klipper / Moonraker adapter.

One adapter per Klipper printer. Polls Moonraker over HTTP every few seconds
in a background thread, normalizes the response to the same wire DTO the
BambuAdapter produces (so `agent.py` doesn't need to branch). Stdlib only —
no requests/httpx, paho not involved. The agent's heartbeat reads via
`status()` like every other adapter.

Scope (PR-1 + PR-2 combined):
- PR-1: connection state, activity state, telemetry (progress%, temps, ETA, filename)
- PR-2: control commands (pause/resume/stop via Moonraker POST), and a job
  tracker that emits terminal print_jobs from observed print_stats.state
  transitions — same wire shape as the Bambu adapter's JobTracker so the
  cloud ingest is vendor-agnostic.

Auth: trusted LAN — Moonraker is typically wide open on the makerspace LAN.
A future PR can add the standard `X-Api-Key` header (operator pastes the
Moonraker API key per-printer). For now we send no auth and the operator
needs Moonraker bound to its LAN interface only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

# Job-buffer cap (mirror of the Bambu JobTracker). If somehow the cloud is
# offline for >MAX_PENDING completed prints we drop the OLDEST so we never
# starve fresh telemetry; the printer's own history covers the dropped ones.
MAX_PENDING_JOBS = 500

# Moonraker's `print_stats.state` values that count as "actively printing"
# for the job tracker.
_ACTIVE_STATES = frozenset({"printing"})
_TERMINAL_DONE_STATES = frozenset({"complete"})
_TERMINAL_FAILED_STATES = frozenset({"cancelled", "error"})
# Paused doesn't end a job — same printer keeps reporting print_stats.

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
        # PR-2: per-printer terminal-job buffer. Fed from the poll loop on
        # each successful tick; drained via pending_jobs() and cleared via
        # ack_jobs() — same shape as the Bambu adapter so the cloud ingest
        # is vendor-agnostic.
        self._job_tracker = KlipperJobTracker(printer_id)

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
        """Unacked terminal jobs observed since the last successful heartbeat
        ack. Same shape as `BambuAdapter.pending_jobs`. Safe to send
        repeatedly — the cloud dedupes on `jobKey`."""
        with self._lock:
            return self._job_tracker.pending()

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Drop jobs the cloud confirmed receiving (after a heartbeat 200)."""
        with self._lock:
            self._job_tracker.ack(job_keys)

    def send_command(self, command: str, params: dict | None = None) -> dict:
        """Pause / resume / stop the active print over Moonraker's standard
        HTTP control endpoints. Same shape as `BambuAdapter.send_command`:
        returns `{ok, reason?, command}`. The cloud control layer already
        gates which commands a workspace can issue + audits; we re-validate
        here as defense-in-depth and produce a directional `reason` so the
        admin UI can render a useful message.

        Wire endpoints (Moonraker docs):
          POST /printer/print/pause   — only valid while printing
          POST /printer/print/resume  — only valid while paused
          POST /printer/print/cancel  — terminates the print (the cloud's
                                        'stop' command maps here)
        We translate 'stop' → cancel to keep the command vocabulary aligned
        with Bambu's; the admin UI doesn't need to branch on vendor.
        """
        cmd_path = {
            "pause": "/printer/print/pause",
            "resume": "/printer/print/resume",
            "stop": "/printer/print/cancel",
        }.get(command)
        if cmd_path is None:
            # ams_dry / skip_objects are Bambu-only; surface as unsupported
            # so the cloud can render a directional "Klipper doesn't support
            # this control" message rather than failing silently.
            return {
                "ok": False,
                "reason": "unsupported_command",
                "command": command,
            }
        url = f"{self.moonraker_url}{cmd_path}"
        # Moonraker accepts POST with an empty body for the three control
        # endpoints; no params needed. We send an explicit Content-Length 0
        # so a stricter reverse-proxy in front of Moonraker doesn't trip.
        req = urllib.request.Request(
            url,
            method="POST",
            data=b"",
            headers={
                "Content-Length": "0",
                "Accept": "application/json",
                "User-Agent": "makeros-hub-agent",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
                body = resp.read(8 * 1024)
        except urllib.error.HTTPError as e:
            # 400/409 from Moonraker means "cannot pause/resume from current
            # state" (e.g. resume while idle). The body carries a message
            # which is useful to surface.
            try:
                err_body = e.read(8 * 1024).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            return {
                "ok": False,
                "reason": _classify_control_http_error(e.code, err_body),
                "command": command,
                "httpStatus": e.code,
            }
        except urllib.error.URLError as e:
            return {
                "ok": False,
                "reason": "unreachable",
                "command": command,
            }
        except TimeoutError:
            return {
                "ok": False,
                "reason": "timeout",
                "command": command,
            }
        # Moonraker 200 returns {"result": "ok"} on the three control
        # endpoints. We accept any 2xx — the printer state will change on
        # the next poll regardless and the cloud's command-result ingest
        # rolls a clean 'done'.
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - 200 with malformed body is still success-y
            parsed = None
        log.info("klipper %s control ok: %s", self.printer_id, command)
        return {
            "ok": True,
            "command": command,
            "result": (
                parsed.get("result") if isinstance(parsed, dict) else None
            ),
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
                    # Feed the job tracker every successful poll. Pure helper —
                    # converts the Moonraker status dict to the (state, filename,
                    # print_duration) it needs and accumulates terminal-state
                    # transitions into pending_jobs. Lock held throughout so a
                    # concurrent `pending_jobs()` / `ack_jobs()` call sees a
                    # consistent buffer.
                    self._job_tracker.observe(data, time.monotonic())
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


def _classify_control_http_error(code: int, body: str) -> str:
    """Map Moonraker's control-endpoint error codes to directional reasons
    the cloud admin UI can render. Moonraker uses 400 + body{"message":...}
    for state-machine rejections (e.g. resume when not paused) and 5xx for
    klippy daemon problems."""
    if code == 400:
        # Try to surface the message — but bounded so we never leak a
        # long upstream debug dump.
        msg = body[:200] if body else ""
        if "not currently paused" in msg.lower():
            return "not_paused"
        if "no print" in msg.lower() or "not started" in msg.lower():
            return "not_printing"
        return "invalid_state"
    if code == 401 or code == 403:
        return "auth_required"
    if code == 404:
        return "endpoint_missing"
    if 500 <= code < 600:
        return "klipper_error"
    return f"http_{code}"


class KlipperJobTracker:
    """Per-printer terminal-job emitter for Klipper. Same shape as the
    Bambu JobTracker — observes each poll's status dict, opens on first
    transition INTO 'printing', closes on transition INTO complete /
    cancelled / error / standby (Moonraker's terminal states).

    Stateful but lock-managed externally (the KlipperAdapter holds its own
    lock around observe + pending + ack). Pure dict / list ops here.

    Job key is a sha256 of (printer_id, filename, started_ts_ms). Klipper
    doesn't expose a stable per-print task id like Bambu's MQTT task_id,
    so a fingerprint is the best we can do — millisecond resolution avoids
    same-second collisions on cancel→restart-of-same-file.
    """

    def __init__(self, printer_id: str):
        self.printer_id = printer_id
        self._active: Optional[dict] = None
        self._pending: list[dict] = []
        self._last_state: str = ""

    def observe(self, status: dict[str, Any], now: float) -> None:
        """Feed one poll's `status` dict (Moonraker's `result.status` shape)
        + a monotonic timestamp. Detects job transitions and accumulates
        terminal records into pending()."""
        print_stats = status.get("print_stats") if isinstance(status, dict) else None
        if not isinstance(print_stats, dict):
            # Pre-pushall / shape error — no signal yet.
            return
        state_raw = print_stats.get("state")
        if not isinstance(state_raw, str):
            return
        # Klipper-internal Latin name passthrough.
        state = state_raw.strip().lower()
        filename = _job_filename(print_stats)
        prev_state = self._last_state

        if self._active is None:
            if state in _ACTIVE_STATES:
                self._open(filename, now)
            elif prev_state == "" and state in _TERMINAL_DONE_STATES.union(
                _TERMINAL_FAILED_STATES
            ):
                # First signal already terminal — agent (re)started onto a
                # printer that finished while we were down. We have no
                # observed start so the time bounds are approximate; emit
                # so the cloud at least gets the FINISH record (cloud's
                # needs_review path catches zero-gram / null-times).
                self._emit_recovered(filename, state, now)
        else:
            if state in _TERMINAL_DONE_STATES:
                self._close("done", now)
            elif state in _TERMINAL_FAILED_STATES:
                self._close("failed", now)
            elif state in _ACTIVE_STATES:
                # Same print continuing. If the filename CHANGED while
                # printing we missed a transition; close as cancelled and
                # open the new one. (Klipper does this rarely, but the
                # Bambu tracker has the same guard so the shape stays
                # symmetric.)
                if (
                    filename
                    and self._active.get("name")
                    and filename != self._active["name"]
                ):
                    self._close("cancelled", now)
                    self._open(filename, now)
                elif filename and not self._active.get("name"):
                    self._active["name"] = filename
            elif state == "paused":
                # Paused doesn't open or close; just remember the state for
                # the next observe call.
                pass
            elif state == "standby":
                # Missed the real ending. Treat as cancelled — same
                # convention as the Bambu tracker.
                self._close("cancelled", now)

        self._last_state = state

    def pending(self) -> list[dict]:
        return list(self._pending)

    def ack(self, job_keys: list[str]) -> None:
        keys = set(job_keys)
        self._pending = [j for j in self._pending if j["jobKey"] not in keys]

    # --- internals --------------------------------------------------------

    def _open(self, filename: Optional[str], now: float) -> None:
        self._active = {
            "key": _job_key(self.printer_id, filename, now),
            "name": filename,
            "startedAt": now,
        }

    def _close(self, status: str, now: float) -> None:
        if self._active is None:
            return
        job = {
            "jobKey": self._active["key"],
            "printerId": self.printer_id,
            "status": status,
            "startedAt": _iso(self._active["startedAt"]),
            "endedAt": _iso(now),
            "printTimeSeconds": max(0, int(now - self._active["startedAt"])),
        }
        if self._active.get("name"):
            job["filename"] = self._active["name"]
        self._buffer(job)
        self._active = None

    def _emit_recovered(
        self, filename: Optional[str], state: str, now: float
    ) -> None:
        """Agent (re)started onto an already-terminal printer. We have no
        observed start; use endedAt=now as a coarse approximation. The
        cloud's needs_review path catches the zero-length / null-times
        case so it stays visible."""
        if not filename:
            # Without a name we'd be inventing both bounds — too much
            # speculation. Skip; the printer's own history covers it.
            log.info(
                "klipper %s: terminal recovery with no filename — skipping",
                self.printer_id,
            )
            return
        status_out = (
            "done" if state in _TERMINAL_DONE_STATES else "failed"
        )
        job = {
            "jobKey": _job_key(self.printer_id, filename, now),
            "printerId": self.printer_id,
            "status": status_out,
            "startedAt": _iso(now),
            "endedAt": _iso(now),
            "printTimeSeconds": 0,
            "filename": filename,
        }
        self._buffer(job)

    def _buffer(self, job: dict) -> None:
        self._pending.append(job)
        if len(self._pending) > MAX_PENDING_JOBS:
            dropped = self._pending[: len(self._pending) - MAX_PENDING_JOBS]
            self._pending = self._pending[-MAX_PENDING_JOBS:]
            log.error(
                "klipper job buffer overflow on %s — dropped %d unacked job(s)",
                self.printer_id,
                len(dropped),
            )


def _job_filename(print_stats: dict[str, Any]) -> Optional[str]:
    fn = print_stats.get("filename")
    if isinstance(fn, str) and fn.strip():
        return fn.strip()[:300]
    return None


def _job_key(printer_id: str, filename: Optional[str], started_ts: float) -> str:
    """Stable per-(printer, file, start-ms) fingerprint. Same shape namespace
    as the Bambu tracker's: prefix `task_` is reserved for printers that
    supply their own task id, so we use `fp_` here (Klipper has none)."""
    basis = f"klipper|{printer_id}|{filename or '?'}|{started_ts:.3f}"
    return "fp_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _iso(monotonic_ts: float) -> str:
    """`time.monotonic` is not wall-clock — convert to ISO via wall time at
    *now* offset by the monotonic delta. Resolution is per-second since
    the cloud doesn't care about sub-second; same as the Bambu tracker."""
    wall = time.time() - (time.monotonic() - monotonic_ts)
    # 2026-06-16T22:30:00Z shape.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall))
