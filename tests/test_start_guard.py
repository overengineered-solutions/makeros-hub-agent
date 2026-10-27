"""The print-start guard (start_guard.py): reported only on an edge this process saw, never the hub's own print, stopped only
on the site's guard_stop and only while the print is still starting."""

from makeros_hub.agent import heartbeat_payload
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.start_guard import still_starting


class FakePrinter:
    def __init__(self, st, pending=()):
        self.st, self.pending, self.sent = st, list(pending), []

    def status(self):
        return dict(self.st)

    def pending_queue_job_ids(self):
        return list(self.pending)

    def send_command(self, command, params=None):
        self.sent.append((command, params))
        return {"ok": True}


IDLE_B = {"connectionState": "connected", "state": "idle", "gcodeState": "IDLE"}
PREP_B = {"connectionState": "connected", "state": "idle", "gcodeState": "PREPARE", "preparing": True, "jobName": "cube.3mf"}
DEEP_B = {"connectionState": "connected", "state": "printing", "gcodeState": "RUNNING", "layer": [5, 80], "progressPct": 12, "jobName": "cube.3mf"}
IDLE_K = {"connectionState": "connected", "state": "idle", "preparing": False}
SOAK_K = {"connectionState": "connected", "state": "printing", "preparing": True, "jobName": "part.gcode", "progressPct": 0.0}
DEEP_K = {"connectionState": "connected", "state": "printing", "preparing": False, "printDurationSec": 300, "progressPct": 0.4,
          "layer": [3, 200], "jobName": "part.gcode"}
OFFLINE = {"connectionState": "offline"}


def setup(st, bambu=True, pending=()):
    m = PrinterManager()
    p = FakePrinter(st, pending)
    m._adapters["p1"] = p
    if bambu:
        m._bambu_configs["p1"] = {"id": "p1", "vendor": "bambu"}
    return m, p


def beat(m, p, st):
    p.st = st
    m.statuses()
    return m.start_guard.reports()


def guard_stop(m, start_id, rid="r1"):
    return m.dispatch_commands([{"requestId": rid, "printerId": "p1", "command": "guard_stop", "params": {"startId": start_id}}])[0]


def test_still_starting_rule():
    c = {"connectionState": "connected"}
    assert still_starting(PREP_B) and still_starting(SOAK_K)
    assert still_starting({**c, "state": "printing", "layer": [1, 50], "progressPct": 0})
    assert still_starting({**c, "state": "printing", "progressPct": 0.5})   # no layer reported
    assert not still_starting({**c, "state": "printing", "layer": [2, 50], "progressPct": 0})
    assert not still_starting({**c, "state": "printing", "layer": [1, 50], "progressPct": 1.5})
    assert not still_starting({**c, "state": "paused", "layer": [0, 50], "progressPct": 0})
    assert not still_starting({**c, "state": "printing", "layer": [0, 50]})   # unknown progress: fail open
    assert not still_starting({**PREP_B, "connectionState": "offline"})
    assert not still_starting(DEEP_B) and not still_starting(DEEP_K)


def test_walk_up_reported_acked_and_stopped_once():
    for idle, start, bambu in ((IDLE_B, PREP_B, True), (IDLE_K, SOAK_K, False)):
        m, p = setup(idle, bambu)
        assert beat(m, p, idle) == []
        [r] = beat(m, p, start)
        assert r["starting"] is True and r["fileName"] == start["jobName"] and r["printerId"] == "p1" and len(r["id"]) == 32
        assert beat(m, p, start) == [r]   # resent until acked, same id
        m.start_guard.ack([r["id"]])
        assert beat(m, p, start) == []
        rid = f"r-{bambu}"   # the command guard persists across managers in one test
        assert guard_stop(m, r["id"], rid) == {"requestId": rid, "command": "guard_stop", "status": "ok"}
        assert p.sent == [("stop", None)]
        assert guard_stop(m, r["id"], rid)["status"] == "ok" and p.sent == [("stop", None)]   # redelivery: no second stop


def test_past_start_is_never_stopped():
    for idle, deep, bambu in ((IDLE_B, DEEP_B, True), (IDLE_K, DEEP_K, False)):
        m, p = setup(idle, bambu)
        beat(m, p, idle)
        [r] = beat(m, p, deep)
        assert r["starting"] is False
        assert guard_stop(m, r["id"])["detail"] == "past_start" and p.sent == []


def test_a_start_that_moved_on_before_the_stop_is_not_stopped():
    m, p = setup(IDLE_B)
    beat(m, p, IDLE_B)
    [r] = beat(m, p, PREP_B)
    p.st = DEEP_B   # the stop arrives after the first layer
    assert guard_stop(m, r["id"])["detail"] == "past_start" and p.sent == []


def test_running_at_hub_start_is_grandfathered():
    m, p = setup(PREP_B)
    assert beat(m, p, PREP_B) == [] and beat(m, p, DEEP_B) == []
    assert guard_stop(m, "0" * 32)["detail"] == "unknown_start" and p.sent == []


def test_partial_bambu_report_before_pushall_is_not_an_idle_observation():
    m, p = setup({"connectionState": "connected", "state": "idle"})   # a delta without gcode_state, right after connect
    beat(m, p, {"connectionState": "connected", "state": "idle"})
    assert beat(m, p, DEEP_B) == []   # the print that was running all along


def test_hub_dispatched_print_is_never_reported_or_stopped():
    m, p = setup(IDLE_B, pending=["job-1"])
    beat(m, p, IDLE_B)
    assert beat(m, p, PREP_B) == []
    assert guard_stop(m, "0" * 32)["detail"] == "unknown_start" and p.sent == []
    # a walk-up reported, then a dispatch of the hub's own appears before the stop: refused
    m, p = setup(IDLE_B)
    beat(m, p, IDLE_B)
    [r] = beat(m, p, PREP_B)
    p.pending = ["job-2"]
    assert guard_stop(m, r["id"])["detail"] == "dispatched" and p.sent == []


def test_nothing_stops_without_the_sites_command():
    m, p = setup(IDLE_B)
    for st in (IDLE_B, PREP_B, PREP_B, IDLE_B, PREP_B, DEEP_B, IDLE_B, PREP_B) * 3:
        beat(m, p, st)
    assert p.sent == []


def test_other_print_and_print_ended_refused():
    m, p = setup(IDLE_B)
    beat(m, p, IDLE_B)
    [r] = beat(m, p, PREP_B)
    p.st = {**PREP_B, "jobName": "other.3mf"}
    assert guard_stop(m, r["id"], "a")["detail"] == "other_print"
    p.st = IDLE_B
    assert guard_stop(m, r["id"], "b")["detail"] == "print_ended"
    p.st = OFFLINE
    assert guard_stop(m, r["id"], "c")["detail"] == "not_connected"
    assert p.sent == []


def test_entry_dropped_on_idle_and_outage_keeps_the_last_observation():
    m, p = setup(IDLE_B)
    beat(m, p, IDLE_B)
    [r] = beat(m, p, PREP_B)
    assert beat(m, p, IDLE_B) == []
    assert guard_stop(m, r["id"])["detail"] == "unknown_start"
    # idle, then the connection drops, a walk-up starts meanwhile, reconnect shows it far along: reported, not starting
    beat(m, p, OFFLINE)
    [r2] = beat(m, p, DEEP_B)
    assert r2["starting"] is False and r2["id"] != r["id"]
    # busy before a drop, busy after: no edge
    m, p = setup(IDLE_B)
    beat(m, p, IDLE_B)
    beat(m, p, DEEP_B)
    m.start_guard.ack([x["id"] for x in m.start_guard.reports()])
    beat(m, p, OFFLINE)
    assert beat(m, p, DEEP_B) == []


def test_guard_failure_never_sinks_a_status_read():
    m, p = setup(IDLE_B)

    def boom():
        raise RuntimeError("x")
    p.pending_queue_job_ids = boom
    assert m.statuses()[0]["gcodeState"] == "IDLE"


def test_heartbeat_carries_printer_starts():
    assert heartbeat_payload([], [])["printerStarts"] == []
    r = {"id": "a" * 32, "printerId": "p1", "fileName": None, "starting": True, "seenAt": 1}
    assert heartbeat_payload([], [], printer_starts=[r] * 20)["printerStarts"] == [r] * 16
