"""Commands for a displayed print must never reach its replacement (review 2026-10-27)."""

import json
import types
from unittest.mock import Mock

import pytest

from makeros_hub.agent import heartbeat_payload
from makeros_hub.printers.bambu import BambuAdapter
from makeros_hub.printers.klipper import KlipperAdapter
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.queue_progress import QueueProgressTracker

EXPECT = {"jobId": "job-a", "assignmentSeq": 3}
REFUSAL = "the print it was for is no longer on the printer"


def setup_adapter(vendor):
    if vendor == "bambu":
        adapter = BambuAdapter("p1", host="127.0.0.1", serial="S", access_code="x")
        adapter._connack = "ok"
        def reading(state, name, run):
            doc = {"print": {"gcode_state": state, "subtask_name": name, "task_id": run}}
            adapter._on_message(None, None, types.SimpleNamespace(payload=json.dumps(doc).encode()))
    else:
        adapter = KlipperAdapter("p1", "http://127.0.0.1:1")
        def reading(state, name, run):
            state = {"RUNNING": "printing", "FINISH": "complete"}.get(state, state)
            adapter._record_poll({"print_stats": {"state": state, "filename": name, "print_duration": 5}})
    adapter.send_command = Mock(return_value={"ok": True})
    adapter.fresh_status = Mock(side_effect=adapter.status)
    adapter._queue_progress.record_dispatch("job-a", [], task_name="a.gcode", assignment_seq=3)
    reading("RUNNING", "a.gcode", "1")
    manager = PrinterManager()
    manager._adapters["p1"] = adapter
    return adapter, reading, manager


def item(command="stop", params=None, **extra):
    return {"requestId": "r1", "printerId": "p1", "command": command, "params": params, **extra}


@pytest.mark.parametrize("vendor", ["bambu", "klipper"])
@pytest.mark.parametrize("command,params", [("skip_objects", {"objList": [1]}), ("stop", None),
                                            ("klipper_action", {"action": "emergency_stop"})])
def test_matching_assignment_reaches_adapter_without_expect_in_params(vendor, command, params):
    adapter, _, manager = setup_adapter(vendor)
    assert manager.dispatch_commands([item(command, params, expect=EXPECT)])[0]["status"] == "ok"
    adapter.send_command.assert_called_once_with(command, params)


@pytest.mark.parametrize("vendor", ["bambu", "klipper"])
@pytest.mark.parametrize("replacement", ["other.gcode", "a.gcode"])
@pytest.mark.parametrize("command,params", [("skip_objects", {"objList": [1]}), ("stop", None),
                                            ("klipper_action", {"action": "emergency_stop"})])
def test_replacement_run_refuses_even_when_old_assignment_still_pending(vendor, replacement, command, params):
    adapter, reading, manager = setup_adapter(vendor)
    reading("FINISH", "a.gcode", "1")
    reading("RUNNING", replacement, "2")
    assert adapter.pending_queue_jobs() == [{"queueJobId": "job-a", "assignmentSeq": 3}]
    report = manager.dispatch_commands([item(command, params, expect=EXPECT)])[0]
    assert report["status"] == "failed" and report["detail"] == REFUSAL
    adapter.send_command.assert_not_called()


@pytest.mark.parametrize("expect", [None, {}, "job-a", {"jobId": "job-a", "assignmentSeq": True},
                                    {"jobId": "job-a", "assignmentSeq": 4}, {"jobId": "job-b", "assignmentSeq": 3}])
def test_missing_or_wrong_assignment_refuses(expect):
    adapter, _, manager = setup_adapter("bambu")
    assert manager.dispatch_commands([item(expect=expect)])[0]["detail"] == REFUSAL
    adapter.send_command.assert_not_called()


def test_staff_machine_command_and_unknown_envelope_keys_keep_working():
    adapter, _, manager = setup_adapter("klipper")
    assert manager.dispatch_commands([item(futureEnvelope={"anything": True})])[0]["status"] == "ok"
    adapter.send_command.assert_called_once_with("stop", None)


def test_absent_adapter_or_identity_checker_refuses_bound_commands():
    manager = PrinterManager()
    assert manager.dispatch_commands([item(expect=EXPECT)])[0]["detail"] == REFUSAL
    manager._adapters["p1"] = types.SimpleNamespace(send_command=Mock())
    assert manager.dispatch_commands([item(expect=EXPECT)])[0]["detail"] == REFUSAL
    manager._adapters["p1"].send_command.assert_not_called()


def test_saved_run_binding_survives_restart_but_unproven_old_records_fail_closed():
    adapter, _, _ = setup_adapter("bambu")
    saved = adapter._queue_progress.to_state()
    active = adapter._jobs.active_key()
    restored = QueueProgressTracker()
    restored.load_state(saved)
    assert restored.matches_expect(EXPECT, active)
    assert not restored.matches_expect(EXPECT, "replacement")
    del saved["dispatches"][0]["activeKey"]
    restored.load_state(saved)
    restored.observe_active("a.gcode", active, [])
    assert not restored.matches_expect(EXPECT, active)


def test_started_survives_manager_heartbeat_and_redelivery_after_restart():
    adapter, _, manager = setup_adapter("klipper")
    adapter.send_command.return_value = {"ok": True, "detail": "started"}
    command = item("klipper_action", {"action": "home"})
    report = manager.dispatch_commands([command])
    assert report == [{"requestId": "r1", "command": "klipper_action", "status": "ok", "detail": "started"}]
    assert heartbeat_payload(command_results=report)["commandResults"] == report
    assert PrinterManager().dispatch_commands([command]) == report
    adapter.send_command.assert_called_once()


def test_late_bambu_task_id_keeps_the_same_run_bound():
    adapter = BambuAdapter("late", host="127.0.0.1", serial="S", access_code="x")
    adapter._connack = "ok"
    adapter._queue_progress.record_dispatch("job-a", [], task_name="a.gcode", assignment_seq=3)
    for task in ("0", "123"):
        doc = {"print": {"gcode_state": "RUNNING", "subtask_name": "a.gcode", "task_id": task}}
        adapter._on_message(None, None, types.SimpleNamespace(payload=json.dumps(doc).encode()))
        assert adapter.matches_expect(EXPECT)


@pytest.mark.parametrize("vendor", ["bambu", "klipper"])
@pytest.mark.parametrize("command,params", [("skip_objects", {"objList": [1]}), ("stop", None)])
def test_fresh_read_replaces_cached_assignment_before_dispatch(vendor, command, params):
    adapter, reading, manager = setup_adapter(vendor)
    assert adapter.matches_expect(EXPECT)
    def refresh():
        reading("FINISH", "a.gcode", "1")
        reading("RUNNING", "b.gcode", "2")
        return adapter.status()
    adapter.fresh_status.side_effect = refresh
    report = manager.dispatch_commands([item(command, params, expect=EXPECT)])[0]
    assert report["detail"] == REFUSAL
    adapter.send_command.assert_not_called()


@pytest.mark.parametrize("fresh", [None, {}, {"connectionState": "connected"},
                                   {"connectionState": "connected", "jobName": "b.gcode"}])
def test_bound_stop_without_a_fresh_matching_name_refuses(fresh):
    adapter, _, manager = setup_adapter("bambu")
    adapter.fresh_status.side_effect = None
    adapter.fresh_status.return_value = fresh
    assert manager.dispatch_commands([item(expect=EXPECT)])[0]["detail"] == REFUSAL
    adapter.send_command.assert_not_called()


def test_bound_emergency_checks_assignment_without_waiting_for_poll():
    adapter, _, manager = setup_adapter("klipper")
    adapter._last_poll_ok_at = None
    adapter.fresh_status.side_effect = AssertionError("emergency never waits for telemetry")
    assert manager.dispatch_commands([item("klipper_action", {"action": "emergency_stop"}, expect=EXPECT)])[0]["status"] == "ok"
    adapter.fresh_status.assert_not_called()


@pytest.mark.parametrize("vendor", ["bambu", "klipper"])
def test_actual_adapter_refresh_updates_identity_before_bound_stop(vendor):
    adapter, _, manager = setup_adapter(vendor)
    del adapter.fresh_status
    if vendor == "bambu":
        def publish(topic, payload):
            doc = {"print": {"gcode_state": "RUNNING", "subtask_name": "b.gcode", "task_id": "2"}}
            adapter._on_message(None, None, types.SimpleNamespace(payload=json.dumps(doc).encode()))
            return types.SimpleNamespace(rc=0)
        adapter._client = types.SimpleNamespace(publish=publish)
    else:
        adapter._fetch_once = Mock(return_value={"print_stats": {"state": "printing", "filename": "b.gcode", "print_duration": 1}})
    assert manager.dispatch_commands([item(expect=EXPECT)])[0]["detail"] == REFUSAL
    adapter.send_command.assert_not_called()


def test_bound_emergency_goes_on_a_held_printer_but_never_into_another_run():
    adapter, reading, manager = setup_adapter("klipper")
    reading("error", "a.gcode", "1")   # the member's print failed and is held: nothing runs, so the stop goes
    stop = item("klipper_action", {"action": "emergency_stop"}, expect={"jobId": "job-b", "assignmentSeq": 9})
    assert manager.dispatch_commands([stop])[0]["status"] == "ok"
    adapter2, reading2, manager2 = setup_adapter("klipper")   # another run is printing: refused
    stop2 = {**stop, "requestId": "r2"}   # a new request: r1 is remembered as sent, so its redelivery re-reports ok
    assert manager2.dispatch_commands([stop2])[0] == {"requestId": "r2", "command": "klipper_action", "status": "failed", "detail": REFUSAL}
    adapter2.send_command.assert_not_called()
