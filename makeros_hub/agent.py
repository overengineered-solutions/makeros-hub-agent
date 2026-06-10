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
from .http import TransportError, get_json, post_json
from .ingest import IngestServer
from .printers.manager import PrinterManager
from .update import maybe_update

log = logging.getLogger("makeros-hub")


def make_cloud_submit(cfg: Config, credential: str):
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
            return {"status": "error", "detail": f"transport: {exc}"}

        if resp.status == 200:
            if resp.body.get("ok"):
                return {"status": "queued", "jobId": resp.body.get("jobId")}
            return {"status": "rejected", "reason": resp.body.get("reason", "rejected")}
        if resp.status == 401:
            # invalid member token (the hub's own bearer is valid — we're enrolled).
            return {"status": "bad_token"}
        return {"status": "error", "detail": f"http_{resp.status}"}

    return submit


def _uptime_sec() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def heartbeat_payload(
    printers: list[dict] | None = None, jobs: list[dict] | None = None
) -> dict:
    return {
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


def _pull_config(cfg: Config, credential: str, manager: PrinterManager) -> None:
    """Fetch the printer list (config-down) and reconcile adapters. Best-effort:
    a transport blip just leaves the current adapters running until next time."""
    try:
        resp = get_json(cfg.config_url, bearer=credential)
    except TransportError as exc:
        log.warning("config pull failed (will retry on next change): %s", exc)
        return
    if resp.status == 200:
        printers = resp.body.get("printers")
        version = resp.body.get("version")
        manager.reconcile(printers if isinstance(printers, list) else [], version)
        log.info("config pulled: %d printers (configVersion=%s)", len(manager.statuses()), version)
    elif resp.status == 401:
        log.error("config pull rejected 401 — credential revoked. Re-enroll.")
    else:
        log.warning("config pull unexpected %s: %s", resp.status, resp.body.get("error"))


def run(cfg: Config) -> int:
    credential = read_credential()
    if not credential:
        raise SystemExit(
            "No credential found — this hub isn't enrolled yet. Run "
            "`makeros-hub enroll --token <token> --cloud-url <url>` first "
            "(get a token at /admin/3dprinting/hubs)."
        )

    interval = cfg.heartbeat_sec
    manager = PrinterManager()
    log.info("makeros-hub %s starting; heartbeat every %ss to %s", __version__, interval, cfg.cloud_url)

    # Pull the printer list up front so the first heartbeat already carries status.
    _pull_config(cfg, credential, manager)

    # Start the OrcaSlicer ingest server (inbound HTTP on the LAN). Best-effort:
    # if the port is taken we log + continue (heartbeat/telemetry still work).
    ingest: IngestServer | None = None
    try:
        ingest = IngestServer(
            make_cloud_submit(cfg, credential),
            port=cfg.ingest_port,
            spool_dir=SPOOL_DIR,
            max_bytes=cfg.max_upload_mb * 1024 * 1024,
        )
        ingest.start()
    except OSError as exc:
        log.error("OrcaSlicer ingest server failed to start on :%d (%s)", cfg.ingest_port, exc)
        ingest = None

    try:
        while True:
            try:
                statuses = manager.statuses()
                jobs = manager.pending_jobs()
                connected = sum(1 for s in statuses if s.get("connectionState") == "connected")
                resp = post_json(
                    cfg.heartbeat_url,
                    heartbeat_payload(statuses, jobs),
                    bearer=credential,
                    retries=3,
                    backoff_base=2.0,
                )
                if resp.status == 200:
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
                        _pull_config(cfg, credential, manager)
                    # Over-the-air self-update: the cloud names the release this
                    # hub should run. No-op unless it's a strictly-newer release
                    # tag and the cooldown has passed; on apply, the update
                    # script restarts the service onto the new version.
                    target = resp.body.get("targetVersion")
                    if isinstance(target, str) and target and maybe_update(__version__, target):
                        log.info("OTA: update to %s launched; the service will restart", target)
                elif resp.status == 401:
                    log.error(
                        "heartbeat rejected 401 — this hub's credential was revoked or is "
                        "unknown. Re-enroll with a fresh token. Exiting."
                    )
                    return 2
                else:
                    log.warning("heartbeat unexpected %s: %s", resp.status, resp.body.get("error"))
            except TransportError as exc:
                log.warning("heartbeat transport error (will retry): %s", exc)

            time.sleep(interval)
    finally:
        manager.stop_all()
        if ingest is not None:
            ingest.stop()
