from makeros_hub.printers.queue_progress import QueueProgressTracker
from makeros_hub.printers.jobs import JobTracker
from makeros_hub.printers.klipper import _klipper_failure
from test_v069 import boot, report


def test_only_the_matching_start_reply_is_recorded_and_survives_restart():
    a = boot()
    report(a, gcode_state="IDLE")
    a._queue_progress.record_dispatch("synthetic-job", [], assignment_seq=7, start_sequence="42")
    report(a, command="project_file", sequence_id="wrong", result="fail", reason=9)
    assert a._queue_progress.to_state()["dispatches"][0]["startReply"] == {"commandReply": "unconfirmed"}
    report(a, command="project_file", sequence_id="42", result="FAIL", reason=9)
    saved = boot()._queue_progress.to_state()["dispatches"][0]
    assert saved["startReply"] == {"commandReply": "refused", "replyCode": "9"}
    a._queue_progress._start_timeout_sec = 0
    (r,) = a.collect_queue_progress()
    assert r["reason"] == "start_refused"
    assert r["startEvidence"]["commandReply"] == "refused"
    assert r["startEvidence"]["started"] is False
    assert r["startEvidence"]["gcodeState"] == "IDLE"
    assert isinstance(r["startEvidence"]["sampledAt"], int)


def test_running_between_beats_is_never_a_never_start_even_when_the_end_is_missing():
    t = QueueProgressTracker(start_timeout_sec=0)
    t.record_dispatch("synthetic-job", [], now=0, assignment_seq=7)
    t.observe_running("RUNNING")
    assert t.collect([], "IDLE", now=1)[0]["state"] == "printing"
    assert t.collect([], "IDLE", now=2)[0]["reason"] == "outcome_unknown"
    assert t.pending_queue_jobs() == []


def test_an_unobserved_restart_is_unknown_not_permission_to_retry():
    t = QueueProgressTracker(start_timeout_sec=0)
    t.record_dispatch("synthetic-job", [], now=0, assignment_seq=7)
    u = QueueProgressTracker(start_timeout_sec=0)
    u.load_state(t.to_state())
    assert u.collect([], "IDLE")[0]["reason"] == "outcome_unknown_after_restart"


def test_bambu_terminal_keeps_fault_and_hms_with_the_print_through_restart(tmp_path):
    path = tmp_path / "terminal.json"
    j = JobTracker("synthetic-printer", "synthetic-serial", state_path=path)
    active = {"print": {"gcode_state": "RUNNING", "subtask_name": "synthetic-cube", "task_id": "7"}}
    j.observe(active, 1000)
    fault = {"print": {**active["print"], "gcode_state": "PAUSE", "print_error": 0x03008008,
                       "hms": [{"attr": 50332160, "code": 65545}]}}
    j.observe(fault, 1010)
    j = JobTracker("synthetic-printer", "synthetic-serial", state_path=path)
    j.observe({"print": {**active["print"], "gcode_state": "FAILED", "print_error": 0}}, 1020)
    again = JobTracker("synthetic-printer", "synthetic-serial", state_path=path)
    (done,) = again.pending()
    assert done["failure"]["printError"] == 0x03008008
    assert done["failure"]["hms"] == fault["print"]["hms"]


def test_klipper_reason_never_contains_a_members_file_or_command():
    assert _klipper_failure({"message": "Heater extruder not heating at expected rate"}) == {"kind": "heater"}
    assert _klipper_failure({"message": "No trigger on z after full movement"}) == {"kind": "homing"}
    assert _klipper_failure({"message": "Error in synthetic-private-file.gcode"}) == {"kind": "error"}
