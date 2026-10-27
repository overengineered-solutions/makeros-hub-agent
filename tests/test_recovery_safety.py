"""Recovery cannot reuse a spent height, and released motors never retain a trusted home."""

import json
import threading
import time
from unittest.mock import Mock, patch

import pytest

from makeros_hub.printers import klipper
from test_screen_controls import STOP, rehome, rehome_refusal, restarted


def adapter():
    a = klipper.KlipperAdapter("recovery", "http://127.0.0.1:1")
    a._stop_pt = dict(STOP)
    a._last_poll_ok_at = time.monotonic()
    a._fetch_once = Mock(return_value=restarted())
    a._printer_objects = Mock(return_value={"gcode_macro ps_rehome_xy", "gcode_macro ps_home_z_here"})
    a._post_ok = Mock()
    return a


def act(a, action="rehome_xy", **params):
    result = a.send_command("klipper_action", {"action": action, **params})
    deadline = time.monotonic() + 2
    while a._running_action and time.monotonic() < deadline:
        time.sleep(0.001)
    assert a._running_action is None
    return result


@pytest.mark.parametrize("top,extra", [(1020, 0), (505, 5), (505, 0)])
def test_repeated_recovery_uses_current_height_and_refuses_at_real_travel_limit(top, extra):
    a = adapter()
    a._stop_pt["z"] = top - 50
    height = a._stop_pt["z"]
    for _ in range(5):
        fresh = restarted(position=[100, 100, height, 0], axis_maximum=[816, 806, top, 0])
        fresh["gcode_macro _PS_CORNERS"]["xy_extra_lift"] = extra
        a._fetch_once.return_value = fresh
        result = act(a)
        if not result["ok"]:
            assert result["reason"] == "no_room_to_lift"
            break
        script = klipper.urllib.parse.parse_qs(klipper.urllib.parse.urlparse(a._post_ok.call_args.args[0]).query)["script"][0]
        args = dict(arg.split("=") for arg in script.split()[1:])
        assert float(args["Z"]) == height
        height += float(args["LIFT"]) + extra
        assert height <= top - 1
        assert json.loads(a._stop_path.read_text())["recoveryAttempted"] is True
    else:
        pytest.fail("repeated recovery never refused at the upper limit")


def test_restart_after_lift_or_an_old_unmarked_record_requires_a_fresh_position():
    a = adapter()
    assert act(a)["ok"]
    for record in (json.loads(a._stop_path.read_text()), dict(STOP)):
        a._stop_path.write_text(json.dumps(record))
        rebooted = klipper.KlipperAdapter("recovery", "http://127.0.0.1:1")
        rebooted._last_poll_ok_at = time.monotonic()
        rebooted._fetch_once = Mock(return_value=restarted(position=[815, 805, 440.37, 0]))
        rebooted._post_ok = Mock()
        assert act(rebooted)["reason"] == "z_unknown"
        rebooted._post_ok.assert_not_called()


def test_failed_or_uncertain_lift_cannot_reuse_stop_height():
    a = adapter()
    def uncertain(*args):
        assert json.loads(a._stop_path.read_text())["recoveryAttempted"] is True
        raise TimeoutError()
    a._post_ok.side_effect = uncertain
    assert act(a)["ok"] is False
    assert act(a)["reason"] == "z_unknown"
    assert a._post_ok.call_count == 1


def test_recovery_never_moves_when_attempt_cannot_be_saved_durably():
    a = adapter()
    with patch.object(klipper, "write_json_durably", return_value=False):
        assert act(a)["reason"] == "state_persist_failed"
    a._post_ok.assert_not_called()
    assert act(a)["reason"] == "z_unknown"


def test_old_macro_without_lift_metadata_reserves_anayas_extra_five_mm():
    fresh = restarted(axis_maximum=[502, 505, 505, 0])
    del fresh["gcode_macro _PS_CORNERS"]["xy_extra_lift"]
    assert rehome({"action": "rehome_xy"}, fresh, stop={**STOP, "z": 480}) == "PS_REHOME_XY Z=480 LIFT=19"
    fresh["gcode_macro _PS_CORNERS"]["xy_extra_lift"] = None
    assert rehome({"action": "rehome_xy"}, fresh, stop={**STOP, "z": 480}) == "PS_REHOME_XY Z=480 LIFT=19"
    assert rehome_refusal({"action": "rehome_xy"}, fresh, stop={**STOP, "z": 495}) == "no_room_to_lift"


def test_anayas_offset_counts_in_the_lift_and_a_downward_or_unknown_offset_is_refused():
    fresh = restarted(axis_maximum=[502, 505, 505, 0])
    fresh["gcode_macro _PS_CORNERS"]["xy_extra_lift"] = 5.0
    fresh["gcode_move"]["homing_origin"][2] = 2.0
    assert rehome({"action": "rehome_xy"}, fresh, stop={**STOP, "z": 480}) == "PS_REHOME_XY Z=480 LIFT=17"
    fresh["gcode_move"]["homing_origin"][2] = -6.0
    assert rehome_refusal({"action": "rehome_xy"}, fresh) == "z_unknown"
    del fresh["gcode_move"]
    assert rehome_refusal({"action": "rehome_xy"}, fresh) == "printer_stale"


def test_cancel_invalidates_home_even_when_printer_never_answers():
    a = adapter()
    a._home_proven = a._xy_proven = a._z_rec = True
    with patch.object(klipper.urllib.request, "urlopen", side_effect=TimeoutError()):
        assert a.send_command("stop")["ok"] is False
    assert not (a._home_proven or a._xy_proven or a._z_rec)
    assert act(a, "jog", axis="x", mm=1)["reason"] == "not_homed"
    assert act(a)["reason"] == "z_unknown"
    a._post_ok.assert_not_called()


@pytest.mark.parametrize("steppers", [None, {}, {"stepper_x": True, "stepper_y": True, "stepper_z": False},
                                      {"stepper_x": True, "stepper_y": True, "stepper_z": True, "stepper_z1": False}])
def test_motor_release_or_missing_enable_reading_ends_trust_despite_homed_xyz(steppers):
    a = adapter()
    a._home_proven = a._xy_proven = a._z_rec = True
    fresh = {**restarted(), "stepper_enable": {"steppers": steppers}}
    a._note_homed(fresh)
    assert not (a._home_proven or a._xy_proven or a._z_rec)
    assert act(a, "jog", axis="x", mm=1)["reason"] == "not_homed"
    assert act(a)["reason"] == "z_unknown"
    a._post_ok.assert_not_called()


def test_m84_and_reenable_between_reads_still_loses_home():
    a = adapter()
    a._note_homed({**restarted(), "gcode_macro M84": {"release_seq": 1}})
    a._home_proven = a._xy_proven = a._z_rec = True
    a._fetch_once.return_value = {**restarted(), "gcode_macro M84": {"release_seq": 2}}
    assert act(a, "jog", axis="x", mm=1)["reason"] == "not_homed"
    assert act(a, "jog", axis="x", mm=1)["reason"] == "not_homed"
    assert act(a)["reason"] == "z_unknown"
    a._post_ok.assert_not_called()


def test_failed_fresh_read_makes_even_an_unspent_stop_height_unusable():
    a = adapter()
    a._fetch_once.side_effect = TimeoutError()
    assert act(a)["ok"] is False
    a._fetch_once.side_effect = None
    assert act(a)["reason"] == "z_unknown"
    a._post_ok.assert_not_called()


def test_stop_while_home_reply_is_in_flight_cannot_restore_trust():
    a = adapter()
    started, finish = threading.Event(), threading.Event()
    def slow_home(*args):
        started.set()
        assert finish.wait(2)
    a._post_ok.side_effect = slow_home
    with patch.object(klipper, "LONG_ACTION_GRACE_SEC", 0.01):
        assert a.send_command("klipper_action", {"action": "home"})["detail"] == "started"
    assert started.wait(1)
    with patch.object(klipper.urllib.request, "urlopen", side_effect=TimeoutError()):
        a.send_command("stop")
    finish.set()
    deadline = time.monotonic() + 2
    while a._running_action and time.monotonic() < deadline:
        time.sleep(0.001)
    assert a._running_action is None
    assert not (a._home_proven or a._xy_proven or a._z_rec)


@pytest.mark.parametrize("release_during_home", [False, True])
def test_homing_from_disabled_axes_earns_trust_unless_a_motor_is_released(release_during_home):
    a = adapter()
    disabled = {**restarted(), "stepper_enable": {"steppers": {f"stepper_{axis}": False for axis in "xyz"}}}
    a._fetch_once.side_effect = [disabled, restarted()]
    def home(*args):
        a._note_homed(disabled)   # the first axis has not started yet
        a._note_homed({**disabled, "stepper_enable": {"steppers": {"stepper_x": True, "stepper_y": False, "stepper_z": False}}})
        if release_during_home:
            a._note_homed(disabled)
    a._post_ok.side_effect = home
    assert act(a, "home")["ok"]
    assert a._home_proven is (not release_during_home)
    assert a._xy_proven is (not release_during_home)


def test_home_reply_without_fresh_enabled_axes_never_grants_trust():
    a = adapter()
    disabled = {**restarted(), "stepper_enable": {"steppers": {f"stepper_{axis}": False for axis in "xyz"}}}
    a._fetch_once.side_effect = [restarted(), disabled]
    assert act(a, "home")["ok"]
    assert not (a._home_proven or a._xy_proven)


def test_queries_use_the_macro_objects_case_sensitive_configured_names():
    for query in (klipper._QUERY_OBJECTS, klipper._ACTION_QUERY):
        objects = klipper.urllib.parse.parse_qs(query)
        assert "release_seq" in objects["gcode_macro M84"][0].split(",")
        assert "gcode_macro _PS_CORNERS" in objects
