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
from typing import Any

log = logging.getLogger("makeros-hub.printers")


def _fingerprint(p: dict) -> tuple:
    """What, if changed, means we must rebuild the adapter (new connection)."""
    return (p.get("vendor"), p.get("host"), p.get("serial"), p.get("accessCode"))


class PrinterManager:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple] = {}
        # Static status for printers the agent can't drive yet (klipper, or a
        # Bambu missing its connection facts) — surfaced so the admin sees why.
        self._static: dict[str, dict] = {}
        # Unacked terminal jobs rescued from torn-down adapters (config change
        # rebuilds the adapter; its in-memory buffer must NOT die with it —
        # Codex review finding). Drained by pending_jobs / cleared by ack_jobs.
        self._orphan_jobs: list[dict] = []
        self.config_version: str | None = None

    def reconcile(self, printers: list[dict], version: str | None) -> None:
        self.config_version = version
        desired_ids = {p["id"] for p in printers if isinstance(p.get("id"), str)}

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
            log.error("cannot start Bambu adapter for %s — paho-mqtt missing: %s", pid, e)
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_missing_paho",
            }
            return
        adapter = BambuAdapter(pid, host=host, serial=serial, access_code=code, model=p.get("model"))
        adapter.start()
        self._adapters[pid] = adapter
        self._fingerprints[pid] = fp

    def _stop(self, pid: str) -> None:
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
                log.warning("could not rescue pending jobs from %s: %s", pid, e)
            adapter.stop()

    def statuses(self) -> list[dict]:
        out: list[dict] = []
        for pid, adapter in self._adapters.items():
            try:
                out.append(adapter.status())
            except Exception as e:  # noqa: BLE001 — one bad adapter must not sink the heartbeat
                log.warning("status read failed for %s: %s", pid, e)
                out.append({"printerId": pid, "connectionState": "error", "errorReason": "agent_status_error"})
        out.extend(self._static.values())
        return out

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs across all adapters + any rescued from
        torn-down adapters. Safe to send repeatedly — the cloud dedupes on
        jobKey."""
        out: list[dict] = list(self._orphan_jobs)
        for pid, adapter in self._adapters.items():
            try:
                out.extend(adapter.pending_jobs())
            except Exception as e:  # noqa: BLE001 — one adapter can't sink the loop
                log.warning("pending_jobs failed for %s: %s", pid, e)
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
                log.warning("ack_jobs failed for %s: %s", pid, e)

    def stop_all(self) -> None:
        for pid in list(self._adapters):
            self._stop(pid)
