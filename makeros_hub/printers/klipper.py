"""Klipper / Moonraker adapter.

One adapter per Klipper printer. Polls Moonraker over HTTP every few seconds
in a background thread, normalizes the response to the same wire DTO the
BambuAdapter produces (so `agent.py` doesn't need to branch). Stdlib only —
no requests/httpx, paho not involved. The agent's heartbeat reads via
`status()` like every other adapter.

Scope (PR-1 + PR-2 combined):
- PR-1: connection state, activity state, telemetry (progress%, temps, ETA, filename)
- PR-2: control commands (pause/resume/stop via Moonraker POST), and a job
  tracker that emits terminal print_jobs from observed print_stats.state
  transitions — same wire shape as the Bambu adapter's JobTracker so the
  cloud ingest is vendor-agnostic.
- Queue dispatch: start_print uploads a cloud-assigned G-code
  (POST /server/files/upload) and starts it (POST /printer/print/start);
  collect_queue_progress turns observed print_stats into queue reports through
  the same QueueProgressTracker as Bambu. Dispatch state and unacked terminal
  jobs are persisted (owner rule: nothing that matters lives only in RAM).

Auth: trusted LAN — Moonraker is typically wide open on the makerspace LAN.
A future PR can add the standard `X-Api-Key` header (operator pastes the
Moonraker API key per-printer). For now we send no auth and the operator
needs Moonraker bound to its LAN interface only.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .gcode_meta import read_gcode_meta
from .queue_progress import QueueProgressTracker, _safe_file_component, write_json_durably

log = logging.getLogger(__name__)

# Same env vars + defaults as bambu.py (one directory per kind of state, one file per printer). Defined here, not
# imported: bambu.py needs paho.
QUEUE_PROGRESS_DIR = Path(os.environ.get("MAKEROS_HUB_QUEUE_PROGRESS_DIR", "/var/lib/makeros-hub/queue-progress"))
TERMINAL_JOBS_DIR = Path(os.environ.get("MAKEROS_HUB_TERMINAL_JOBS_DIR", "/var/lib/makeros-hub/terminal-jobs"))

# Job-buffer cap (mirror of the Bambu JobTracker). If somehow the cloud is
# offline for >MAX_PENDING completed prints we drop the OLDEST so we never
# starve fresh telemetry; the printer's own history covers the dropped ones.
MAX_PENDING_JOBS = 500

# Moonraker's `print_stats.state` values that count as "actively printing"
# for the job tracker.
_ACTIVE_STATES = frozenset({"printing"})
_TERMINAL_DONE_STATES = frozenset({"complete"})
_TERMINAL_FAILED_STATES = frozenset({"cancelled", "error"})
# Paused doesn't end a job — same printer keeps reporting print_stats.

# Poll cadence. 5s gives 6 samples per heartbeat (30s) — enough to feel live
# without hammering Moonraker. The bambu adapter is event-driven, but for
# Klipper a quiet poll keeps the code stdlib-only.
POLL_INTERVAL_SEC = 5.0
# GET (poll) is fast — query response is <2KB and Moonraker answers without
# touching klippy. 4s is generous.
HTTP_GET_TIMEOUT_SEC = 4.0
# Control commands (pause/resume/cancel) dispatch macros to klippy and only
# return once klippy acknowledges; cancel mid-print can take several seconds
# while motion drains. The cloud already retries idempotent control commands,
# so a too-aggressive timeout here surfaces as "unreachable" when the printer
# is actually mid-cancel. 15s is the Moonraker community's de-facto floor.
HTTP_CONTROL_TIMEOUT_SEC = 15.0
# Backwards-compat — older direct references still resolve, but new code
# should pick the per-call constant above.
HTTP_TIMEOUT_SEC = HTTP_GET_TIMEOUT_SEC
# A large-format G-code over the shop LAN; Moonraker also parses its metadata before it answers.
HTTP_UPLOAD_TIMEOUT_SEC = 120.0
# Where dispatched files land under Moonraker's gcodes root, and the cap on the whole relative path.
REMOTE_DIR = "makeros"
REMOTE_PATH_MAX_CHARS = 200
# Backoff caps on consecutive poll failures so a long-unreachable printer
# stops hammering the network at 5s intervals.
POLL_BACKOFF_MAX_SEC = 30.0
# How long without a successful poll before we mark the printer offline.
STALE_SEC = 30.0
# How long to give the FIRST poll before declaring 'unreachable'.
CONNECT_TIMEOUT_SEC = 20.0
# v0.70: the slicer's estimated_time comes from one GET /server/files/metadata per NEW filename, cached here (a shop
# printer sees a handful of files a day; 32 is far above that). A failed fetch is remembered too, and asked again only
# every METADATA_RETRY_SEC, so an unscanned or missing file costs one request a minute, never one per poll.
METADATA_CACHE_MAX = 32
METADATA_RETRY_SEC = 60.0
# display_status.message is M117 text the start macro sets ("Bed soak 20 min before calibration"); the site shows it on
# the card, so it is trimmed to a card's worth.
DISPLAY_MESSAGE_MAX_CHARS = 120
# A print the tracker saved as running is the same print after a restart when the printer still shows that file and its
# start (wall now − print_stats.total_duration) agrees within this. ponytail: a cancel + re-start of the SAME file within
# this window while the hub was down is taken for the saved print; a wall-clock step across the restart falls back to a
# new key (the old behaviour).
RESUME_START_SLACK_SEC = 30.0
# Filename truncation — matches the cloud's PrinterStatusDTO + NativeJobDTO
# (apps/web/lib/print/hub-jobs.ts, max 300). Single source of truth here.
JOB_NAME_MAX_CHARS = 300

# The exact Moonraker query we issue every tick. Asking for only the fields
# we use keeps response size small (< 2 KB) and means we never have to parse
# a giant query object.
# v0.70: print_stats.info (SET_PRINT_STATS_INFO layers) and message, toolhead.homed_axes and idle_timeout.state join the
# same request (Moonraker omits any object or attribute a printer does not have, so an older Klipper is unaffected). The
# operator, 2026-10-15: "it says printing but... it also looks like its been sitting there for a long time".
# v0.76: the Elegoo OrangeStorm Giga's bed is four zones, heater_bed plus "heater_generic heater_bed1".."3", and its OrcaSlicer
# start G-code heats only the zones under the part, so heater_bed alone read "23 C" under a print on zone 2. A printer without
# the zones answers them with null attributes (checked on the Giga's Klipper and the SV08's, 2026-10-19).
BED_ZONES = ("heater_bed", "heater_generic heater_bed1", "heater_generic heater_bed2", "heater_generic heater_bed3")
_QUERY_OBJECTS = (
    "print_stats=state,filename,print_duration,total_duration,info,message"
    "&extruder=temperature,target"
    + "".join(f"&{urllib.parse.quote(zone)}=temperature,target" for zone in BED_ZONES)
    + "&virtual_sdcard=progress"
    "&display_status=progress,message"
    "&toolhead=homed_axes,position,axis_minimum,axis_maximum"
    "&idle_timeout=state"
    # v0.76: the shared touchscreen's readings (speed, Z offset, part fan, light). The SV08 has no [fan]: its M106 macro
    # drives "fan_generic fan0" and fan1 together, so fan0 stands in for it.
    "&gcode_move=speed_factor,homing_origin"
    "&fan=speed"
    "&fan_generic%20fan0=speed"
    "&output_pin%20main_led=value"
    # v0.82: the printer's heater hold (pstation printers/*/config: shop.cfg on the SV08s, pstation-hold.cfg on the Giga).
    # A printer without it answers null attributes (read on both, 2026-10-23).
    "&gcode_macro%20_pstation_hold=active,minutes,ceiling_minutes"
)
# v0.82 (operator, 2026-10-23: "I do not want to just turn heaters off if we have a chance to recover a print"): a print
# stopped on an error or paused keeps its bed warm with the nozzle off, the motors on, until someone answers or the
# printer's own ceiling (48 h). The printer owns the timing, so it holds with the hub down too; the hub only reports it.
_HOLD = "gcode_macro _pstation_hold"

# Mapping from Moonraker `print_stats.state` to the wire DTO's activity state.
# Moonraker's state machine: 'standby' | 'printing' | 'paused' | 'complete' |
# 'cancelled' | 'error'. We collapse complete/cancelled/standby into 'idle'
# because the cloud's activity state is what's happening RIGHT NOW.
_STATE_MAP = {
    "standby": "idle",
    "printing": "printing",
    "paused": "paused",
    "complete": "idle",
    "cancelled": "idle",
    "error": "error",
}

# print_stats.state → the Bambu gcode_state vocabulary QueueProgressTracker speaks.
_GCODE_STATE_MAP = {
    "printing": "RUNNING",
    "paused": "PAUSE",
    "standby": "IDLE",
    "complete": "FINISH",
    "cancelled": "FAILED",
    "error": "FAILED",
}
# start_print's live check: is klippy up, is the printer between prints, and is klippy not busy running commands (a QGL /
# bed mesh / heat-soak macro from the screen leaves print_stats at standby but idle_timeout at "Printing"; a start sent
# then waits behind it on klippy's gcode mutex and our start POST times out)?
_LIVE_QUERY = "webhooks=state&print_stats=state,filename&idle_timeout=state"
_STARTABLE_STATES = frozenset({"standby", "complete", "cancelled"})
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# v0.76: the shared touchscreen's controls (operator, 2026-10-19: "all of it needs to run through the screen"). The site
# sends {command: "klipper_action", params: {action, ...}} and the hub runs only the named actions below, built here from
# validated params: never G-code the site hands it. Each action's params (besides `action`); any other key is bad_params.
# v0.79: preheat, load and unload name a material (MATERIALS), never a temperature.
_ACTION_PARAMS: dict[str, tuple[str, ...]] = {
    "home": (), "jog": ("axis", "mm"), "center": (), "motors_off": (),
    "load_filament": ("material",), "load_more": (), "unload_filament": ("material",),
    "preheat": ("material",), "cooldown": (), "light": ("on",),
    "qgl": (), "bed_mesh": (), "clean_nozzle": (),
    "z_offset": ("delta",), "speed": ("pct",), "fan": ("pct",),
    "emergency_stop": (), "firmware_restart": (),
    "aux_fan": ("pct",), "exhaust_fan": ("pct",),
}
# Parameterless actions -> their G-code, or (the two that bypass klippy's G-code queue) a Moonraker endpoint. home is
# Klipper's G28; on the SV08, Sovol's G28 macro runs Z_OFFSET_CALIBRATION and SAVE_CONFIG (a Klipper restart) when the
# probe is uncalibrated, so v0.79 refuses every homing action then (probe_uncalibrated). MOVE_TO_CENTER homes first on the
# SV08 too. v0.79: Home ends 10 mm up, so a hot nozzle never rests on the plate (the SV08's homing_override already ends at
# Z10: no move there); QGL re-homes Z after levelling, as the SV08's START_PRINT does, so the Z a jog trusts is true again.
# Load and unload get their temperature and lift from _check_state.
_PLAIN_ACTIONS = {
    "home": "G28\nG90\nG1 Z10 F600", "center": "MOVE_TO_CENTER", "motors_off": "M84",
    "load_filament": "LOAD_FILAMENT", "load_more": "_CONTINUE_LOAD_FILAMENT", "unload_filament": "UNLOAD_FILAMENT",
    "cooldown": "TURN_OFF_HEATERS", "qgl": "QUAD_GANTRY_LEVEL\nG28 Z", "bed_mesh": "BED_MESH_CALIBRATE",
    "clean_nozzle": "CLEAN_NOZZLE",
    "emergency_stop": "/printer/emergency_stop", "firmware_restart": "/printer/firmware_restart",
}
# The Klipper object (lower-cased, as /printer/objects/list names it) an action needs; light's is its macro (see below).
_ACTION_NEEDS = {
    "center": "gcode_macro move_to_center", "load_filament": "gcode_macro load_filament",
    "load_more": "gcode_macro _continue_load_filament", "unload_filament": "gcode_macro unload_filament",
    "qgl": "quad_gantry_level", "bed_mesh": "bed_mesh", "clean_nozzle": "gcode_macro clean_nozzle",
    "aux_fan": "fan_generic fan2", "exhaust_fan": "fan_generic fan3",
}
# v0.79 (operator, 2026-10-21, the sv08 report's question 6: "sure"): staff get the SV08's auxiliary and exhaust fans, any
# state, 0-100 %. Refused on a printer without them (unsupported_on_printer).
# ponytail: the SV08's names (printer.cfg: fan_generic fan2 auxiliary, fan3 exhaust); map per printer when another has them.
_GENERIC_FANS = {"aux_fan": "fan2", "exhaust_fan": "fan3"}
# Refused while a print runs or is paused (printer_busy) / refused unless one does (not_printing). v0.79: filament moves
# run idle or paused (a runout pause needs them; Sovol's macros reheat to the print's own temperature then) and are refused
# only while printing. Everything else runs in any state: light, and the two endpoint actions.
_IDLE_ONLY_ACTIONS = frozenset({"home", "jog", "center", "motors_off", "preheat", "qgl", "bed_mesh", "clean_nozzle",
                                "cooldown"})
_FILAMENT_ACTIONS = frozenset({"load_filament", "load_more", "unload_filament"})
_PRINT_ONLY_ACTIONS = frozenset({"z_offset", "speed", "fan"})
# v0.79: every action that homes or probes. Refused when the probe says it is uncalibrated (on the SV08 a G28 would then
# calibrate and SAVE_CONFIG, a Klipper restart: Barnes, 2026-10-21 report D10) or the bed is at PROBE_BED_MAX_C or more
# (the SV08's eddy probe left its valid range at a 100 C bed: D9). Each also drops the home a jog trusts until it ends.
_HOMING_ACTIONS = frozenset({"home", "center", "qgl", "bed_mesh", "clean_nozzle"})
# ...and these restore it when they finish: each ends with a real G28 Z (qgl's own, MOVE_TO_CENTER's G28).
_PROVES_HOME = frozenset({"home", "center", "qgl"})
# Skip the running-action check, the fresh read and the state rules (still not_connected on a stale poll): the way
# out of a stuck printer.
_ENDPOINT_ACTIONS = frozenset({"emergency_stop", "firmware_restart"})
# Minutes, not seconds (homing with the SV08's probe calibration, QGL, a mesh, a heat-and-load): sent on a thread.
_LONG_ACTIONS = frozenset({"home", "center", "load_filament", "load_more", "unload_filament", "qgl", "bed_mesh",
                           "clean_nozzle"})
# The state an action is checked against, read fresh when it arrives (start_print's rule: the poll may be seconds old,
# and a print just started from the screen or the queue must never get a jog inside its start routine).
# v0.79 adds the heat, the probe and Sovol's load temperature variable (Klipper leaves out an object a printer lacks).
_ACTION_QUERY = ("print_stats=state&toolhead=homed_axes,position,axis_minimum,axis_maximum&extruder=temperature,target"
                 "&heater_bed=temperature,target&probe=is_calibrated"
                 "&gcode_macro%20_global_var=load_filament_extruder_temp&gcode_macro%20_pstation_resume_heat=nozzle")
_JOG_STEPS_MM = frozenset({0.1, 1.0, 10.0, 50.0})
_JOG_FEED = {"x": 6000, "y": 6000, "z": 600}

# v0.79: the machine controls protect the printer from a tap (operator, 2026-10-21: "i definitely also want the stupid
# logic of z and everything else fixed like we are doing weith the giga to make it run smoothly and protect it form
# itself"). The rules are the Giga screen's (pstation reports/giga-screen-logic-audit-2026-10-21.md, "Decisions"); each
# printer's own limits (axis range, max_temp, min_extrude_temp, its macros) still come from its Klipper.
# Calibration knob: the lowest Z a jog may reach, mm, or the printer's own position_min when that is higher. Sovol's
# stock screen stops at Z 0, where the nozzle touches the plate; 2 clears plates up to 2 mm apart (the Giga's are 0.84).
JOG_Z_FLOOR_MM = 2.0
LIFT_Z_MM = 10.0  # Home ends here, and an idle load or unload lifts here first when lower (the SV08's unload pushes 30 mm)
# Load and unload temperature (Sovol's _global_var.load_filament_extruder_temp, 250 for everything) and the preheat bed,
# per material; preheat holds the nozzle at NOZZLE_STANDBY_C so it neither oozes nor cooks while the bed settles (the
# print's start G-code raises it). PLA and PETG only (the operator's rule for the Giga, 2026-10-21; for the SV08 it is
# question 1 of reports/sv08-tft-screens-2026-10-21.md). Caps: nozzle 250, bed 100 (the Giga's Q3); a test holds every
# value under them, and Klipper's own max_temp still refuses past the printer's limit.
# ponytail: one table for every printer; make it per printer when one needs other numbers (the Giga's 1.0 mm nozzle).
MATERIALS = {"PLA": (215, 60), "PETG": (240, 70)}
NOZZLE_STANDBY_C = 140
# Load more waits for the heat: the nozzle set to a filament temperature, and within AT_TARGET_C of it (Sovol's
# _CONTINUE_LOAD_FILAMENT checks only min_extrude_temp 150, so it would push PETG at 151 on its way to 240).
MIN_EXTRUDE_C = 170
AT_TARGET_C = 5
# After a filament change nothing waits hot: this long after the last filament move with no print, the nozzle goes to
# NOZZLE_STANDBY_C, then off (the Giga's Q2; the SV08's own idle timeout is 30 min, Sovol's load leaves 250 on until then).
FIL_STANDBY_SEC, FIL_OFF_SEC = 600.0, 1800.0
SPEED_MAX_PCT = 150  # the Giga's Q9
# Calibration knob: how far the Z offset may be nudged from here during one print, mm in total. Each nudge is at most 0.1,
# but nothing stopped twenty of them pushing the nozzle 2 mm into the plate.
Z_TUNE_MAX_MM = 0.3
PROBE_BED_MAX_C = 90.0
# A paused print that sits (operator, 2026-10-21, the sv08 report's D6: "makes sense"). At PAUSE_ALERT_SEC the status
# carries printFault "paused_long", the shop's print-fault alert (as a Bambu printError is), and the diag ring hears it.
# At PAUSE_COOL_SEC the nozzle goes to NOZZLE_STANDBY_C and the bed is left on, so the part stays stuck; but only on a
# printer whose RESUME heats back first (the gcode_macro below, a printer change in the audit report). Sovol's RESUME does
# not reheat, and at 140 Klipper refuses to extrude (min_extrude_temp 150), so a Resume from Sovol's screen would end the
# print. A Resume from the site always heats back to the print's temperature and waits (_resume_script).
# v0.82: never on a printer with its own heater hold (_HOLD below): it turns the nozzle off itself after 30 min.
PAUSE_ALERT_SEC, PAUSE_COOL_SEC = 1800.0, 3600.0
_RESUME_HEAT = "_pstation_resume_heat"   # its variable `nozzle`: the target a paused print had, 0 = none
# dispatch_commands runs on the heartbeat thread: a short action waits this long for klippy's answer, a long one this long
# for an immediate refusal before it reports "started".
ACTION_WAIT_SEC = 2.0
LONG_ACTION_GRACE_SEC = 1.0
# Moonraker answers /printer/gcode/script only when the script finished; past this the thread stops waiting (the printer
# goes on regardless).
LONG_ACTION_TIMEOUT_SEC = 900.0
# Which macros and modules a printer has changes only with its config (a Klipper restart); firmware_restart drops it too.
OBJECTS_LIST_TTL_SEC = 300.0


class KlipperAdapter:
    """Owns one Klipper printer's Moonraker connection. Thread-safe reads via
    `status()`; a background polling thread keeps the cached state fresh."""

    def __init__(self, printer_id: str, moonraker_url: str, diagnostics=None, *, model: Optional[str] = None):
        self.printer_id = printer_id
        self.moonraker_url = _normalize_base(moonraker_url)
        # The cloud's machines.model. start_print refuses a G-code whose printer_model is not exactly this; the
        # status DTO never carries it (the `model` property below stays None).
        self._model = model
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._last_poll_ok_at: Optional[float] = None
        self._last_error_reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started: float = 0.0
        # Consecutive poll failures — drives exponential backoff so a long-
        # unreachable printer doesn't hammer the network every 5s.
        self._consecutive_failures: int = 0
        # Hub diagnostics writer (optional; defaults to module-level default).
        # Surfaces poll failures, control errors, and job-buffer overflows on
        # the cloud admin diag page — operator doesn't need journalctl.
        self._diagnostics = diagnostics
        # PR-2: per-printer terminal-job buffer. Fed from the poll loop on
        # each successful tick; drained via pending_jobs() and cleared via
        # ack_jobs() — same shape as the Bambu adapter so the cloud ingest
        # is vendor-agnostic.
        safe_id = _safe_file_component(printer_id)
        self._job_tracker = KlipperJobTracker(printer_id, state_path=TERMINAL_JOBS_DIR / f"{safe_id}.json")
        # Queue assignment ↔ print correlation, driven by OBSERVED print_stats
        # (same tracker as Bambu), persisted so an update / power cut never
        # loses which queue job the printer is running.
        self._queue_progress = QueueProgressTracker()
        self._queue_progress_path = QUEUE_PROGRESS_DIR / f"{safe_id}.json"
        self._queue_progress_saved = ""
        try:
            self._queue_progress.load_state(json.loads(self._queue_progress_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        # v0.70: filename -> (metadata or None after a failed fetch, monotonic fetched-at); insertion-ordered so the
        # oldest file is evicted first. Written on the poll thread, read by status() under _lock.
        self._metadata: OrderedDict[str, tuple[Optional[dict], float]] = OrderedDict()
        # v0.76: the screen's long action in flight (one at a time: klippy would only queue a second behind it), and the
        # cached /printer/objects/list names (lower-cased) with when they were read.
        self._running_action: Optional[str] = None
        self._objects: Optional[tuple[frozenset, float]] = None
        # v0.79, under _lock: a full home this hub saw finish and no reading has seen lost since (jogs and the load lift
        # trust only that: _check_state); when the last filament move ended (_filament_standby); and the Z offset this hub
        # has nudged during the current print (Z_TUNE_MAX_MM). All start empty: a restarted hub asks for a home again.
        self._home_proven = False
        self._fil_at: Optional[float] = None
        self._z_tuned = 0.0
        # The pause watch (PAUSE_ALERT_SEC), also under _lock: the nozzle target last seen while printing (what a resume
        # heats back to), and this pause's {"at": first seen, "alerted", "cooled"}, None when not paused. A restarted hub
        # counts a pause from when it first sees it (late, never early).
        self._print_target: Optional[float] = None
        self._pause: Optional[dict] = None

    # Bambu compat — every adapter exposes these. The model field is only
    # set by Bambu so the agent's status pipeline can omit it.
    @property
    def model(self) -> None:
        return None

    @property
    def host(self) -> str:
        # Used by camera dispatch's "moonrakerUrl or host" fallback.
        return self.moonraker_url

    def start(self) -> None:
        # Lifecycle fix: a prior stop() left self._stop SET, so a naive
        # restart would launch a thread that exits on its first
        # `while not self._stop.is_set()` check — adapter would look
        # alive but never poll. Adversarial-review HIGH finding. Clear
        # the event AFTER joining any still-running old thread so we
        # never have two pollers racing on self._data + the job tracker.
        if self._thread is not None and self._thread.is_alive():
            # Give the old loop one HTTP_GET_TIMEOUT_SEC worth of wind-down,
            # then proceed regardless — its daemon flag means a stuck thread
            # dies with the process.
            try:
                self._thread.join(timeout=HTTP_GET_TIMEOUT_SEC + 1.0)
            except RuntimeError:
                pass
        # Each poller gets its own stop event (codex review of release-0.68.0): an old poller that outlived the join
        # above still sees ITS event set and records nothing, so two pollers never both write.
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._consecutive_failures = 0
        self._thread = threading.Thread(
            target=self._poll_loop, args=(self._stop,), name=f"klipper-{self.printer_id[:8]}", daemon=True
        )
        self._thread.start()
        log.info("klipper adapter %s polling %s", self.printer_id, self.moonraker_url)

    def stop(self) -> None:
        # Under the lock (codex review of release-0.68.0): a poll records under it and checks its event first, so once
        # stop() returns this adapter never records or saves again. The manager rescues its jobs after this and builds
        # the replacement on the same state file; a poll still in flight is dropped, and the replacement's first poll
        # sees the same printer. No join: the thread is daemon, and nothing it does after this reaches state.
        with self._lock:
            self._stop.set()

    def _diag(self, subsystem: str, message: str) -> None:
        """Cloud-visible diagnostic record. Resolves the default diagnostics
        instance lazily so a constructor-less import still works in tests."""
        if self._diagnostics is None:
            try:
                from ..diagnostics import get_default

                self._diagnostics = get_default()
            except Exception:  # noqa: BLE001
                return
        try:
            self._diagnostics.record(subsystem, message)
        except Exception:  # noqa: BLE001
            pass

    # --- status read (any thread) -----------------------------------------
    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            last = self._last_poll_ok_at
            err = self._last_error_reason
            data = self._data
            started = self._started
            metadata = self._metadata_for(data)
            proven = self._home_proven
            paused_at = self._pause["at"] if self._pause else None

        if last is not None:
            out = _build_status(self.printer_id, data, "connected" if (now - last) <= STALE_SEC else "offline",
                                metadata=metadata)
            if not proven and "toolhead" in out:
                # v0.79: "homed" is what a jog from the site may trust, so the screen says "Home first" exactly when
                # the hub would refuse one (Klipper's homed_axes counts the SV08's guessed Z and the Giga's boot home).
                out["toolhead"]["homed"] = ""
            if paused_at is not None and now - paused_at >= PAUSE_ALERT_SEC:
                out["printFault"] = "paused_long"   # v0.79: the site alerts the shop, once per print
            # v0.70: when this reading was taken (wall ms), so the site can say "updated 4 s ago" instead of dating
            # everything by the heartbeat's arrival. The poll clock is monotonic; its age is moved onto the wall clock.
            out["sampledAt"] = int((time.time() - (now - last)) * 1000)
            return out

        # No successful poll yet. If we have an error reason AND we've been
        # running for the connect timeout, surface it. Else we're still
        # connecting.
        if err and (now - started) > CONNECT_TIMEOUT_SEC:
            return _build_status(
                self.printer_id, {}, "error", error_reason=err
            )
        return _build_status(self.printer_id, {}, "connecting")

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs observed since the last successful heartbeat
        ack. Same shape as `BambuAdapter.pending_jobs`. Safe to send
        repeatedly — the cloud dedupes on `jobKey`."""
        with self._lock:
            return self._job_tracker.pending()

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Drop jobs the cloud confirmed receiving (after a heartbeat 200)."""
        with self._lock:
            self._job_tracker.ack(job_keys)

    # --- queue dispatch (cloud assignment → Moonraker) --------------------
    def pending_queue_job_ids(self) -> list[str]:
        with self._lock:
            return self._queue_progress.pending_queue_job_ids()

    def pending_queue_jobs(self) -> list[dict]:
        with self._lock:
            return self._queue_progress.pending_queue_jobs()

    def start_print(
        self,
        local_path,
        file_name: str,
        *,
        plate: int = 1,
        use_ams: bool = False,
        ams_mapping=None,
        queue_job_id: str | None = None,
        raw_print: dict | None = None,
        required_filaments: list | None = None,
        assignment_seq: int | None = None,
    ) -> dict:
        """Upload a cloud-assigned G-code to Moonraker and start it. Same keywords as BambuAdapter.start_print; plate,
        use_ams, ams_mapping, raw_print and required_filaments are Bambu-only and ignored (Klipper has no AMS mirror —
        the cloud checks the staff-set loaded filament before it assigns).

        Order: idempotency → model gate → live idle check → record + persist the dispatch → upload → start. Nothing is
        uploaded before the dispatch is durable; a step that certainly failed forgets the dispatch again."""
        if not isinstance(queue_job_id, str) or not queue_job_id:
            return {"ok": False, "reason": "bad_assignment"}
        with self._lock:
            pending = self._queue_progress.pending_queue_jobs()
            mine = [p for p in pending if p["queueJobId"] == queue_job_id]
            if mine:
                old_seq = mine[0].get("assignmentSeq")
                if old_seq is None or assignment_seq is None or old_seq == assignment_seq:
                    # the durable dispatch state already holds THIS assignment: nothing is uploaded or started twice
                    log.info("klipper %s: %s already dispatched here — not starting it again", self.printer_id, queue_job_id)
                    return {"ok": True, "already_dispatched": True}
                log.warning("klipper %s: %s re-assigned (seq %s → %s) — superseding the stale dispatch record",
                            self.printer_id, queue_job_id, old_seq, assignment_seq)
                self._queue_progress.discard_dispatch(queue_job_id)
                self._save_queue_progress()
                pending = [p for p in pending if p["queueJobId"] != queue_job_id]
            if pending:
                # another job's outcome on this printer is still unknown: never stack a second start on it
                return {"ok": False, "reason": "printer_busy: dispatch_pending"}

        # The cloud matched the job to this printer by model; the file itself must agree (G-code for another machine
        # must never reach a printer — the same rule as Bambu's _sliced_model_matches).
        try:
            meta = read_gcode_meta(local_path)
        except OSError:
            meta = None
        sliced = (meta or {}).get("printerModel")
        if not (self._model and sliced and sliced.strip() == self._model.strip()):
            log.warning("klipper %s: refusing %s — sliced for %r, this printer is %r", self.printer_id, queue_job_id, sliced, self._model)
            return {"ok": False, "reason": "model_mismatch"}

        refusal = self._live_refusal()
        if refusal:
            log.info("klipper %s: not starting %s — %s", self.printer_id, queue_job_id, refusal)
            return {"ok": False, "reason": refusal}

        remote = _remote_path(queue_job_id, file_name)
        if not self._record_dispatch(queue_job_id, remote, assignment_seq):
            return {"ok": False, "reason": "state_persist_failed"}
        try:
            uploaded = self._upload(Path(local_path), remote)
        except Exception as exc:  # noqa: BLE001 — any failure: the file is not (provably) there
            detail = _http_detail(exc)
            log.warning("klipper %s: upload of %s failed: %s", self.printer_id, remote, detail)
            self._diag("klipper", f"upload for {queue_job_id[:8]} failed: {detail}")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "upload_failed"}
        if uploaded != remote:
            # Moonraker names the file it stored; print_stats.filename will be that name, so the dispatch must be too.
            self._discard_dispatch(queue_job_id)
            if not self._record_dispatch(queue_job_id, uploaded, assignment_seq):
                return {"ok": False, "reason": "state_persist_failed"}

        url = f"{self.moonraker_url}/printer/print/start?{urllib.parse.urlencode({'filename': uploaded})}"
        req = urllib.request.Request(
            url, method="POST", data=b"",
            headers={"Content-Length": "0", "Accept": "application/json", "User-Agent": "makeros-hub-agent"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_CONTROL_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
                body = resp.read(8 * 1024)
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            log.warning("klipper %s: start of %s refused: %s", self.printer_id, uploaded, detail)
            self._diag("klipper", f"print start for {queue_job_id[:8]} refused: {detail}")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            # The command may have reached klippy (no answer, or a broken one): keep the dispatch and let telemetry decide
            # (RUNNING → printing; nothing within the start timeout → start_not_observed). Never a second start on top of a
            # maybe-first.
            log.warning("klipper %s: start of %s unconfirmed (%s) — waiting on telemetry", self.printer_id, uploaded, _classify_error(exc))
            return {"ok": True}
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            # a truncated / garbled 2xx body is a broken reply too, not a refusal: same no-answer path
            log.warning("klipper %s: start of %s answered unreadably — waiting on telemetry", self.printer_id, uploaded)
            return {"ok": True}
        if not (isinstance(parsed, dict) and parsed.get("result") == "ok"):
            self._diag("klipper", f"print start for {queue_job_id[:8]} answered without ok")
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        log.info("klipper %s: started %s for %s", self.printer_id, uploaded, queue_job_id)
        return {"ok": True}

    def _live_refusal(self) -> Optional[str]:
        """None when klippy is ready and the printer is between prints; else the held reason. A fresh query, not the
        poll cache — the cache may be seconds old and a member may just have started something at the screen."""
        try:
            status = self._fetch_once(_LIVE_QUERY)
        except Exception as exc:  # noqa: BLE001
            return f"printer_stale: {_classify_error(exc)}"
        hooks = status.get("webhooks")
        stats = status.get("print_stats")
        klippy = hooks.get("state") if isinstance(hooks, dict) else None
        state = stats.get("state") if isinstance(stats, dict) else None
        if not isinstance(klippy, str) or not isinstance(state, str):
            return "printer_stale: shape_mismatch"
        if klippy != "ready":
            return f"printer_busy: klippy_{_UNSAFE_NAME.sub('_', klippy)[:24]}"
        if state in ("printing", "paused"):
            return "printer_busy: printing"
        if state == "error":
            return "printer_busy: klipper_error"
        if state not in _STARTABLE_STATES:
            return f"printer_busy: {_UNSAFE_NAME.sub('_', state)[:24]}"
        idle = status.get("idle_timeout")
        idle_state = idle.get("state") if isinstance(idle, dict) else None
        if not isinstance(idle_state, str):
            return "printer_stale: shape_mismatch"   # klippy always loads idle_timeout: without it we cannot rule out a macro
        if idle_state == "Printing":
            return "printer_busy: klippy_busy"
        return None

    def _record_dispatch(self, queue_job_id: str, task_name: str, assignment_seq) -> bool:
        with self._lock:
            self._queue_progress.record_dispatch(
                queue_job_id, self._job_tracker.pending(), task_name=task_name,
                active_key=self._job_tracker.active_key(), assignment_seq=assignment_seq,
            )
            if self._save_queue_progress():
                return True
            # a print we cannot track across a restart is a print we do not start
            self._queue_progress.discard_dispatch(queue_job_id)
            log.error("klipper %s: refusing to start %s — dispatch state could not be persisted", self.printer_id, queue_job_id)
            return False

    def _discard_dispatch(self, queue_job_id: str) -> None:
        with self._lock:
            self._queue_progress.discard_dispatch(queue_job_id)
            self._save_queue_progress()

    def _upload(self, local: Path, remote: str) -> str:
        """POST /server/files/upload, streamed (the file is never read into memory whole). Returns the stored path
        relative to the gcodes root (item.path). `print` is deliberately NOT sent: with it Moonraker swallows a failed
        start, or silently job-queues the file to start later (file_manager._finish_gcode_upload)."""
        directory, _, name = remote.rpartition("/")
        digest = hashlib.sha256()
        with local.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        boundary = "makeros" + os.urandom(12).hex()
        fields = {"root": "gcodes", "path": directory, "checksum": digest.hexdigest()}
        head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        head_bytes = head.encode("utf-8")
        tail_bytes = f"\r\n--{boundary}--\r\n".encode("utf-8")
        size = local.stat().st_size

        def body():
            yield head_bytes
            with local.open("rb") as src:
                yield from iter(lambda: src.read(1 << 20), b"")
            yield tail_bytes

        req = urllib.request.Request(
            f"{self.moonraker_url}/server/files/upload",
            data=body(),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head_bytes) + size + len(tail_bytes)),
                "Accept": "application/json",
                "User-Agent": "makeros-hub-agent",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_UPLOAD_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
            status = resp.status
            raw = resp.read(64 * 1024)
        if status != 201:
            raise ValueError(f"upload answered http_{status}, expected 201")
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        # The upload body is NOT wrapped in "result" (application.py FileUploadHandler); accept a wrapped one anyway.
        result = parsed.get("result", parsed) if isinstance(parsed, dict) else None
        item = result.get("item") if isinstance(result, dict) else None
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path:
            raise ValueError("upload answered without item.path")
        return path

    def collect_queue_progress(self, on_report=None) -> list[dict]:
        """Drain queue-status reports inferred from observed print_stats. `on_report` receives each report BEFORE the
        popped dispatch state is persisted (the agent writes it to the durable outbox there); a report that could not be
        made durable rolls the tracker back and hands out nothing — the same reports come again next beat."""
        with self._lock:
            last = self._last_poll_ok_at
            stats = self._data.get("print_stats") if isinstance(self._data, dict) else None
            raw_state = stats.get("state") if isinstance(stats, dict) else None
            # None = no FRESH observation: silence is not idle, so no timeouts run on it
            fresh = last is not None and (time.monotonic() - last) <= STALE_SEC
            gcode_state = _GCODE_STATE_MAP.get(raw_state) if fresh and isinstance(raw_state, str) else None
            before = self._queue_progress.to_state()
            task_names = {d["queueJobId"]: d.get("taskName") for d in before["dispatches"]}
            head_task = before["dispatches"][0].get("taskName") if before["dispatches"] else None
            if gcode_state in ("RUNNING", "PAUSE") and head_task and _job_filename(stats) != head_task:
                # Another file is running (a walk-up print from the screen): it is not this job's print, so it must never
                # report 'printing' for it. The job can only time out to start_not_observed (or link its own terminal).
                gcode_state = "IDLE"
            reports = self._queue_progress.collect(self._job_tracker.pending(), gcode_state)
            for report in reports:
                task = task_names.get(report.get("queueJobId"))
                # only a DURABLE key: a key a restart cannot find again would make the cloud refuse the real terminal
                # (hub_job_ref set to another key); without one the cloud's time-window match still finds the print
                key = (self._job_tracker.active_key(task)
                       if report.get("state") == "printing" and task and not self._job_tracker.unsaved else None)
                if key:
                    report["printerJobKey"] = key   # the cloud stamps hub_job_ref from it, so the terminal job links
            if callable(on_report):
                for report in reports:
                    try:
                        on_report(report)
                    except Exception as exc:  # noqa: BLE001
                        log.error("progress report not durable (%s) — keeping the dispatch state, retrying next beat", exc)
                        self._queue_progress.load_state(before, now=time.monotonic(), now_wall=time.time())
                        return []
            self._save_queue_progress()
            return reports

    def _save_queue_progress(self) -> bool:
        """Write only when the state changed (SD-card friendly). False = not durable."""
        encoded = json.dumps(self._queue_progress.to_state(), sort_keys=True)
        if encoded == self._queue_progress_saved:
            return True
        if not write_json_durably(self._queue_progress_path, encoded):
            log.error("klipper %s: queue progress could not be made durable", self.printer_id)
            return False
        self._queue_progress_saved = encoded
        return True

    def send_command(self, command: str, params: dict | None = None) -> dict:
        """Pause / resume / stop the active print over Moonraker's standard
        HTTP control endpoints. Same shape as `BambuAdapter.send_command`:
        returns `{ok, reason?, command}`. The cloud control layer already
        gates which commands a workspace can issue + audits; we re-validate
        here as defense-in-depth and produce a directional `reason` so the
        admin UI can render a useful message.

        Wire endpoints (Moonraker docs):
          POST /printer/print/pause   — only valid while printing
          POST /printer/print/resume  — only valid while paused
          POST /printer/print/cancel  — terminates the print (the cloud's
                                        'stop' command maps here)
        We translate 'stop' → cancel to keep the command vocabulary aligned
        with Bambu's; the admin UI doesn't need to branch on vendor.

        v0.76: 'klipper_action' is the shared touchscreen's named actions (_klipper_action).
        """
        if command == "klipper_action":
            return {**self._klipper_action(params), "command": command}
        if command == "resume":
            with self._lock:
                last_ok = self._last_poll_ok_at
            if last_ok is not None and time.monotonic() - last_ok <= STALE_SEC:
                try:
                    script = self._resume_script()
                except _Refused as refused:
                    return {"ok": False, "reason": refused.reason, "command": command}
                except Exception as exc:  # noqa: BLE001 - the fresh state could not be read: resume nothing blind
                    return {"ok": False, "reason": _action_failure(exc)[0], "command": command}
                if script:
                    return {**self._start_long_action("resume", script), "command": command}
        cmd_path = {
            "pause": "/printer/print/pause",
            "resume": "/printer/print/resume",
            "stop": "/printer/print/cancel",
        }.get(command)
        if cmd_path is None:
            # ams_dry / skip_objects are Bambu-only; surface as unsupported
            # so the cloud can render a directional "Klipper doesn't support
            # this control" message rather than failing silently.
            return {
                "ok": False,
                "reason": "unsupported_command",
                "command": command,
            }
        # Liveness pre-check (adversarial review MED #15): a pause issued
        # against an offline printer used to fly straight to a TCP refuse,
        # spending HTTP_CONTROL_TIMEOUT_SEC of admin patience before the
        # cloud got the directional error. Short-circuit when the adapter
        # already knows the printer isn't reachable. Stale-window safety:
        # the heartbeat marks "offline" at STALE_SEC, which is well past
        # the freshest reasonable command latency.
        now = time.monotonic()
        with self._lock:
            last_ok = self._last_poll_ok_at
        if last_ok is None or (now - last_ok) > STALE_SEC:
            return {
                "ok": False,
                "reason": "not_connected",
                "command": command,
            }
        url = f"{self.moonraker_url}{cmd_path}"
        # Moonraker accepts POST with an empty body for the three control
        # endpoints; no params needed. We send an explicit Content-Length 0
        # so a stricter reverse-proxy in front of Moonraker doesn't trip.
        req = urllib.request.Request(
            url,
            method="POST",
            data=b"",
            headers={
                "Content-Length": "0",
                "Accept": "application/json",
                "User-Agent": "makeros-hub-agent",
            },
        )
        try:
            # Adversarial review HIGH #7: GET timeout (4s) is too short for
            # cancel, which dispatches a klippy macro and only returns once
            # motion drains. Use the longer control timeout.
            with urllib.request.urlopen(req, timeout=HTTP_CONTROL_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
                body = resp.read(8 * 1024)
        except urllib.error.HTTPError as e:
            # 400/409 from Moonraker means "cannot pause/resume from current
            # state" (e.g. resume while idle). The body carries a message
            # which is useful to surface.
            try:
                err_body = e.read(8 * 1024).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            reason = _classify_control_http_error(e.code, err_body)
            self._diag(
                "klipper",
                f"control {command} failed: {reason} (HTTP {e.code})",
            )
            return {
                "ok": False,
                "reason": reason,
                "command": command,
                "httpStatus": e.code,
            }
        except urllib.error.URLError as e:
            # Adversarial review MED #3: stdlib urllib wraps socket timeouts
            # in URLError(reason=TimeoutError(...)); the bare-URLError branch
            # would misclassify those as 'unreachable'. Unwrap so the cloud
            # admin UI gets the truthful directional reason — and so the
            # cloud's retry policy doesn't fire-and-forget what was actually
            # a delivered-but-slow command.
            reason = _classify_url_error(e)
            self._diag("klipper", f"control {command} failed: {reason}")
            return {
                "ok": False,
                "reason": reason,
                "command": command,
            }
        except TimeoutError:
            self._diag("klipper", f"control {command} failed: timeout")
            return {
                "ok": False,
                "reason": "timeout",
                "command": command,
            }
        # Moonraker 200 returns {"result": "ok"} on the three control
        # endpoints. Adversarial review MED #9: be strict about the shape —
        # a 200 with a missing/non-ok result is suspicious (proxy in the
        # way?) and worth surfacing rather than swallowing.
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, dict) and parsed.get("result") == "ok":
            log.info("klipper %s control ok: %s", self.printer_id, command)
            return {"ok": True, "command": command, "result": "ok"}
        # 200 + non-ok body — fail loud rather than report success.
        log.warning(
            "klipper %s control %s: 200 but unexpected body: %r",
            self.printer_id,
            command,
            (body[:80] if isinstance(body, (bytes, bytearray)) else None),
        )
        self._diag(
            "klipper",
            f"control {command} returned 200 but body shape mismatch",
        )
        return {
            "ok": False,
            "reason": "shape_mismatch",
            "command": command,
            "httpStatus": 200,
        }

    # --- screen controls (v0.76) -----------------------------------------
    def _klipper_action(self, params: Any) -> dict:
        """One named action from the shared touchscreen: {ok, reason?, detail?}. Refusals, in order: _parse_action's
        (unsupported_action, bad_params), not_connected (the poll's liveness rule, as for pause), then for everything but
        the two endpoint actions action_running (a long one is still going), _check_state's against a fresh read (v0.79:
        the Z floor, a proven home, the heat, the probe, the Z tune; it also gives load and unload their temperature), and
        unsupported_on_printer (the macro or module is not on this printer).

        How it is sent: Moonraker's POST /printer/gcode/script answers only once the script has run, and everything here
        talks to Moonraker over stdlib urllib (no websocket client; writing one for this is not worth it). So a short
        action (a jog, a temperature, the light, M220/M106, SET_GCODE_OFFSET) waits ACTION_WAIT_SEC for its answer, and
        a long one (homing, QGL, a mesh, load/unload, cleaning, centring) is POSTed from its own daemon thread: the
        heartbeat thread waits LONG_ACTION_GRACE_SEC for an immediate refusal and reports "started", and a failure that
        comes back later goes to the log and the diag ring. A short action that times out may still run once klippy is
        free, so it is reported as timeout, never retried here."""
        now = time.monotonic()
        with self._lock:
            last_ok, running = self._last_poll_ok_at, self._running_action
        try:
            action, script, need = _parse_action(params)
            if last_ok is None or (now - last_ok) > STALE_SEC:
                raise _Refused("not_connected")
            if action not in _ENDPOINT_ACTIONS:
                if running:
                    raise _Refused("action_running")
                fresh = self._fetch_once(_ACTION_QUERY, timeout=ACTION_WAIT_SEC)
                self._note_homed(fresh)
                with self._lock:
                    proven, z_tuned = self._home_proven, self._z_tuned
                script = _check_state(params, script, fresh, home_proven=proven, z_tuned=z_tuned)
                if need and need not in self._printer_objects():
                    raise _Refused("unsupported_on_printer")
        except _Refused as refused:
            return {"ok": False, "reason": refused.reason}
        except Exception as exc:  # noqa: BLE001 - the fresh state or the objects list could not be read
            return {"ok": False, "reason": _action_failure(exc)[0]}
        if action in _ENDPOINT_ACTIONS or action == "motors_off":   # whatever the answer, the home is gone
            with self._lock:
                self._home_proven = False
        if action in _ENDPOINT_ACTIONS or action in ("preheat", "cooldown"):   # a new heat plan: no filament standby
            with self._lock:
                self._fil_at = None
        if action in _LONG_ACTIONS:
            return self._start_long_action(action, script)
        path = script if action in _ENDPOINT_ACTIONS else _script_path(script)
        try:
            self._post_ok(path, ACTION_WAIT_SEC)
        except Exception as exc:  # noqa: BLE001
            reason, message = _action_failure(exc)
            log.warning("klipper %s: action %s failed: %s %s", self.printer_id, action, reason, message)
            self._diag("klipper", f"action {action} failed: {reason}")
            if action == "jog" and isinstance(exc, urllib.error.HTTPError):
                # klippy stops a script at its first error, so a refused G1 left the printer in G91: put G90 back
                try:
                    self._post_ok(_script_path("G90"), ACTION_WAIT_SEC)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("klipper %s: G90 after a refused jog failed: %s", self.printer_id, _action_failure(exc2)[0])
            return {"ok": False, "reason": reason}
        with self._lock:
            if action == "firmware_restart":
                self._objects = None
            if action == "z_offset":
                self._z_tuned += params["delta"]
        log.info("klipper %s: action %s ok", self.printer_id, action)
        return {"ok": True}

    def _start_long_action(self, action: str, script: str) -> dict:
        with self._lock:
            if self._running_action:
                return {"ok": False, "reason": "action_running"}
            self._running_action = action
            if action in _HOMING_ACTIONS:
                self._home_proven = False
        outcome: list[Optional[str]] = []   # appended once by the thread: None = finished ok, else the reason

        def run() -> None:
            try:
                self._post_ok(_script_path(script), LONG_ACTION_TIMEOUT_SEC)
                outcome.append(None)
                log.info("klipper %s: action %s finished", self.printer_id, action)
                if action in _PROVES_HOME:
                    with self._lock:
                        self._home_proven = True
            except Exception as exc:  # noqa: BLE001 - never let the thread die with the action still marked running
                reason, message = _action_failure(exc)
                outcome.append(reason)
                log.warning("klipper %s: action %s failed: %s %s", self.printer_id, action, reason, message)
                self._diag("klipper", f"action {action} failed: {reason}")
            finally:
                with self._lock:
                    self._running_action = None
                    if action in _FILAMENT_ACTIONS:   # failed or not, the macro may have left the nozzle hot
                        self._fil_at = time.monotonic()

        thread = threading.Thread(target=run, name=f"klipper-{action}-{self.printer_id[:8]}", daemon=True)
        thread.start()
        thread.join(LONG_ACTION_GRACE_SEC)
        if outcome and outcome[0] is not None:
            return {"ok": False, "reason": outcome[0]}
        return {"ok": True, "detail": "done" if outcome else "started"}

    def _note_homed(self, status: dict[str, Any]) -> None:
        """A reading (the poll or an action's fresh read) where any axis is not homed ends the proven home: motors off, a
        Klipper restart, a print's own M84."""
        if not _all_homed(status):
            with self._lock:
                self._home_proven = False

    def _after_poll(self, data: dict[str, Any]) -> None:
        """v0.79, on the poll thread after each good reading: the proven home, the per-print Z tune, the pause watch, the
        filament standby."""
        self._note_homed(data)
        state = _obj(data, "print_stats").get("state")
        target = _num(_obj(data, "extruder").get("target")) or 0.0
        with self._lock:
            if state not in ("printing", "paused"):
                self._z_tuned = 0.0
                self._print_target = None
            elif target >= MIN_EXTRUDE_C and (state == "printing" or self._print_target is None):
                self._print_target = target   # paused: only when the hub never saw it print (PAUSE keeps the target)
            if state != "paused":
                self._pause = None
            elif self._pause is None:
                self._pause = {"at": time.monotonic(), "alerted": False, "cooled": False}
            pause = dict(self._pause) if self._pause else None
            fil_at = self._fil_at if self._running_action is None else None
        if pause is not None:
            self._pause_watch(data, pause)
        if fil_at is not None:
            self._filament_standby(data, fil_at)

    def _filament_standby(self, data: dict[str, Any], fil_at: float) -> None:
        """The Giga's Q2 (operator, 2026-10-21): FIL_STANDBY_SEC after this hub's last filament move with no print, the
        nozzle goes to NOZZLE_STANDBY_C; at FIL_OFF_SEC, off. A print ends it (the print owns its heat). A pause waits
        and never cools: the SV08's RESUME does not reheat, so a paused nozzle must stay at the print's temperature.
        ponytail: decided on the poll's reading, so a print started in the last 5 s can get one M104 during its start
        routine, which heats again itself; a fresh read here would close that."""
        state = _obj(data, "print_stats").get("state")
        target = _num(_obj(data, "extruder").get("target")) or 0.0
        age = time.monotonic() - fil_at
        if state == "paused" or (state != "printing" and age < FIL_STANDBY_SEC):
            return
        ends = state == "printing" or age >= FIL_OFF_SEC
        script = None
        if state != "printing" and age >= FIL_OFF_SEC and target > 0:
            script = "M104 S0"
        elif state != "printing" and not ends and target > NOZZLE_STANDBY_C:
            script = f"M104 S{NOZZLE_STANDBY_C}"
        if script:
            try:
                self._post_ok(_script_path(script), ACTION_WAIT_SEC)
                log.info("klipper %s: filament standby %s (%.0f min after the last filament move, no print)",
                         self.printer_id, script, age / 60)
            except Exception as exc:  # noqa: BLE001 - the printer's own idle timeout still turns the heat off
                log.warning("klipper %s: filament standby %s not sent: %s", self.printer_id, script, _action_failure(exc)[0])
                ends = True
        if ends:
            with self._lock:
                if self._fil_at == fil_at:   # not a newer filament move since
                    self._fil_at = None

    def _resume_script(self) -> Optional[str]:
        """v0.79 (D6): a Resume from the site never starts a print cold. -> None to resume as before (the nozzle is at the
        print's temperature, or the print is not paused: Moonraker answers that), else "M109 S<t> / RESUME", sent as a long
        action. t is the target last seen while printing, else the one the printer saved (_RESUME_HEAT); never below the
        current one.
        Raises _Refused("nozzle_cold") when none of them is a filament temperature."""
        fresh = self._fetch_once(_ACTION_QUERY, timeout=ACTION_WAIT_SEC)
        if _obj(fresh, "print_stats").get("state") != "paused":
            return None
        heat = _obj(fresh, "extruder")
        temp, target = _num(heat.get("temperature")) or 0.0, _num(heat.get("target")) or 0.0
        with self._lock:
            seen = self._print_target
        saved = _num(_obj(fresh, f"gcode_macro {_RESUME_HEAT}").get("nozzle")) or 0.0
        # what this hub saw it print at first: the printer's variable can be left over from a print cancelled while paused
        want = max(seen or (saved if saved >= MIN_EXTRUDE_C else 0.0), target)
        if want < MIN_EXTRUDE_C:
            raise _Refused("nozzle_cold")
        if target >= want - 0.5 and temp >= want - AT_TARGET_C:
            return None
        return f"M109 S{want:g}\nRESUME"

    def _pause_watch(self, data: dict[str, Any], pause: dict) -> None:
        """v0.79, on the poll thread while paused: at PAUSE_ALERT_SEC tell the log and the diag ring once (the status
        carries printFault from then on); at PAUSE_COOL_SEC drop the nozzle to NOZZLE_STANDBY_C once, the bed untouched,
        on a printer whose RESUME heats back (saving the print's target for it first)."""
        age = time.monotonic() - pause["at"]
        if age >= PAUSE_ALERT_SEC and not pause["alerted"]:
            with self._lock:
                if self._pause is not None:
                    self._pause["alerted"] = True
            log.warning("klipper %s: paused %.0f min", self.printer_id, age / 60)
            self._diag("klipper", f"print paused {age / 60:.0f} min on {self.printer_id[:12]}")
        if age < PAUSE_COOL_SEC or pause["cooled"]:
            return
        if isinstance(_obj(data, _HOLD).get("active"), bool):
            return   # v0.82: the printer runs its own heater hold (its nozzle is off after 30 min); one owner of the heat
        with self._lock:
            if self._pause is not None:
                self._pause["cooled"] = True   # once, whatever happens next
            want = self._print_target
        target = _num(_obj(data, "extruder").get("target")) or 0.0
        if target <= NOZZLE_STANDBY_C:
            return
        try:
            if f"gcode_macro {_RESUME_HEAT}" not in self._printer_objects():
                log.warning("klipper %s: paused %.0f min, nozzle left at %g: this printer's RESUME does not heat back "
                            "(no gcode_macro %s)", self.printer_id, age / 60, target, _RESUME_HEAT)
                return
            keep = max(want or 0.0, target)
            self._post_ok(_script_path(f"SET_GCODE_VARIABLE MACRO={_RESUME_HEAT} VARIABLE=nozzle VALUE={keep:g}\n"
                                       f"M104 S{NOZZLE_STANDBY_C}"), ACTION_WAIT_SEC)
        except Exception as exc:  # noqa: BLE001
            log.warning("klipper %s: paused nozzle not cooled: %s", self.printer_id, _action_failure(exc)[0])
            return
        log.info("klipper %s: paused %.0f min, nozzle %g -> %d (Resume heats it back)", self.printer_id, age / 60, keep,
                 NOZZLE_STANDBY_C)
        self._diag("klipper", f"paused {age / 60:.0f} min: nozzle down to {NOZZLE_STANDBY_C} on {self.printer_id[:12]}")

    def _post_ok(self, path: str, timeout: float) -> None:
        """POST one Moonraker endpoint; returns only on {"result": "ok"}, raises otherwise."""
        req = urllib.request.Request(
            f"{self.moonraker_url}{path}", method="POST", data=b"",
            headers={"Content-Length": "0", "Accept": "application/json", "User-Agent": "makeros-hub-agent"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(8 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        if not (isinstance(parsed, dict) and parsed.get("result") == "ok"):
            raise ValueError("answered without ok")

    def _printer_objects(self) -> frozenset:
        """GET /printer/objects/list, lower-cased ("gcode_macro load_filament", "quad_gantry_level"), cached for
        OBJECTS_LIST_TTL_SEC. Raises when it cannot be read: an action is never run on a guess."""
        now = time.monotonic()
        with self._lock:
            cached = self._objects
        if cached is not None and now - cached[1] < OBJECTS_LIST_TTL_SEC:
            return cached[0]
        req = urllib.request.Request(f"{self.moonraker_url}/printer/objects/list",
                                     headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"})
        with urllib.request.urlopen(req, timeout=ACTION_WAIT_SEC) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(256 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        result = parsed.get("result") if isinstance(parsed, dict) else None
        objects = result.get("objects") if isinstance(result, dict) else None
        if not isinstance(objects, list):
            raise ValueError("missing objects")
        names = frozenset(o.lower() for o in objects if isinstance(o, str))
        with self._lock:
            self._objects = (names, now)
        return names

    # --- polling loop -----------------------------------------------------
    def _poll_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                data = self._fetch_once()
            except Exception as e:  # noqa: BLE001 - never let the thread die
                reason = _classify_error(e)
                with self._lock:
                    was_clean = self._last_error_reason is None
                    self._last_error_reason = reason
                    self._consecutive_failures += 1
                    failures = self._consecutive_failures
                    self._home_proven = False   # v0.79: the printer may be restarting (the Giga fakes a home at boot)
                # Adversarial review MED #13: first failure after a run of
                # successes is WARN-level so it lands in the diag ring (which
                # gates on WARN+). Subsequent failures stay at DEBUG so a
                # long-offline printer doesn't drown the journal.
                if was_clean:
                    log.warning(
                        "klipper %s poll failed: %s", self.printer_id, reason
                    )
                    self._diag(
                        "klipper",
                        f"poll failed for {self.printer_id[:12]}: {reason}",
                    )
                else:
                    log.debug(
                        "klipper %s poll failed (#%d): %s",
                        self.printer_id,
                        failures,
                        reason,
                    )
                # Adversarial review MED #8: exponential-ish backoff on
                # consecutive failures so an unreachable printer stops
                # hammering the network at 5s intervals. Capped at
                # POLL_BACKOFF_MAX_SEC. Reset on next success.
                wait = min(
                    POLL_INTERVAL_SEC * min(2 ** (failures - 1), 8),
                    POLL_BACKOFF_MAX_SEC,
                )
            else:
                was_first = self._record_poll(data, stop)
                if was_first is None:
                    return   # stopped while this poll was in flight
                self._after_poll(data)
                self._refresh_metadata(data)   # Publish and date the reading before a metadata request can delay it.
                # Adversarial review MED #14: explicit "connected" log on
                # the first-poll-success transition so the cloud admin diag
                # gets a clear event (mirrors the Bambu adapter's
                # `bambu adapter %s connected` log).
                if was_first:
                    log.info(
                        "klipper adapter %s connected to %s",
                        self.printer_id,
                        self.moonraker_url,
                    )
                wait = POLL_INTERVAL_SEC
            # `wait` returns True if .set() fires mid-sleep — exit immediately.
            if stop.wait(wait):
                return

    def _record_poll(self, data: dict[str, Any], stop: Optional[threading.Event] = None) -> Optional[bool]:
        """Apply one successful poll; True on the first one since start. None (nothing applied) once `stop` is set."""
        with self._lock:
            if stop is not None and stop.is_set():
                return None
            was_first = self._last_poll_ok_at is None
            self._data = data
            self._last_poll_ok_at = time.monotonic()
            self._last_error_reason = None
            self._consecutive_failures = 0
            # Feed the job tracker every successful poll. Pure helper —
            # converts the Moonraker status dict to the (state, filename,
            # print_duration) it needs and accumulates terminal-state
            # transitions into pending_jobs. Lock held throughout so a
            # concurrent `pending_jobs()` / `ack_jobs()` call sees a
            # consistent buffer.
            self._job_tracker.observe(data, time.monotonic())
        return was_first

    def _fetch_once(self, query: str = _QUERY_OBJECTS, *, timeout: float = HTTP_GET_TIMEOUT_SEC) -> dict[str, Any]:
        url = f"{self.moonraker_url}/printer/objects/query?{query}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        # Moonraker shape: { result: { status: { print_stats: {...}, ... } } }
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(result, dict):
            raise ValueError("missing result")
        status = result.get("status")
        if not isinstance(status, dict):
            raise ValueError("missing status")
        return status

    # --- slicer metadata (v0.70) -----------------------------------------
    def _refresh_metadata(self, data: dict[str, Any]) -> None:
        """Fetch the slicer metadata of the file print_stats names, once per filename (again after METADATA_RETRY_SEC
        when the last try failed). Runs on the poll thread; never raises."""
        filename = _current_filename(data)
        if filename is None:
            return
        now = time.monotonic()
        with self._lock:
            cached = self._metadata.get(filename)
        if cached is not None and (cached[0] is not None or now - cached[1] < METADATA_RETRY_SEC):
            return
        try:
            meta: Optional[dict] = self._fetch_metadata(filename)
        except Exception as e:  # noqa: BLE001 - the ETA degrades to the progress formula, the poll goes on
            meta = None
            log.debug("klipper %s metadata for %r unavailable: %s", self.printer_id, filename[:80], _classify_error(e))
        with self._lock:
            self._metadata[filename] = (meta, now)
            self._metadata.move_to_end(filename)
            while len(self._metadata) > METADATA_CACHE_MAX:
                self._metadata.popitem(last=False)

    def _fetch_metadata(self, filename: str) -> Optional[dict]:
        """GET /server/files/metadata?filename=<file>; only the fields the status uses, shape-checked. None when the
        result has no usable slicer estimate (Moonraker answers 404 for an unscanned or unknown file, which raises)."""
        url = f"{self.moonraker_url}/server/files/metadata?filename={urllib.parse.quote(filename, safe='')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "makeros-hub-agent"})
        with urllib.request.urlopen(req, timeout=HTTP_GET_TIMEOUT_SEC) as resp:  # noqa: S310 - operator LAN URL
            body = resp.read(256 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(result, dict):
            raise ValueError("missing result")
        estimated = _num(result.get("estimated_time"))
        if estimated is None or estimated <= 0:
            return None
        return {"estimated_time": estimated}

    def _metadata_for(self, data: dict[str, Any]) -> Optional[dict]:
        """The cached metadata of the file print_stats names right now (caller holds _lock)."""
        filename = _current_filename(data)
        cached = self._metadata.get(filename) if filename is not None else None
        return cached[0] if cached is not None else None


# ---------------------------------------------------------------------------
# Pure helpers (testable without HTTP)
# ---------------------------------------------------------------------------


def _current_filename(status: dict[str, Any]) -> Optional[str]:
    print_stats = status.get("print_stats")
    fn = print_stats.get("filename") if isinstance(print_stats, dict) else None
    return fn if isinstance(fn, str) and fn.strip() else None


def _normalize_base(url: str) -> str:
    """Trim trailing slash + add http:// if the operator forgot the scheme."""
    u = url.strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = f"http://{u}"
    return u


def _classify_error(err: BaseException) -> str:
    """Short machine-readable error reason for the wire DTO."""
    if isinstance(err, urllib.error.HTTPError):
        return f"http_{err.code}"
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", "url_error")
        if isinstance(reason, TimeoutError):
            return "timeout"
        # `[Errno 111] Connection refused` → 'unreachable'
        if hasattr(reason, "errno") and getattr(reason, "errno", None) == 111:
            return "unreachable"
        return "unreachable"
    if isinstance(err, TimeoutError):
        return "timeout"
    if isinstance(err, (ValueError, json.JSONDecodeError)):
        return "shape_mismatch"
    return "unknown"


def _remote_path(queue_job_id: str, file_name: str) -> str:
    """makeros/<job8>_<file>, at most REMOTE_PATH_MAX_CHARS, the extension kept (Moonraker only treats .gcode/.gco/.g
    as printable). Names are reduced to [A-Za-z0-9._-] so they are safe in a multipart header and a query string."""
    name = _UNSAFE_NAME.sub("_", PurePosixPath(file_name).name) or "print.gcode"
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    prefix = f"{REMOTE_DIR}/{_UNSAFE_NAME.sub('_', queue_job_id[:8])}_"
    return prefix + stem[: max(1, REMOTE_PATH_MAX_CHARS - len(prefix) - len(suffix))] + suffix


def _http_detail(err: BaseException) -> str:
    """A short, log-safe description of a failed Moonraker call (HTTP code + Moonraker's own message)."""
    if isinstance(err, urllib.error.HTTPError):
        try:
            message = _extract_moonraker_error_message(err.read(8 * 1024).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            message = ""
        return f"HTTP {err.code} {message}".strip()[:200]
    return f"{_classify_error(err)}: {err}"[:200]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _int(v: Any) -> Optional[int]:
    """A whole number (Klipper sends layer counts as ints, null when unset); bools and fractions are not one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _build_status(
    printer_id: str,
    status: dict[str, Any],
    connection_state: str,
    error_reason: Optional[str] = None,
    *,
    metadata: Optional[dict] = None,
) -> dict:
    """Pure: Moonraker status dict → the cloud's PrinterStatusDTO shape.

    Same key set + casing as `bambu_parse.normalize_status` so the cloud
    ingest doesn't branch on vendor. Omit-when-absent: the cloud DTO is
    strict() and rejects an explicit null for an optional number.

    v0.70 (docs/telemetry-v1.md): targets, the M117 message, layers, durations and `preparing` ride along, progress is
    the slicer's M73 value when there is one, and no ETA is given before the first extrusion. Klipper calls the whole
    start routine "printing" (the file's first line is START_PRINT: soak, home, QGL, mesh), and virtual_sdcard.progress
    is header + thumbnail bytes over file size, which is why the SV08 read "printing, 8 %, no ETA" for twenty minutes
    on 2026-10-15 while M117 said "Bed soak 20 min before calibration". No Klipper `gcodeState` on purpose: the site
    feeds that key into its plate-clear gate (_GCODE_STATE_MAP above is for the queue tracker only).
    """
    out: dict[str, Any] = {"printerId": printer_id, "connectionState": connection_state}
    if connection_state == "error" and error_reason:
        out["errorReason"] = error_reason

    if connection_state == "connected":
        print_dur: Optional[float] = None
        state_raw: Any = None
        print_stats = status.get("print_stats")
        if isinstance(print_stats, dict):
            state_raw = print_stats.get("state")
            if isinstance(state_raw, str):
                out["state"] = _STATE_MAP.get(state_raw, "idle")
            fn = print_stats.get("filename")
            if isinstance(fn, str) and fn.strip():
                out["jobName"] = fn.strip()[:JOB_NAME_MAX_CHARS]
            print_dur = _num(print_stats.get("print_duration"))
            if print_dur is not None:
                print_dur = max(0.0, print_dur)
                out["printDurationSec"] = print_dur
            total_dur = _num(print_stats.get("total_duration"))
            if total_dur is not None:
                out["elapsedSec"] = max(0.0, total_dur)
            # print_duration counts from the first extrusion (Klipper's init_duration covers everything before), so a
            # "printing" print with none is still in its start routine.
            if print_dur is not None:
                out["preparing"] = state_raw == "printing" and print_dur == 0
            info = print_stats.get("info")
            if isinstance(info, dict):
                current, total = _int(info.get("current_layer")), _int(info.get("total_layer"))
                if current is not None and total is not None and total > 0 and current >= 0:
                    out["layer"] = [current, total]

        # Progress: display_status.progress is the slicer's M73 value (OrcaSlicer writes M73 P0 as the file's first
        # executable line, so it stays 0 through the start routine); Klipper itself falls back to the byte position
        # when a file has no M73, and so do we when the object is absent.
        progress: Optional[float] = None
        display = status.get("display_status")
        if isinstance(display, dict):
            progress = _num(display.get("progress"))
            message = display.get("message")
            if isinstance(message, str) and message.strip():
                out["displayMessage"] = message.strip()[:DISPLAY_MESSAGE_MAX_CHARS]
        if progress is None:
            vs = status.get("virtual_sdcard")
            progress = _num(vs.get("progress")) if isinstance(vs, dict) else None
        if progress is not None:
            progress = max(0.0, min(1.0, progress))
            out["progressPct"] = progress * 100.0
            # ETA: nothing before the first extrusion (print_duration 0 made the old formula say "1 min left" the
            # moment extrusion began, because the header bytes already counted as done). Then the slicer's own
            # estimate minus the time printed, the way Mainsail's "slicer" estimate works, when the file's metadata
            # is known; the progress extrapolation otherwise, or once a print has outrun its estimate.
            if print_dur is not None and print_dur > 0:
                estimated = _num(metadata.get("estimated_time")) if isinstance(metadata, dict) else None
                eta_sec: Optional[float] = None
                if estimated is not None and estimated - print_dur > 0:
                    eta_sec = estimated - print_dur
                elif progress > 0.01:
                    eta_sec = print_dur * (1.0 - progress) / progress
                if eta_sec is not None and eta_sec > 0:
                    out["etaMinutes"] = int(eta_sec / 60)

        heater = status.get("extruder")
        if isinstance(heater, dict):
            t = _num(heater.get("temperature"))
            if t is not None:
                out["nozzleTempC"] = t
            # The target tells 140/140 (held for calibration) from 140/215 (heating); today's card could not.
            target = _num(heater.get("target"))
            if target is not None:
                out["nozzleTargetC"] = target
        # The bed is the zone with the highest target, else the hottest one (a bed cooling after a print).
        zones = [(_num(z.get("target")), _num(z.get("temperature")))
                 for z in (status.get(name) for name in BED_ZONES) if isinstance(z, dict)]
        zones = [z for z in zones if z != (None, None)]
        if zones:
            target, t = max(zones, key=lambda z: (z[0] or 0.0, -1e9 if z[1] is None else z[1]))
            if t is not None:
                out["bedTempC"] = t
            if target is not None:
                out["bedTargetC"] = target

        # v0.76: what the shared touchscreen shows beside its controls; each omitted unless Moonraker gave a number (it
        # answers an object the printer lacks with null attributes).
        toolhead = status.get("toolhead")
        if isinstance(toolhead, dict) and isinstance(toolhead.get("homed_axes"), str):
            head: dict[str, Any] = {"homed": toolhead["homed_axes"]}
            xyz = [_axis(toolhead.get("position"), i) for i in range(3)]
            if None not in xyz:
                head.update(zip("xyz", (round(v, 2) for v in xyz)))
            out["toolhead"] = head
        move = status.get("gcode_move")
        if isinstance(move, dict):
            factor = _num(move.get("speed_factor"))
            if factor is not None:
                out["speedPct"] = round(factor * 100)
            z_offset = _axis(move.get("homing_origin"), 2)
            if z_offset is not None:
                out["zOffset"] = round(z_offset, 3)
        fan_speeds = [_num(f.get("speed")) for f in (status.get("fan"), status.get("fan_generic fan0")) if isinstance(f, dict)]
        fan_speed = next((v for v in fan_speeds if v is not None), None)
        if fan_speed is not None:
            out["fanPct"] = round(fan_speed * 100)
        led = status.get("output_pin main_led")
        led_value = _num(led.get("value")) if isinstance(led, dict) else None
        if led_value is not None:
            out["light"] = led_value > 0
        # v0.82: only what the printer says it is holding; omitted when it holds nothing or has no hold
        hold = status.get(_HOLD)
        if isinstance(hold, dict) and hold.get("active") is True:
            minutes, ceiling = _int(hold.get("minutes")), _int(hold.get("ceiling_minutes"))
            if minutes is not None and ceiling is not None and 0 <= minutes <= ceiling:
                out["hold"] = {"minutes": minutes, "ceilingMinutes": ceiling}

        # If print_stats said nothing, default to idle so the cloud doesn't
        # render this connection as 'offline' (no state) by accident.
        if "state" not in out:
            out["state"] = "idle"

    return out


def _classify_control_http_error(code: int, body: str) -> str:
    """Map Moonraker's control-endpoint error codes to directional reasons
    the cloud admin UI can render. Moonraker wraps state-machine rejections
    as 400 + {"error":{"message":"..."}} (newer builds) or {"message":"..."}
    (older). We parse the message out before substring-matching so we
    correctly catch the documented + observed phrasings."""
    if code == 400:
        message = _extract_moonraker_error_message(body)
        m = message.lower()
        # Adversarial review HIGH #6: widen the match. These cover the
        # documented Moonraker error phrasings + the variants observed
        # across community projects (pybambu / ha-bambulab / Mainsail).
        if any(
            kw in m
            for kw in (
                "not currently paused",
                "not paused",
                "no paused print",
                "cannot resume",
            )
        ):
            return "not_paused"
        if any(
            kw in m
            for kw in (
                "no print",
                "not started",
                "no active print",
                "nothing is printing",
                "cannot pause",
                "cannot cancel",
            )
        ):
            return "not_printing"
        if "klippy" in m and ("busy" in m or "shutdown" in m or "not ready" in m):
            return "klipper_error"
        return "invalid_state"
    if code == 401 or code == 403:
        return "auth_required"
    if code == 404:
        return "endpoint_missing"
    if 500 <= code < 600:
        return "klipper_error"
    return f"http_{code}"


def _extract_moonraker_error_message(body: str) -> str:
    """Pull the human message out of Moonraker's error envelope. Tries the
    `{error:{message}}` and `{message}` shapes and falls back to the raw
    body bounded at 200 chars (never the full body — we don't want a
    multi-KB klippy trace landing in our event log)."""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001
        return body[:200]
    if isinstance(parsed, dict):
        inner = parsed.get("error")
        if isinstance(inner, dict):
            msg = inner.get("message")
            if isinstance(msg, str):
                return msg[:200]
        msg = parsed.get("message")
        if isinstance(msg, str):
            return msg[:200]
    return body[:200]


def _classify_url_error(err: urllib.error.URLError) -> str:
    """Adversarial review MED #3: stdlib urllib wraps socket-level timeouts
    in URLError(reason=TimeoutError(...)). The bare-URLError branch would
    misclassify those as 'unreachable'. Map the inner reason to the right
    directional code so the cloud admin UI / retry policy can branch."""
    import socket

    reason = getattr(err, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_failure"
    if isinstance(reason, ConnectionRefusedError):
        return "unreachable"
    # Generic socket error / OSError — most often connection refused or
    # no-route-to-host. 'unreachable' is the safe default.
    return "unreachable"


class _Refused(Exception):
    """A klipper_action the hub will not run; `reason` is the directional word the site shows."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v: Any, low: int, high: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and low <= v <= high


def _axis(v: Any, i: int) -> Optional[float]:
    return _num(v[i]) if isinstance(v, list) and len(v) > i else None


def _script_path(script: str) -> str:
    return "/printer/gcode/script?" + urllib.parse.urlencode({"script": script}, quote_via=urllib.parse.quote)


def _parse_action(params: Any) -> tuple[str, str, Optional[str]]:
    """Pure: the site's klipper_action params -> (action, the G-code script or Moonraker endpoint path, the Klipper object
    it needs or None). Raises _Refused: unsupported_action, then bad_params (every param type, range and key checked)."""
    action = params.get("action") if isinstance(params, dict) else None
    if not isinstance(action, str) or action not in _ACTION_PARAMS:
        raise _Refused("unsupported_action")
    if set(params) != {"action", *_ACTION_PARAMS[action]}:
        raise _Refused("bad_params")
    need = _ACTION_NEEDS.get(action)
    if "material" in params and not (isinstance(params["material"], str) and params["material"] in MATERIALS):
        raise _Refused("bad_params")
    if action in _PLAIN_ACTIONS:
        script = _PLAIN_ACTIONS[action]
    elif action == "jog":
        axis, mm = params["axis"], params["mm"]
        if not isinstance(axis, str) or axis not in _JOG_FEED or not _finite(mm) or abs(mm) not in _JOG_STEPS_MM:
            raise _Refused("bad_params")
        script = f"G91\nG1 {axis.upper()}{mm:g} F{_JOG_FEED[axis]}\nG90"
    elif action == "preheat":
        script = f"M104 S{NOZZLE_STANDBY_C}\nM140 S{MATERIALS[params['material']][1]}"
    elif action == "light":
        if not isinstance(params["on"], bool):
            raise _Refused("bad_params")
        script = "mainled_on" if params["on"] else "mainled_off"
        need = f"gcode_macro {script}"
    elif action == "z_offset":
        delta = params["delta"]
        if not (_finite(delta) and 0 < abs(delta) <= 0.1):
            raise _Refused("bad_params")
        script = f"SET_GCODE_OFFSET Z_ADJUST={delta:g} MOVE=1"
    elif action in _GENERIC_FANS:
        if not _whole(params["pct"], 0, 100):
            raise _Refused("bad_params")
        script = f"SET_FAN_SPEED FAN={_GENERIC_FANS[action]} SPEED={params['pct'] / 100:g}"
    elif action == "speed":
        if not _whole(params["pct"], 10, SPEED_MAX_PCT):
            raise _Refused("bad_params")
        script = f"M220 S{params['pct']}"
    else:  # fan
        if not _whole(params["pct"], 0, 100):
            raise _Refused("bad_params")
        script = f"M106 S{round(params['pct'] * 2.55)}"
    return action, script, need


def _obj(status: dict[str, Any], name: str) -> dict:
    v = status.get(name)
    return v if isinstance(v, dict) else {}


def _all_homed(status: dict[str, Any]) -> bool:
    homed = _obj(status, "toolhead").get("homed_axes")
    return isinstance(homed, str) and set("xyz") <= set(homed.lower())


def _check_state(params: dict, script: str, status: dict[str, Any], *, home_proven: bool = False,
                 z_tuned: float = 0.0) -> str:
    """Pure: may this parsed action run in `status` (the fresh _ACTION_QUERY read), and the script to send then. Raises
    _Refused: printer_stale, printer_busy / not_printing ("printing" and "paused" both count as a print; filament moves are
    refused only while printing), probe_uncalibrated, bed_too_hot, not_homed, z_floor, lift_first, out_of_range,
    nozzle_cold, nozzle_heating, unsupported_on_printer, z_offset_limit.

    home_proven: a full home this hub saw finish, never lost since (the adapter's _home_proven). homed_axes alone is not
    enough: the SV08's homing_override marks Z homed at a guessed 0 on any G28 X or Y, and the Giga fakes "xyz" at boot.
    z_tuned: the Z offset this hub has already nudged during this print."""
    action = params["action"]
    if action in _ENDPOINT_ACTIONS:
        return script
    state = _obj(status, "print_stats").get("state")
    if not isinstance(state, str):
        raise _Refused("printer_stale")
    printing = state in ("printing", "paused")
    if (printing and action in _IDLE_ONLY_ACTIONS) or (state == "printing" and action in _FILAMENT_ACTIONS):
        raise _Refused("printer_busy")
    if not printing and action in _PRINT_ONLY_ACTIONS:
        raise _Refused("not_printing")
    toolhead = _obj(status, "toolhead")
    z, z_low, z_high = (_axis(toolhead.get(k), 2) for k in ("position", "axis_minimum", "axis_maximum"))
    if action in _HOMING_ACTIONS:
        if _obj(status, "probe").get("is_calibrated") is False:
            raise _Refused("probe_uncalibrated")
        bed = _obj(status, "heater_bed")
        if max(_num(bed.get("temperature")) or 0.0, _num(bed.get("target")) or 0.0) >= PROBE_BED_MAX_C:
            raise _Refused("bed_too_hot")
    if action == "jog":
        axis, mm = params["axis"], params["mm"]
        if not (home_proven and _all_homed(status)):
            raise _Refused("not_homed")
        i = "xyz".index(axis)
        pos, low, high = (_axis(toolhead.get(k), i) for k in ("position", "axis_minimum", "axis_maximum"))
        if pos is None or low is None or high is None or z is None or z_low is None:
            raise _Refused("printer_stale")
        floor = max(JOG_Z_FLOOR_MM, z_low)
        if axis != "z" and z < floor - 1e-6:
            raise _Refused("lift_first")   # sideways only clear of the plate, its clips and the SV08's wiper
        if axis == "z" and mm < 0 and pos + mm < floor - 1e-6:
            raise _Refused("z_floor")      # up is always allowed, even from below the floor
        # toolhead.position is the commanded position, the frame axis_minimum/maximum are in (a relative G1 moves it by mm)
        if not low - 1e-6 <= pos + mm <= high + 1e-6:
            raise _Refused("out_of_range")
    elif action == "load_more":
        heat = _obj(status, "extruder")
        temp, target = _num(heat.get("temperature")) or 0.0, _num(heat.get("target")) or 0.0
        if target < MIN_EXTRUDE_C:
            raise _Refused("nozzle_cold")      # a nozzle cooling with its heater off is cold, however hot it still reads
        if temp < max(MIN_EXTRUDE_C, target - AT_TARGET_C):
            raise _Refused("nozzle_heating")
    elif action in ("load_filament", "unload_filament"):
        # Sovol's macros heat to _global_var.load_filament_extruder_temp and wait (paused: the print's own target when it
        # is at least min_extrude_temp). Set it to the material's for this move only, then put the printer's value back.
        # A printer without that variable cannot be told a temperature: refused rather than loaded at 250.
        was = _num(_obj(status, "gcode_macro _global_var").get("load_filament_extruder_temp"))
        if was is None:
            raise _Refused("unsupported_on_printer")
        lift = ""
        if (state != "paused" and home_proven and _all_homed(status) and z is not None and z_high is not None
                and z < LIFT_Z_MM - 1e-6 <= z_high):   # paused: parked by PAUSE, and RESUME puts it back
            lift = f"G90\nG1 Z{LIFT_Z_MM:g} F600\n"
        var = "SET_GCODE_VARIABLE MACRO=_global_var VARIABLE=load_filament_extruder_temp VALUE="
        script = f"{lift}{var}{MATERIALS[params['material']][0]}\n{script}\n{var}{was:g}"
    elif action == "z_offset" and abs(z_tuned + params["delta"]) > Z_TUNE_MAX_MM + 1e-9:
        raise _Refused("z_offset_limit")
    return script


def _action_failure(err: BaseException) -> tuple[str, str]:
    """(directional reason, Klipper's message for the log) for a failed action. Klipper's own refusals are 400s that name
    themselves ("Must home axis first", "Move out of range"), mapped to the words the hub's own checks use (Klipper checks
    the same limits, against moves the hub cannot see coming)."""
    if isinstance(err, urllib.error.HTTPError):
        try:
            message = _extract_moonraker_error_message(err.read(8 * 1024).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            message = ""
        m = message.lower()
        if "must home" in m:
            return "not_homed", message
        if "out of range" in m:
            return "out_of_range", message
        if "shutdown" in m or "not ready" in m:
            return "klipper_error", message
        return ("gcode_error" if err.code == 400 else _classify_control_http_error(err.code, "")), message
    if isinstance(err, urllib.error.URLError):
        return _classify_url_error(err), ""
    return _classify_error(err), ""


class KlipperJobTracker:
    """Per-printer terminal-job emitter for Klipper. Same shape as the
    Bambu JobTracker — observes each poll's status dict, opens on first
    transition INTO 'printing', closes on transition INTO complete /
    cancelled / error / standby (Moonraker's terminal states).

    Stateful but lock-managed externally (the KlipperAdapter holds its own
    lock around observe + pending + ack). Dict / list ops, plus a durable
    save (state_path) whenever the terminal buffer changes.

    Job key is a sha256 of (printer_id, filename, started_ts_ms). Klipper
    doesn't expose a stable per-print task id like Bambu's MQTT task_id,
    so a fingerprint is the best we can do — millisecond resolution avoids
    same-second collisions on cancel→restart-of-same-file.
    """

    def __init__(self, printer_id: str, state_path: Optional[Path] = None):
        self.printer_id = printer_id
        self._active: Optional[dict] = None
        self._pending: list[dict] = []
        self._last_state: str = ""
        # Durability (owner rule: not RAM-only): unacked terminal jobs, plus the last print this tracker reported
        # ({filename, printDuration}) — without it every restart onto a printer still showing that print as complete
        # re-reports it under a NEW key (the key is built from monotonic time) and the cloud books it twice.
        self._state_path = state_path
        self._last_terminal: Optional[dict] = None
        self._seen_duration: Optional[float] = None
        # The print that was running when the state was last saved. One print must keep ONE key across a restart (the
        # cloud stamps hub_job_ref from it while printing and links the terminal by it): the first observation adopts it
        # when it is provably the same print, else drops it.
        self._restored: Optional[dict] = None
        self.unsaved = False   # the last save failed: retried on every observation
        self._load()
        # Track print_duration across polls so a printing→complete→printing
        # cycle that fits inside one 5s poll window is detected via the
        # reset (new print_duration < prior). Adversarial-review HIGH #2:
        # without this the cycle is silently elided and prior print time
        # bleeds into the new one — a billing dispute waiting to happen.
        self._last_print_duration: Optional[float] = None

    def observe(self, status: dict[str, Any], now: float) -> None:
        """Feed one poll's `status` dict (Moonraker's `result.status` shape)
        + a monotonic timestamp. Detects job transitions and accumulates
        terminal records into pending()."""
        print_stats = status.get("print_stats") if isinstance(status, dict) else None
        if not isinstance(print_stats, dict):
            # Pre-pushall / shape error — no signal yet.
            return
        state_raw = print_stats.get("state")
        if not isinstance(state_raw, str):
            return
        # Klipper-internal Latin name passthrough.
        state = state_raw.strip().lower()
        filename = _job_filename(print_stats)
        prev_state = self._last_state
        # `print_duration` is a monotonic counter Klipper resets to ~0 at
        # the start of every print. We track it across polls so a
        # printing→complete→printing cycle that lands inside one 5s poll
        # window is detected via the reset edge.
        print_duration = _num(print_stats.get("print_duration"))
        self._seen_duration = print_duration
        total = _num(print_stats.get("total_duration"))
        # When this print started, on the wall clock (only meaningful while it runs: total_duration freezes at the end).
        print_start = _wall(now) - total if total is not None and state in ("printing", "paused") else None
        if self.unsaved:
            self._save()
        if self._restored is not None:
            self._resume_or_drop(state, filename, print_start, total, now)

        if self._active is None:
            # paused opens too: a print paused before its first poll is still this print (and needs its key)
            if state in _ACTIVE_STATES or state == "paused":
                self._open(filename, now, print_start)
            elif prev_state == "" and state in _TERMINAL_DONE_STATES.union(
                _TERMINAL_FAILED_STATES
            ):
                # First signal already terminal — agent (re)started onto a
                # printer that finished while we were down. We have no
                # observed start so the time bounds are approximate; emit
                # so the cloud at least gets the FINISH record (cloud's
                # needs_review path catches zero-gram / null-times).
                self._emit_recovered(filename, state, now, print_stats)
        else:
            if state in _TERMINAL_DONE_STATES:
                self._close("done", now)
            elif state in _TERMINAL_FAILED_STATES:
                # Moonraker, unlike Bambu, says which: 'cancelled' is a person stopping it (the cloud's evidence-only
                # cancelled lane, member text "cancelled at the printer"); 'error' is a failure.
                self._close("cancelled" if state == "cancelled" else "failed", now)
            elif state in _ACTIVE_STATES:
                # Hidden-cycle detection (adversarial review HIGH #2): a
                # full print→complete→printing or printing→cancelled→printing
                # cycle inside ONE poll window would otherwise be silently
                # joined into one job (or misclassified). Klipper resets
                # print_duration on every new print, so if the current
                # duration is LOWER than what we last saw, a fresh print
                # started in between. Close the prior — as 'done' when the
                # filename matches (most common: re-print of the same file
                # after success) and 'cancelled' when it differs (abort +
                # restart with a different file) — and open a new one.
                duration_reset = (
                    self._last_print_duration is not None
                    and print_duration is not None
                    and print_duration + 1.0 < self._last_print_duration
                )
                filename_changed = (
                    filename
                    and self._active.get("name")
                    and filename != self._active["name"]
                )
                if duration_reset:
                    # If the filename also changed, the prior print was
                    # explicitly aborted (cancel + new file). If it's the
                    # same file, the operator re-printed it after the prior
                    # one finished — close as 'done', not 'cancelled'.
                    self._close("cancelled" if filename_changed else "done", now)
                    self._open(filename, now, print_start)
                elif filename_changed:
                    # Filename swap without a duration reset — we missed
                    # the gap between two prints; close old as cancelled.
                    self._close("cancelled", now)
                    self._open(filename, now, print_start)
                elif filename and not self._active.get("name"):
                    self._active["name"] = filename
                    self._save()
            elif state == "paused":
                # Paused doesn't open or close; just remember the state for
                # the next observe call.
                pass
            elif state == "standby":
                # Missed the real ending. Treat as cancelled — same
                # convention as the Bambu tracker.
                self._close("cancelled", now)

        # Track the last observed print_duration regardless of state — a
        # reset edge during paused→printing is also a cycle signal.
        if print_duration is not None:
            self._last_print_duration = print_duration

        self._last_state = state

    def pending(self) -> list[dict]:
        return list(self._pending)

    def active_key(self, filename: Optional[str] = None) -> Optional[str]:
        """The print running now (its jobKey), or None. With `filename`: only when the running print is that file."""
        if self._active is None or (filename is not None and self._active.get("name") != filename):
            return None
        return self._active["key"]

    def ack(self, job_keys: list[str]) -> None:
        keys = set(job_keys)
        before = len(self._pending)
        self._pending = [j for j in self._pending if j["jobKey"] not in keys]
        if len(self._pending) != before:
            self._save()

    # --- internals --------------------------------------------------------
    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        pending = raw.get("pending") if isinstance(raw.get("pending"), list) else []
        self._pending = [j for j in pending if isinstance(j, dict) and isinstance(j.get("jobKey"), str)][-MAX_PENDING_JOBS:]
        last = raw.get("lastTerminal")
        if isinstance(last, dict) and isinstance(last.get("filename"), str):
            self._last_terminal = {"filename": last["filename"], "printDuration": _num(last.get("printDuration"))}
        active = raw.get("active")
        if (isinstance(active, dict) and isinstance(active.get("key"), str) and isinstance(active.get("name"), str)
                and _num(active.get("startedWall")) is not None):
            self._restored = {"key": active["key"], "name": active["name"], "startedWall": _num(active["startedWall"]),
                              "printStart": _num(active.get("printStart"))}

    def _resume_or_drop(self, state: str, filename: Optional[str], print_start: Optional[float],
                        total: Optional[float], now: float) -> None:
        """First observation after a load: adopt the saved running print, or forget it."""
        saved, self._restored = self._restored, None
        if filename != saved["name"]:
            same = False
        elif state in ("printing", "paused"):
            same = (saved["printStart"] is not None and print_start is not None
                    and abs(saved["printStart"] - print_start) <= RESUME_START_SLACK_SEC)
        else:
            # It ended while the hub was down and the printer still shows it: close it under its own key (observe's normal
            # branch does, from this state). ponytail: a same-file re-print that ALSO finished while the hub was down is
            # booked under the saved key. Standby (reset, filename gone) is never adopted: its outcome is unknown, and the
            # queue's outcome_unknown_after_restart covers that, not an invented "cancelled".
            same = state in _TERMINAL_DONE_STATES.union(_TERMINAL_FAILED_STATES)
        if same:
            self._active = {"key": saved["key"], "name": saved["name"], "printStart": saved["printStart"],
                            "startedAt": now - (_wall(now) - saved["startedWall"])}
            if state not in ("printing", "paused") and saved["printStart"] is not None and total is not None:
                # it ended at its start + the frozen total_duration, not when the hub came back
                ended = now - (_wall(now) - (saved["printStart"] + total))
                if self._active["startedAt"] <= ended <= now:
                    self._active["endedAt"] = ended
            return
        log.info("klipper %s: %s is no longer the running print after the restart — its key is dropped", self.printer_id, saved["name"])
        self._save()

    def _save(self) -> None:
        if self._state_path is None:
            return
        running = None
        if self._active is not None and self._active.get("name"):
            running = {"key": self._active["key"], "name": self._active["name"],
                       "startedWall": _wall(self._active["startedAt"]), "printStart": self._active.get("printStart")}
        elif self._restored is not None:
            running = self._restored   # not observed yet since the load: keep it on disk
        state = json.dumps({"pending": self._pending, "lastTerminal": self._last_terminal, "active": running}, sort_keys=True)
        was_unsaved, self.unsaved = self.unsaved, not write_json_durably(self._state_path, state)
        if self.unsaved and not was_unsaved:
            # A terminal cannot be refused (the print already ended); loud once so the journal shows it. Retried every poll.
            log.error("klipper %s: terminal jobs not durable (RAM-only until a save succeeds)", self.printer_id)

    def _open(self, filename: Optional[str], now: float, print_start: Optional[float] = None) -> None:
        self._active = {
            "key": _job_key(self.printer_id, filename, now),
            "name": filename,
            "startedAt": now,
            "printStart": print_start,
        }
        self._save()   # a restart mid-print must find this key again

    def _close(self, status: str, now: float) -> None:
        if self._active is None:
            return
        now = self._active.get("endedAt", now)
        job = {
            "jobKey": self._active["key"],
            "printerId": self.printer_id,
            "status": status,
            "startedAt": _iso(self._active["startedAt"]),
            "endedAt": _iso(now),
            "printTimeSeconds": max(0, int(now - self._active["startedAt"])),
        }
        if self._active.get("name"):
            job["filename"] = self._active["name"]
            self._last_terminal = {"filename": self._active["name"], "printDuration": self._seen_duration}
        self._active = None   # before the save in _buffer: the closed print is no longer the running one
        self._buffer(job)

    def _emit_recovered(
        self,
        filename: Optional[str],
        state: str,
        now: float,
        print_stats: dict[str, Any],
    ) -> None:
        """Agent (re)started onto an already-terminal printer. We have no
        observed start, but Klipper's `print_stats.print_duration` is the
        last print's wall-clock duration in seconds — derive
        printTimeSeconds + back-compute startedAt from it so the cloud
        sees real bounds (adversarial review MED #10). When duration is
        missing, fall back to zero + endedAt=now (the cloud's needs_review
        path then catches the zero-length record)."""
        if not filename:
            # Without a name we'd be inventing both bounds — too much
            # speculation. Skip; the printer's own history covers it.
            log.info(
                "klipper %s: terminal recovery with no filename — skipping",
                self.printer_id,
            )
            return
        status_out = "done" if state in _TERMINAL_DONE_STATES else ("cancelled" if state == "cancelled" else "failed")
        print_dur = _num(print_stats.get("print_duration"))
        last = self._last_terminal
        if last is not None and last["filename"] == filename and _same_duration(last["printDuration"], print_dur):
            log.info("klipper %s: %s was already reported before the restart — not again", self.printer_id, filename)
            return
        self._last_terminal = {"filename": filename, "printDuration": print_dur}
        if print_dur is not None and print_dur > 0:
            print_time_seconds = int(print_dur)
            started_at = now - print_dur
        else:
            print_time_seconds = 0
            started_at = now
        job = {
            "jobKey": _job_key(self.printer_id, filename, started_at),
            "printerId": self.printer_id,
            "status": status_out,
            "startedAt": _iso(started_at),
            "endedAt": _iso(now),
            "printTimeSeconds": print_time_seconds,
            "filename": filename,
        }
        self._buffer(job)

    def _buffer(self, job: dict) -> None:
        self._pending.append(job)
        if len(self._pending) > MAX_PENDING_JOBS:
            dropped = self._pending[: len(self._pending) - MAX_PENDING_JOBS]
            self._pending = self._pending[-MAX_PENDING_JOBS:]
            log.error(
                "klipper job buffer overflow on %s — dropped %d unacked job(s)",
                self.printer_id,
                len(dropped),
            )
        self._save()


def _same_duration(a: Optional[float], b: Optional[float]) -> bool:
    """print_duration freezes when a print ends, so the same print reads the same after a restart (1 s of slack)."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1.0


def _job_filename(print_stats: dict[str, Any]) -> Optional[str]:
    fn = print_stats.get("filename")
    if isinstance(fn, str) and fn.strip():
        return fn.strip()[:JOB_NAME_MAX_CHARS]
    return None


def _job_key(printer_id: str, filename: Optional[str], started_ts: float) -> str:
    """Stable per-(printer, file, start-ms) fingerprint. Same shape namespace
    as the Bambu tracker's: prefix `task_` is reserved for printers that
    supply their own task id, so we use `fp_` here (Klipper has none)."""
    basis = f"klipper|{printer_id}|{filename or '?'}|{started_ts:.3f}"
    return "fp_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _wall(monotonic_ts: float) -> float:
    return time.time() - (time.monotonic() - monotonic_ts)


def _iso(monotonic_ts: float) -> str:
    """`time.monotonic` is not wall-clock — convert to ISO via wall time at
    *now* offset by the monotonic delta. Resolution is per-second since
    the cloud doesn't care about sub-second; same as the Bambu tracker."""
    wall = _wall(monotonic_ts)
    # 2026-06-16T22:30:00Z shape.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall))
