"""The print-start guard: prints started at the printer, not through the site (operator, 2026-10-26: "I would prefer people
re runs to come through the site and that we just cancel prints on the printer that didnt come through the site. this
would stop people bypassing the site."; "but i odnt want to cancel her print").

The SITE decides; this hub only notices and checks. Each heartbeat the manager feeds every connected printer's status here.
A start is reported (heartbeat `printerStarts`) only on an idle -> busy edge this process saw, with no dispatch of the hub's
own pending on that printer. The site answers with a `guard_stop` command naming the report's id when its switch is on and
no staff allowance covers it; check() runs every rule again on a fresh status before the existing stop is sent. Nothing here
ever stops a print by itself: no site, no switch, no command, no stop.

In memory only, on purpose: a restart grandfathers whatever is running (its first observation is already busy, so there is
no edge), which is exactly the rule for prints running when this version ships or after an outage of the hub itself.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

# The same set as manager.BUSY_GCODE_STATES (not imported: the manager imports this module)
BUSY_GCODE_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE"})
MAX_REPORTS = 16


def _connected(st: Any) -> bool:
    return isinstance(st, dict) and st.get("connectionState") == "connected"


def busy(st: Any) -> bool:
    """Connected and preparing, printing or paused, in the printer's own words."""
    return _connected(st) and (str(st.get("gcodeState", "")).upper() in BUSY_GCODE_STATES
                               or st.get("state") in ("printing", "paused") or st.get("preparing") is True)


def still_starting(st: Any) -> bool:
    """Whether a print is still starting, so a stop costs nothing printed. On the normalized status, both vendors:

    connected, and either
      - `preparing` is True: Bambu gcode_state PREPARE (download, heat-up, homing, calibration before the print), or Klipper
        print_stats "printing" with print_duration 0 (still inside START_PRINT: soak, home, QGL, mesh, before the first
        extrusion); or
      - `state` is "printing", the layer is not reported or is at most 1 (layer_num / SET_PRINT_STATS_INFO current_layer),
        and progressPct is a real number no higher than 1.0 (Bambu mc_percent; Klipper M73 or byte progress).
    Paused, idle, error, an unknown progress or a lost connection is never starting: the guard fails open."""
    if not _connected(st):
        return False
    if st.get("preparing") is True:
        return True
    if st.get("state") != "printing":
        return False
    layer, pct = st.get("layer"), st.get("progressPct")
    first = layer is None or (isinstance(layer, list) and len(layer) == 2 and type(layer[0]) is int and layer[0] <= 1)
    return first and type(pct) in (int, float) and pct <= 1.0


def _name(st: dict) -> str | None:
    n = st.get("jobName")
    return n.strip()[:300] if isinstance(n, str) and n.strip() else None


class StartGuard:
    def __init__(self) -> None:
        self._last: dict[str, str] = {}             # printerId -> "idle" | "busy", the last connected observation
        self._starts: dict[str, dict] = {}          # printerId -> the start reported for its current run

    def observe(self, pid: str, st: Any, *, dispatching: bool, needs_gcode_state: bool = False) -> None:
        """One heartbeat's status. `dispatching`: the adapter holds a pending dispatch of the hub's own (its print).
        `needs_gcode_state` (Bambu): only a status carrying the printer's gcode_state counts, so a partial report before the
        connect-time pushall never reads as idle and turns a print running at hub start into a false edge."""
        if not _connected(st) or (needs_gcode_state and "gcodeState" not in st):
            return   # no fresh word from the printer: the last observation stands (an MQTT drop changes nothing)
        now_busy = busy(st)
        prev = self._last.get(pid)
        self._last[pid] = "busy" if now_busy else "idle"
        if not now_busy:
            self._starts.pop(pid, None)   # the run ended, reported or not
            return
        entry = self._starts.get(pid)
        if prev == "idle":
            if dispatching:
                self._starts.pop(pid, None)   # the hub's own print: never reported
                return
            entry = {"id": uuid.uuid4().hex, "printerId": pid, "fileName": _name(st), "seenAt": int(time.time() * 1000), "acked": False}
            self._starts[pid] = entry
        if entry is not None:
            entry["starting"] = still_starting(st)
            if entry["fileName"] is None and not entry["acked"]:
                entry["fileName"] = _name(st)   # Bambu names the file a moment into PREPARE

    def reports(self) -> list[dict]:
        return [{k: e[k] for k in ("id", "printerId", "fileName", "starting", "seenAt")}
                for e in self._starts.values() if not e["acked"]][:MAX_REPORTS]

    def ack(self, ids: Any) -> None:
        if not isinstance(ids, list):
            return
        keep = {i for i in ids if isinstance(i, str)}
        for e in self._starts.values():
            if e["id"] in keep:
                e["acked"] = True

    def forget(self, pid: str) -> None:
        self._last.pop(pid, None)
        self._starts.pop(pid, None)

    def check(self, pid: str, start_id: Any, st: Any, *, dispatching: bool) -> str | None:
        """Why a site guard_stop must not run on this fresh status, or None when it may."""
        e = self._starts.get(pid)
        if e is None or not isinstance(start_id, str) or e["id"] != start_id:
            return "unknown_start"
        if not _connected(st):
            return "not_connected"
        if not busy(st):
            return "print_ended"
        name = _name(st)
        if name and e["fileName"] and name != e["fileName"]:
            return "other_print"
        if dispatching:
            return "dispatched"
        if not still_starting(st):
            return "past_start"
        return None
