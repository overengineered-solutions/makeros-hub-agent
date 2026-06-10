"""Queue-assignment progress correlation for local printer sends.

The hub can publish a Bambu local-print command, but the publish ack is only a
broker send result; it is not proof the printer accepted or started the print.
This helper keeps the queue state driven by observed printer telemetry instead:
RUNNING/PAUSE proves "printing", and JobTracker terminal records provide the
real printer job key plus done/failed/cancelled outcome.

Heuristic limits: a Bambu printer runs one job at a time, so only the oldest
pending dispatch is treated as active. If more than one new terminal job appears
after a dispatch baseline, correlation is ambiguous and no queue link is
emitted; the terminal JobTracker record still reports the printer job through
the normal heartbeat billing path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("makeros-hub.queue-progress")

START_TIMEOUT_SEC = 120
_ACTIVE_GCODE_STATES = {"RUNNING", "PAUSE"}
_IDLE_GCODE_STATES = {"", "IDLE", "FINISH", "FAILED", "UNKNOWN"}


def _job_key(job: dict) -> str | None:
    key = job.get("jobKey")
    return key if isinstance(key, str) and key else None


def _job_keys(jobs: list[dict]) -> set[str]:
    return {key for job in jobs if isinstance(job, dict) for key in [_job_key(job)] if key}


def _gcode_state(value: Any) -> str:
    return value.upper() if isinstance(value, str) else ""


class QueueProgressTracker:
    """Tracks cloud queue assignments until observed printer telemetry resolves
    them. Not thread-safe; callers should serialize access with their adapter
    lock."""

    def __init__(self, *, start_timeout_sec: float = START_TIMEOUT_SEC):
        self._start_timeout_sec = start_timeout_sec
        self._dispatches: list[dict[str, Any]] = []
        self._linked_job_keys: set[str] = set()

    def record_dispatch(
        self,
        queue_job_id: str,
        pending_jobs: list[dict],
        *,
        now: float | None = None,
    ) -> None:
        self._dispatches.append(
            {
                "queueJobId": queue_job_id,
                "started_monotonic": time.monotonic() if now is None else now,
                "reported_printing": False,
                "baselineKeys": _job_keys(pending_jobs),
            }
        )

    def collect(
        self,
        pending_jobs: list[dict],
        gcode_state: Any,
        *,
        now: float | None = None,
    ) -> list[dict]:
        """Return queue-status reports inferred from current printer telemetry."""
        if now is None:
            now = time.monotonic()
        pending_keys = _job_keys(pending_jobs)
        # Once the terminal printer job is acked out of JobTracker.pending(),
        # it can no longer be re-linked, so keep this suppression set bounded.
        self._linked_job_keys.intersection_update(pending_keys)

        if not self._dispatches:
            return []

        dispatch = self._dispatches[0]
        candidates = [
            job
            for job in pending_jobs
            if isinstance(job, dict)
            and (key := _job_key(job))
            and key not in dispatch["baselineKeys"]
            and key not in self._linked_job_keys
        ]
        if candidates:
            if len(candidates) > 1:
                log.warning(
                    "ambiguous queue dispatch correlation for %s: %s",
                    dispatch["queueJobId"],
                    [_job_key(job) for job in candidates],
                )
                return []
            job = candidates[0]
            job_key = _job_key(job)
            self._linked_job_keys.add(job_key)
            self._dispatches.pop(0)
            status = str(job.get("status") or "unknown")
            report = {
                "queueJobId": dispatch["queueJobId"],
                "state": "completed" if status == "done" else "held",
                "printerJobKey": job_key,
            }
            if status != "done":
                report["reason"] = f"print_{status}"
            # The cloud's transition map is strict: uploading→completed is NOT
            # allowed (only uploading→printing→completed), but uploading→held IS.
            # So ONLY a 'completed' outcome we reach without ever having observed
            # RUNNING (a print that ran + finished entirely between heartbeats)
            # needs the intervening 'printing' synthesized, or it would 409 and
            # strand the job in 'uploading'. A 'held' terminal is fine as-is.
            if report["state"] == "completed" and not dispatch["reported_printing"]:
                return [
                    {"queueJobId": dispatch["queueJobId"], "state": "printing"},
                    report,
                ]
            return [report]

        state = _gcode_state(gcode_state)
        if state in _ACTIVE_GCODE_STATES and not dispatch["reported_printing"]:
            dispatch["reported_printing"] = True
            return [{"queueJobId": dispatch["queueJobId"], "state": "printing"}]

        elapsed = now - dispatch["started_monotonic"]
        if (
            elapsed >= self._start_timeout_sec
            and not dispatch["reported_printing"]
            and state in _IDLE_GCODE_STATES
        ):
            self._dispatches.pop(0)
            return [
                {
                    "queueJobId": dispatch["queueJobId"],
                    "state": "held",
                    "reason": "start_not_observed",
                }
            ]

        return []
