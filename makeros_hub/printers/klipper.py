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
# GET (poll) is fast — query response is <2KB and Moonraker answers without
# touching klippy. 4s is generous.
HTTP_GET_TIMEOUT_SEC = 4.0
# Control commands (pause/resume/cancel) dispatch macros to klippy and only
# return once klippy acknowledges; cancel mid-print can take several seconds
# while motion drains. The cloud already retries idempotent control commands,
# so a too-aggressive timeout here surfaces as "unreachable" when the printer
# is actually mid-cancel. 15s is the Moonraker community's de-facto floor.
HTTP_CONTROL_TIMEOUT_SEC = 15.0
# Backwards-compat — older direct references still resolve, but new code
# should pick the per-call constant above.
HTTP_TIMEOUT_SEC = HTTP_GET_TIMEOUT_SEC
# Backoff caps on consecutive poll failures so a long-unreachable printer
# stops hammering the network at 5s intervals.
POLL_BACKOFF_MAX_SEC = 30.0
# How long without a successful poll before we mark the printer offline.
STALE_SEC = 30.0
# How long to give the FIRST poll before declaring 'unreachable'.
CONNECT_TIMEOUT_SEC = 20.0
# Filename truncation — matches the cloud's PrinterStatusDTO + NativeJobDTO
# (apps/web/lib/print/hub-jobs.ts, max 300). Single source of truth here.
JOB_NAME_MAX_CHARS = 300

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

    def __init__(self, printer_id: str, moonraker_url: str, diagnostics=None):
        self.printer_id = printer_id
        self.moonraker_url = _normalize_base(moonraker_url)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._last_poll_ok_at: Optional[float] = None
        self._last_error_reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started: float = 0.0
        # Consecutive poll failures — drives exponential backoff so a long-
        # unreachable printer doesn't hammer the network every 5s.
        self._consecutive_failures: int = 0
        # Hub diagnostics writer (optional; defaults to module-level default).
        # Surfaces poll failures, control errors, and job-buffer overflows on
        # the cloud admin diag page — operator doesn't need journalctl.
        self._diagnostics = diagnostics
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
        # Lifecycle fix: a prior stop() left self._stop SET, so a naive
        # restart would launch a thread that exits on its first
        # `while not self._stop.is_set()` check — adapter would look
        # alive but never poll. Adversarial-review HIGH finding. Clear
        # the event AFTER joining any still-running old thread so we
        # never have two pollers racing on self._data + the job tracker.
        if self._thread is not None and self._thread.is_alive():
            # Give the old loop one HTTP_GET_TIMEOUT_SEC worth of wind-down,
            # then proceed regardless — its daemon flag means a stuck thread
            # dies with the process.
            try:
                self._thread.join(timeout=HTTP_GET_TIMEOUT_SEC + 1.0)
            except RuntimeError:
                pass
        self._stop.clear()
        self._started = time.monotonic()
        self._consecutive_failures = 0
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"klipper-{self.printer_id[:8]}", daemon=True
        )
        self._thread.start()
        log.info("klipper adapter %s polling %s", self.printer_id, self.moonraker_url)

    def stop(self) -> None:
        self._stop.set()
        # Don't join — the thread is daemon, polls are 4s timeouts max.
        # On start(), we'll join any still-alive thread before clearing
        # self._stop so we never race two pollers.

    def _diag(self, subsystem: str, message: str) -> None:
        """Cloud-visible diagnostic record. Resolves the default diagnostics
        instance lazily so a constructor-less import still works in tests."""
        if self._diagnostics is None:
            try:
                from ..diagnostics import get_default

                self._diagnostics = get_default()
            except Exception:  # noqa: BLE001
                return
        try:
            self._diagnostics.record(subsystem, message)
        except Exception:  # noqa: BLE001
            pass

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
        # Liveness pre-check (adversarial review MED #15): a pause issued
        # against an offline printer used to fly straight to a TCP refuse,
        # spending HTTP_CONTROL_TIMEOUT_SEC of admin patience before the
        # cloud got the directional error. Short-circuit when the adapter
        # already knows the printer isn't reachable. Stale-window safety:
        # the heartbeat marks "offline" at STALE_SEC, which is well past
        # the freshest reasonable command latency.
        now = time.monotonic()
        with self._lock:
            last_ok = self._last_poll_ok_at
        if last_ok is None or (now - last_ok) > STALE_SEC:
            return {
                "ok": False,
                "reason": "not_connected",
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
            # Adversarial review HIGH #7: GET timeout (4s) is too short for
            # cancel, which dispatches a klippy macro and only returns once
            # motion drains. Use the longer control timeout.
            with urllib.request.urlopen(req, timeout=HTTP_CONTROL_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
                body = resp.read(8 * 1024)
        except urllib.error.HTTPError as e:
            # 400/409 from Moonraker means "cannot pause/resume from current
            # state" (e.g. resume while idle). The body carries a message
            # which is useful to surface.
            try:
                err_body = e.read(8 * 1024).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            reason = _classify_control_http_error(e.code, err_body)
            self._diag(
                "klipper",
                f"control {command} failed: {reason} (HTTP {e.code})",
            )
            return {
                "ok": False,
                "reason": reason,
                "command": command,
                "httpStatus": e.code,
            }
        except urllib.error.URLError as e:
            # Adversarial review MED #3: stdlib urllib wraps socket timeouts
            # in URLError(reason=TimeoutError(...)); the bare-URLError branch
            # would misclassify those as 'unreachable'. Unwrap so the cloud
            # admin UI gets the truthful directional reason — and so the
            # cloud's retry policy doesn't fire-and-forget what was actually
            # a delivered-but-slow command.
            reason = _classify_url_error(e)
            self._diag("klipper", f"control {command} failed: {reason}")
            return {
                "ok": False,
                "reason": reason,
                "command": command,
            }
        except TimeoutError:
            self._diag("klipper", f"control {command} failed: timeout")
            return {
                "ok": False,
                "reason": "timeout",
                "command": command,
            }
        # Moonraker 200 returns {"result": "ok"} on the three control
        # endpoints. Adversarial review MED #9: be strict about the shape —
        # a 200 with a missing/non-ok result is suspicious (proxy in the
        # way?) and worth surfacing rather than swallowing.
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, dict) and parsed.get("result") == "ok":
            log.info("klipper %s control ok: %s", self.printer_id, command)
            return {"ok": True, "command": command, "result": "ok"}
        # 200 + non-ok body — fail loud rather than report success.
        log.warning(
            "klipper %s control %s: 200 but unexpected body: %r",
            self.printer_id,
            command,
            (body[:80] if isinstance(body, (bytes, bytearray)) else None),
        )
        self._diag(
            "klipper",
            f"control {command} returned 200 but body shape mismatch",
        )
        return {
            "ok": False,
            "reason": "shape_mismatch",
            "command": command,
            "httpStatus": 200,
        }

    # --- polling loop -----------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._fetch_once()
            except Exception as e:  # noqa: BLE001 - never let the thread die
                reason = _classify_error(e)
                with self._lock:
                    was_clean = self._last_error_reason is None
                    self._last_error_reason = reason
                    self._consecutive_failures += 1
                    failures = self._consecutive_failures
                # Adversarial review MED #13: first failure after a run of
                # successes is WARN-level so it lands in the diag ring (which
                # gates on WARN+). Subsequent failures stay at DEBUG so a
                # long-offline printer doesn't drown the journal.
                if was_clean:
                    log.warning(
                        "klipper %s poll failed: %s", self.printer_id, reason
                    )
                    self._diag(
                        "klipper",
                        f"poll failed for {self.printer_id[:12]}: {reason}",
                    )
                else:
                    log.debug(
                        "klipper %s poll failed (#%d): %s",
                        self.printer_id,
                        failures,
                        reason,
                    )
                # Adversarial review MED #8: exponential-ish backoff on
                # consecutive failures so an unreachable printer stops
                # hammering the network at 5s intervals. Capped at
                # POLL_BACKOFF_MAX_SEC. Reset on next success.
                wait = min(
                    POLL_INTERVAL_SEC * min(2 ** (failures - 1), 8),
                    POLL_BACKOFF_MAX_SEC,
                )
            else:
                with self._lock:
                    was_first = self._last_poll_ok_at is None
                    self._data = data
                    self._last_poll_ok_at = time.monotonic()
                    self._last_error_reason = None
                    self._consecutive_failures = 0
                    # Feed the job tracker every successful poll. Pure helper —
                    # converts the Moonraker status dict to the (state, filename,
                    # print_duration) it needs and accumulates terminal-state
                    # transitions into pending_jobs. Lock held throughout so a
                    # concurrent `pending_jobs()` / `ack_jobs()` call sees a
                    # consistent buffer.
                    self._job_tracker.observe(data, time.monotonic())
                # Adversarial review MED #14: explicit "connected" log on
                # the first-poll-success transition so the cloud admin diag
                # gets a clear event (mirrors the Bambu adapter's
                # `bambu adapter %s connected` log).
                if was_first:
                    log.info(
                        "klipper adapter %s connected to %s",
                        self.printer_id,
                        self.moonraker_url,
                    )
                wait = POLL_INTERVAL_SEC
            # `wait` returns True if .set() fires mid-sleep — exit immediately.
            if self._stop.wait(wait):
                return

    def _fetch_once(self) -> dict[str, Any]:
        url = f"{self.moonraker_url}/printer/objects/query?{_QUERY_OBJECTS}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_GET_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
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
                out["jobName"] = fn.strip()[:JOB_NAME_MAX_CHARS]
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
    the cloud admin UI can render. Moonraker wraps state-machine rejections
    as 400 + {"error":{"message":"..."}} (newer builds) or {"message":"..."}
    (older). We parse the message out before substring-matching so we
    correctly catch the documented + observed phrasings."""
    if code == 400:
        message = _extract_moonraker_error_message(body)
        m = message.lower()
        # Adversarial review HIGH #6: widen the match. These cover the
        # documented Moonraker error phrasings + the variants observed
        # across community projects (pybambu / ha-bambulab / Mainsail).
        if any(
            kw in m
            for kw in (
                "not currently paused",
                "not paused",
                "no paused print",
                "cannot resume",
            )
        ):
            return "not_paused"
        if any(
            kw in m
            for kw in (
                "no print",
                "not started",
                "no active print",
                "nothing is printing",
                "cannot pause",
                "cannot cancel",
            )
        ):
            return "not_printing"
        if "klippy" in m and ("busy" in m or "shutdown" in m or "not ready" in m):
            return "klipper_error"
        return "invalid_state"
    if code == 401 or code == 403:
        return "auth_required"
    if code == 404:
        return "endpoint_missing"
    if 500 <= code < 600:
        return "klipper_error"
    return f"http_{code}"


def _extract_moonraker_error_message(body: str) -> str:
    """Pull the human message out of Moonraker's error envelope. Tries the
    `{error:{message}}` and `{message}` shapes and falls back to the raw
    body bounded at 200 chars (never the full body — we don't want a
    multi-KB klippy trace landing in our event log)."""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001
        return body[:200]
    if isinstance(parsed, dict):
        inner = parsed.get("error")
        if isinstance(inner, dict):
            msg = inner.get("message")
            if isinstance(msg, str):
                return msg[:200]
        msg = parsed.get("message")
        if isinstance(msg, str):
            return msg[:200]
    return body[:200]


def _classify_url_error(err: urllib.error.URLError) -> str:
    """Adversarial review MED #3: stdlib urllib wraps socket-level timeouts
    in URLError(reason=TimeoutError(...)). The bare-URLError branch would
    misclassify those as 'unreachable'. Map the inner reason to the right
    directional code so the cloud admin UI / retry policy can branch."""
    import socket

    reason = getattr(err, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_failure"
    if isinstance(reason, ConnectionRefusedError):
        return "unreachable"
    # Generic socket error / OSError — most often connection refused or
    # no-route-to-host. 'unreachable' is the safe default.
    return "unreachable"


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
        # Track print_duration across polls so a printing→complete→printing
        # cycle that fits inside one 5s poll window is detected via the
        # reset (new print_duration < prior). Adversarial-review HIGH #2:
        # without this the cycle is silently elided and prior print time
        # bleeds into the new one — a billing dispute waiting to happen.
        self._last_print_duration: Optional[float] = None

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
        # `print_duration` is a monotonic counter Klipper resets to ~0 at
        # the start of every print. We track it across polls so a
        # printing→complete→printing cycle that lands inside one 5s poll
        # window is detected via the reset edge.
        print_duration = _num(print_stats.get("print_duration"))

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
                self._emit_recovered(filename, state, now, print_stats)
        else:
            if state in _TERMINAL_DONE_STATES:
                self._close("done", now)
            elif state in _TERMINAL_FAILED_STATES:
                self._close("failed", now)
            elif state in _ACTIVE_STATES:
                # Hidden-cycle detection (adversarial review HIGH #2): a
                # full print→complete→printing or printing→cancelled→printing
                # cycle inside ONE poll window would otherwise be silently
                # joined into one job (or misclassified). Klipper resets
                # print_duration on every new print, so if the current
                # duration is LOWER than what we last saw, a fresh print
                # started in between. Close the prior — as 'done' when the
                # filename matches (most common: re-print of the same file
                # after success) and 'cancelled' when it differs (abort +
                # restart with a different file) — and open a new one.
                duration_reset = (
                    self._last_print_duration is not None
                    and print_duration is not None
                    and print_duration + 1.0 < self._last_print_duration
                )
                filename_changed = (
                    filename
                    and self._active.get("name")
                    and filename != self._active["name"]
                )
                if duration_reset:
                    # If the filename also changed, the prior print was
                    # explicitly aborted (cancel + new file). If it's the
                    # same file, the operator re-printed it after the prior
                    # one finished — close as 'done', not 'cancelled'.
                    self._close("cancelled" if filename_changed else "done", now)
                    self._open(filename, now)
                elif filename_changed:
                    # Filename swap without a duration reset — we missed
                    # the gap between two prints; close old as cancelled.
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

        # Track the last observed print_duration regardless of state — a
        # reset edge during paused→printing is also a cycle signal.
        if print_duration is not None:
            self._last_print_duration = print_duration

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
        self,
        filename: Optional[str],
        state: str,
        now: float,
        print_stats: dict[str, Any],
    ) -> None:
        """Agent (re)started onto an already-terminal printer. We have no
        observed start, but Klipper's `print_stats.print_duration` is the
        last print's wall-clock duration in seconds — derive
        printTimeSeconds + back-compute startedAt from it so the cloud
        sees real bounds (adversarial review MED #10). When duration is
        missing, fall back to zero + endedAt=now (the cloud's needs_review
        path then catches the zero-length record)."""
        if not filename:
            # Without a name we'd be inventing both bounds — too much
            # speculation. Skip; the printer's own history covers it.
            log.info(
                "klipper %s: terminal recovery with no filename — skipping",
                self.printer_id,
            )
            return
        status_out = "done" if state in _TERMINAL_DONE_STATES else "failed"
        print_dur = _num(print_stats.get("print_duration"))
        if print_dur is not None and print_dur > 0:
            print_time_seconds = int(print_dur)
            started_at = now - print_dur
        else:
            print_time_seconds = 0
            started_at = now
        job = {
            "jobKey": _job_key(self.printer_id, filename, started_at),
            "printerId": self.printer_id,
            "status": status_out,
            "startedAt": _iso(started_at),
            "endedAt": _iso(now),
            "printTimeSeconds": print_time_seconds,
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
        return fn.strip()[:JOB_NAME_MAX_CHARS]
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
