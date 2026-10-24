import json
from unittest import mock

import pytest

from makeros_hub.printers import bambu
from makeros_hub.printers.klipper import KlipperJobTracker
from test_slicer_wrapper import ps


def test_orca_warnings_keep_plate_identity_and_reject_bad_shapes():
    assert ps.plate_warnings({"sliced_plates": [
        {"id": 1, "warning_message": "floating regions"}, {"id": 2, "warning_message": ""},
        {"id": 3, "warning_message": "too thin"},
    ]}) == [{"plate": 1, "message": "floating regions"}, {"plate": 3, "message": "too thin"}]
    for raw in ({}, {"sliced_plates": []}, {"sliced_plates": [{"id": 1, "warning_message": []}]},
                {"sliced_plates": [{"id": True, "warning_message": "x"}]}):
        with pytest.raises(ps.SliceError):
            ps.plate_warnings(raw)


def test_slicer_main_preserves_the_warnings_even_when_a_prepared_slice_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "JOB", str(tmp_path))
    monkeypatch.setattr(ps, "caps", lambda: {})
    out = tmp_path / "out"
    out.mkdir()
    (out / "result.json").write_text(json.dumps({"sliced_plates": [{"id": 1, "warning_message": "floating regions"}]}))
    monkeypatch.setattr(ps, "run", mock.Mock(side_effect=ps.SliceError("The slice still has missing or floating parts.")))
    assert ps.main(["ps_slice.py"]) == 3
    result = json.loads((out / "result.json").read_text())
    assert result["warnings"] == [{"plate": 1, "message": "floating regions"}]


def message(adapter, body):
    adapter._on_message(None, None, type("Message", (), {"payload": json.dumps(body).encode()})())


def test_version_is_ota_not_an_ams_component_and_lan_needs_its_own_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(bambu, "TERMINAL_JOBS_DIR", tmp_path)
    monkeypatch.setattr(bambu, "QUEUE_PROGRESS_DIR", tmp_path)
    a = bambu.BambuAdapter(printer_id="test", host="127.0.0.1", serial="SYNTHETIC", access_code="synthetic", model="A1 Mini")
    a._client = mock.Mock()
    a._on_connect(a._client, None, None, 0)
    assert a.status()["lanCommands"] == "unknown"
    message(a, {"info": {"command": "get_version", "module": [{"name": "ams/0", "sw_ver": "99"}]}})
    assert "firmwareVersion" not in a.status()
    message(a, {"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.02.03.04", "sn": "PRIVATE"}]}})
    assert a.status()["firmwareVersion"] == "01.02.03.04"
    assert a.status()["lanCommands"] == "unknown"
    assert "PRIVATE" not in json.dumps(a.status())
    seq = a._lan_probe
    message(a, {"print": {"command": "gcode_line", "sequence_id": "not-ours", "result": "SUCCESS"}})
    assert a.status()["lanCommands"] == "unknown"
    message(a, {"print": {"command": "gcode_line", "sequence_id": seq, "result": "SUCCESS"}})
    assert a.status()["lanCommands"] == "accepted"
    message(a, {"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.02.03.05"}]}})
    assert a.status()["firmwareVersion"] == "01.02.03.05"
    assert a.status()["lanCommands"] == "unknown"
    a._on_disconnect()
    assert a.status()["lanCommands"] == "unknown"
    a._on_connect(a._client, None, None, 0)
    assert "firmwareVersion" not in a.status()
    message(a, {"print": {"command": "gcode_line", "sequence_id": seq, "result": "SUCCESS"}})
    assert a.status()["lanCommands"] == "unknown"
    message(a, {"print": {"command": "gcode_line", "sequence_id": a._lan_probe, "result": "FAIL"}})
    assert a.status()["lanCommands"] == "rejected"
    a._probe_lan_commands()
    seq = a._lan_probe
    with mock.patch.object(bambu.time, "monotonic", return_value=a._lan_probe_at + 31):
        message(a, {"print": {"command": "gcode_line", "sequence_id": seq, "result": "SUCCESS"}})
    assert a.status()["lanCommands"] == "unknown"
    probes = [json.loads(call.args[1]) for call in a._client.publish.call_args_list if "gcode_line" in call.args[1]]
    assert all(p["print"]["param"] == "M105\n" for p in probes)


def observe(t, state, name="synthetic.gcode", duration=100, now=100):
    t.observe({"print_stats": {"state": state, "filename": name, "print_duration": duration}}, now)


@pytest.mark.parametrize("state, status, reason", [("cancelled", "cancelled", "cancelled"), ("error", "failed", "error"),
                                                  ("complete", "done", "completed"), ("standby", "cancelled", "lost")])
def test_observed_klipper_end_is_durable_and_keeps_the_print_key(tmp_path, state, status, reason):
    path = tmp_path / "terminal.json"
    t = KlipperJobTracker("test", path)
    observe(t, "printing")
    key = t.active_key()
    observe(t, state, duration=105, now=105)
    assert t.pending()[0]["status"] == status
    assert t.pending()[0]["endReason"] == reason
    assert t.pending()[0]["jobKey"] == key
    assert KlipperJobTracker("test", path).pending() == t.pending()


@pytest.mark.parametrize("state,name,duration", [("cancelled", "other.gcode", 200), ("cancelled", "synthetic.gcode", 1),
                                                ("printing", "synthetic.gcode", 1), ("standby", "synthetic.gcode", 200)])
def test_klipper_missed_end_never_claims_cancel_or_success(state, name, duration):
    t = KlipperJobTracker("test")
    observe(t, "printing")
    observe(t, state, name, duration, now=200)
    assert t.pending()[0]["endReason"] == "lost"
    assert t.pending()[0]["status"] == "cancelled"


def test_restart_onto_same_named_terminal_does_not_prove_it_is_the_saved_run(tmp_path):
    path = tmp_path / "terminal.json"
    t = KlipperJobTracker("test", path)
    observe(t, "printing")
    t = KlipperJobTracker("test", path)
    observe(t, "cancelled")
    assert t.pending()[0]["endReason"] == "lost"


@pytest.mark.parametrize("missing", ["before", "after", "gap", "gap_then_printing"])
def test_klipper_cancellation_without_continuous_run_evidence_is_lost(missing):
    t = KlipperJobTracker("test")
    observe(t, "printing", duration=None if missing == "before" else 100)
    if missing == "gap_then_printing":
        observe(t, "printing", duration=200, now=200)
    observe(t, "cancelled", duration=None if missing == "after" else 205,
            now=205 if missing.startswith("gap") else 105)
    assert t.pending()[0]["endReason"] == "lost"


def test_klipper_still_running_with_the_same_start_reestablishes_run_identity():
    t = KlipperJobTracker("test")
    with mock.patch("makeros_hub.printers.klipper._wall", side_effect=lambda now: now + 1000):
        for state, now in [("printing", 100), ("printing", 200), ("cancelled", 205)]:
            t.observe({"print_stats": {"state": state, "filename": "synthetic.gcode",
                                       "print_duration": now, "total_duration": now}}, now)
    assert t.pending()[0]["endReason"] == "cancelled"


def test_firmware_and_lan_are_rechecked_after_an_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(bambu, "TERMINAL_JOBS_DIR", tmp_path)
    monkeypatch.setattr(bambu, "QUEUE_PROGRESS_DIR", tmp_path)
    a = bambu.BambuAdapter(printer_id="test-hour", host="127.0.0.1", serial="SYNTHETIC", access_code="synthetic", model="A1 Mini")
    a._client = mock.Mock()
    a._version_logged = True
    a._version_asked_at = 10
    with mock.patch.object(bambu.time, "monotonic", return_value=3610):
        a.request_version_if_missing()
        assert a._client.publish.call_count == 2
        a.request_version_if_missing()
        assert a._client.publish.call_count == 2
    assert a.status()["lanCommands"] == "unknown"
