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

log = logging.getLogger("makeros-hub.printers")

MAX_DISPATCHED_QUEUE_JOBS = 1000
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
                "cameraEnabled": bool(p.get("cameraEnabled")),
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
            else:
                # Klipper/other adapters land in a later slice.
                self._static[pid] = {
                    "printerId": pid,
                    "connectionState": "error",
                    "errorReason": f"{vendor}_not_supported_yet",
                }

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

            reports.append({"queueJobId": queue_job_id, "state": "uploading"})
            try:
                result = start_print(
                    local_path,
                    file_name,
                    plate=assignment.get("plate") or 1,
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
