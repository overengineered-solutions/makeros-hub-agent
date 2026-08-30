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


def save(reports: list[dict], path: Path | None = None) -> bool:
    p = path or PATH
    keep = [r for r in reports if isinstance(r, dict)][-MAX_REPORTS:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        with tmp.open("w") as fh:
            json.dump(keep, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        try:
            dfd = os.open(str(p.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True
    except OSError as exc:
        log.error("report outbox could not be written (%s) — reports stay in memory only until the next save", exc)
        return False
