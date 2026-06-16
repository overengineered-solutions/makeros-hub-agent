"""PrinterManager — reconciles live printer adapters against the cloud's
config-down and gathers their normalized status for the heartbeat.

The cloud is the source of truth for WHICH printers exist + their connection
facts (operator adds them in /admin/3dprinting/hubs). The agent pulls that list
(GET /api/print/hub/config) whenever the heartbeat's `configVersion` changes,
then starts/stops/replaces adapters to match. A printer whose access code or IP
changed gets a fresh adapter (its fingerprint changed).

Imports the paho-backed BambuAdapter LAZILY so this module — and the heartbeat
loop — stay importable on a box where paho isn't installed yet.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..diagnostics import get_default, redact
from .threemf_objects import parse_plate_objects

log = logging.getLogger("makeros-hub.printers")

MAX_DISPATCHED_QUEUE_JOBS = 1000
MAX_DISPATCHED_COMMANDS = 1000
_SUBMISSION_UID_RE = re.compile(r"^[a-f0-9]{8,64}$")


def _fingerprint(p: dict) -> tuple:
    """What, if changed, means we must rebuild the adapter (new connection)."""
    return (p.get("vendor"), p.get("host"), p.get("serial"), p.get("accessCode"))


class PrinterManager:
    def __init__(self, diagnostics=None) -> None:
        self._adapters: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple] = {}
        self._diagnostics = diagnostics or get_default()
        # Static status for printers the agent can't drive yet (klipper, or a
        # Bambu missing its connection facts) — surfaced so the admin sees why.
        self._static: dict[str, dict] = {}
        # Unacked terminal jobs rescued from torn-down adapters (config change
        # rebuilds the adapter; its in-memory buffer must NOT die with it —
        # Codex review finding). Drained by pending_jobs / cleared by ack_jobs.
        self._orphan_jobs: list[dict] = []
        # Queue assignments are at-least-once from the cloud. Once this process
        # has successfully sent a start command for a queueJobId, a same-window
        # resend must not re-upload or re-start the printer. Terminal progress
        # reports prune this bounded guard.
        self._dispatched_queue_jobs: OrderedDict[str, float] = OrderedDict()
        # Same at-least-once guard for control commands. Cloud delivery is
        # one-shot (queued->delivered is atomic; delivered rows are never
        # re-selected), but a lost result report could in theory get the row
        # redelivered — a second ams_dry mode=1 would restart the heater cycle.
        # A successfully-dispatched requestId here makes a redelivery re-report
        # ok without republishing. Bounded like _dispatched_queue_jobs.
        self._dispatched_commands: OrderedDict[str, float] = OrderedDict()
        # Per-printer camera-routing facts (vendor/model/host/accessCode/urls),
        # rebuilt every reconcile. Lets the camera capturer pick the right source
        # (Bambu :6000 / HTTP snapshot / …) without re-plumbing config-down.
        self._camera_meta: dict[str, dict] = {}
        self.config_version: str | None = None

    def _record_failure(self, message, extra_secrets=None) -> None:
        if self._diagnostics is not None:
            self._diagnostics.record("printers", message, extra_secrets=extra_secrets)

    def _access_code_for(self, pid: str):
        fp = self._fingerprints.get(pid)
        if isinstance(fp, tuple) and len(fp) >= 4 and fp[3]:
            return fp[3]
        adapter = self._adapters.get(pid)
        return getattr(adapter, "_access_code", None)

    def _redact_printer_exception(self, exc, code=None) -> str:
        return redact(str(exc), extra_secrets=[code] if code else None)

    def reconcile(self, printers: list[dict], version: str | None) -> None:
        self.config_version = version
        desired_ids = {p["id"] for p in printers if isinstance(p.get("id"), str)}

        # Rebuild camera-routing meta from the full desired list (no stale rows).
        self._camera_meta = {
            p["id"]: {
                "printerId": p["id"],
                "vendor": p.get("vendor"),
                "model": p.get("model"),
                "host": p.get("host"),
                "accessCode": p.get("accessCode"),
                "moonrakerUrl": p.get("moonrakerUrl"),
                "cameraSnapshotUrl": p.get("cameraSnapshotUrl"),
                # Strict True only — a malformed/older payload sending a string
                # like "false" must NOT enable capture (preserve default-off).
                "cameraEnabled": p.get("cameraEnabled") is True,
                # V5 — AI failure-watch per-printer opt-in (cloud also gates on
                # the workspace feature). Same strict-True default-off semantics.
                "aiFailureWatchEnabled": p.get("aiFailureWatchEnabled") is True,
                "aiFailureSensitivity": (
                    str(p.get("aiFailureSensitivity") or "medium").lower()
                    if p.get("aiFailureSensitivity") in ("low", "medium", "high")
                    else "medium"
                ),
            }
            for p in printers
            if isinstance(p.get("id"), str)
        }

        # Drop adapters / static entries for printers no longer in config.
        for pid in list(self._adapters):
            if pid not in desired_ids:
                self._stop(pid)
        for pid in list(self._static):
            if pid not in desired_ids:
                self._static.pop(pid, None)

        for p in printers:
            pid = p.get("id")
            if not isinstance(pid, str):
                continue
            vendor = p.get("vendor")
            if vendor == "bambu":
                self._reconcile_bambu(pid, p)
            elif vendor == "klipper":
                self._reconcile_klipper(pid, p)
            else:
                # 'other' is the catch-all for non-Bambu non-Klipper printers
                # the operator may add (raw OctoPrint, Marlin host, etc.).
                # We don't drive these yet — surface clearly.
                self._static[pid] = {
                    "printerId": pid,
                    "connectionState": "error",
                    "errorReason": f"{vendor}_not_supported_yet",
                }

    def _reconcile_klipper(self, pid: str, p: dict) -> None:
        """Klipper printer: needs a Moonraker URL. Builds + starts a polling
        KlipperAdapter; no MQTT/paho dependency. Same fingerprint shape as
        Bambu so an admin edit (e.g. moved the printer to a new IP) triggers
        a clean restart."""
        moonraker_url = p.get("moonrakerUrl")
        if not moonraker_url:
            self._stop(pid)
            self._record_failure(f"klipper printer {pid} missing moonrakerUrl")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "incomplete_config",
            }
            return
        self._static.pop(pid, None)
        # Klipper fingerprint = (vendor, moonrakerUrl) — same shape contract
        # as Bambu's (vendor, host, serial, accessCode) so reconcile() can
        # treat them uniformly. Different from _fingerprint() so we compute
        # it inline here.
        fp = ("klipper", moonraker_url)
        if self._fingerprints.get(pid) == fp and pid in self._adapters:
            return
        self._stop(pid)
        try:
            from .klipper import KlipperAdapter
        except ImportError as e:
            log.error("cannot import klipper adapter for %s: %s", pid, e)
            self._record_failure(f"cannot import klipper adapter for {pid}: {e}")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_klipper_module_missing",
            }
            return
        adapter = KlipperAdapter(pid, moonraker_url=moonraker_url)
        try:
            adapter.start()
        except Exception as e:  # noqa: BLE001 - one bad printer can't sink config-down
            safe = redact(str(e))
            log.warning("cannot start klipper adapter for %s: %s", pid, safe)
            self._record_failure(f"cannot start klipper adapter for {pid}: {safe}")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_start_failed",
            }
            return
        self._adapters[pid] = adapter
        self._fingerprints[pid] = fp

    def _reconcile_bambu(self, pid: str, p: dict) -> None:
        host, serial, code = p.get("host"), p.get("serial"), p.get("accessCode")
        if not (host and serial and code):
            # Incomplete config — can't connect. Make it visible, don't crash.
            self._stop(pid)
            self._record_failure(f"printer {pid} incomplete_config")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "incomplete_config",
            }
            return
        self._static.pop(pid, None)
        fp = _fingerprint(p)
        if self._fingerprints.get(pid) == fp and pid in self._adapters:
            return  # unchanged — keep the live connection
        # New or changed connection facts → (re)build the adapter.
        self._stop(pid)
        try:
            from .bambu import BambuAdapter  # lazy: needs paho
        except ImportError as e:  # paho not installed
            safe = self._redact_printer_exception(e, code)
            log.error("cannot start Bambu adapter for %s — paho-mqtt missing: %s", pid, safe)
            self._record_failure(
                f"cannot start Bambu adapter for {pid}: paho-mqtt missing: {safe}",
                extra_secrets=[code],
            )
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_missing_paho",
            }
            return
        adapter = BambuAdapter(pid, host=host, serial=serial, access_code=code, model=p.get("model"))
        try:
            adapter.start()
        except Exception as e:  # noqa: BLE001 - one bad printer must not sink config-down
            safe = self._redact_printer_exception(e, code)
            log.warning("cannot start Bambu adapter for %s: %s", pid, safe)
            self._record_failure(f"cannot start Bambu adapter for {pid}: {safe}", extra_secrets=[code])
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_start_failed",
            }
            return
        self._adapters[pid] = adapter
        self._fingerprints[pid] = fp

    def _stop(self, pid: str) -> None:
        code = self._access_code_for(pid)
        adapter = self._adapters.pop(pid, None)
        self._fingerprints.pop(pid, None)
        if adapter is not None:
            # Rescue unacked terminal jobs BEFORE teardown — a config edit
            # (e.g. rotated access code) rebuilds the adapter and its buffer
            # would otherwise vanish with it.
            try:
                rescued = adapter.pending_jobs()
                if rescued:
                    self._orphan_jobs.extend(rescued)
                    log.info("rescued %d unacked job(s) from %s before teardown", len(rescued), pid)
            except Exception as e:  # noqa: BLE001
                safe = self._redact_printer_exception(e, code)
                log.warning("could not rescue pending jobs from %s: %s", pid, safe)
                self._record_failure(f"could not rescue pending jobs from {pid}: {safe}", extra_secrets=[code])
            adapter.stop()

    def statuses(self) -> list[dict]:
        out: list[dict] = []
        for pid, adapter in self._adapters.items():
            try:
                out.append(adapter.status())
            except Exception as e:  # noqa: BLE001 — one bad adapter must not sink the heartbeat
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("status read failed for %s: %s", pid, safe)
                self._record_failure(f"status read failed for {pid}: {safe}", extra_secrets=[code])
                out.append({"printerId": pid, "connectionState": "error", "errorReason": "agent_status_error"})
        out.extend(self._static.values())
        return out

    def camera_targets(self) -> list[dict]:
        """Per-printer camera-routing facts (vendor/model/host/accessCode/urls)
        for the camera capturer. One entry per configured printer; the capturer
        decides which have a usable camera source and which don't."""
        return list(self._camera_meta.values())

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs across all adapters + any rescued from
        torn-down adapters. Safe to send repeatedly — the cloud dedupes on
        jobKey."""
        out: list[dict] = list(self._orphan_jobs)
        for pid, adapter in self._adapters.items():
            try:
                out.extend(adapter.pending_jobs())
            except Exception as e:  # noqa: BLE001 — one adapter can't sink the loop
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("pending_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"pending_jobs failed for {pid}: {safe}", extra_secrets=[code])
        return out

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Fan a confirmed-send ack out to every adapter + the orphan buffer."""
        if not job_keys:
            return
        keys = set(job_keys)
        self._orphan_jobs = [j for j in self._orphan_jobs if j["jobKey"] not in keys]
        for pid, adapter in self._adapters.items():
            try:
                adapter.ack_jobs(job_keys)
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("ack_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"ack_jobs failed for {pid}: {safe}", extra_secrets=[code])

    def _remember_dispatched_queue_job(self, queue_job_id: str) -> None:
        self._dispatched_queue_jobs[queue_job_id] = time.monotonic()
        self._dispatched_queue_jobs.move_to_end(queue_job_id)
        while len(self._dispatched_queue_jobs) > MAX_DISPATCHED_QUEUE_JOBS:
            self._dispatched_queue_jobs.popitem(last=False)

    def _forget_dispatched_queue_job(self, queue_job_id: str) -> None:
        self._dispatched_queue_jobs.pop(queue_job_id, None)

    def _assignment_path_ok(self, submission_uid: str, file_name: str) -> bool:
        return (
            bool(_SUBMISSION_UID_RE.fullmatch(submission_uid))
            and file_name == os.path.basename(file_name)
            and file_name not in (".", "..")
            and "/" not in file_name
            and "\\" not in file_name
        )

    def dispatch_commands(self, pending_commands) -> list[dict]:
        """Execute cloud-delivered control commands (pause/resume/stop) against
        the target adapters and return result reports for the next heartbeat.
        Mirrors dispatch_assignments: look the adapter up by printerId, run the
        command, map ok/failure to a {requestId, command, status, detail} report
        the cloud's ingestPrinterCommandResults consumes (status 'ok' -> done,
        anything else -> failed with the detail)."""
        # R6.7 defense-in-depth: the cloud caps delivery at 5/heartbeat, so a
        # larger burst means a buggy or compromised control plane. Process the
        # first N and fail the rest as rate_limited so the cloud still closes
        # them out (rather than the agent blindly publishing an unbounded flood
        # of MQTT control messages).
        max_per_heartbeat = 16
        reports: list[dict] = []
        items = pending_commands if isinstance(pending_commands, list) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            request_id = item.get("requestId")
            printer_id = item.get("printerId")
            command = item.get("command")
            if not all(isinstance(v, str) and v for v in (request_id, printer_id, command)):
                # The cloud only ever delivers validated rows, so a malformed
                # item has no real command row to close — log it (not silent) and
                # drop, since we can't emit a valid-command terminal result for it.
                log.warning("skipping malformed command: %s", item)
                continue
            if request_id in self._dispatched_commands:
                # Already executed this requestId in a prior beat — do NOT
                # republish (a duplicate ams_dry mode=1 would restart the dryer).
                # Re-report ok so the cloud can close out a row whose first
                # result report was lost. Doesn't consume a rate-limit slot.
                reports.append({"requestId": request_id, "command": command, "status": "ok"})
                continue
            if index >= max_per_heartbeat:
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "rate_limited",
                    }
                )
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else None
            adapter = self._adapters.get(printer_id)
            send = getattr(adapter, "send_command", None) if adapter is not None else None
            if not callable(send):
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "printer_unavailable",
                    }
                )
                continue
            try:
                result = send(command, params)
            except Exception as exc:  # noqa: BLE001
                log.warning("command %s for %s raised: %s", command, printer_id, exc)
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "exception",
                    }
                )
                continue
            if isinstance(result, dict) and result.get("ok"):
                # Record only successful dispatches so a failed one (e.g. a
                # transient printer_unavailable) can still be retried if the cloud
                # ever redelivers; bound it like the queue-job guard.
                self._dispatched_commands[request_id] = time.monotonic()
                self._dispatched_commands.move_to_end(request_id)
                while len(self._dispatched_commands) > MAX_DISPATCHED_COMMANDS:
                    self._dispatched_commands.popitem(last=False)
                reports.append({"requestId": request_id, "command": command, "status": "ok"})
            else:
                reason = result.get("reason") if isinstance(result, dict) else "unknown"
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": str(reason),
                    }
                )
        return reports

    def dispatch_assignments(self, assignments, spool_dir) -> list[dict]:
        """Start cloud-assigned queue jobs and return queue-status reports.

        Reports are ordered for the cloud transition map: assigned -> uploading,
        or uploading -> held after a real send failure. The transition to
        printing is observed later from printer telemetry.
        """
        reports: list[dict] = []
        base = Path(spool_dir)
        for assignment in assignments if isinstance(assignments, list) else []:
            if not isinstance(assignment, dict):
                continue
            queue_job_id = assignment.get("queueJobId")
            printer_id = assignment.get("printerId")
            submission_uid = assignment.get("submissionUid")
            file_name = assignment.get("fileName")
            if not all(isinstance(v, str) and v for v in (queue_job_id, printer_id, submission_uid, file_name)):
                log.warning("skipping malformed assignment: %s", assignment)
                continue
            if queue_job_id in self._dispatched_queue_jobs:
                continue
            if not self._assignment_path_ok(submission_uid, file_name):
                reports.append(
                    {
                        "queueJobId": queue_job_id,
                        "state": "held",
                        "reason": "bad_assignment",
                    }
                )
                continue

            adapter = self._adapters.get(printer_id)
            start_print = getattr(adapter, "start_print", None) if adapter is not None else None
            if not callable(start_print):
                reports.append(
                    {
                        "queueJobId": queue_job_id,
                        "state": "held",
                        "reason": "printer_unavailable",
                    }
                )
                continue

            local_path = base / submission_uid / file_name
            if not local_path.is_file():
                reports.append(
                    {
                        "queueJobId": queue_job_id,
                        "state": "held",
                        "reason": "file_not_found",
                    }
                )
                continue

            plate = assignment.get("plate") or 1
            plate_int = plate if isinstance(plate, int) and not isinstance(plate, bool) else 1
            # Enumerate the plate's skippable objects from the staged 3MF so the
            # cloud can offer per-object cancel. Best-effort: a parse miss just
            # omits the list (skip simply isn't offered for that job).
            uploading: dict = {"queueJobId": queue_job_id, "state": "uploading"}
            objects = parse_plate_objects(local_path, plate_int)
            if objects:
                uploading["objects"] = objects
            reports.append(uploading)
            try:
                result = start_print(
                    local_path,
                    file_name,
                    plate=plate_int,
                    use_ams=bool(assignment.get("useAms", False)),
                    ams_mapping=assignment.get("amsMapping"),
                    queue_job_id=queue_job_id,
                )
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(printer_id)
                safe = self._redact_printer_exception(e, code)
                log.warning("assignment dispatch failed for %s on %s: %s", queue_job_id, printer_id, safe)
                self._record_failure(
                    f"assignment dispatch failed for {queue_job_id} on {printer_id}: {safe}",
                    extra_secrets=[code],
                )
                result = {"ok": False, "reason": "start_failed"}
            if not isinstance(result, dict):
                result = {"ok": False, "reason": "start_failed"}

            if result.get("ok"):
                self._remember_dispatched_queue_job(queue_job_id)
            else:
                reports.append(
                    {
                        "queueJobId": queue_job_id,
                        "state": "held",
                        "reason": result.get("reason", "start_failed"),
                    }
                )
        return reports

    def collect_queue_progress(self) -> list[dict]:
        """Drain queue-status updates observed by printer adapters."""
        reports: list[dict] = []
        for pid, adapter in self._adapters.items():
            collect_progress = getattr(adapter, "collect_queue_progress", None)
            if not callable(collect_progress):
                continue
            try:
                for report in collect_progress():
                    reports.append(report)
                    if report.get("state") in ("completed", "held") and isinstance(
                        report.get("queueJobId"), str
                    ):
                        self._forget_dispatched_queue_job(report["queueJobId"])
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("collect_queue_progress failed for %s: %s", pid, safe)
                self._record_failure(f"collect_queue_progress failed for {pid}: {safe}", extra_secrets=[code])
        return reports

    def stop_all(self) -> None:
        for pid in list(self._adapters):
            self._stop(pid)
