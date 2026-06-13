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

import logging
import platform
import socket
import time

from . import __version__
from .config import SPOOL_DIR, Config, read_credential
from .diagnostics import Diagnostics, collect_cheap_diagnostics, install_log_handler, redact, set_default
from .http import TransportError, get_json, post_json
from .ingest import IngestServer
from .probes import PROBES, run_probe, set_effective_config
from .printers.manager import PrinterManager
from .tailscale import current_tailscale_status, reconcile_tailscale, tailscale_binary_exists
from .update import maybe_update

log = logging.getLogger("makeros-hub")

QUEUE_STATUS_SENT = "sent"
QUEUE_STATUS_RETRY = "retry"
QUEUE_STATUS_DROP = "drop"
MAX_PROBES_PER_HEARTBEAT = 3


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
    diagnostics=None,
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


def _pull_config(
    cfg: Config,
    credential: str,
    manager: PrinterManager,
    tailscale_reconciler=reconcile_tailscale,
    tailscale_state: _TailscaleRuntimeState | None = None,
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
        log.info("config pulled: %d printers (configVersion=%s)", len(manager.statuses()), version)
        return tailscale_status
    elif resp.status == 401:
        _record_diagnostic(diagnostics, "config", "config pull rejected 401")
        log.error("config pull rejected 401 — credential revoked. Re-enroll.")
    else:
        safe = redact(resp.body.get("error"))
        _record_diagnostic(diagnostics, "config", f"config pull unexpected {resp.status}: {safe}")
        log.warning("config pull unexpected %s: %s", resp.status, safe)
    return None


def run(cfg: Config) -> int:
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
    queue_status_reporter = make_queue_status_reporter(cfg, credential, diagnostics=diagnostics)
    pending_queue_reports: list[dict] = []
    pending_probe_results: list[dict] = []
    tailscale_state = _TailscaleRuntimeState()
    tailscale_status: dict | None = None
    log.info("makeros-hub %s starting; heartbeat every %ss to %s", __version__, interval, cfg.cloud_url)

    # Pull the printer list up front so the first heartbeat already carries status.
    pulled_tailscale_status = _pull_config(
        cfg,
        credential,
        manager,
        tailscale_state=tailscale_state,
        diagnostics=diagnostics,
    )
    if pulled_tailscale_status:
        tailscale_status = pulled_tailscale_status

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
        while True:
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
                statuses = manager.statuses()
                jobs = manager.pending_jobs()
                connected = sum(1 for s in statuses if s.get("connectionState") == "connected")
                resp = post_json(
                    cfg.heartbeat_url,
                    heartbeat_payload(
                        statuses,
                        jobs,
                        tailscale_status,
                        probe_results=pending_probe_results,
                        diagnostics=diagnostics,
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
                            diagnostics=diagnostics,
                        )
                        if pulled_tailscale_status:
                            tailscale_status = pulled_tailscale_status
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
                elif resp.status == 401:
                    _record_diagnostic(diagnostics, "heartbeat", "heartbeat rejected 401")
                    log.error(
                        "heartbeat rejected 401 — this hub's credential was revoked or is "
                        "unknown. Re-enroll with a fresh token. Exiting."
                    )
                    return 2
                else:
                    safe = redact(resp.body.get("error"))
                    _record_diagnostic(diagnostics, "heartbeat", f"heartbeat unexpected {resp.status}: {safe}")
                    log.warning("heartbeat unexpected %s: %s", resp.status, safe)
            except TransportError as exc:
                safe = redact(str(exc))
                _record_diagnostic(diagnostics, "heartbeat", f"heartbeat transport error: {safe}")
                log.warning("heartbeat transport error (will retry): %s", safe)

            time.sleep(interval)
    finally:
        manager.stop_all()
        if ingest is not None:
            ingest.stop()
