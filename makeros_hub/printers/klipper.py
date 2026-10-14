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
- Queue dispatch: start_print uploads a cloud-assigned G-code
  (POST /server/files/upload) and starts it (POST /printer/print/start);
  collect_queue_progress turns observed print_stats into queue reports through
  the same QueueProgressTracker as Bambu. Dispatch state and unacked terminal
  jobs are persisted (owner rule: nothing that matters lives only in RAM).

Auth: trusted LAN — Moonraker is typically wide open on the makerspace LAN.
A future PR can add the standard `X-Api-Key` header (operator pastes the
Moonraker API key per-printer). For now we send no auth and the operator
needs Moonraker bound to its LAN interface only.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .gcode_meta import read_gcode_meta
from .queue_progress import QueueProgressTracker, _safe_file_component, write_json_durably

log = logging.getLogger(__name__)

# Same env vars + defaults as bambu.py (one directory per kind of state, one file per printer). Defined here, not
# imported: bambu.py needs paho.
QUEUE_PROGRESS_DIR = Path(os.environ.get("MAKEROS_HUB_QUEUE_PROGRESS_DIR", "/var/lib/makeros-hub/queue-progress"))
TERMINAL_JOBS_DIR = Path(os.environ.get("MAKEROS_HUB_TERMINAL_JOBS_DIR", "/var/lib/makeros-hub/terminal-jobs"))

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
# A large-format G-code over the shop LAN; Moonraker also parses its metadata before it answers.
HTTP_UPLOAD_TIMEOUT_SEC = 120.0
# Where dispatched files land under Moonraker's gcodes root, and the cap on the whole relative path.
REMOTE_DIR = "makeros"
REMOTE_PATH_MAX_CHARS = 200
# Backoff caps on consecutive poll failures so a long-unreachable printer
# stops hammering the network at 5s intervals.
POLL_BACKOFF_MAX_SEC = 30.0
# How long without a successful poll before we mark the printer offline.
STALE_SEC = 30.0
# How long to give the FIRST poll before declaring 'unreachable'.
CONNECT_TIMEOUT_SEC = 20.0
# A print the tracker saved as running is the same print after a restart when the printer still shows that file and its
# start (wall now − print_stats.total_duration) agrees within this. ponytail: a cancel + re-start of the SAME file within
# this window while the hub was down is taken for the saved print; a wall-clock step across the restart falls back to a
# new key (the old behaviour).
RESUME_START_SLACK_SEC = 30.0
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

# print_stats.state → the Bambu gcode_state vocabulary QueueProgressTracker speaks.
_GCODE_STATE_MAP = {
    "printing": "RUNNING",
    "paused": "PAUSE",
    "standby": "IDLE",
    "complete": "FINISH",
    "cancelled": "FAILED",
    "error": "FAILED",
}
# start_print's live check: is klippy up, is the printer between prints, and is klippy not busy running commands (a QGL /
# bed mesh / heat-soak macro from the screen leaves print_stats at standby but idle_timeout at "Printing"; a start sent
# then waits behind it on klippy's gcode mutex and our start POST times out)?
_LIVE_QUERY = "webhooks=state&print_stats=state,filename&idle_timeout=state"
_STARTABLE_STATES = frozenset({"standby", "complete", "cancelled"})
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class KlipperAdapter:
    """Owns one Klipper printer's Moonraker connection. Thread-safe reads via
    `status()`; a background polling thread keeps the cached state fresh."""

    def __init__(self, printer_id: str, moonraker_url: str, diagnostics=None, *, model: Optional[str] = None):
        self.printer_id = printer_id
        self.moonraker_url = _normalize_base(moonraker_url)
        # The cloud's machines.model. start_print refuses a G-code whose printer_model is not exactly this; the
        # status DTO never carries it (the `model` property below stays None).
        self._model = model
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
        safe_id = _safe_file_component(printer_id)
        self._job_tracker = KlipperJobTracker(printer_id, state_path=TERMINAL_JOBS_DIR / f"{safe_id}.json")
        # Queue assignment ↔ print correlation, driven by OBSERVED print_stats
        # (same tracker as Bambu), persisted so an update / power cut never
        # loses which queue job the printer is running.
        self._queue_progress = QueueProgressTracker()
        self._queue_progress_path = QUEUE_PROGRESS_DIR / f"{safe_id}.json"
        self._queue_progress_saved = ""
        try:
            self._queue_progress.load_state(json.loads(self._queue_progress_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

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
        # Join (codex review of release-0.68.0): the manager builds the replacement adapter right after this, and it
        # loads the same state file. A poll still in flight would record and save a finished print after that load,
        # and the replacement's next save would drop it. ponytail: bounded by one HTTP GET; a poll stuck past that
        # still races (the thread is daemon). start() also joins before it clears self._stop.
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=HTTP_GET_TIMEOUT_SEC + 1.0)

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

    # --- queue dispatch (cloud assignment → Moonraker) --------------------
    def pending_queue_job_ids(self) -> list[str]:
        with self._lock:
            return self._queue_progress.pending_queue_job_ids()

    def pending_queue_jobs(self) -> list[dict]:
        with self._lock:
            return self._queue_progress.pending_queue_jobs()

    def start_print(
        self,
        local_path,
        file_name: str,
        *,
        plate: int = 1,
        use_ams: bool = False,
        ams_mapping=None,
        queue_job_id: str | None = None,
        raw_print: dict | None = None,
        required_filaments: list | None = None,
        assignment_seq: int | None = None,
    ) -> dict:
        """Upload a cloud-assigned G-code to Moonraker and start it. Same keywords as BambuAdapter.start_print; plate,
        use_ams, ams_mapping, raw_print and required_filaments are Bambu-only and ignored (Klipper has no AMS mirror —
        the cloud checks the staff-set loaded filament before it assigns).

        Order: idempotency → model gate → live idle check → record + persist the dispatch → upload → start. Nothing is
        uploaded before the dispatch is durable; a step that certainly failed forgets the dispatch again."""
        if not isinstance(queue_job_id, str) or not queue_job_id:
            return {"ok": False, "reason": "bad_assignment"}
        with self._lock:
            pending = self._queue_progress.pending_queue_jobs()
            mine = [p for p in pending if p["queueJobId"] == queue_job_id]
            if mine:
                old_seq = mine[0].get("assignmentSeq")
                if old_seq is None or assignment_seq is None or old_seq == assignment_seq:
                    # the durable dispatch state already holds THIS assignment: nothing is uploaded or started twice
                    log.info("klipper %s: %s already dispatched here — not starting it again", self.printer_id, queue_job_id)
                    return {"ok": True, "already_dispatched": True}
                log.warning("klipper %s: %s re-assigned (seq %s → %s) — superseding the stale dispatch record",
                            self.printer_id, queue_job_id, old_seq, assignment_seq)
                self._queue_progress.discard_dispatch(queue_job_id)
                self._save_queue_progress()
                pending = [p for p in pending if p["queueJobId"] != queue_job_id]
            if pending:
                # another job's outcome on this printer is still unknown: never stack a second start on it
                return {"ok": False, "reason": "printer_busy: dispatch_pending"}

        # The cloud matched the job to this printer by model; the file itself must agree (G-code for another machine
        # must never reach a printer — the same rule as Bambu's _sliced_model_matches).
        try:
            meta = read_gcode_meta(local_path)
        except OSError:
            meta = None
        sliced = (meta or {}).get("printerModel")
        if not (self._model and sliced and sliced.strip() == self._model.strip()):
            log.warning("klipper %s: refusing %s — sliced for %r, this printer is %r", self.printer_id, queue_job_id, sliced, self._model)
            return {"ok": False, "reason": "model_mismatch"}

        refusal = self._live_refusal()
        if refusal:
            log.info("klipper %s: not starting %s — %s", self.printer_id, queue_job_id, refusal)
            return {"ok": False, "reason": refusal}

        remote = _remote_path(queue_job_id, file_name)
        if not self._record_dispatch(queue_job_id, remote, assignment_seq):
            return {"ok": False, "reason": "state_persist_failed"}
        try:
            uploaded = self._upload(Path(local_path), remote)
        except Exception as exc:  # noqa: BLE001 — any failure: the file is not (provably) there
            detail = _http_detail(exc)
            log.warning("klipper %s: upload of %s failed: %s", self.printer_id, remote, detail)
            self._diag("klipper", f"upload for {queue_job_id[:8]} failed: {detail}")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "upload_failed"}
        if uploaded != remote:
            # Moonraker names the file it stored; print_stats.filename will be that name, so the dispatch must be too.
            self._discard_dispatch(queue_job_id)
            if not self._record_dispatch(queue_job_id, uploaded, assignment_seq):
                return {"ok": False, "reason": "state_persist_failed"}

        url = f"{self.moonraker_url}/printer/print/start?{urllib.parse.urlencode({'filename': uploaded})}"
        req = urllib.request.Request(
            url, method="POST", data=b"",
            headers={"Content-Length": "0", "Accept": "application/json", "User-Agent": "makeros-hub-agent"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_CONTROL_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
                body = resp.read(8 * 1024)
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            log.warning("klipper %s: start of %s refused: %s", self.printer_id, uploaded, detail)
            self._diag("klipper", f"print start for {queue_job_id[:8]} refused: {detail}")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            # The command may have reached klippy (no answer, or a broken one): keep the dispatch and let telemetry decide
            # (RUNNING → printing; nothing within the start timeout → start_not_observed). Never a second start on top of a
            # maybe-first.
            log.warning("klipper %s: start of %s unconfirmed (%s) — waiting on telemetry", self.printer_id, uploaded, _classify_error(exc))
            return {"ok": True}
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            # a truncated / garbled 2xx body is a broken reply too, not a refusal: same no-answer path
            log.warning("klipper %s: start of %s answered unreadably — waiting on telemetry", self.printer_id, uploaded)
            return {"ok": True}
        if not (isinstance(parsed, dict) and parsed.get("result") == "ok"):
            self._diag("klipper", f"print start for {queue_job_id[:8]} answered without ok")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        log.info("klipper %s: started %s for %s", self.printer_id, uploaded, queue_job_id)
        return {"ok": True}

    def _live_refusal(self) -> Optional[str]:
        """None when klippy is ready and the printer is between prints; else the held reason. A fresh query, not the
        poll cache — the cache may be seconds old and a member may just have started something at the screen."""
        try:
            status = self._fetch_once(_LIVE_QUERY)
        except Exception as exc:  # noqa: BLE001
            return f"printer_stale: {_classify_error(exc)}"
        hooks = status.get("webhooks")
        stats = status.get("print_stats")
        klippy = hooks.get("state") if isinstance(hooks, dict) else None
        state = stats.get("state") if isinstance(stats, dict) else None
        if not isinstance(klippy, str) or not isinstance(state, str):
            return "printer_stale: shape_mismatch"
        if klippy != "ready":
            return f"printer_busy: klippy_{_UNSAFE_NAME.sub('_', klippy)[:24]}"
        if state in ("printing", "paused"):
            return "printer_busy: printing"
        if state == "error":
            return "printer_busy: klipper_error"
        if state not in _STARTABLE_STATES:
            return f"printer_busy: {_UNSAFE_NAME.sub('_', state)[:24]}"
        idle = status.get("idle_timeout")
        idle_state = idle.get("state") if isinstance(idle, dict) else None
        if not isinstance(idle_state, str):
            return "printer_stale: shape_mismatch"   # klippy always loads idle_timeout: without it we cannot rule out a macro
        if idle_state == "Printing":
            return "printer_busy: klippy_busy"
        return None

    def _record_dispatch(self, queue_job_id: str, task_name: str, assignment_seq) -> bool:
        with self._lock:
            self._queue_progress.record_dispatch(
                queue_job_id, self._job_tracker.pending(), task_name=task_name,
                active_key=self._job_tracker.active_key(), assignment_seq=assignment_seq,
            )
            if self._save_queue_progress():
                return True
            # a print we cannot track across a restart is a print we do not start
            self._queue_progress.discard_dispatch(queue_job_id)
            log.error("klipper %s: refusing to start %s — dispatch state could not be persisted", self.printer_id, queue_job_id)
            return False

    def _discard_dispatch(self, queue_job_id: str) -> None:
        with self._lock:
            self._queue_progress.discard_dispatch(queue_job_id)
            self._save_queue_progress()

    def _upload(self, local: Path, remote: str) -> str:
        """POST /server/files/upload, streamed (the file is never read into memory whole). Returns the stored path
        relative to the gcodes root (item.path). `print` is deliberately NOT sent: with it Moonraker swallows a failed
        start, or silently job-queues the file to start later (file_manager._finish_gcode_upload)."""
        directory, _, name = remote.rpartition("/")
        digest = hashlib.sha256()
        with local.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        boundary = "makeros" + os.urandom(12).hex()
        fields = {"root": "gcodes", "path": directory, "checksum": digest.hexdigest()}
        head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        head_bytes = head.encode("utf-8")
        tail_bytes = f"\r\n--{boundary}--\r\n".encode("utf-8")
        size = local.stat().st_size

        def body():
            yield head_bytes
            with local.open("rb") as src:
                yield from iter(lambda: src.read(1 << 20), b"")
            yield tail_bytes

        req = urllib.request.Request(
            f"{self.moonraker_url}/server/files/upload",
            data=body(),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head_bytes) + size + len(tail_bytes)),
                "Accept": "application/json",
                "User-Agent": "makeros-hub-agent",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_UPLOAD_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
            status = resp.status
            raw = resp.read(64 * 1024)
        if status != 201:
            raise ValueError(f"upload answered http_{status}, expected 201")
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        # The upload body is NOT wrapped in "result" (application.py FileUploadHandler); accept a wrapped one anyway.
        result = parsed.get("result", parsed) if isinstance(parsed, dict) else None
        item = result.get("item") if isinstance(result, dict) else None
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path:
            raise ValueError("upload answered without item.path")
        return path

    def collect_queue_progress(self, on_report=None) -> list[dict]:
        """Drain queue-status reports inferred from observed print_stats. `on_report` receives each report BEFORE the
        popped dispatch state is persisted (the agent writes it to the durable outbox there); a report that could not be
        made durable rolls the tracker back and hands out nothing — the same reports come again next beat."""
        with self._lock:
            last = self._last_poll_ok_at
            stats = self._data.get("print_stats") if isinstance(self._data, dict) else None
            raw_state = stats.get("state") if isinstance(stats, dict) else None
            # None = no FRESH observation: silence is not idle, so no timeouts run on it
            fresh = last is not None and (time.monotonic() - last) <= STALE_SEC
            gcode_state = _GCODE_STATE_MAP.get(raw_state) if fresh and isinstance(raw_state, str) else None
            before = self._queue_progress.to_state()
            task_names = {d["queueJobId"]: d.get("taskName") for d in before["dispatches"]}
            head_task = before["dispatches"][0].get("taskName") if before["dispatches"] else None
            if gcode_state in ("RUNNING", "PAUSE") and head_task and _job_filename(stats) != head_task:
                # Another file is running (a walk-up print from the screen): it is not this job's print, so it must never
                # report 'printing' for it. The job can only time out to start_not_observed (or link its own terminal).
                gcode_state = "IDLE"
            reports = self._queue_progress.collect(self._job_tracker.pending(), gcode_state)
            for report in reports:
                task = task_names.get(report.get("queueJobId"))
                # only a DURABLE key: a key a restart cannot find again would make the cloud refuse the real terminal
                # (hub_job_ref set to another key); without one the cloud's time-window match still finds the print
                key = (self._job_tracker.active_key(task)
                       if report.get("state") == "printing" and task and not self._job_tracker.unsaved else None)
                if key:
                    report["printerJobKey"] = key   # the cloud stamps hub_job_ref from it, so the terminal job links
            if callable(on_report):
                for report in reports:
                    try:
                        on_report(report)
                    except Exception as exc:  # noqa: BLE001
                        log.error("progress report not durable (%s) — keeping the dispatch state, retrying next beat", exc)
                        self._queue_progress.load_state(before, now=time.monotonic(), now_wall=time.time())
                        return []
            self._save_queue_progress()
            return reports

    def _save_queue_progress(self) -> bool:
        """Write only when the state changed (SD-card friendly). False = not durable."""
        encoded = json.dumps(self._queue_progress.to_state(), sort_keys=True)
        if encoded == self._queue_progress_saved:
            return True
        if not write_json_durably(self._queue_progress_path, encoded):
            log.error("klipper %s: queue progress could not be made durable", self.printer_id)
            return False
        self._queue_progress_saved = encoded
        return True

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
                was_first = self._record_poll(data)
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

    def _record_poll(self, data: dict[str, Any]) -> bool:
        """Apply one successful poll; True on the first one since start."""
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
        return was_first

    def _fetch_once(self, query: str = _QUERY_OBJECTS) -> dict[str, Any]:
        url = f"{self.moonraker_url}/printer/objects/query?{query}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_GET_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        # Moonraker shape: { result: { status: { print_stats: {...}, ... } } }
        result = parsed.get("result") if isinstance(parsed, dict) else None
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


def _remote_path(queue_job_id: str, file_name: str) -> str:
    """makeros/<job8>_<file>, at most REMOTE_PATH_MAX_CHARS, the extension kept (Moonraker only treats .gcode/.gco/.g
    as printable). Names are reduced to [A-Za-z0-9._-] so they are safe in a multipart header and a query string."""
    name = _UNSAFE_NAME.sub("_", PurePosixPath(file_name).name) or "print.gcode"
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    prefix = f"{REMOTE_DIR}/{_UNSAFE_NAME.sub('_', queue_job_id[:8])}_"
    return prefix + stem[: max(1, REMOTE_PATH_MAX_CHARS - len(prefix) - len(suffix))] + suffix


def _http_detail(err: BaseException) -> str:
    """A short, log-safe description of a failed Moonraker call (HTTP code + Moonraker's own message)."""
    if isinstance(err, urllib.error.HTTPError):
        try:
            message = _extract_moonraker_error_message(err.read(8 * 1024).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            message = ""
        return f"HTTP {err.code} {message}".strip()[:200]
    return f"{_classify_error(err)}: {err}"[:200]


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
    lock around observe + pending + ack). Dict / list ops, plus a durable
    save (state_path) whenever the terminal buffer changes.

    Job key is a sha256 of (printer_id, filename, started_ts_ms). Klipper
    doesn't expose a stable per-print task id like Bambu's MQTT task_id,
    so a fingerprint is the best we can do — millisecond resolution avoids
    same-second collisions on cancel→restart-of-same-file.
    """

    def __init__(self, printer_id: str, state_path: Optional[Path] = None):
        self.printer_id = printer_id
        self._active: Optional[dict] = None
        self._pending: list[dict] = []
        self._last_state: str = ""
        # Durability (owner rule: not RAM-only): unacked terminal jobs, plus the last print this tracker reported
        # ({filename, printDuration}) — without it every restart onto a printer still showing that print as complete
        # re-reports it under a NEW key (the key is built from monotonic time) and the cloud books it twice.
        self._state_path = state_path
        self._last_terminal: Optional[dict] = None
        self._seen_duration: Optional[float] = None
        # The print that was running when the state was last saved. One print must keep ONE key across a restart (the
        # cloud stamps hub_job_ref from it while printing and links the terminal by it): the first observation adopts it
        # when it is provably the same print, else drops it.
        self._restored: Optional[dict] = None
        self.unsaved = False   # the last save failed: retried on every observation
        self._load()
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
        self._seen_duration = print_duration
        total = _num(print_stats.get("total_duration"))
        # When this print started, on the wall clock (only meaningful while it runs: total_duration freezes at the end).
        print_start = _wall(now) - total if total is not None and state in ("printing", "paused") else None
        if self.unsaved:
            self._save()
        if self._restored is not None:
            self._resume_or_drop(state, filename, print_start, total, now)

        if self._active is None:
            # paused opens too: a print paused before its first poll is still this print (and needs its key)
            if state in _ACTIVE_STATES or state == "paused":
                self._open(filename, now, print_start)
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
                # Moonraker, unlike Bambu, says which: 'cancelled' is a person stopping it (the cloud's evidence-only
                # cancelled lane, member text "cancelled at the printer"); 'error' is a failure.
                self._close("cancelled" if state == "cancelled" else "failed", now)
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
                    self._open(filename, now, print_start)
                elif filename_changed:
                    # Filename swap without a duration reset — we missed
                    # the gap between two prints; close old as cancelled.
                    self._close("cancelled", now)
                    self._open(filename, now, print_start)
                elif filename and not self._active.get("name"):
                    self._active["name"] = filename
                    self._save()
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

    def active_key(self, filename: Optional[str] = None) -> Optional[str]:
        """The print running now (its jobKey), or None. With `filename`: only when the running print is that file."""
        if self._active is None or (filename is not None and self._active.get("name") != filename):
            return None
        return self._active["key"]

    def ack(self, job_keys: list[str]) -> None:
        keys = set(job_keys)
        before = len(self._pending)
        self._pending = [j for j in self._pending if j["jobKey"] not in keys]
        if len(self._pending) != before:
            self._save()

    # --- internals --------------------------------------------------------
    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        pending = raw.get("pending") if isinstance(raw.get("pending"), list) else []
        self._pending = [j for j in pending if isinstance(j, dict) and isinstance(j.get("jobKey"), str)][-MAX_PENDING_JOBS:]
        last = raw.get("lastTerminal")
        if isinstance(last, dict) and isinstance(last.get("filename"), str):
            self._last_terminal = {"filename": last["filename"], "printDuration": _num(last.get("printDuration"))}
        active = raw.get("active")
        if (isinstance(active, dict) and isinstance(active.get("key"), str) and isinstance(active.get("name"), str)
                and _num(active.get("startedWall")) is not None):
            self._restored = {"key": active["key"], "name": active["name"], "startedWall": _num(active["startedWall"]),
                              "printStart": _num(active.get("printStart"))}

    def _resume_or_drop(self, state: str, filename: Optional[str], print_start: Optional[float],
                        total: Optional[float], now: float) -> None:
        """First observation after a load: adopt the saved running print, or forget it."""
        saved, self._restored = self._restored, None
        if filename != saved["name"]:
            same = False
        elif state in ("printing", "paused"):
            same = (saved["printStart"] is not None and print_start is not None
                    and abs(saved["printStart"] - print_start) <= RESUME_START_SLACK_SEC)
        else:
            # It ended while the hub was down and the printer still shows it: close it under its own key (observe's normal
            # branch does, from this state). ponytail: a same-file re-print that ALSO finished while the hub was down is
            # booked under the saved key. Standby (reset, filename gone) is never adopted: its outcome is unknown, and the
            # queue's outcome_unknown_after_restart covers that, not an invented "cancelled".
            same = state in _TERMINAL_DONE_STATES.union(_TERMINAL_FAILED_STATES)
        if same:
            self._active = {"key": saved["key"], "name": saved["name"], "printStart": saved["printStart"],
                            "startedAt": now - (_wall(now) - saved["startedWall"])}
            if state not in ("printing", "paused") and saved["printStart"] is not None and total is not None:
                # it ended at its start + the frozen total_duration, not when the hub came back
                ended = now - (_wall(now) - (saved["printStart"] + total))
                if self._active["startedAt"] <= ended <= now:
                    self._active["endedAt"] = ended
            return
        log.info("klipper %s: %s is no longer the running print after the restart — its key is dropped", self.printer_id, saved["name"])
        self._save()

    def _save(self) -> None:
        if self._state_path is None:
            return
        running = None
        if self._active is not None and self._active.get("name"):
            running = {"key": self._active["key"], "name": self._active["name"],
                       "startedWall": _wall(self._active["startedAt"]), "printStart": self._active.get("printStart")}
        elif self._restored is not None:
            running = self._restored   # not observed yet since the load: keep it on disk
        state = json.dumps({"pending": self._pending, "lastTerminal": self._last_terminal, "active": running}, sort_keys=True)
        was_unsaved, self.unsaved = self.unsaved, not write_json_durably(self._state_path, state)
        if self.unsaved and not was_unsaved:
            # A terminal cannot be refused (the print already ended); loud once so the journal shows it. Retried every poll.
            log.error("klipper %s: terminal jobs not durable (RAM-only until a save succeeds)", self.printer_id)

    def _open(self, filename: Optional[str], now: float, print_start: Optional[float] = None) -> None:
        self._active = {
            "key": _job_key(self.printer_id, filename, now),
            "name": filename,
            "startedAt": now,
            "printStart": print_start,
        }
        self._save()   # a restart mid-print must find this key again

    def _close(self, status: str, now: float) -> None:
        if self._active is None:
            return
        now = self._active.get("endedAt", now)
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
            self._last_terminal = {"filename": self._active["name"], "printDuration": self._seen_duration}
        self._active = None   # before the save in _buffer: the closed print is no longer the running one
        self._buffer(job)

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
        status_out = "done" if state in _TERMINAL_DONE_STATES else ("cancelled" if state == "cancelled" else "failed")
        print_dur = _num(print_stats.get("print_duration"))
        last = self._last_terminal
        if last is not None and last["filename"] == filename and _same_duration(last["printDuration"], print_dur):
            log.info("klipper %s: %s was already reported before the restart — not again", self.printer_id, filename)
            return
        self._last_terminal = {"filename": filename, "printDuration": print_dur}
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
        self._save()


def _same_duration(a: Optional[float], b: Optional[float]) -> bool:
    """print_duration freezes when a print ends, so the same print reads the same after a restart (1 s of slack)."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1.0


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


def _wall(monotonic_ts: float) -> float:
    return time.time() - (time.monotonic() - monotonic_ts)


def _iso(monotonic_ts: float) -> str:
    """`time.monotonic` is not wall-clock — convert to ISO via wall time at
    *now* offset by the monotonic delta. Resolution is per-second since
    the cloud doesn't care about sub-second; same as the Bambu tracker."""
    wall = _wall(monotonic_ts)
    # 2026-06-16T22:30:00Z shape.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall))
