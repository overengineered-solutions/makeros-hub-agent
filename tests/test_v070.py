"""v0.70: the phase, not the word "Printing" (docs/telemetry-v1.md).

Klipper: a start-routine poll is `preparing`, carries targets and the M117 message, reads 0 % from M73 and gives no
ETA; after extrusion the slicer's estimate wins when the file's metadata is known; the metadata cache asks once per
filename, remembers a failure, and stays bounded. Bambu: flat and packed temperature layouts, stage sentinels, layers,
PREPARE, and silence while offline. The telemetry push: coalescing, rounding, full snapshots at start / every 60 s /
after any failure, the 404 backoff, nextMs and wantFull, a reused connection — against an injected connection and once
against a real loopback HTTP server. Fakes only: no printer, no site."""

import http.server
import json
import logging
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from makeros_hub import agent, config as config_module, telemetry
from makeros_hub.config import Config
from makeros_hub.printers import bambu as bambu_module, bambu_parse, klipper
from makeros_hub.printers.klipper import KlipperAdapter, _build_status

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1760540400.0 + 600   # ten minutes after the fixtures' gcode_start_time


def fixture_print(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["print"]


def bambu_status(print_obj: dict, connection_state: str = "connected") -> dict:
    return bambu_parse.normalize_status("p", {"print": print_obj}, connection_state=connection_state, model="X", now=NOW)


def start_routine(**print_stats) -> dict:
    """The SV08 on 2026-10-15: inside START_PRINT's bed soak. The file position is past the header and thumbnails
    (8 % of the bytes), the nozzle is parked at its 140 C calibration target, nothing has been extruded."""
    ps = {"state": "printing", "filename": "cube.gcode", "print_duration": 0.0, "total_duration": 312.4,
          "info": {"current_layer": None, "total_layer": None}, "message": ""}
    ps.update(print_stats)
    return {
        "print_stats": ps,
        "virtual_sdcard": {"progress": 0.08},
        "display_status": {"progress": 0.0, "message": "Bed soak 20 min before calibration"},
        "extruder": {"temperature": 139.7, "target": 140.0},
        "heater_bed": {"temperature": 64.9, "target": 65.0},
        "toolhead": {"homed_axes": "xyz"},
        "idle_timeout": {"state": "Printing"},
    }


def extruding(**print_stats) -> dict:
    """Ten minutes of real printing, M73 at 25 %."""
    status = start_routine(print_duration=600.0, total_duration=1800.0, info={"current_layer": 12, "total_layer": 80},
                           **print_stats)
    status["display_status"] = {"progress": 0.25, "message": ""}
    status["virtual_sdcard"] = {"progress": 0.31}
    status["extruder"] = {"temperature": 219.8, "target": 220.0}
    return status


class _Msg:
    def __init__(self, doc):
        self.payload = json.dumps(doc).encode()


# ---------------------------------------------------------------------------
# Klipper
# ---------------------------------------------------------------------------


class TestKlipperStatus(unittest.TestCase):
    def test_start_routine_is_preparing_with_targets_message_zero_progress_and_no_eta(self):
        out = _build_status("p1", start_routine(), "connected")
        self.assertEqual(out["state"], "printing")
        self.assertIs(out["preparing"], True)
        self.assertEqual(out["progressPct"], 0.0)          # M73, not the 8 % byte position
        self.assertNotIn("etaMinutes", out)                 # nothing before the first extrusion
        self.assertEqual(out["displayMessage"], "Bed soak 20 min before calibration")
        self.assertEqual((out["nozzleTempC"], out["nozzleTargetC"]), (139.7, 140.0))
        self.assertEqual((out["bedTempC"], out["bedTargetC"]), (64.9, 65.0))
        self.assertEqual(out["printDurationSec"], 0.0)
        self.assertEqual(out["elapsedSec"], 312.4)
        self.assertNotIn("layer", out)                      # null layers until SET_PRINT_STATS_INFO
        self.assertNotIn("gcodeState", out)                 # the site's plate-clear gate reads that key

    def test_after_extrusion_the_slicer_eta_wins_when_metadata_is_known(self):
        out = _build_status("p1", extruding(), "connected", metadata={"estimated_time": 3600.0})
        self.assertIs(out["preparing"], False)
        self.assertEqual(out["progressPct"], 25.0)
        self.assertEqual(out["etaMinutes"], 50)             # (3600 - 600) / 60
        self.assertEqual(out["layer"], [12, 80])
        self.assertNotIn("displayMessage", out)            # cleared message is omitted, never ""
        # No metadata: the extrapolation, as before. Estimate already outrun: the extrapolation again.
        self.assertEqual(_build_status("p1", extruding(), "connected")["etaMinutes"], 30)
        self.assertEqual(_build_status("p1", extruding(), "connected", metadata={"estimated_time": 500.0})["etaMinutes"], 30)
        self.assertEqual(_build_status("p1", extruding(), "connected", metadata=None)["etaMinutes"], 30)

    def test_byte_progress_only_when_display_status_is_absent(self):
        status = start_routine()
        del status["display_status"]
        out = _build_status("p1", status, "connected")
        self.assertAlmostEqual(out["progressPct"], 8.0)
        self.assertNotIn("displayMessage", out)

    def test_preparing_is_false_outside_printing_and_message_is_trimmed(self):
        paused = start_routine(state="paused")
        paused["display_status"]["message"] = "x" * 300
        out = _build_status("p1", paused, "connected")
        self.assertIs(out["preparing"], False)
        self.assertEqual(len(out["displayMessage"]), klipper.DISPLAY_MESSAGE_MAX_CHARS)
        # Layers need a known total and whole numbers.
        for info in ({"current_layer": 0, "total_layer": 0}, {"current_layer": 3}, {"current_layer": 1.5, "total_layer": 9}):
            self.assertNotIn("layer", _build_status("p1", start_routine(info=info), "connected"), info)
        self.assertEqual(_build_status("p1", start_routine(info={"current_layer": 0, "total_layer": 9}), "connected")["layer"], [0, 9])

    def test_query_asks_for_the_new_objects(self):
        for part in ("print_stats=state,filename,print_duration,total_duration,info,message", "toolhead=homed_axes", "idle_timeout=state"):
            self.assertIn(part, klipper._QUERY_OBJECTS)


class TestKlipperMetadataCache(unittest.TestCase):
    def _adapter(self):
        return KlipperAdapter("p1", "http://127.0.0.1:9")   # never started: no poll thread

    def test_one_fetch_per_filename_a_failure_remembered_then_retried_and_the_cache_bounded(self):
        a = self._adapter()
        calls = []

        def fake(filename):
            calls.append(filename)
            if filename == "bad.gcode":
                raise ValueError("missing result")
            return {"estimated_time": 3600.0}

        a._fetch_metadata = fake
        a._refresh_metadata(extruding())
        a._refresh_metadata(extruding())
        self.assertEqual(calls, ["cube.gcode"])
        a._record_poll(extruding())
        self.assertEqual(a.status()["etaMinutes"], 50)      # the slicer ETA rides status()
        bad = extruding(filename="bad.gcode")
        a._refresh_metadata(bad)
        a._refresh_metadata(bad)
        self.assertEqual(calls.count("bad.gcode"), 1)        # a failure is remembered, not re-asked every poll
        a._record_poll(bad)
        self.assertEqual(a.status()["etaMinutes"], 30)       # no metadata: the extrapolation
        with mock.patch.object(klipper, "METADATA_RETRY_SEC", 0.0):
            a._refresh_metadata(bad)
        self.assertEqual(calls.count("bad.gcode"), 2)        # ...but asked again after the retry window
        for i in range(40):
            a._refresh_metadata(extruding(filename=f"f{i}.gcode"))
        self.assertEqual(len(a._metadata), klipper.METADATA_CACHE_MAX)
        self.assertNotIn("cube.gcode", a._metadata)          # oldest evicted first
        self.assertIn("f39.gcode", a._metadata)
        a._refresh_metadata({"print_stats": {"state": "standby", "filename": ""}})   # no file: nothing asked
        self.assertEqual(len([c for c in calls if c == ""]), 0)

    def test_the_real_get_hits_moonrakers_metadata_route_and_a_404_is_tolerated(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if "missing" in self.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"result": {"estimated_time": 1234, "layer_count": 25}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            a = KlipperAdapter("p1", f"http://127.0.0.1:{srv.server_port}")
            self.assertEqual(a._fetch_metadata("my cube.gcode"), {"estimated_time": 1234.0})
            self.assertEqual(seen[-1], "/server/files/metadata?filename=my%20cube.gcode")
            with self.assertRaises(HTTPError):
                a._fetch_metadata("missing.gcode")
            a._refresh_metadata(extruding(filename="missing.gcode"))   # swallowed: remembered as None
            self.assertEqual(a._metadata["missing.gcode"][0], None)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_sampled_at_is_the_polls_wall_time(self):
        a = self._adapter()
        self.assertNotIn("sampledAt", a.status())              # no poll yet: no reading to date
        a._record_poll(start_routine())
        s = a.status()
        self.assertEqual(s["connectionState"], "connected")
        self.assertLess(abs(s["sampledAt"] - time.time() * 1000), 2000)


# ---------------------------------------------------------------------------
# Bambu
# ---------------------------------------------------------------------------


class TestBambuStatus(unittest.TestCase):
    def test_flat_report_heating_the_nozzle(self):
        s = bambu_status(fixture_print("bambu-p1-flat-report.json"))
        self.assertEqual(s["state"], "printing")
        self.assertEqual((s["nozzleTempC"], s["nozzleTargetC"]), (180.5, 220.0))
        self.assertEqual((s["bedTempC"], s["bedTargetC"]), (59.8, 60.0))
        self.assertNotIn("chamberC", s)                     # a P1/A1 has no chamber sensor
        self.assertEqual((s["stageCode"], s["stageSecondsLeft"]), (7, 42))
        self.assertEqual(s["layer"], [12, 80])
        self.assertIs(s["preparing"], False)
        self.assertEqual(s["elapsedSec"], 600)
        self.assertEqual((s["progressPct"], s["etaMinutes"], s["jobName"]), (15, 65, "Clamshell Parts Box"))

    def test_packed_report_reads_current_and_target_from_the_device_words(self):
        s = bambu_status(fixture_print("bambu-p2s-packed-report.json"))
        self.assertEqual((s["nozzleTempC"], s["nozzleTargetC"]), (180.0, 220.0))
        self.assertEqual((s["bedTempC"], s["bedTargetC"]), (59.0, 60.0))
        self.assertEqual(s["chamberC"], 35.0)
        self.assertEqual((s["stageCode"], s["stageSecondsLeft"]), (0, 0))   # 0 = printing, kept while RUNNING
        self.assertEqual(s["layer"], [150, 340])
        # The shop's H2D fixture: heaters off, the same packed rule (27 -> 27 / 0), the active extruder (state 2 -> id 0).
        raw = json.loads((FIXTURES / "h2d-device-observed-2026-09-09.json").read_text())["raw"]
        self.assertEqual(bambu_parse.build_temperatures(raw), {"nozzleTempC": 27.0, "nozzleTargetC": 0.0})

    def test_flat_fields_win_over_packed_and_packed_garbage_is_dropped(self):
        packed = fixture_print("bambu-p2s-packed-report.json")
        packed["nozzle_temper"] = 180.5                      # a firmware with both: the float is the finer reading
        temps = bambu_parse.build_temperatures(packed)
        self.assertEqual((temps["nozzleTempC"], temps["nozzleTargetC"]), (180.5, 220.0))
        self.assertIsNone(bambu_parse._packed_temp(0xFFFF))             # the firmware's "none" is not 65535 C
        self.assertIsNone(bambu_parse._packed_temp(-1))
        self.assertIsNone(bambu_parse._packed_temp("x"))
        self.assertEqual(bambu_parse._packed_temp(6553700), (100.0, 100.0))   # ha-bambulab's own example
        self.assertEqual(bambu_parse._packed_temp("6553700"), (100.0, 100.0))
        # The active extruder, not the first: state 0x12 = two extruders, the second active.
        device = {"extruder": {"state": 0x12, "info": [{"id": 0, "temp": 30}, {"id": 1, "temp": (250 << 16) | 240}]}}
        self.assertEqual(bambu_parse._packed_extruder_temp(device), (240.0, 250.0))

    def test_stage_sentinels_and_layers(self):
        def stage(code, gcode_state="RUNNING", **extra):
            return bambu_parse.build_stage({"stg_cur": code, "gcode_state": gcode_state, **extra})

        self.assertEqual(stage(-1, "IDLE"), {})              # X1 idle
        self.assertEqual(stage(255, "IDLE"), {})             # P1/A1 idle
        self.assertEqual(stage(0, "IDLE"), {})               # the boot-time 0
        self.assertEqual(stage(0, "FINISH"), {})             # the last print's 0, kept after it ended
        self.assertEqual(stage(0, "RUNNING"), {"stageCode": 0})
        self.assertEqual(stage(0, "PREPARE", stg_cd=30), {"stageCode": 0, "stageSecondsLeft": 30})
        self.assertEqual(stage("7", "RUNNING", stg_cd="42"), {"stageCode": 7, "stageSecondsLeft": 42})
        self.assertEqual(stage(13, "IDLE"), {"stageCode": 13})   # a real stage outside a print (a screen-started QGL)
        self.assertEqual(stage(1001), {})
        self.assertEqual(stage(7, stg_cd=-1), {"stageCode": 7})
        self.assertIsNone(bambu_parse.build_layer({"layer_num": 3, "total_layer_num": 0}))
        self.assertIsNone(bambu_parse.build_layer({"layer_num": 3}))
        self.assertEqual(bambu_parse.build_layer({"layer_num": "12", "total_layer_num": "80"}), [12, 80])

    def test_prepare_is_preparing_and_still_idle(self):
        s = bambu_status({"gcode_state": "PREPARE", "gcode_file_prepare_percent": "40", "stg_cur": 0})
        self.assertEqual(s["state"], "idle")                 # what the site has always been told
        self.assertEqual(s["gcodeState"], "PREPARE")
        self.assertIs(s["preparing"], True)
        self.assertEqual(s["stageCode"], 0)
        self.assertIs(bambu_status({"gcode_state": "RUNNING"})["preparing"], False)

    def test_elapsed_only_while_a_print_is_under_way_and_never_from_the_future(self):
        self.assertIsNone(bambu_parse.build_elapsed_sec({"gcode_state": "IDLE", "gcode_start_time": "1760540400"}, NOW))
        self.assertIsNone(bambu_parse.build_elapsed_sec({"gcode_state": "RUNNING", "gcode_start_time": str(int(NOW) + 3600)}, NOW))
        self.assertIsNone(bambu_parse.build_elapsed_sec({"gcode_state": "RUNNING", "gcode_start_time": "0"}, NOW))
        self.assertEqual(bambu_parse.build_elapsed_sec({"gcode_state": "PAUSE", "gcode_start_time": "1760540400"}, NOW), 600)

    def test_offline_printer_sends_no_telemetry(self):
        for state in ("offline", "error", "connecting"):
            s = bambu_status(fixture_print("bambu-p1-flat-report.json"), connection_state=state)
            self.assertEqual(s["connectionState"], state)
            self.assertEqual(s["model"], "X")                # identity, not telemetry
            for key in ("progressPct", "nozzleTempC", "nozzleTargetC", "bedTempC", "jobName", "etaMinutes", "stageCode",
                        "layer", "preparing", "elapsedSec", "state", "gcodeState"):
                self.assertNotIn(key, s, (state, key))

    def test_adapter_dates_the_newest_report(self):
        a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
        a._connack = "ok"
        self.assertNotIn("sampledAt", a.status())
        a._on_message(None, None, _Msg({"print": fixture_print("bambu-p1-flat-report.json")}))
        s = a.status()
        self.assertEqual(s["connectionState"], "connected")
        self.assertLess(abs(s["sampledAt"] - time.time() * 1000), 2000)
        self.assertEqual(s["stageCode"], 7)
        # and the heartbeat carries the same row unchanged
        self.assertEqual(agent.heartbeat_payload([s])["printers"][0]["nozzleTargetC"], 220.0)


# ---------------------------------------------------------------------------
# The telemetry push
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n=-1):
        body, self._body = self._body, b""
        return body


class FakeConn:
    """A scripted http.client connection: records each request, answers from a shared script of (status, body) items
    or raises the exception found there."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, json.loads(body), dict(headers or {})))

    def getresponse(self):
        item = self.script.pop(0) if self.script else (200, {"ok": True})
        if isinstance(item, BaseException):
            raise item
        status, body = item
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return _Response(status, raw)

    def close(self):
        self.closed = True


def row(pid="p1", **over):
    base = {"printerId": pid, "connectionState": "connected", "state": "printing", "gcodeState": "RUNNING",
            "preparing": False, "sampledAt": 1760540400000, "nozzleTempC": 215.2, "nozzleTargetC": 220.0,
            "bedTempC": 60.0, "bedTargetC": 60.0, "progressPct": 12.34, "etaMinutes": 65.0, "printDurationSec": 600.7,
            "jobName": "cube", "model": "A1 mini", "mqttConnectedAtMs": 5}
    base.update(over)
    return base


class Harness:
    def __init__(self, rows, script=None, interval=5):
        self.rows = rows
        self.script = script if script is not None else []
        self.conns = []
        self.t = 1000.0
        self.pusher = telemetry.TelemetryPusher(
            "https://cloud.test/api/print/hub/telemetry", "secret", lambda: list(self.rows),
            interval_sec=interval, connect=self._connect, clock=lambda: self.t, wall=lambda: 1760540400.0,
        )

    def _connect(self):
        conn = FakeConn(self.script)
        self.conns.append(conn)
        return conn

    def tick(self, advance=5.0):
        """One sampling tick after `advance` seconds: what run() does when the flush is due."""
        self.t += advance
        self.pusher.sample()
        if self.pusher.due():
            self.pusher.flush()

    @property
    def sent(self):
        return [r[2] for c in self.conns for r in c.requests]


class TestTelemetryPush(unittest.TestCase):
    def test_first_send_is_a_full_of_connected_printers_with_rounded_contract_keys(self):
        h = Harness([row("p1"), row("p2", connectionState="offline")])
        h.tick(0)
        self.assertEqual(len(h.sent), 1)
        body = h.sent[0]
        self.assertEqual((body["v"], body["full"], body["sentAt"]), (1, True, 1760540400000))
        self.assertEqual(len(body["printers"]), 1)
        p = body["printers"][0]
        self.assertEqual(p["printerId"], "p1")
        self.assertIs(p["connected"], True)
        self.assertEqual((p["nozzleC"], p["nozzleTargetC"], p["bedC"]), (215.0, 220.0, 60.0))   # 0.5 C
        self.assertEqual(p["progressPct"], 12.3)                                            # 0.1 %
        self.assertEqual((p["etaMinutes"], p["printDurationSec"]), (65, 600))               # whole units
        self.assertEqual(p["sampledAt"], 1760540400000)
        for absent in ("nozzleTempC", "bedTempC", "model", "mqttConnectedAtMs", "connectionState"):
            self.assertNotIn(absent, p)
        method, path, _, headers = h.conns[0].requests[0]
        self.assertEqual((method, path), ("POST", "/api/print/hub/telemetry"))
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_unchanged_printer_is_absent_and_a_delta_carries_only_what_moved(self):
        h = Harness([row("p1"), row("p2", progressPct=50.0)])
        h.tick(0)
        h.tick()                                   # nothing changed: no request at all
        self.assertEqual(len(h.sent), 1)
        h.rows[0] = row("p1", nozzleTempC=216.3, progressPct=12.36)
        h.tick()
        self.assertEqual(len(h.sent), 2)
        self.assertIs(h.sent[1]["full"], False)
        self.assertEqual(h.sent[1]["printers"],
                         [{"printerId": "p1", "sampledAt": 1760540400000, "nozzleC": 216.5, "progressPct": 12.4}])

    def test_rounding_hides_the_wobble(self):
        h = Harness([row("p1")])
        h.tick(0)
        h.rows[0] = row("p1", nozzleTempC=215.1, progressPct=12.31, etaMinutes=65.4, printDurationSec=600.2)
        h.tick()
        self.assertEqual(len(h.sent), 1)           # 215.1 and 215.2 both read 215.0; 12.31 is still 12.3

    def test_idle_printer_is_silent_until_its_state_moves(self):
        h = Harness([row("p1", state="idle", gcodeState="IDLE", nozzleTempC=24.0)])
        h.tick(0)
        h.rows[0] = row("p1", state="idle", gcodeState="IDLE", nozzleTempC=26.0, bedTempC=23.0)
        h.tick()
        self.assertEqual(len(h.sent), 1)           # drift on an idle printer is noise
        h.rows[0] = row("p1", state="idle", gcodeState="FINISH", nozzleTempC=26.0, bedTempC=23.0)
        h.tick()
        self.assertEqual(len(h.sent), 2)           # FINISH is the plate-clear gate's signal: it goes out, with the drift
        self.assertEqual(h.sent[1]["printers"][0]["gcodeState"], "FINISH")
        self.assertEqual(h.sent[1]["printers"][0]["nozzleC"], 26.0)
        h.rows[0] = row("p1", state="printing", gcodeState="RUNNING", nozzleTempC=100.0)
        h.tick()
        self.assertEqual(h.sent[2]["printers"][0]["state"], "printing")
        h.rows[0] = row("p1", connectionState="offline")
        h.tick()
        self.assertEqual(h.sent[3]["printers"][0]["connected"], False)
        self.assertEqual(h.sent[3]["printers"][0]["state"], "offline")

    def test_a_vanished_key_is_sent_as_null(self):
        h = Harness([row("p1", displayMessage="Bed soak", etaMinutes=None)])
        h.tick(0)
        self.assertEqual(h.sent[0]["printers"][0]["displayMessage"], "Bed soak")
        r = row("p1")
        del r["etaMinutes"]
        h.rows[0] = r
        h.tick()
        self.assertEqual(h.sent[1]["printers"][0]["displayMessage"], None)
        self.assertEqual(h.sent[1]["printers"][0]["etaMinutes"], None)

    def test_full_every_60s_after_a_failure_and_when_the_site_asks(self):
        h = Harness([row("p1")], script=[(200, {"ok": True}), (500, {}), (200, {"ok": True}), (200, {"ok": True}),
                                         ConnectionResetError("peer"), (200, {"ok": True}),
                                         (200, {"ok": False}), (200, {"ok": True}), (200, b"not json"), (200, {"ok": True}),
                                         (200, {"ok": True, "wantFull": True}), (200, {"ok": True})])
        h.tick(0)                                                  # full, accepted
        h.rows[0] = row("p1", progressPct=13.0)
        h.tick()                                                   # delta, 500
        self.assertIs(h.sent[1]["full"], False)
        h.tick()                                                   # full after the failure, even with nothing new
        self.assertIs(h.sent[2]["full"], True)
        h.rows[0] = row("p1", progressPct=14.0)
        h.tick(60)                                                 # 60 s since the last full: full again
        self.assertIs(h.sent[3]["full"], True)
        h.rows[0] = row("p1", progressPct=15.0)
        h.tick()                                                   # transport error: connection dropped
        self.assertTrue(h.conns[0].closed)
        h.rows[0] = row("p1", progressPct=15.5)
        h.tick()
        self.assertEqual(len(h.conns), 2)                          # re-created on the next send
        self.assertIs(h.sent[5]["full"], True)
        h.rows[0] = row("p1", progressPct=16.0)
        h.tick()                                                   # {ok: false} is not acceptance
        h.rows[0] = row("p1", progressPct=16.5)
        h.tick()
        self.assertIs(h.sent[7]["full"], True)
        h.rows[0] = row("p1", progressPct=17.0)
        h.tick()                                                   # a non-JSON 200 is not acceptance either
        h.rows[0] = row("p1", progressPct=17.5)
        h.tick()
        self.assertIs(h.sent[9]["full"], True)
        h.rows[0] = row("p1", progressPct=18.0)
        h.tick()                                                   # delta accepted with wantFull
        self.assertIs(h.sent[10]["full"], False)
        h.tick()                                                   # nothing changed, yet the site asked: full
        self.assertIs(h.sent[11]["full"], True)
        self.assertEqual(len(h.sent), 12)

    def test_baseline_moves_only_on_acceptance(self):
        h = Harness([row("p1")], script=[(200, {"ok": True}), (503, {}), (200, {"ok": True}), (200, {"ok": True})])
        h.tick(0)
        h.rows[0] = row("p1", progressPct=20.0)
        h.tick()                                                   # rejected delta
        h.tick()                                                   # the full that follows carries the 20.0
        self.assertEqual(h.sent[2]["printers"][0]["progressPct"], 20.0)
        h.tick()
        self.assertEqual(len(h.sent), 3)                           # ...and after it, nothing is left to send

    def test_404_is_logged_once_and_retried_every_60s(self):
        h = Harness([row("p1")], script=[(404, {}), (404, {}), (200, {"ok": True})])
        with self.assertLogs("makeros-hub.telemetry", level="INFO") as logs:
            h.tick(0)
            h.tick(59)
            self.assertEqual(len(h.sent), 1)                       # not due before 60 s
            h.tick(1)
            self.assertEqual(len(h.sent), 2)
            h.tick(60)
            self.assertEqual(len(h.sent), 3)
            self.assertIs(h.sent[2]["full"], True)
        self.assertEqual(sum("no /api/print/hub/telemetry route yet" in m for m in logs.output), 1)
        self.assertEqual(sum("accepted again" in m for m in logs.output), 1)
        self.assertFalse(any("WARNING" in m for m in logs.output))   # a missing route is expected, not a failure

    def test_next_ms_is_clamped_and_honoured(self):
        h = Harness([row("p1")], script=[(200, {"ok": True, "nextMs": 1000}), (200, {"ok": True, "nextMs": 120000}),
                                         (200, {"ok": True, "nextMs": "soon"})])
        h.tick(0)
        self.assertEqual(h.pusher._next_at, 1000.0 + 2.0)          # 1000 ms clamps up to 2 s
        h.rows[0] = row("p1", progressPct=30.0)
        h.tick(1.9)
        self.assertEqual(len(h.sent), 1)
        h.tick(0.1)
        self.assertEqual(len(h.sent), 2)
        self.assertEqual(h.pusher._next_at, h.t + 60.0)            # 120000 ms clamps down to 60 s
        h.rows[0] = row("p1", progressPct=31.0)
        h.tick(60)
        self.assertEqual(h.pusher._next_at, h.t + 60.0)            # a non-number leaves the interval alone
        self.assertEqual(telemetry.TelemetryPusher("https://c.test/t", "b", list, interval_sec=1, connect=lambda: None)._interval, 2.0)

    def test_the_connection_is_reused_across_sends(self):
        h = Harness([row("p1")])
        for pct in (10.0, 20.0, 30.0):
            h.rows[0] = row("p1", progressPct=pct)
            h.tick()
        self.assertEqual(len(h.conns), 1)
        self.assertEqual(len(h.conns[0].requests), 3)

    def test_garbage_statuses_and_a_raising_reader_never_escape_run(self):
        calls = {"n": 0}

        def statuses():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("adapter exploded")
            return [None, {"connectionState": "connected"}, {"printerId": 7}, row("p1")]

        conns = []

        def connect():
            conns.append(FakeConn([]))
            return conns[-1]

        pusher = telemetry.TelemetryPusher("https://cloud.test/api/print/hub/telemetry", "s", statuses, connect=connect)
        stop = threading.Event()
        with mock.patch.object(telemetry, "SAMPLE_SEC", 0.01):
            t = threading.Thread(target=pusher.run, args=(stop,), daemon=True)
            t.start()
            deadline = time.time() + 5
            while not conns and time.time() < deadline:
                time.sleep(0.01)
            stop.set()
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual([p["printerId"] for p in conns[0].requests[0][2]["printers"]], ["p1"])   # only the real row
        self.assertTrue(conns[0].closed)                                                           # closed on stop

    def test_project_maps_disconnected_rows_to_offline_or_error(self):
        self.assertEqual(telemetry.project({"printerId": "p", "connectionState": "error", "errorReason": "unreachable"}),
                         {"printerId": "p", "connected": False, "state": "error"})
        self.assertEqual(telemetry.project({"printerId": "p", "connectionState": "connecting"}),
                         {"printerId": "p", "connected": False, "state": "offline"})
        self.assertIsNone(telemetry.project({"connectionState": "connected"}))

    def test_against_a_real_loopback_server(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"          # keep-alive, as Caddy does

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(length))))
                body = json.dumps({"ok": True, "nextMs": 3000}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rows = [row("p1")]
            t = {"now": 0.0}
            pusher = telemetry.TelemetryPusher(f"http://127.0.0.1:{srv.server_port}/api/print/hub/telemetry", "secret",
                                               lambda: list(rows), clock=lambda: t["now"])
            pusher.sample()
            pusher.flush()
            first_conn = pusher._conn
            rows[0] = row("p1", progressPct=40.0)
            t["now"] = 3.0
            pusher.sample()
            self.assertTrue(pusher.due())            # the server's nextMs 3000 took
            pusher.flush()
            self.assertIs(pusher._conn, first_conn)  # the same keep-alive connection
            self.assertEqual([r[0] for r in received], ["/api/print/hub/telemetry"] * 2)
            self.assertEqual(received[0][1], "Bearer secret")
            self.assertIs(received[0][2]["full"], True)
            self.assertEqual(received[1][2]["printers"], [{"printerId": "p1", "sampledAt": 1760540400000, "progressPct": 40.0}])
        finally:
            pusher._drop_connection()
            srv.shutdown()
            srv.server_close()


class TestTelemetryConfig(unittest.TestCase):
    def test_start_telemetry_is_off_at_zero_and_a_daemon_thread_otherwise(self):
        stop = threading.Event()
        self.assertIsNone(telemetry.start_telemetry(Config("https://x.test", telemetry_sec=0), "b", list, stop))
        with mock.patch.object(telemetry.TelemetryPusher, "flush", lambda self: None):
            t = telemetry.start_telemetry(Config("https://x.test"), "b", list, stop)
            self.assertEqual((t.name, t.daemon), ("telemetry", True))
            stop.set()
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertEqual(Config("https://x.test").telemetry_sec, 5)
        self.assertEqual(Config("https://x.test").telemetry_url, "https://x.test/api/print/hub/telemetry")

    def test_zero_survives_env_and_config_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            with mock.patch.object(config_module, "CONFIG_PATH", path), \
                 mock.patch.dict("os.environ", {"MAKEROS_HUB_CLOUD_URL": "https://x.test"}, clear=False):
                import os
                os.environ.pop("MAKEROS_HUB_TELEMETRY_SEC", None)
                self.assertEqual(config_module.load_config().telemetry_sec, 5)
                path.write_text('telemetry_sec = 0\n')
                self.assertEqual(config_module.load_config().telemetry_sec, 0)
                path.write_text('telemetry_sec = 10\n')
                self.assertEqual(config_module.load_config().telemetry_sec, 10)
                os.environ["MAKEROS_HUB_TELEMETRY_SEC"] = "0"
                self.assertEqual(config_module.load_config().telemetry_sec, 0)
                os.environ["MAKEROS_HUB_TELEMETRY_SEC"] = "-3"
                self.assertEqual(config_module.load_config().telemetry_sec, 0)


if __name__ == "__main__":
    unittest.main()
