"""The heartbeat loop. Once enrolled, the agent reports liveness + per-printer
status to the cloud every ~heartbeat_sec using its per-hub bearer credential.

Printer flow (PR 5):
  - The cloud is the source of truth for which printers this hub drives. The
    operator adds them in /admin/3dprinting/hubs.
  - On start, and whenever the heartbeat response's `configVersion` changes, the
    agent pulls GET /api/print/hub/config (the printer list + access codes) and
    reconciles its live MQTT adapters to match.
  - Each heartbeat carries normalized per-printer status (connection + activity
    state, progress, temps) — telemetry ONLY, never the access code/serial/IP.

The cloud dictates the next poll cadence in the response, so cadence changes
need no agent redeploy. systemd `Restart=always` is the self-healing primitive.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import signal
import shutil
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import SPOOL_DIR, Config, parse_virtual_printer_config, read_credential
from .vprinter.live_pool import updated_config_if_pool_changed
from .diagnostics import Diagnostics, collect_cheap_diagnostics, install_log_handler, redact, set_default
from .http import TransportError, get_json, post_json
from .ingest import IngestServer
from .probes import PROBES, run_probe, set_effective_config
from .printers.manager import PrinterManager
from .printers.camera import CameraScheduler, collect_camera_frames
from .tailscale import current_tailscale_status, reconcile_tailscale, tailscale_binary_exists
from .update import maybe_update
from .vprinter.capture import CapturedJob, build_vp_submit_body
from .vprinter.manager import VirtualPrinterManager
from .vprinter.outbox import VPrinterOutbox, validate_submission_uid

log = logging.getLogger("makeros-hub")

QUEUE_STATUS_SENT = "sent"
QUEUE_STATUS_RETRY = "retry"
QUEUE_STATUS_DROP = "drop"
MAX_PROBES_PER_HEARTBEAT = 3
VP_SUBMISSION_QUEUE_MAX = 256
VP_SUBMISSIONS_PER_HEARTBEAT = 16
VP_SUBMISSION_MAX_ATTEMPTS = 10
# Job-INTRINSIC rejections only: the SAME job will never succeed on retry, so
# drop it immediately instead of head-of-line-blocking the queue. Auth/route/
# conflict errors (401/403/404/409) are CONTEXT failures that can recover (e.g. a
# rotated hub credential or a redeploy), so they retry under the attempt cap
# rather than instantly losing the member's captured job.
VP_SUBMIT_DEADLETTER_STATUSES = {400, 413, 422}
# Per-submit timeout + a wall-clock budget for the whole per-heartbeat drain, so
# vp-submit (which runs on the heartbeat thread) can never stall the heartbeat
# enough to flap the hub offline. Healthy submits are sub-second.
VP_SUBMIT_TIMEOUT = 8
VP_DRAIN_BUDGET_SEC = 8
VPRINTER_RETRY_INITIAL_SEC = 30
VPRINTER_RETRY_MAX_SEC = 300
VP_CAPTURE_RECENT_UIDS_MAX = 64


def make_cloud_submit(cfg: Config, credential: str, diagnostics=None):
    """Closure the ingest server calls to register an OrcaSlicer upload with the
    cloud. Maps the /api/print/hub/submit response to an ingest outcome dict."""

    def submit(*, member_token, submission_uid, file_name, file_sha256, file_size, print_now):
        try:
            resp = post_json(
                cfg.submit_url,
                {
                    "memberToken": member_token,
                    "hubSubmissionUid": submission_uid,
                    "fileName": file_name,
                    "fileSha256": file_sha256,
                    "fileSizeBytes": file_size,
                },
                bearer=credential,
                retries=2,
                backoff_base=1.0,
            )
        except TransportError as exc:
            safe = redact(str(exc))
            _record_diagnostic(diagnostics, "ingest", f"cloud submit transport: {safe}")
            return {"status": "error", "detail": f"transport: {safe}"}

        if resp.status == 200:
            if resp.body.get("ok"):
                return {"status": "queued", "jobId": resp.body.get("jobId")}
            return {"status": "rejected", "reason": resp.body.get("reason", "rejected")}
        if resp.status == 401:
            # invalid member token (the hub's own bearer is valid — we're enrolled).
            return {"status": "bad_token"}
        _record_diagnostic(diagnostics, "ingest", f"cloud submit unexpected {resp.status}")
        return {"status": "error", "detail": f"http_{resp.status}"}

    return submit


def make_queue_status_reporter(cfg: Config, credential: str, diagnostics=None):
    """Closure for queue assignment state transitions."""

    def report(status_report: dict) -> str:
        body = {
            "queueJobId": status_report.get("queueJobId"),
            "state": status_report.get("state"),
        }
        if status_report.get("printerJobKey"):
            body["printerJobKey"] = status_report["printerJobKey"]
        if status_report.get("reason"):
            body["reason"] = status_report["reason"]
        if status_report.get("objects"):
            body["objects"] = status_report["objects"]
        try:
            resp = post_json(
                cfg.queue_status_url,
                body,
                bearer=credential,
                retries=2,
                backoff_base=1.0,
            )
        except TransportError as exc:
            safe = redact(str(exc))
            _record_diagnostic(diagnostics, "heartbeat", f"queue status transport: {safe}")
            log.warning(
                "queue status report failed for %s/%s: %s",
                body.get("queueJobId"),
                body.get("state"),
                safe,
            )
            return QUEUE_STATUS_RETRY
        if 200 <= resp.status < 300:
            return QUEUE_STATUS_SENT
        if 400 <= resp.status < 500:
            safe = redact(resp.body.get("error"))
            _record_diagnostic(diagnostics, "heartbeat", f"queue status HTTP {resp.status}: {safe}")
            log.warning(
                "dropping deterministic queue status report after HTTP %s for %s/%s: %s",
                resp.status,
                body.get("queueJobId"),
                body.get("state"),
                safe,
            )
            return QUEUE_STATUS_DROP
        safe = redact(resp.body.get("error"))
        _record_diagnostic(diagnostics, "heartbeat", f"queue status unexpected {resp.status}: {safe}")
        log.warning(
            "queue status report unexpected %s for %s/%s: %s",
            resp.status,
            body.get("queueJobId"),
            body.get("state"),
            safe,
        )
        return QUEUE_STATUS_RETRY

    return report


def _flush_queue_status_reports(reporter, reports: list[dict]) -> list[dict]:
    """POST in order; retry transport/5xx, drop deterministic 4xx."""
    for idx, report in enumerate(reports):
        result = reporter(report)
        if result in (True, QUEUE_STATUS_SENT):
            continue
        if result == QUEUE_STATUS_DROP:
            continue
        if result is False:
            result = QUEUE_STATUS_RETRY
        if result == QUEUE_STATUS_RETRY:
            return reports[idx:]
        return reports[idx:]
    return []


def _enqueue_vprinter_submission(submission_queue: queue.Queue[CapturedJob], job: CapturedJob) -> None:
    try:
        submission_queue.put_nowait(job)
        return
    except queue.Full:
        dropped = getattr(submission_queue, "_makeros_dropped", 0) + 1
        setattr(submission_queue, "_makeros_dropped", dropped)
        try:
            submission_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            log.warning("vprinter.submit.queue_full dropped_oldest_count=%d", dropped)
    try:
        submission_queue.put_nowait(job)
    except queue.Full:
        dropped = getattr(submission_queue, "_makeros_dropped", 0) + 1
        setattr(submission_queue, "_makeros_dropped", dropped)
        log.warning("vprinter.submit.queue_full dropped_newest_count=%d", dropped)


def _stage_vprinter_spool_file(job: CapturedJob, spool_dir: Path | str = SPOOL_DIR) -> bool:
    uid = validate_submission_uid(job.submission_uid)
    filename = _validate_spool_filename(job.filename)
    dest_dir = Path(spool_dir) / uid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    if dest_path.exists():
        return False

    tmp_path = dest_dir / f".{filename}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
    try:
        with Path(job.file_path).open("rb") as source, tmp_path.open("wb") as tmp:
            shutil.copyfileobj(source, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        if dest_path.exists():
            tmp_path.unlink(missing_ok=True)
            return False
        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


def _validate_spool_filename(filename: str) -> str:
    value = str(filename)
    if (
        not value
        or value in (".", "..")
        or value != os.path.basename(value)
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"invalid virtual printer spool filename: {value!r}")
    return value


def _remember_recent_vprinter_uid(
    recent_uids: OrderedDict[str, None],
    submission_uid: str,
    *,
    limit: int = VP_CAPTURE_RECENT_UIDS_MAX,
) -> None:
    recent_uids[submission_uid] = None
    recent_uids.move_to_end(submission_uid)
    while len(recent_uids) > max(1, int(limit)):
        recent_uids.popitem(last=False)


def _make_vprinter_capture_handler(
    submission_queue: queue.Queue[CapturedJob],
    outbox: VPrinterOutbox | None = None,
    *,
    spool_dir: Path | str | None = None,
):
    stage_spool_dir = SPOOL_DIR if spool_dir is None else Path(spool_dir)
    recent_uids: OrderedDict[str, None] = OrderedDict()

    def on_capture(job: CapturedJob) -> None:
        if outbox is not None:
            # Durability is best-effort: a persist failure (transient IO / full
            # disk) must NEVER drop the job — fall back to the in-memory queue so
            # it still submits this session. The retry path re-persists later.
            try:
                outbox.persist(job)
            except Exception as exc:  # noqa: BLE001 - never lose a captured job on a durability write
                log.warning(
                    "vprinter.outbox.persist_failed submission_uid=%s: %s",
                    job.submission_uid,
                    redact(str(exc)),
                )
        if job.submission_uid in recent_uids:
            log.info("vprinter.capture.duplicate uid=%s", job.submission_uid)
        _remember_recent_vprinter_uid(recent_uids, job.submission_uid)
        try:
            staged = _stage_vprinter_spool_file(job, stage_spool_dir)
        except Exception as exc:  # noqa: BLE001 - staging is best-effort; never drop captures
            log.warning(
                "vprinter.spool.stage_failed uid=%s file=%s: %s",
                job.submission_uid,
                job.filename,
                redact(str(exc)),
            )
        else:
            if staged:
                log.info("vprinter.spool.staged uid=%s file=%s", job.submission_uid, job.filename)
        log.info(
            "vprinter.capture_observed member_id=%s filename=%s size=%d sha256=%s "
            "use_ams=%s ams_mapping=%s required_filaments=%s submitted_at=%s",
            job.member_id,
            job.filename,
            job.size,
            job.file_sha256,
            job.use_ams,
            json.dumps(job.ams_mapping, sort_keys=True, default=str),
            json.dumps(job.required_filaments, sort_keys=True),
            job.submitted_at.isoformat(),
        )
        _enqueue_vprinter_submission(submission_queue, job)

    return on_capture


def _rehydrate_vprinter_submissions(
    submission_queue: queue.Queue[CapturedJob],
    outbox: VPrinterOutbox,
) -> int:
    count = 0
    for job in outbox.load_all():
        _enqueue_vprinter_submission(submission_queue, job)
        count += 1
    if count:
        log.info("vprinter.outbox.rehydrated count=%d", count)
    return count


def _drop_vprinter_submissions(
    submission_queue: queue.Queue[CapturedJob],
    *,
    max_jobs: int | None = None,
) -> int:
    dropped = 0
    limit = None if max_jobs is None else max(0, int(max_jobs))
    while limit is None or dropped < limit:
        try:
            submission_queue.get_nowait()
        except queue.Empty:
            break
        dropped += 1
    return dropped


def _deadletter_vprinter_submission(job: CapturedJob, reason: str, diagnostics=None) -> None:
    safe = redact(reason)
    _record_diagnostic(diagnostics, "vprinter", f"vp-submit deadletter: {safe}")
    log.warning(
        "vprinter.submit.deadletter member_id=%s filename=%s submission_uid=%s attempts=%d reason=%s",
        job.member_id,
        job.filename,
        job.submission_uid,
        job.attempts,
        safe,
    )


def _retry_or_deadletter_vprinter_submission(
    submission_queue: queue.Queue[CapturedJob],
    job: CapturedJob,
    reason: str,
    *,
    diagnostics=None,
    outbox: VPrinterOutbox | None = None,
    max_attempts: int = VP_SUBMISSION_MAX_ATTEMPTS,
) -> None:
    attempts = job.attempts + 1
    if attempts >= max(1, int(max_attempts)):
        terminal_job = replace(job, attempts=attempts)
        _deadletter_vprinter_submission(terminal_job, reason, diagnostics)
        if outbox is not None:
            outbox.remove(terminal_job.submission_uid)
        return
    retried_job = replace(job, attempts=attempts)
    if outbox is not None:
        outbox.persist(retried_job)
    _enqueue_vprinter_submission(submission_queue, retried_job)


def _drain_vprinter_submissions(
    submission_queue: queue.Queue[CapturedJob],
    cfg: Config,
    credential: str,
    *,
    model: str | None,
    diagnostics=None,
    outbox: VPrinterOutbox | None = None,
    poster=post_json,
    max_jobs: int = VP_SUBMISSIONS_PER_HEARTBEAT,
) -> int:
    if not model:
        dropped = _drop_vprinter_submissions(submission_queue)
        if dropped:
            log.warning("vprinter.submit.dropped_unconfigured count=%d", dropped)
        return 0

    submitted = 0
    start = time.monotonic()
    to_process = min(max(0, int(max_jobs)), submission_queue.qsize())
    for _ in range(to_process):
        # Bound the per-heartbeat drain so a slow/erroring cloud can't stall the
        # heartbeat loop into looking offline — the backlog drains over the next
        # beats instead. (Healthy submits are sub-second, so this never trips
        # under a responsive cloud.)
        if time.monotonic() - start > VP_DRAIN_BUDGET_SEC:
            break
        try:
            job = submission_queue.get_nowait()
        except queue.Empty:
            break
        body = build_vp_submit_body(job, model=model)
        try:
            resp = poster(cfg.vp_submit_url, body, bearer=credential, timeout=VP_SUBMIT_TIMEOUT)
        except TransportError as exc:
            safe = redact(str(exc))
            _record_diagnostic(diagnostics, "vprinter", f"vp-submit transport: {safe}")
            log.warning(
                "vprinter.submit.retry transport member_id=%s filename=%s submission_uid=%s: %s",
                job.member_id,
                job.filename,
                job.submission_uid,
                safe,
            )
            _retry_or_deadletter_vprinter_submission(
                submission_queue,
                job,
                f"transport: {safe}",
                diagnostics=diagnostics,
                outbox=outbox,
            )
            continue

        if resp.status == 200 and resp.body.get("ok") is True:
            submitted += 1
            log.info(
                "vprinter.submit.ok member_id=%s filename=%s submission_uid=%s jobId=%s",
                job.member_id,
                job.filename,
                job.submission_uid,
                resp.body.get("jobId"),
            )
            if outbox is not None:
                outbox.remove(job.submission_uid)
            continue

        if resp.status == 200 and resp.body.get("ok") is False:
            reason = redact(resp.body.get("reason") or "rejected")
            log.warning(
                "vprinter.submit.rejected member_id=%s filename=%s submission_uid=%s reason=%s",
                job.member_id,
                job.filename,
                job.submission_uid,
                reason,
            )
            if outbox is not None:
                outbox.remove(job.submission_uid)
            continue

        safe = redact(resp.body.get("error") or resp.body.get("reason") or resp.body)
        _record_diagnostic(diagnostics, "vprinter", f"vp-submit HTTP {resp.status}: {safe}")
        if resp.status in VP_SUBMIT_DEADLETTER_STATUSES:
            _deadletter_vprinter_submission(job, f"HTTP {resp.status}: {safe}", diagnostics)
            if outbox is not None:
                outbox.remove(job.submission_uid)
            continue
        if resp.status in (401, 403):
            # Hub-level auth OUTAGE (revoked/rotated credential), NOT a job failure:
            # re-enqueue WITHOUT counting toward the dead-letter cap and WITHOUT
            # removing the durable outbox record, so captured jobs survive the
            # outage until the credential is restored (re-enroll / rotation).
            _record_diagnostic(diagnostics, "vprinter", f"vp-submit auth outage HTTP {resp.status}")
            log.warning(
                "vprinter.submit.auth_outage status=%s member_id=%s filename=%s submission_uid=%s",
                resp.status,
                job.member_id,
                job.filename,
                job.submission_uid,
            )
            _enqueue_vprinter_submission(submission_queue, job)
            continue
        log.warning(
            "vprinter.submit.retry http_status=%s member_id=%s filename=%s submission_uid=%s: %s",
            resp.status,
            job.member_id,
            job.filename,
            job.submission_uid,
            safe,
        )
        _retry_or_deadletter_vprinter_submission(
            submission_queue,
            job,
            f"HTTP {resp.status}: {safe}",
            diagnostics=diagnostics,
            outbox=outbox,
        )

    return submitted


def _uptime_sec() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


TAILSCALE_HEARTBEAT_FIELDS = (
    "tailscaleIp",
    "tailscaleHostname",
    "tailscaleStatus",
    "tailscaleStatusReason",
)

TAILSCALE_RETRY_INITIAL_SEC = 30
TAILSCALE_RETRY_MAX_SEC = 300


def _record_diagnostic(diagnostics, subsystem: str, message) -> None:
    if diagnostics is not None:
        diagnostics.record(subsystem, message)


def heartbeat_payload(
    printers: list[dict] | None = None,
    jobs: list[dict] | None = None,
    tailscale_status: dict | None = None,
    probe_results: list[dict] | None = None,
    command_results: list[dict] | None = None,
    diagnostics=None,
    camera_frames: list[dict] | None = None,
    camera_failures: list[str] | None = None,
) -> dict:
    payload = {
        "agentVersion": __version__,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
        "uptimeSec": _uptime_sec(),
        "printers": printers or [],
        # Terminal jobs observed since the last confirmed send — the cloud
        # ingests them into print_jobs (observe-only until a printer is
        # billing-authoritative) and dedupes on jobKey, so re-sends are safe.
        "jobs": jobs or [],
    }
    if isinstance(tailscale_status, dict):
        for key in TAILSCALE_HEARTBEAT_FIELDS:
            value = tailscale_status.get(key)
            if value not in (None, ""):
                payload[key] = value
    if probe_results:
        payload["probeResults"] = list(probe_results)
    if command_results:
        payload["commandResults"] = list(command_results)
    if camera_frames:
        payload["cameraFrames"] = list(camera_frames)
    if camera_failures:
        payload["cameraFailures"] = list(camera_failures)
    diag = collect_cheap_diagnostics(diagnostics)
    if diag:
        payload["diagnostics"] = diag
    return payload


def _run_pending_probes(
    pending_probes,
    *,
    runner=run_probe,
    max_probes: int = MAX_PROBES_PER_HEARTBEAT,
    diagnostics=None,
) -> list[dict]:
    if not isinstance(pending_probes, list):
        return []
    results: list[dict] = []
    for pending in pending_probes:
        if len(results) >= max(0, int(max_probes)):
            break
        if not isinstance(pending, dict):
            continue
        name = pending.get("name")
        if not isinstance(name, str) or name not in PROBES:
            continue
        try:
            result = runner(name)
            if not isinstance(result, dict):
                raise RuntimeError("probe runner returned non-object")
        except Exception as exc:  # noqa: BLE001 - one probe must not sink heartbeat
            safe = redact(str(exc))
            _record_diagnostic(diagnostics, "heartbeat", f"probe {name} failed: {safe}")
            result = {
                "name": name,
                "status": "error",
                "exitCode": None,
                "error": safe,
                "output": "",
                "durationMs": 0,
                "truncated": False,
            }
        item = {"requestId": pending.get("requestId"), "name": name}
        item.update(result)
        results.append(item)
    return results


def _tailscale_config_enabled(tailscale_cfg) -> bool:
    return bool(tailscale_cfg.get("enabled")) if isinstance(tailscale_cfg, dict) else False


def _tailscale_connected(status: dict | None) -> bool:
    return isinstance(status, dict) and status.get("tailscaleStatus") == "connected"


def _tailscale_secret(tailscale_cfg) -> str | None:
    if not isinstance(tailscale_cfg, dict):
        return None
    auth_key = tailscale_cfg.get("authKey")
    return auth_key if isinstance(auth_key, str) and auth_key else None


def _redact_secret(value: str | None, secret: str | None) -> str | None:
    if value is None:
        return None
    return redact(str(value), extra_secrets=[secret] if secret else None)


def _sanitize_tailscale_status(status, tailscale_cfg) -> dict:
    if not isinstance(status, dict):
        return {
            "tailscaleIp": None,
            "tailscaleHostname": None,
            "tailscaleStatus": "error",
            "tailscaleStatusReason": "tailscale reconcile failed",
        }
    clean = dict(status)
    if clean.get("tailscaleStatusReason"):
        clean["tailscaleStatusReason"] = _redact_secret(
            clean.get("tailscaleStatusReason"),
            _tailscale_secret(tailscale_cfg),
        )
    return clean


class _TailscaleRuntimeState:
    def __init__(self):
        self.config: dict | None = None
        self.status: dict | None = None
        self.reconcile_status_pending = False
        self.next_retry_at = 0.0
        self.retry_delay_sec = TAILSCALE_RETRY_INITIAL_SEC

    def remember_config(self, tailscale_cfg) -> None:
        self.config = dict(tailscale_cfg) if isinstance(tailscale_cfg, dict) else None

    def needs_retry(self) -> bool:
        return _tailscale_config_enabled(self.config) and not _tailscale_connected(self.status)

    def retry_due(self, now: float) -> bool:
        if not self.needs_retry():
            return False
        return self.next_retry_at <= 0 or now >= self.next_retry_at

    def record_reconcile_status(self, status: dict | None, now: float) -> None:
        self.status = status if isinstance(status, dict) else None
        self.reconcile_status_pending = True
        if self.needs_retry():
            self.next_retry_at = now + self.retry_delay_sec
            self.retry_delay_sec = min(self.retry_delay_sec * 2, TAILSCALE_RETRY_MAX_SEC)
        else:
            self.next_retry_at = 0.0
            self.retry_delay_sec = TAILSCALE_RETRY_INITIAL_SEC

    def record_observed_status(self, status: dict | None, now: float) -> None:
        self.status = status if isinstance(status, dict) else None
        if self.needs_retry() and self.next_retry_at <= 0:
            self.next_retry_at = now + self.retry_delay_sec
        elif not self.needs_retry():
            self.next_retry_at = 0.0
            self.retry_delay_sec = TAILSCALE_RETRY_INITIAL_SEC

    def mark_reported(self) -> None:
        self.reconcile_status_pending = False

    def should_read_status(self) -> bool:
        if self.reconcile_status_pending:
            return False
        return _tailscale_config_enabled(self.config) or tailscale_binary_exists()


class _VirtualPrinterRuntimeState:
    def __init__(self):
        self.config = None
        self.next_retry_at = 0.0
        self.retry_delay_sec = VPRINTER_RETRY_INITIAL_SEC

    def remember_config(self, vp_config) -> None:
        # Only remember a config we actually want RUNNING. A disabled VP (or no
        # config block) must not arm the retry timer — needs_retry() keys on
        # "current_model is None", so a non-None *disabled* config would otherwise
        # re-reconcile a deliberately-stopped VP every heartbeat.
        wanted = vp_config if (vp_config is not None and getattr(vp_config, "enabled", False)) else None
        self.config = wanted
        if wanted is None:
            self.next_retry_at = 0.0
            self.retry_delay_sec = VPRINTER_RETRY_INITIAL_SEC

    def needs_retry(self, current_model: str | None) -> bool:
        return self.config is not None and current_model is None

    def retry_due(self, now: float, current_model: str | None) -> bool:
        if not self.needs_retry(current_model):
            return False
        return self.next_retry_at <= 0 or now >= self.next_retry_at

    def record_reconcile_attempt(self, current_model: str | None, now: float) -> None:
        if self.needs_retry(current_model):
            self.next_retry_at = now + self.retry_delay_sec
            self.retry_delay_sec = min(self.retry_delay_sec * 2, VPRINTER_RETRY_MAX_SEC)
        else:
            self.next_retry_at = 0.0
            self.retry_delay_sec = VPRINTER_RETRY_INITIAL_SEC


def _reconcile_tailscale_config(tailscale_cfg, reconciler=reconcile_tailscale, diagnostics=None) -> dict:
    try:
        status = reconciler(tailscale_cfg)
    except Exception:  # noqa: BLE001 - config-down must not break heartbeat
        status = {
            "tailscaleIp": None,
            "tailscaleHostname": None,
            "tailscaleStatus": "error",
            "tailscaleStatusReason": "tailscale reconcile failed",
        }
        _record_diagnostic(diagnostics, "tailscale", status["tailscaleStatusReason"])
    else:
        status = _sanitize_tailscale_status(status, tailscale_cfg)
    if status.get("tailscaleStatus") == "error":
        reason = status.get("tailscaleStatusReason") or "unknown"
        _record_diagnostic(diagnostics, "tailscale", reason)
        log.error("tailscale.error: %s", reason)
    return status


def _maybe_retry_tailscale_config(
    tailscale_state: _TailscaleRuntimeState,
    now: float,
    reconciler=reconcile_tailscale,
    diagnostics=None,
) -> dict | None:
    if not tailscale_state.retry_due(now):
        return None
    status = _reconcile_tailscale_config(tailscale_state.config, reconciler, diagnostics)
    tailscale_state.record_reconcile_status(status, now)
    return status


def _reconcile_vprinter_config(
    vp_manager: VirtualPrinterManager,
    vp_state: _VirtualPrinterRuntimeState,
    vp_config,
    *,
    now: float,
    diagnostics=None,
) -> None:
    vp_state.remember_config(vp_config)
    try:
        vp_manager.reconcile_sync(vp_config)
    except Exception as exc:  # noqa: BLE001 - config-down must not sink heartbeat
        safe = redact(str(exc))
        _record_diagnostic(diagnostics, "vprinter", f"virtual printer reconcile failed: {safe}")
        log.warning("virtual printer reconcile failed: %s", safe)
    finally:
        vp_state.record_reconcile_attempt(vp_manager.current_model(), now)


def _maybe_retry_vprinter_config(
    vp_state: _VirtualPrinterRuntimeState,
    vp_manager: VirtualPrinterManager,
    now: float,
    diagnostics=None,
) -> bool:
    if not vp_state.retry_due(now, vp_manager.current_model()):
        return False
    _reconcile_vprinter_config(
        vp_manager,
        vp_state,
        vp_state.config,
        now=now,
        diagnostics=diagnostics,
    )
    return True


def _pull_config(
    cfg: Config,
    credential: str,
    manager: PrinterManager,
    tailscale_reconciler=reconcile_tailscale,
    tailscale_state: _TailscaleRuntimeState | None = None,
    virtual_printer_manager: VirtualPrinterManager | None = None,
    virtual_printer_state: _VirtualPrinterRuntimeState | None = None,
    diagnostics=None,
) -> dict | None:
    """Fetch the printer list (config-down) and reconcile adapters. Best-effort:
    a transport blip just leaves the current adapters running until next time."""
    try:
        resp = get_json(cfg.config_url, bearer=credential)
    except TransportError as exc:
        safe = redact(str(exc))
        _record_diagnostic(diagnostics, "config", f"config pull failed: {safe}")
        log.warning("config pull failed (will retry on next change): %s", safe)
        return None
    if resp.status == 200:
        printers = resp.body.get("printers")
        version = resp.body.get("version")
        manager.reconcile(printers if isinstance(printers, list) else [], version)
        vp_config = parse_virtual_printer_config(_virtual_printer_config_block(resp.body))
        if virtual_printer_manager is not None:
            if virtual_printer_state is not None:
                _reconcile_vprinter_config(
                    virtual_printer_manager,
                    virtual_printer_state,
                    vp_config,
                    now=time.monotonic(),
                    diagnostics=diagnostics,
                )
            else:
                try:
                    virtual_printer_manager.reconcile_sync(vp_config)
                except Exception as exc:  # noqa: BLE001 - config-down must not sink heartbeat
                    safe = redact(str(exc))
                    _record_diagnostic(diagnostics, "vprinter", f"virtual printer reconcile failed: {safe}")
                    log.warning("virtual printer reconcile failed: %s", safe)
        tailscale_cfg = resp.body.get("tailscale")
        if tailscale_state is not None:
            tailscale_state.remember_config(tailscale_cfg)
        tailscale_status = _reconcile_tailscale_config(
            tailscale_cfg,
            tailscale_reconciler,
            diagnostics,
        )
        if tailscale_state is not None:
            tailscale_state.record_reconcile_status(tailscale_status, time.monotonic())
        log.info(
            "config pulled: %d printers, virtual_printer=%s (configVersion=%s)",
            len(manager.statuses()),
            "enabled" if vp_config is not None else "disabled",
            version,
        )
        return tailscale_status
    elif resp.status == 401:
        _record_diagnostic(diagnostics, "config", "config pull rejected 401")
        log.error("config pull rejected 401 — credential revoked. Re-enroll.")
    else:
        safe = redact(resp.body.get("error"))
        _record_diagnostic(diagnostics, "config", f"config pull unexpected {resp.status}: {safe}")
        log.warning("config pull unexpected %s: %s", resp.status, safe)
    return None


def _virtual_printer_config_block(body: dict) -> object:
    if "virtualPrinter" in body:
        return body.get("virtualPrinter")
    return body.get("virtual_printer")


def _drain_vprinter_submissions_safely(
    submission_queue: queue.Queue[CapturedJob],
    cfg: Config,
    credential: str,
    *,
    model: str | None,
    diagnostics=None,
    outbox: VPrinterOutbox | None = None,
    poster=None,
    max_jobs: int = VP_SUBMISSIONS_PER_HEARTBEAT,
) -> int:
    try:
        return _drain_vprinter_submissions(
            submission_queue,
            cfg,
            credential,
            model=model,
            diagnostics=diagnostics,
            outbox=outbox,
            poster=poster or post_json,
            max_jobs=max_jobs,
        )
    except Exception as exc:  # noqa: BLE001 - drain must not sink heartbeat/shutdown
        safe = redact(str(exc))
        _record_diagnostic(diagnostics, "vprinter", f"vp-submit drain failed: {safe}")
        log.warning("vprinter.submit.drain_failed: %s", safe)
        return 0


def _request_shutdown(stop_event: threading.Event, signum: int | None = None) -> None:
    if signum is None:
        log.info("shutdown requested")
    else:
        log.info("shutdown requested by signal %s", signum)
    stop_event.set()


def _install_shutdown_signal_handlers(stop_event: threading.Event) -> dict[int, object]:
    previous: dict[int, object] = {}

    def handler(signum, _frame) -> None:
        _request_shutdown(stop_event, signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        except (OSError, ValueError):
            continue
    return previous


def _restore_shutdown_signal_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            continue


def run(
    cfg: Config,
    *,
    _stop_event: threading.Event | None = None,
    _install_signals: bool = True,
    _submission_queue: queue.Queue[CapturedJob] | None = None,
    _outbox: VPrinterOutbox | None = None,
) -> int:
    set_effective_config(cfg)
    diagnostics = Diagnostics(cloud_url=cfg.cloud_url, agent_version=__version__)
    set_default(diagnostics)
    install_log_handler(diagnostics)

    credential = read_credential()
    if not credential:
        raise SystemExit(
            "No credential found — this hub isn't enrolled yet. Run "
            "`makeros-hub enroll --token <token> --cloud-url <url>` first "
            "(get a token at /admin/3dprinting/hubs)."
        )

    interval = cfg.heartbeat_sec
    manager = PrinterManager(diagnostics=diagnostics)
    vp_submission_queue: queue.Queue[CapturedJob] = (
        _submission_queue
        if _submission_queue is not None
        else queue.Queue(maxsize=VP_SUBMISSION_QUEUE_MAX)
    )
    vp_outbox = _outbox if _outbox is not None else VPrinterOutbox()
    vp_manager = VirtualPrinterManager(
        on_capture=_make_vprinter_capture_handler(vp_submission_queue, vp_outbox),
        diagnostics=diagnostics,
    )
    queue_status_reporter = make_queue_status_reporter(cfg, credential, diagnostics=diagnostics)
    pending_queue_reports: list[dict] = []
    pending_probe_results: list[dict] = []
    pending_command_results: list[dict] = []
    tailscale_state = _TailscaleRuntimeState()
    vprinter_state = _VirtualPrinterRuntimeState()
    # Per-printer camera capture is normally driven by the admin toggle
    # (cameraEnabled, via config-down). MAKEROS_HUB_CAMERA_ENABLED=1 is a GLOBAL
    # override that force-enables capture on every camera-capable printer (handy
    # for a one-off test); unset, only admin-enabled printers are captured.
    camera_scheduler = CameraScheduler()
    camera_enabled = os.environ.get("MAKEROS_HUB_CAMERA_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    tailscale_status: dict | None = None
    vp_outbox_rehydrated = False
    stop_event = _stop_event if _stop_event is not None else threading.Event()
    previous_signal_handlers = (
        _install_shutdown_signal_handlers(stop_event) if _install_signals else {}
    )
    log.info("makeros-hub %s starting; heartbeat every %ss to %s", __version__, interval, cfg.cloud_url)

    def maybe_rehydrate_vprinter_outbox() -> None:
        nonlocal vp_outbox_rehydrated
        if vp_outbox_rehydrated or not vp_manager.current_model():
            return
        _rehydrate_vprinter_submissions(vp_submission_queue, vp_outbox)
        vp_outbox_rehydrated = True

    # Pull the printer list up front so the first heartbeat already carries status.
    pulled_tailscale_status = _pull_config(
        cfg,
        credential,
        manager,
        tailscale_state=tailscale_state,
        virtual_printer_manager=vp_manager,
        virtual_printer_state=vprinter_state,
        diagnostics=diagnostics,
    )
    if pulled_tailscale_status:
        tailscale_status = pulled_tailscale_status
    maybe_rehydrate_vprinter_outbox()

    # Start the OrcaSlicer ingest server (inbound HTTP on the LAN). Best-effort:
    # if the port is taken we log + continue (heartbeat/telemetry still work).
    ingest: IngestServer | None = None
    try:
        ingest = IngestServer(
            make_cloud_submit(cfg, credential, diagnostics=diagnostics),
            port=cfg.ingest_port,
            spool_dir=SPOOL_DIR,
            max_bytes=cfg.max_upload_mb * 1024 * 1024,
        )
        ingest.start()
    except OSError as exc:
        safe = redact(str(exc))
        _record_diagnostic(diagnostics, "ingest", f"ingest server failed to start on :{cfg.ingest_port}: {safe}")
        log.error("OrcaSlicer ingest server failed to start on :%d (%s)", cfg.ingest_port, safe)
        ingest = None

    try:
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                retry_tailscale_status = _maybe_retry_tailscale_config(
                    tailscale_state,
                    now,
                    diagnostics=diagnostics,
                )
                if retry_tailscale_status:
                    tailscale_status = retry_tailscale_status
                elif tailscale_state.should_read_status():
                    current_ts = current_tailscale_status(
                        enabled=_tailscale_config_enabled(tailscale_state.config)
                    )
                    if current_ts:
                        tailscale_status = current_ts
                        tailscale_state.record_observed_status(current_ts, now)
                elif not tailscale_state.reconcile_status_pending:
                    tailscale_status = None
                _maybe_retry_vprinter_config(
                    vprinter_state,
                    vp_manager,
                    now,
                    diagnostics=diagnostics,
                )
                maybe_rehydrate_vprinter_outbox()
                statuses = manager.statuses()
                # Live-mirror the VP's reported AMS from the agent's OWN read of the
                # real printers — so OrcaSlicer's Device tab (the send-time source of
                # truth) tracks reality every heartbeat: no cloud round-trip, no
                # config re-pull, no VP restart. Only reconciles on a display change
                # (updated_config_if_pool_changed returns None otherwise → no churn).
                if vp_manager is not None and vprinter_state.config is not None:
                    try:
                        live_vp = updated_config_if_pool_changed(vprinter_state.config, statuses)
                        if live_vp is not None:
                            # Apply to the running VP FIRST; only mark it the
                            # current config once the hot-apply succeeds. A failed
                            # apply leaves config at the last-applied pool, so the
                            # next heartbeat re-derives and retries instead of
                            # silently remembering a pool that never reached the VP.
                            vp_manager.reconcile_sync(live_vp)
                            vprinter_state.remember_config(live_vp)
                            log.info(
                                "VP AMS live-mirror applied (%d trays from real printers)",
                                len(live_vp.pool),
                            )
                    except Exception as exc:  # noqa: BLE001 - must not sink heartbeat
                        _record_diagnostic(
                            diagnostics,
                            "vprinter",
                            f"live AMS apply failed: {redact(str(exc))}",
                        )
                jobs = manager.pending_jobs()
                # Camera frames (opt-in). A printer is captured when EITHER the
                # admin enabled it (cameraEnabled, via config-down) OR the global
                # env override is set. Default-off: neither → no capture. Phase-
                # adaptive cadence + parallel/bounded — best-effort, never sinks
                # the heartbeat (a failure just sends no frame this beat).
                camera_frames: list[dict] | None = None
                camera_failures: list[str] | None = None
                try:
                    all_targets = manager.camera_targets()
                    eligible = [
                        t for t in all_targets if camera_enabled or t.get("cameraEnabled")
                    ]
                    # Keep scheduler state ONLY for currently-eligible printers, so
                    # a removed OR camera-disabled printer is forgotten — and
                    # re-enabling one later grabs a fresh frame immediately.
                    camera_scheduler.forget(
                        {t["printerId"] for t in eligible if t.get("printerId")}
                    )
                    if eligible:
                        status_by_id = {
                            s.get("printerId"): s for s in statuses if s.get("printerId")
                        }
                        camera_frames, camera_failures = collect_camera_frames(
                            eligible,
                            status_by_id,
                            camera_scheduler,
                            time.monotonic(),
                        )
                        # R4.5 agent-side loudness — when failures > 0, mirror
                        # the cloud's loud signal to the Pi-local diagnostics
                        # board so an offline operator can triage without a
                        # cloud round-trip. Summary only (one line/beat), with
                        # bounded printerId echo so a 16-printer fleet doesn't
                        # bloat the diag string. PrinterIds are non-PII.
                        if camera_failures:
                            _record_diagnostic(
                                diagnostics,
                                "camera",
                                f"no frame from {len(camera_failures)}/{len(eligible)} eligible: "
                                + ",".join(camera_failures[:5]),
                            )
                except Exception as exc:  # noqa: BLE001 - never sink the heartbeat
                    _record_diagnostic(
                        diagnostics, "camera", f"frame collection failed: {redact(str(exc))}"
                    )
                    camera_frames = None
                    camera_failures = None
                connected = sum(1 for s in statuses if s.get("connectionState") == "connected")
                resp = post_json(
                    cfg.heartbeat_url,
                    heartbeat_payload(
                        statuses,
                        jobs,
                        tailscale_status,
                        probe_results=pending_probe_results,
                        command_results=pending_command_results,
                        diagnostics=diagnostics,
                        camera_frames=camera_frames,
                        camera_failures=camera_failures,
                    ),
                    bearer=credential,
                    retries=3,
                    backoff_base=2.0,
                )
                tailscale_state.mark_reported()
                if resp.status == 200:
                    reported_probe_count = len(pending_probe_results)
                    pending_probe_results = []
                    if reported_probe_count:
                        log.info("reported %d probe result(s) to the cloud", reported_probe_count)
                    reported_command_count = len(pending_command_results)
                    pending_command_results = []
                    if reported_command_count:
                        log.info("reported %d command result(s) to the cloud", reported_command_count)
                    assignments = resp.body.get("assignments")
                    dispatch_reports = manager.dispatch_assignments(
                        assignments if isinstance(assignments, list) else [],
                        SPOOL_DIR,
                    )
                    if dispatch_reports:
                        pending_queue_reports.extend(dispatch_reports)
                        log.info(
                            "dispatched %d assignment report(s) from %d assignment(s)",
                            len(dispatch_reports),
                            len(assignments) if isinstance(assignments, list) else 0,
                        )
                    progress_reports = manager.collect_queue_progress()
                    if progress_reports:
                        pending_queue_reports.extend(progress_reports)
                    if pending_queue_reports:
                        before = len(pending_queue_reports)
                        pending_queue_reports = _flush_queue_status_reports(
                            queue_status_reporter,
                            pending_queue_reports,
                        )
                        sent = before - len(pending_queue_reports)
                        if sent:
                            log.info("flushed %d queue status update(s) from the local outbox", sent)
                    # Confirmed delivery — drop the sent jobs from the buffers.
                    # (A non-200 keeps them; the cloud dedupes re-sends.)
                    if jobs:
                        manager.ack_jobs([j["jobKey"] for j in jobs])
                        log.info("reported %d terminal job(s) to the cloud", len(jobs))
                    log.info(
                        "heartbeat ok 200 (hub %s) — %d printers, %d connected",
                        resp.body.get("hubId", "?"),
                        len(statuses),
                        connected,
                    )
                    next_ms = resp.body.get("nextPollMs")
                    if isinstance(next_ms, (int, float)) and next_ms > 0:
                        interval = max(5, int(next_ms / 1000))
                    # Re-pull the printer config when the cloud says it changed
                    # (a printer was added/edited/removed in the admin UI).
                    new_version = resp.body.get("configVersion")
                    if isinstance(new_version, str) and new_version != manager.config_version:
                        log.info("configVersion changed (%s) — re-pulling", new_version)
                        pulled_tailscale_status = _pull_config(
                            cfg,
                            credential,
                            manager,
                            tailscale_state=tailscale_state,
                            virtual_printer_manager=vp_manager,
                            virtual_printer_state=vprinter_state,
                            diagnostics=diagnostics,
                        )
                        if pulled_tailscale_status:
                            tailscale_status = pulled_tailscale_status
                        maybe_rehydrate_vprinter_outbox()
                    # Over-the-air self-update: the cloud names the release this
                    # hub should run. No-op unless it's a strictly-newer release
                    # tag and the cooldown has passed; on apply, the update
                    # script restarts the service onto the new version.
                    target = resp.body.get("targetVersion")
                    if isinstance(target, str) and target and maybe_update(__version__, target):
                        log.info("OTA: update to %s launched; the service will restart", target)
                    probe_results = _run_pending_probes(
                        resp.body.get("pendingProbes"),
                        diagnostics=diagnostics,
                    )
                    if probe_results:
                        pending_probe_results.extend(probe_results)
                        log.info("queued %d probe result(s) for the next heartbeat", len(probe_results))
                    # Execute any control commands the cloud delivered (pause/
                    # resume/stop) and queue their results for the next heartbeat.
                    command_reports = manager.dispatch_commands(resp.body.get("pendingCommands"))
                    if command_reports:
                        pending_command_results.extend(command_reports)
                        log.info(
                            "executed %d printer command(s) from the heartbeat", len(command_reports)
                        )
                elif resp.status == 401:
                    # Graceful-degradation, NOT exit: a 401 means this hub's
                    # credential was revoked/rotated. Exiting here would tear down
                    # the VP + printers and respawn into the SAME dead credential
                    # every 5s (systemd Restart=always) — a permanent black-hole
                    # with all listeners DOWN. Instead keep serving: the VP stays
                    # up so members can still send (captures queue durably in the
                    # on-disk outbox + survive vp-submit 401s), and we retry each
                    # beat so a re-enroll / rotated credential self-heals.
                    _record_diagnostic(
                        diagnostics, "heartbeat", "heartbeat rejected 401 (degraded — re-enroll/rotate)"
                    )
                    log.error(
                        "heartbeat rejected 401 — credential revoked/rotated. Staying UP in "
                        "degraded mode (VP + captures preserved); re-enroll or rotate to recover."
                    )
                else:
                    safe = redact(resp.body.get("error"))
                    _record_diagnostic(diagnostics, "heartbeat", f"heartbeat unexpected {resp.status}: {safe}")
                    log.warning("heartbeat unexpected %s: %s", resp.status, safe)
            except TransportError as exc:
                safe = redact(str(exc))
                _record_diagnostic(diagnostics, "heartbeat", f"heartbeat transport error: {safe}")
                log.warning("heartbeat transport error (will retry): %s", safe)

            _drain_vprinter_submissions_safely(
                vp_submission_queue,
                cfg,
                credential,
                model=vp_manager.current_model(),
                diagnostics=diagnostics,
                outbox=vp_outbox,
            )
            stop_event.wait(interval)
    finally:
        _drain_vprinter_submissions_safely(
            vp_submission_queue,
            cfg,
            credential,
            model=vp_manager.current_model(),
            diagnostics=diagnostics,
            outbox=vp_outbox,
            max_jobs=VP_SUBMISSION_QUEUE_MAX,
        )
        try:
            vp_manager.stop_sync()
        except Exception as exc:  # noqa: BLE001 - best-effort shutdown
            log.warning("virtual printer shutdown failed: %s", redact(str(exc)))
        manager.stop_all()
        if ingest is not None:
            ingest.stop()
        _restore_shutdown_signal_handlers(previous_signal_handlers)
    return 0
