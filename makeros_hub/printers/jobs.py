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
  v0.69: the running print (key, name, start, material, the printer's ETA) and
  the last closed key are saved IN the terminal file, as one list entry that is
  not a job ({"hubState": …}), so a restart mid-print keeps ONE key for one
  print, and a print that FINISHED while the hub was down is closed under that
  key, ending at the printer's last ETA — LAN prints carry no task id ("0"), and
  before this such a print was never reported at all (its queue job was held
  as outcome_unknown_after_restart). The saved print is adopted only when the
  printer still shows it (same name / task id, active or terminal); otherwise
  it is dropped, never invented as an outcome. One file, one atomic write: a
  close can never be torn from its job, and v0.68 (a rollback) skips the entry
  when it loads and drops it when it next saves — so a print it went on to
  report can never come back as this saved print after a later upgrade.

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
import logging
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from .queue_progress import _fsync_dir   # True only once the directory entry is durable

log = logging.getLogger("makeros-hub.jobs")

# Unacked-terminal-job buffer cap per printer. Each entry is a tiny dict, so
# the cap is sized for a hub offline for WEEKS, not days — and hitting it is
# LOGGED loudly (Codex review: silent truncation of billable jobs is a
# delivery-loss path, not housekeeping).
MAX_PENDING = 500

_TERMINAL_DONE = "FINISH"
_TERMINAL_FAILED = "FAILED"
_ACTIVE = "RUNNING"
# PAUSE keeps the job active (a paused print resumes into the same job).
_STILL_ACTIVE = {_ACTIVE, "PAUSE", "PREPARE", "SLICING", "INIT"}
ETA_SLACK_S = 300   # the saved ETA is rewritten only when the printer's estimate moves by more (one write, not one a second)
# A same-name print still running this long after the saved print's ETA may be a re-print started during the outage:
# with no task id nothing tells them apart, so it is dropped (held for staff), never booked under the old key.
ADOPT_PAST_ETA_S = 1800


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


def _eta(merged: dict, now: float) -> float | None:
    """When the printer expects to finish: now + print.mc_remaining_time (MINUTES), or None when it does not say."""
    p = merged.get("print")
    m = p.get("mc_remaining_time") if isinstance(p, dict) else None
    return now + m * 60 if isinstance(m, (int, float)) and not isinstance(m, bool) and m >= 0 else None


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
    if idx < 0:
        return None  # malformed — never index from the list end (Codex review)
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

    def __init__(self, printer_id: str, serial: str, state_path: "Path | None" = None):
        self.printer_id = printer_id
        self.serial = serial
        self._active: dict | None = None  # {key, name, startedAt(ts), material}
        self._pending: list[dict] = []
        self._last_state: str = ""
        # v0.54 durability: unacked terminal jobs survive an update/power cut/restart (owner rule: not RAM-only) — a
        # print that ended just before a restart still reaches the cloud, so the machine never stays "occupied".
        self._state_path = state_path
        # v0.69: the running print + the last closed key ride in the same file (module docstring)
        self._restored: dict | None = None
        self._last_key: str | None = None
        self._active_saved = ""
        self._unsaved = False   # the last write failed: every observation retries until one lands
        # The saved running print was dropped after a restart (reset, another file, or the same file long past its ETA).
        # Its dispatch, if it had one, is now an outcome nobody can prove: until the hub records its NEXT dispatch, every
        # print that opens here is marked `notDispatched` (the walk-up or re-print that replaced it, and any after it), in
        # the same atomic write as the print itself, and queue progress never takes such an ending as a queue job's
        # completion; the old job is held when the printer is idle. Terminal records already pending at the drop stay
        # linkable (codex reviews of af50a73, 9ac3ff7 and 135cb83).
        self._mark_next = False
        self._load_pending()

    def active_key(self) -> str | None:
        """The job the printer is running right now (its dedupe key), or None."""
        return self._active["key"] if self._active else None

    def _load_pending(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, list):
            self._pending = [j for j in raw if isinstance(j, dict) and isinstance(j.get("jobKey"), str)][-MAX_PENDING:]
            states = [e["hubState"] for e in raw if isinstance(e, dict) and isinstance(e.get("hubState"), dict)]
            if states:
                self._load_active(states[-1])

    def _state(self) -> dict:
        return {"active": self._active if self._active is not None else self._restored, "lastKey": self._last_key,
                **({"notDispatchedNext": True} if self._mark_next else {})}

    def _save_pending(self) -> bool:
        """Write the jobs + the running-print state atomically. True only once it is durable (file AND directory)."""
        if self._state_path is None:
            return True
        state = self._state()
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                # the state entry has no jobKey, so v0.68's loader skips it (and its next save drops it)
                json.dump(self._pending + [{"hubState": state}], fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
            if not _fsync_dir(self._state_path.parent):
                raise OSError("directory fsync failed")
        except OSError as e:
            # A terminal cannot be refused (the print already ended); loud (once per failing run) so the ops digest/journal
            # shows the box is failing to make state durable. Not marked saved, so the next observation writes again.
            self._active_saved = ""
            if not self._unsaved:
                log.error("could not persist terminal jobs for %s (RAM-only until a save lands): %s", self.printer_id, e)
            self._unsaved = True
            return False
        self._active_saved = json.dumps(state, sort_keys=True)
        self._unsaved = False
        return True

    def _load_active(self, raw: dict) -> None:
        last = raw.get("lastKey")
        self._last_key = last if isinstance(last, str) else None
        self._mark_next = raw.get("notDispatchedNext") is True
        a = raw.get("active")
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)   # noqa: E731
        if (isinstance(a, dict) and isinstance(a.get("key"), str) and num(a.get("startedAt"))
                and all(a.get(k) is None or isinstance(a.get(k), str) for k in ("name", "material"))
                and a["key"] not in {j["jobKey"] for j in self._pending}):
            self._restored = {"key": a["key"], "name": a.get("name"), "startedAt": float(a["startedAt"]),
                              "material": a.get("material"), "etaAt": float(a["etaAt"]) if num(a.get("etaAt")) else None,
                              **({"notDispatched": True} if a.get("notDispatched") is True else {}),
                              **({"taskKey": a["taskKey"]} if isinstance(a.get("taskKey"), str) else {})}
        self._active_saved = json.dumps(self._state(), sort_keys=True)

    def _save_active(self) -> bool:
        # one write per change; a failed write left _active_saved empty, so this retries it
        if self._state_path is not None and json.dumps(self._state(), sort_keys=True) != self._active_saved:
            return self._save_pending()
        return True

    def _adopt_or_drop(self, merged: dict, state: str, now: float) -> None:
        """First observation after a load: the saved running print is this tracker's active job again only if the
        printer still shows it. A same-name print still running well past the saved print's ETA (ADOPT_PAST_ETA_S) is
        not taken for it: the outage spanned the saved print's end, so it may be a re-print. The ETA is refreshed while
        the print runs and while it is paused, so a restart of any length short of that never drops a running print.
        ponytail: with no saved ETA a same-name running print is still adopted, and a same-file re-print that ALSO
        finished while the hub was down is booked under the saved key (Bambu reports no start time to tell them apart)."""
        saved, self._restored = self._restored, None
        name, task_id = _job_name(merged), _printer_task_id(merged)
        eta = saved.get("etaAt")
        # the printer's own id (the key, or the alias a fingerprinted print learned): no ambiguity to judge
        exact = bool(task_id) and self._task_key(task_id) in (saved["key"], saved.get("taskKey"))
        overran = (state in _STILL_ACTIVE and not exact and eta is not None and eta > saved["startedAt"]
                   and now > eta + ADOPT_PAST_ETA_S)
        same = (state in _STILL_ACTIVE or state in (_TERMINAL_DONE, _TERMINAL_FAILED)) and (exact or (
            not overran
            and not (name and saved["name"] and name != saved["name"])   # a partial report's gcode_file never beats the id
            and not (task_id and str(saved["key"]).startswith("task_") and self._task_key(task_id) != saved["key"])
        ))
        if same:
            # adopted by name only: a task id that turns up later and differs means it was another print (observe)
            self._active = saved if exact else {**saved, "adopted": True}
        else:
            # IDLE after a restart = the printer was reset; another name = another print. The outcome is unknown, so
            # nothing is invented (the queue's outcome_unknown_after_restart covers a dispatched job).
            self._mark_next = True
            log.info("%s: %s is no longer the running print after the restart (%s%s) — its key is dropped",
                     self.printer_id, saved["name"] or saved["key"], state, ", long past its ETA" if overran else "")

    # -- internals ----------------------------------------------------------
    def _fingerprint(self, name: str | None, started_ts: float) -> str:
        # Millisecond resolution (Codex re-review): whole seconds could collide
        # when a job closes and a same-named one opens within one second (e.g.
        # cancel→restart of the same file) — the cloud would dedupe two real
        # jobs into one row. A printer can't transition twice in the same ms.
        basis = f"{self.serial}|{name or '?'}|{started_ts:.3f}"
        return "fp_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]

    def _task_key(self, task_id: str) -> str:
        # Namespaced by SERIAL (Codex review): in LAN mode task ids may be
        # printer-local counters — two printers on one hub reporting the same
        # task_id must not alias to one cloud dedupe key.
        return f"task_{self.serial}_{task_id}"

    def _open(self, merged: dict, now: float) -> None:
        name = _job_name(merged)
        task_id = _printer_task_id(merged)
        eta = _eta(merged, now)
        self._active = {
            "key": self._task_key(task_id) if task_id else self._fingerprint(name, now),
            "name": name,
            "startedAt": now,
            "material": decode_active_material(merged),
            "etaAt": round(eta) if eta is not None else None,
            **({"notDispatched": True} if self._mark_next else {}),
        }

    def _close(self, status: str, now: float) -> None:
        if self._active is None:
            return
        a, self._active = self._active, None   # cleared first: the one save below holds the job AND no running print
        end = now
        # The first state since a restart is already terminal: it ended while the hub was down, so `now` would book the
        # whole outage as print time. Its end is the printer's last ETA (never past now); with no ETA the end is unknown,
        # and like _emit_recovered no duration is sent. An ETA at the start itself is the previous print's 0 still in the
        # merged report on the first RUNNING frame, not an estimate.
        unseen = self._last_state == ""
        eta = a.get("etaAt") if a.get("etaAt") is not None and a["etaAt"] > a["startedAt"] else None
        if unseen and eta is not None:
            end = min(now, eta)
        job = {
            "jobKey": a["key"],
            "printerId": self.printer_id,
            "status": status,
            "startedAt": _iso(a["startedAt"]),
            "endedAt": _iso(end),
        }
        if not unseen or eta is not None:
            job["printTimeSeconds"] = max(0, int(end - a["startedAt"]))
        if a["name"]:
            job["filename"] = a["name"]
        if a["material"]:
            job["materialKey"] = a["material"]
        if a.get("notDispatched"):
            job["notDispatched"] = True   # queue progress never links it (the site ignores the key)
        # _emit_recovered dedupes on the printer's task key: a print keyed by fingerprint whose task id showed up later
        # must not come back under that task key on a restart onto its FINISH (codex review of 3b59933)
        self._last_key = a.get("taskKey") or job["jobKey"]
        self._buffer(job)

    def _buffer(self, job: dict) -> None:
        self._pending.append(job)
        self._save_pending()
        if len(self._pending) > MAX_PENDING:
            dropped = self._pending[: len(self._pending) - MAX_PENDING]
            self._pending = self._pending[-MAX_PENDING:]
            # Loud, never silent (Codex review): these jobs will NOT reach the
            # cloud — the printer's own on-screen history is the only record.
            log.error(
                "job buffer overflow on %s — dropped %d oldest unacked job(s): %s",
                self.printer_id,
                len(dropped),
                [j["jobKey"] for j in dropped],
            )

    def _emit_recovered(self, merged: dict, state: str, now: float) -> None:
        """First signal after tracker creation is already FINISH/FAILED — the
        print ran (or ended) while the agent was down, so there's no observed
        start. Emit a terminal record ONLY when the printer supplies its own
        task id: that key is stable across restarts, so if the pre-restart
        agent already reported this job the cloud dedupe absorbs the re-send.
        Without a printer id we'd have to invent a key (risking a duplicate
        row for an already-reported job) — we skip instead; the cloud's
        needs_review/zero-gram paths and the printer's own history cover it.
        endedAt=now is approximate (the real end predates the restart)."""
        task_id = _printer_task_id(merged)
        if not task_id:
            log.info(
                "recovered terminal state %s on %s with no printer task id — not emitting",
                state,
                self.printer_id,
            )
            return
        key = self._task_key(task_id)
        if key == self._last_key:
            # v0.69: already reported (and acked) before this restart — the printer keeps showing FINISH for hours.
            return
        if any(j["jobKey"] == key for j in self._pending):
            # A reloaded tracker (restart or adapter rebuild) already holds this print, observed in full (codex review
            # of release-0.68.0): a second, poorer row under the same key made queue progress see two candidates for
            # one print and hold the job (ambiguous_queue_correlation).
            return
        job = {
            "jobKey": key,
            "printerId": self.printer_id,
            "status": "done" if state == _TERMINAL_DONE else "failed",
            "endedAt": _iso(now),
        }
        name = _job_name(merged)
        if name:
            job["filename"] = name
        material = decode_active_material(merged)
        if material:
            job["materialKey"] = material
        if self._mark_next:
            job["notDispatched"] = True   # found finished in place of a dropped saved print (another task id)
        self._last_key = key
        self._buffer(job)

    # -- public -------------------------------------------------------------
    def observe(self, merged: dict, now: float) -> None:
        state = _gcode_state(merged)
        if not state:
            return  # no signal yet (pre-pushall)
        if self._restored is not None:
            self._adopt_or_drop(merged, state, now)
        task_id = _printer_task_id(merged)
        if self._active is not None and task_id:
            key = str(self._active["key"])
            known = key if key.startswith("task_") else self._active.get("taskKey")
            if known and known != self._task_key(task_id):
                # The printer's own id says this is ANOTHER print (codex review of 3b59933: Alice's task 77 adopted by name
                # from a report without an id, then Bob's task 78 showed up and finished under Alice's key).
                if self._active.get("adopted"):
                    # restored after a restart and never confirmed: its outcome is unknown, nothing is invented, and the
                    # print in its place is handled as if first seen now (opened or recovered, and marked)
                    log.info("%s: the printer's task id %s is not the adopted print's — its key is dropped", self.printer_id, task_id)
                    self._active, self._mark_next, self._last_state = None, True, ""
                else:
                    self._close("cancelled", now)   # the gap between two prints was missed while the hub watched
            elif known:
                self._active.pop("adopted", None)   # confirmed by the printer's own id
            elif not key.startswith("task_"):
                self._active["taskKey"] = self._task_key(task_id)   # a dedupe alias; the key the site holds is kept
        elif (task_id and state in (_TERMINAL_DONE, _TERMINAL_FAILED) and self._last_state in (_TERMINAL_DONE, _TERMINAL_FAILED)
              and self._last_key and not self._last_key.startswith("task_")):
            # the print just closed under its fingerprint is still on screen and names its task id only now: dedupe on that
            # id too, or a restart onto this FINISH re-emits it (codex review of 921c14a). ponytail: an id first seen after
            # a restart cannot be tied to the fingerprint; Bambu's pushall on connect carries it, so that needs a printer
            # that left task_id out of every report of the whole print.
            self._last_key = self._task_key(task_id)

        if self._active is None:
            if state == _ACTIVE:
                self._open(merged, now)
            elif self._last_state == "" and state in (_TERMINAL_DONE, _TERMINAL_FAILED):
                # Agent (re)started onto an already-terminal printer — the
                # RUNNING phase happened while we were down (Codex review:
                # this was a silent job-loss path). Edge-gated on the very
                # first signal so the FINISH frames a printer keeps sending
                # for hours after a print never re-emit.
                self._emit_recovered(merged, state, now)
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
                name, task_id = _job_name(merged), _printer_task_id(merged)
                same_id = bool(task_id) and self._active["key"] == self._task_key(task_id)   # the id beats a fallback name
                if name and self._active["name"] and name != self._active["name"] and not same_id:
                    self._close("cancelled", now)
                    self._open(merged, now)
                else:
                    if name and not self._active["name"]:
                        self._active["name"] = name
                    if task_id and not str(self._active["key"]).startswith("task_"):
                        self._active["key"] = self._task_key(task_id)
                    if not self._active["material"]:
                        self._active["material"] = decode_active_material(merged)
            elif state not in _STILL_ACTIVE:
                # Missed the real ending (agent blip / printer reboot) — close
                # unconfirmed. Never billed (cancelled), still visible.
                self._close("cancelled", now)
            if self._active is not None and state in _STILL_ACTIVE:
                # running or paused: keep the saved ETA current (a pause pushes it out), within ETA_SLACK_S
                eta = _eta(merged, now)
                if eta is not None and abs(eta - (self._active.get("etaAt") or 0)) > ETA_SLACK_S:
                    self._active["etaAt"] = round(eta)

        self._last_state = state
        self._save_active()

    def dispatch_recorded(self) -> bool:
        """The adapter durably recorded a new dispatch: from here a print can be ours again, so stop marking. Saved now,
        before the start is published, so a crash cannot leave the mark to land on that dispatch's print; False when that
        save did not land (the mark stays, and the adapter does not start). A new dispatch is recorded only once every
        older one is resolved, so nothing the mark protected is still open."""
        if not self._mark_next:
            return True
        self._mark_next = False
        if self._save_active():
            return True
        self._mark_next = True
        return False

    def pending(self) -> list[dict]:
        """Unacked terminal jobs (oldest first). Safe to send repeatedly —
        the cloud dedupes on jobKey."""
        return list(self._pending)

    def ack(self, job_keys: list[str]) -> None:
        """Drop jobs the cloud confirmed receiving (heartbeat 200)."""
        keys = set(job_keys)
        before = len(self._pending)
        self._pending = [j for j in self._pending if j["jobKey"] not in keys]
        if len(self._pending) != before:
            self._save_pending()
