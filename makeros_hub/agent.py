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
from .config import Config, read_credential
from .http import TransportError, get_json, post_json
from .printers.manager import PrinterManager
from .update import maybe_update

log = logging.getLogger("makeros-hub")


def _uptime_sec() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def heartbeat_payload(printers: list[dict] | None = None) -> dict:
    return {
        "agentVersion": __version__,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
        "uptimeSec": _uptime_sec(),
        "printers": printers or [],
        "jobs": [],  # native job -> billing ingestion is a later slice
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

    try:
        while True:
            try:
                statuses = manager.statuses()
                connected = sum(1 for s in statuses if s.get("connectionState") == "connected")
                resp = post_json(
                    cfg.heartbeat_url,
                    heartbeat_payload(statuses),
                    bearer=credential,
                    retries=3,
                    backoff_base=2.0,
                )
                if resp.status == 200:
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
