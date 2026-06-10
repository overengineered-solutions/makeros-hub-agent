"""Job tracking — PURE (stdlib, no paho, no I/O), fully unit-testable.

Watches a printer's merged report state across observations and emits TERMINAL
job records for the heartbeat's `jobs[]` (the cloud ingests them into its
billing pipeline). In-flight state is NOT emitted here — the live printer
status (printers[]) already carries progress.

State machine (driven by print.gcode_state):
  not-RUNNING → RUNNING            : a job became active (record start, name,
                                     identity, material)
  RUNNING     → FINISH             : emit status='done'
  RUNNING     → FAILED             : emit status='failed' (cloud maps failed →
                                     cancelled; never auto-billed)
  RUNNING     → IDLE/other         : the end transition was missed (agent blip,
                                     printer reboot) — emit status='cancelled'
                                     so the job is still visible, never billed.
  RUNNING     → RUNNING w/ new name: back-to-back prints with a missed gap —
                                     close the old job ('cancelled') and open
                                     the new one.

Job identity (`jobKey` — the cloud's native idempotency key, opaque to it):
  prefer the printer's own task/subtask id from the report (stable across agent
  restarts), else a fingerprint of serial + job name + observed start time.
  Caveat (documented): with no printer-supplied id, an agent restart MID-print
  re-fingerprints with a new start time — the terminal job may then appear
  under a new key (a duplicate observe-only row, admin-visible) rather than
  being silently merged with a different print of the same file. We bias
  toward duplicates over silent merges.

Grams: Bambu LAN telemetry does not reliably report consumed filament, so we
OMIT grams — the cloud's zero-gram path flags the row needs_review instead of
silently billing $0. (Klipper/Moonraker does report filament_used; its adapter
can fill grams in.) Material comes from the active AMS tray / external spool.

Delivery: pending terminal jobs stay buffered until the agent confirms a
heartbeat 200 (`ack`), so a failed POST can't drop a job; the cloud's
(workspace, hub, jobKey) dedupe makes re-sends harmless.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

# Keep at most this many unacked terminal jobs per printer (a hub offline for
# a week shouldn't grow unbounded; oldest dropped first — they'd be visible in
# the printer's own history if it came to that).
MAX_PENDING = 50

_TERMINAL_DONE = "FINISH"
_TERMINAL_FAILED = "FAILED"
_ACTIVE = "RUNNING"
# PAUSE keeps the job active (a paused print resumes into the same job).
_STILL_ACTIVE = {_ACTIVE, "PAUSE", "PREPARE", "SLICING", "INIT"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _gcode_state(merged: dict) -> str:
    p = merged.get("print")
    s = p.get("gcode_state") if isinstance(p, dict) else None
    return s.upper() if isinstance(s, str) else ""


def _job_name(merged: dict) -> str | None:
    p = merged.get("print")
    if not isinstance(p, dict):
        return None
    name = p.get("subtask_name") or p.get("gcode_file")
    return name.strip()[:300] if isinstance(name, str) and name.strip() else None


def _printer_task_id(merged: dict) -> str | None:
    """The printer's own job id when it gives us one (task_id/subtask_id on the
    report). '0'/''/absent → None (some local prints don't carry one)."""
    p = merged.get("print")
    if not isinstance(p, dict):
        return None
    for key in ("task_id", "subtask_id"):
        v = p.get(key)
        if isinstance(v, (int, str)):
            s = str(v).strip()
            if s and s != "0":
                return s
    return None


def decode_active_material(merged: dict) -> str | None:
    """The material family loaded in the ACTIVE slot: print.ams.tray_now decodes
    '255'=none, '254'=external spool (print.vt_tray), else ams_id*4+tray_id into
    print.ams.ams[].tray[]. Returns e.g. 'PLA', or None when unknowable."""
    p = merged.get("print")
    if not isinstance(p, dict):
        return None
    ams_obj = p.get("ams") if isinstance(p.get("ams"), dict) else {}

    def tray_type(tray: Any) -> str | None:
        t = tray.get("tray_type") if isinstance(tray, dict) else None
        return t.strip() if isinstance(t, str) and t.strip() else None

    tray_now = str(ams_obj.get("tray_now", "")).strip()
    if tray_now == "254":
        return tray_type(p.get("vt_tray"))
    if tray_now in ("", "255"):
        return None
    try:
        idx = int(tray_now)
    except ValueError:
        return None
    units = ams_obj.get("ams")
    if not isinstance(units, list):
        return None
    unit_idx, slot_idx = idx // 4, idx % 4
    if unit_idx >= len(units):
        return None
    trays = units[unit_idx].get("tray") if isinstance(units[unit_idx], dict) else None
    if not isinstance(trays, list) or slot_idx >= len(trays):
        return None
    return tray_type(trays[slot_idx])


class JobTracker:
    """Per-printer. Feed every merged-state observation through observe();
    drain terminal jobs via pending(); clear them after a confirmed send via
    ack(). All methods are pure dict/list ops — thread-safety is the caller's
    concern (the Bambu adapter calls observe() under its own lock)."""

    def __init__(self, printer_id: str, serial: str):
        self.printer_id = printer_id
        self.serial = serial
        self._active: dict | None = None  # {key, name, startedAt(ts), material}
        self._pending: list[dict] = []
        self._last_state: str = ""

    # -- internals ----------------------------------------------------------
    def _fingerprint(self, name: str | None, started_ts: float) -> str:
        basis = f"{self.serial}|{name or '?'}|{int(started_ts)}"
        return "fp_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]

    def _open(self, merged: dict, now: float) -> None:
        name = _job_name(merged)
        task_id = _printer_task_id(merged)
        self._active = {
            "key": f"task_{task_id}" if task_id else self._fingerprint(name, now),
            "name": name,
            "startedAt": now,
            "material": decode_active_material(merged),
        }

    def _close(self, status: str, now: float) -> None:
        if self._active is None:
            return
        job = {
            "jobKey": self._active["key"],
            "printerId": self.printer_id,
            "status": status,
            "startedAt": _iso(self._active["startedAt"]),
            "endedAt": _iso(now),
            "printTimeSeconds": max(0, int(now - self._active["startedAt"])),
        }
        if self._active["name"]:
            job["filename"] = self._active["name"]
        if self._active["material"]:
            job["materialKey"] = self._active["material"]
        self._pending.append(job)
        if len(self._pending) > MAX_PENDING:
            self._pending = self._pending[-MAX_PENDING:]
        self._active = None

    # -- public -------------------------------------------------------------
    def observe(self, merged: dict, now: float) -> None:
        state = _gcode_state(merged)
        if not state:
            return  # no signal yet (pre-pushall)

        if self._active is None:
            if state == _ACTIVE:
                self._open(merged, now)
        else:
            if state == _TERMINAL_DONE:
                self._close("done", now)
            elif state == _TERMINAL_FAILED:
                self._close("failed", now)
            elif state == _ACTIVE:
                # Same job still running — refresh identity bits that may have
                # arrived late (task_id often lands after the first RUNNING
                # frame), but never reopen/re-time it. A NAME change while
                # RUNNING means we missed the gap between two prints: close the
                # old one (unconfirmed end → cancelled) and open the new.
                name = _job_name(merged)
                if name and self._active["name"] and name != self._active["name"]:
                    self._close("cancelled", now)
                    self._open(merged, now)
                else:
                    if name and not self._active["name"]:
                        self._active["name"] = name
                    task_id = _printer_task_id(merged)
                    if task_id and not str(self._active["key"]).startswith("task_"):
                        self._active["key"] = f"task_{task_id}"
                    if not self._active["material"]:
                        self._active["material"] = decode_active_material(merged)
            elif state not in _STILL_ACTIVE:
                # Missed the real ending (agent blip / printer reboot) — close
                # unconfirmed. Never billed (cancelled), still visible.
                self._close("cancelled", now)

        self._last_state = state

    def pending(self) -> list[dict]:
        """Unacked terminal jobs (oldest first). Safe to send repeatedly —
        the cloud dedupes on jobKey."""
        return list(self._pending)

    def ack(self, job_keys: list[str]) -> None:
        """Drop jobs the cloud confirmed receiving (heartbeat 200)."""
        keys = set(job_keys)
        self._pending = [j for j in self._pending if j["jobKey"] not in keys]
