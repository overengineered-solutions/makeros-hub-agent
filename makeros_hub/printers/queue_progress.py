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


def _gcode_state(value: Any) -> str | None:
    """None = NO observation yet (fresh process, pre-pushall) — never treated as idle. A reported empty string is idle."""
    if value is None:
        return None
    return value.upper() if isinstance(value, str) else ""


class QueueProgressTracker:
    """Tracks cloud queue assignments until observed printer telemetry resolves
    them. Not thread-safe; callers should serialize access with their adapter
    lock."""

    def __init__(self, *, start_timeout_sec: float = START_TIMEOUT_SEC):
        self._start_timeout_sec = start_timeout_sec
        self._dispatches: list[dict[str, Any]] = []
        self._linked_job_keys: set[str] = set()

    # ── durability (v0.54) ── the owner's rule: nothing that matters lives only in RAM. An update, a power cut or a
    # restart must not lose the link between a cloud queue job and the print running on the machine — without it the
    # cloud never hears the print finished and the printer stays "occupied" forever. Wall-clock stamps on disk; monotonic
    # in memory (a rehydrated record gets `rehydrated_monotonic` so the after-restart timeouts are measured from boot).
    def to_state(self) -> dict[str, Any]:
        return {
            "dispatches": [
                {
                    "queueJobId": d["queueJobId"],
                    "dispatched_wall": d.get("dispatched_wall", time.time()),
                    "reported_printing": bool(d["reported_printing"]),
                    "baselineKeys": sorted(d["baselineKeys"]),
                    "taskName": d.get("taskName"),
                }
                for d in self._dispatches
            ],
            "linked": sorted(self._linked_job_keys),
        }

    def load_state(self, state: Any, *, now: float | None = None, now_wall: float | None = None) -> None:
        """Rehydrate after a restart. Tolerates garbage; never raises."""
        if not isinstance(state, dict):
            return
        now = time.monotonic() if now is None else now
        now_wall = time.time() if now_wall is None else now_wall
        self._dispatches = []
        for d in state.get("dispatches") or []:
            if not isinstance(d, dict) or not isinstance(d.get("queueJobId"), str):
                continue
            wall = d.get("dispatched_wall")
            age = max(0.0, now_wall - float(wall)) if isinstance(wall, (int, float)) else 0.0
            self._dispatches.append(
                {
                    "queueJobId": d["queueJobId"],
                    "started_monotonic": now - age,
                    "dispatched_wall": float(wall) if isinstance(wall, (int, float)) else now_wall,
                    "reported_printing": bool(d.get("reported_printing")),
                    "baselineKeys": {k for k in (d.get("baselineKeys") or []) if isinstance(k, str)},
                    "taskName": d.get("taskName") if isinstance(d.get("taskName"), str) else None,
                    "rehydrated_monotonic": now,
                }
            )
        self._linked_job_keys = {k for k in (state.get("linked") or []) if isinstance(k, str)}

    def record_dispatch(
        self,
        queue_job_id: str,
        pending_jobs: list[dict],
        *,
        now: float | None = None,
        task_name: str | None = None,
        active_key: str | None = None,
    ) -> None:
        """`active_key` = the job the printer was ALREADY running when we dispatched (JobTracker.active_key()); it can
        never be ours. `task_name` = the subtask name we sent; a recovered terminal with a different filename is not ours."""
        baseline = _job_keys(pending_jobs)
        if isinstance(active_key, str) and active_key:
            baseline.add(active_key)
        self._dispatches.append(
            {
                "queueJobId": queue_job_id,
                "started_monotonic": time.monotonic() if now is None else now,
                "dispatched_wall": time.time(),
                "reported_printing": False,
                "baselineKeys": baseline,
                "taskName": task_name if isinstance(task_name, str) and task_name else None,
            }
        )

    def discard_dispatch(self, queue_job_id: str) -> None:
        """The start command never left the box (publish failed): forget the dispatch we recorded ahead of it."""
        self._dispatches = [d for d in self._dispatches if d["queueJobId"] != queue_job_id]

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
        task_name = dispatch.get("taskName")
        candidates = [
            job
            for job in pending_jobs
            if isinstance(job, dict)
            and (key := _job_key(job))
            and key not in dispatch["baselineKeys"]
            and key not in self._linked_job_keys
            # a terminal record that names a different file — or, when we know our file, names NONE — is not provably ours
            # (an older print recovered after a restart); it stays a plain terminal report, never this job's completion
            and not (task_name and (not isinstance(job.get("filename"), str) or not job.get("filename") or job["filename"] != task_name))
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
        if state is None:
            return []   # nothing observed since (re)start — no timeouts run on silence (codex v0.54 #1/#2)
        # Timeouts on a REHYDRATED dispatch count from the first post-restart observation, never from boot or from the
        # original dispatch time: the box may have been down for hours while the printer worked.
        if "rehydrated_monotonic" in dispatch and "observed_monotonic" not in dispatch:
            dispatch["observed_monotonic"] = now
        if state in _ACTIVE_GCODE_STATES and not dispatch["reported_printing"]:
            dispatch["reported_printing"] = True
            return [{"queueJobId": dispatch["queueJobId"], "state": "printing"}]

        elapsed = now - dispatch.get("observed_monotonic", dispatch["started_monotonic"])
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

        # v0.54: a rehydrated dispatch that HAD reached printing, now sees an idle printer and no terminal record (the
        # printer supplied no task id, or the end happened while the box was down and left no trace) — never leave the
        # cloud believing it is still printing. Held for a human; billing never guesses "completed".
        observed_at = dispatch.get("observed_monotonic")
        if (
            "rehydrated_monotonic" in dispatch
            and observed_at is not None
            and dispatch["reported_printing"]
            and state in _IDLE_GCODE_STATES
            and now - observed_at >= self._start_timeout_sec
        ):
            self._dispatches.pop(0)
            return [
                {
                    "queueJobId": dispatch["queueJobId"],
                    "state": "held",
                    "reason": "outcome_unknown_after_restart",
                }
            ]

        return []
