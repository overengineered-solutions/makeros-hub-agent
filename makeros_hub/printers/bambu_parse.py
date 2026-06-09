"""Pure Bambu LAN report parsing — NO paho, NO I/O, so it is fully unit-testable
on a dev box without a printer.

Bambu printers in LAN/Developer mode stream their state on the MQTT topic
`device/<serial>/report`. Messages are PARTIAL DELTAS, not full snapshots: after
the initial `pushall` snapshot the printer sends small objects with only changed
keys. We keep a merged state dict and deep-merge each frame into it, then read
the handful of telemetry fields we care about out of the merged `print` object.

We deliberately own this parse (rather than depend on pybambu/bambulabs_api) so a
firmware field rename degrades to a missing-optional instead of crashing the
agent, and so we can emit the repo's shape_observed / iterated observability.

Field references (verified against ha-bambulab/pybambu + bambulabs_api):
  print.gcode_state      job lifecycle: IDLE/PREPARE/RUNNING/PAUSE/FINISH/FAILED
  print.mc_percent       0-100 progress (the reliable signal; never bill off grams)
  print.mc_remaining_time MINUTES remaining (NOT seconds)
  print.subtask_name     current job/plate name; fallback print.gcode_file
  print.nozzle_temper    nozzle temp C ; print.bed_temper  bed temp C
"""

from __future__ import annotations

from typing import Any

# Bambu gcode_state -> our normalized printer activity state. The cloud column
# enum is idle|printing|paused|error|offline.
_GCODE_STATE = {
    "RUNNING": "printing",
    "PAUSE": "paused",
    "PREPARE": "idle",
    "SLICING": "idle",
    "INIT": "idle",
    "IDLE": "idle",
    "FINISH": "idle",  # completed OK — printer is now idle/awaiting clear
    "FAILED": "error",
    "OFFLINE": "offline",
    "UNKNOWN": "idle",
}


def map_activity_state(gcode_state: Any) -> str:
    if not isinstance(gcode_state, str):
        return "idle"
    return _GCODE_STATE.get(gcode_state.upper(), "idle")


def merge_report(state: dict, delta: dict) -> dict:
    """Deep-merge a report delta into the running state dict, in place.

    Recursive on nested dicts (so a partial `print` delta updates only its
    changed keys); scalar/list values overwrite. Returns `state` for chaining.
    """
    for k, v in delta.items():
        if isinstance(v, dict) and isinstance(state.get(k), dict):
            merge_report(state[k], v)
        else:
            state[k] = v
    return state


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def normalize_status(
    printer_id: str,
    merged: dict,
    *,
    connection_state: str,
    error_reason: str | None = None,
) -> dict:
    """Build the wire DTO the cloud heartbeat expects (PrinterStatusDTO).

    `merged` is the deep-merged report state (telemetry under merged['print']).
    Only set keys are included — the cloud DTO is strict() and rejects unknown
    keys AND rejects an explicit null for an optional number, so omit-when-absent
    is required. NEVER include the serial / IP / access code (telemetry only).
    """
    out: dict[str, Any] = {"printerId": printer_id, "connectionState": connection_state}
    if connection_state == "error" and error_reason:
        out["errorReason"] = error_reason

    print_obj = merged.get("print") if isinstance(merged.get("print"), dict) else {}

    # Activity state only meaningful once we're actually connected.
    if connection_state == "connected":
        out["state"] = map_activity_state(print_obj.get("gcode_state"))

    pct = _num(print_obj.get("mc_percent"))
    if pct is not None:
        out["progressPct"] = max(0.0, min(100.0, pct))

    nozzle = _num(print_obj.get("nozzle_temper"))
    if nozzle is not None:
        out["nozzleTempC"] = nozzle
    bed = _num(print_obj.get("bed_temper"))
    if bed is not None:
        out["bedTempC"] = bed

    name = print_obj.get("subtask_name") or print_obj.get("gcode_file")
    if isinstance(name, str) and name.strip():
        out["jobName"] = name.strip()[:300]

    eta = _num(print_obj.get("mc_remaining_time"))  # MINUTES
    if eta is not None:
        out["etaMinutes"] = int(eta)

    return out


def summarize_shape(merged: dict) -> dict:
    """Redacted shape summary for the one-time `bambu.shape_observed` log — key
    names + array lengths only, never values (no serial/IP/material leak)."""
    print_obj = merged.get("print") if isinstance(merged.get("print"), dict) else {}
    array_lengths = {k: len(v) for k, v in print_obj.items() if isinstance(v, list)}
    return {
        "topLevelKeys": sorted(merged.keys()),
        "printKeys": sorted(print_obj.keys()),
        "arrayLengths": array_lengths,
        "values": "[redacted]",
    }
