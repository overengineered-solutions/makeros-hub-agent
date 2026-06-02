"""The heartbeat loop. Once enrolled, the agent reports liveness to the cloud
every ~heartbeat_sec using its per-hub bearer credential. The cloud dictates the
next poll cadence in the response, so cadence changes need no agent redeploy.

Printer state goes in the (currently empty) `printers`/`jobs` arrays in PR 5;
the loop and transport are already proven by then.
"""

from __future__ import annotations

import logging
import platform
import socket
import time

from . import __version__
from .config import Config, read_credential
from .http import TransportError, post_json

log = logging.getLogger("makeros-hub")


def _uptime_sec() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def heartbeat_payload() -> dict:
    return {
        "agentVersion": __version__,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
        "uptimeSec": _uptime_sec(),
        "printers": [],  # populated by the printer adapter (PR 5)
        "jobs": [],
    }


def run(cfg: Config) -> int:
    credential = read_credential()
    if not credential:
        raise SystemExit(
            "No credential found — this hub isn't enrolled yet. Run "
            "`makeros-hub enroll --token <token> --cloud-url <url>` first "
            "(get a token at /admin/3dprinting/hubs)."
        )

    interval = cfg.heartbeat_sec
    log.info("makeros-hub %s starting; heartbeat every %ss to %s", __version__, interval, cfg.cloud_url)

    while True:
        try:
            resp = post_json(
                cfg.heartbeat_url,
                heartbeat_payload(),
                bearer=credential,
                retries=3,
                backoff_base=2.0,
            )
            if resp.status == 200:
                log.info("heartbeat ok 200 (hub %s)", resp.body.get("hubId", "?"))
                next_ms = resp.body.get("nextPollMs")
                if isinstance(next_ms, (int, float)) and next_ms > 0:
                    interval = max(5, int(next_ms / 1000))
            elif resp.status == 401:
                # Revoked or unknown credential — the kill-switch. Stop loudly so
                # systemd's RestartSec gives the operator time to re-enroll.
                log.error(
                    "heartbeat rejected 401 — this hub's credential was revoked or is "
                    "unknown. Re-enroll with a fresh token. Exiting."
                )
                return 2
            else:
                log.warning("heartbeat unexpected %s: %s", resp.status, resp.body.get("error"))
        except TransportError as exc:
            # Network blip — log and keep going; this is the self-healing reconnect.
            log.warning("heartbeat transport error (will retry): %s", exc)

        time.sleep(interval)
