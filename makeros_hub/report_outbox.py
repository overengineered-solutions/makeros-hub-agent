"""Durable outbox for queue-status reports (v0.58, audit 2026-08-30).

`uploading` / `printing` / `held` / `completed` reports used to live only in the heartbeat loop's list: a power cut
between a start and a successful flush lost them, while the persisted dispatch guard kept the job from being
re-dispatched — the cloud never learned the file went up. The list is now written (atomic replace + fsync) on every
change and rehydrated on boot; the cloud dedupes re-sends.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("makeros-hub.report-outbox")

PATH = Path(os.environ.get("MAKEROS_HUB_REPORT_OUTBOX", "/var/lib/makeros-hub/queue-reports.json"))
MAX_REPORTS = 2000   # bounded: a hub cut off for days keeps the newest, never grows without limit


def load(path: Path | None = None) -> list[dict]:
    p = path or PATH
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("report outbox unreadable (%s) — starting empty", exc)
        return []
    if not isinstance(data, list):
        return []
    out = [r for r in data if isinstance(r, dict) and isinstance(r.get("queueJobId"), str) and isinstance(r.get("state"), str)]
    if out:
        log.info("report outbox: %d queued report(s) rehydrated", len(out))
    return out


def compact(reports: list[dict]) -> list[dict]:
    """Over the bound, keep each queue job's LAST report (its current state — the cloud accepts it without the earlier
    transitions) rather than blindly dropping the oldest entries (codex v0.58 r3); then, still over, drop the oldest."""
    rows = [r for r in reports if isinstance(r, dict)]
    if len(rows) <= MAX_REPORTS:
        return rows
    last_index: dict[str, int] = {}
    for i, r in enumerate(rows):
        jid = r.get("queueJobId")
        if isinstance(jid, str):
            last_index[jid] = i
    kept = [r for i, r in enumerate(rows) if not isinstance(r.get("queueJobId"), str) or last_index[r["queueJobId"]] == i]
    return kept[-MAX_REPORTS:]


def save(reports: list[dict], path: Path | None = None) -> bool:
    p = path or PATH
    keep = compact(reports)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        with tmp.open("w") as fh:
            json.dump(keep, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        dfd = os.open(str(p.parent), os.O_RDONLY)   # the rename itself must be durable: a dir-fsync failure is a failure (codex r4)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        reports[:] = keep   # the LIVE list is bounded the same way as the file (codex r4): what was compacted away is gone
        return True
    except OSError as exc:
        log.error("report outbox could not be written (%s) — the report is NOT durable", exc)
        return False
