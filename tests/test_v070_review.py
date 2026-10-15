"""Release review regressions: status must be valid and never claim knowledge it lacks."""

import threading
from unittest import mock

from makeros_hub import telemetry
from makeros_hub.printers import bambu, bambu_parse, klipper
from test_v070 import Harness, _Msg, row, start_routine


def test_never_observed_adapter_cannot_poison_a_telemetry_batch():
    h = Harness([row(), {"printerId": "unreachable", "connectionState": "error"}])
    h.tick(0)
    h.rows[0] = row(progressPct=14)
    h.tick()
    assert [p["printerId"] for p in h.sent[-1]["printers"]] == ["p1"]
    assert all("sampledAt" in p for body in h.sent for p in body["printers"])
    for invalid in (None, 0, -1, float("nan"), float("inf"), True):
        assert telemetry.project(row(sampledAt=invalid)) is None


def test_active_nozzle_missing_does_not_borrow_the_inactive_temperature():
    device = {"extruder": {"state": 0x12, "info": [{"id": 0, "temp": 30}]}}
    assert bambu_parse.build_temperatures({"device": device}) == {}
    device["extruder"].pop("state")
    assert bambu_parse.build_temperatures({"device": device}) == {}


def test_new_stage_layout_is_forwarded():
    assert bambu_parse.build_stage({"gcode_state": "RUNNING", "stage": {"_id": 7}}) == {"stageCode": 7}


def test_version_reply_does_not_refresh_the_age_of_print_status():
    a = bambu.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
    a._connack = "ok"
    with mock.patch.object(bambu.time, "monotonic", return_value=100), mock.patch.object(bambu.time, "time", return_value=1000):
        a._on_message(None, None, _Msg({"print": {"gcode_state": "RUNNING", "mc_percent": 25}}))
        assert a.status()["sampledAt"] == 1_000_000
    with mock.patch.object(bambu.time, "monotonic", return_value=120), mock.patch.object(bambu.time, "time", return_value=1020):
        a._on_message(None, None, _Msg({"info": {"command": "get_version"}}))
        assert a.status()["sampledAt"] == 1_000_000


def test_status_and_its_timestamp_are_one_snapshot_during_concurrent_mqtt_updates():
    a = bambu.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
    a._on_message(None, None, _Msg({"print": {"gcode_state": "RUNNING", "mc_percent": 25}}))
    normalize = bambu_parse.normalize_status

    def concurrent_update(printer_id, data, **kwargs):
        a._on_message(None, None, _Msg({"print": {"mc_percent": 50}}))
        return normalize(printer_id, data, **kwargs)

    with mock.patch.object(bambu_parse, "normalize_status", side_effect=concurrent_update):
        assert a.status()["progressPct"] == 25


def test_slow_metadata_does_not_delay_or_redate_successful_poll():
    a = klipper.KlipperAdapter("p1", "http://127.0.0.1:9")
    stop = threading.Event()
    a._fetch_once = start_routine
    observed = []

    def metadata(_data):
        observed.append(a.status())
        stop.set()

    a._refresh_metadata = metadata
    a._poll_loop(stop)
    assert observed[0]["connectionState"] == "connected"
    assert observed[0]["preparing"] is True
    assert "sampledAt" in observed[0]


def test_rejected_http_response_reopens_connection_before_retry():
    h = Harness([row()], script=[(503, {}), (200, {"ok": True})])
    h.tick(0)
    assert h.conns[0].closed
    h.tick()
    assert len(h.conns) == 2
    assert h.sent[-1]["full"] is True


def test_nonfinite_reply_interval_does_not_break_the_push_loop():
    h = Harness([row()], script=[(200, {"ok": True, "nextMs": float("nan")})])
    h.tick(0)
    assert h.pusher._next_at == h.t + 5
