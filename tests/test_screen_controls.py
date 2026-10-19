"""v0.76: the shared touchscreen's Klipper actions (klipper_action) and the readings shown beside them. The planner is pure
(_plan_action); the wire path runs against a threaded stdlib Moonraker stub that records every request."""

import http.server
import json
import threading
import time
import unittest
import urllib.parse

from makeros_hub import telemetry
from makeros_hub.printers import klipper
from makeros_hub.printers.klipper import KlipperAdapter, _build_status, _check_state, _parse_action, _Refused

IDLE = {"print_stats": {"state": "standby"},
        "toolhead": {"homed_axes": "xyz", "position": [100.0, 200.0, 10.0, 0.0],
                     "axis_minimum": [0.0, 0.0, -5.0, 0.0], "axis_maximum": [500.0, 500.0, 500.0, 0.0]}}
SV08_OBJECTS = ["gcode_macro MOVE_TO_CENTER", "gcode_macro LOAD_FILAMENT", "gcode_macro _CONTINUE_LOAD_FILAMENT",
                "gcode_macro UNLOAD_FILAMENT", "gcode_macro mainled_on", "gcode_macro mainled_off",
                "gcode_macro CLEAN_NOZZLE", "quad_gantry_level", "bed_mesh", "toolhead", "output_pin main_led"]


def state(s: str) -> dict:
    return {**IDLE, "print_stats": {"state": s}}


def plan(params, status=IDLE):
    """_parse_action, then _check_state: the adapter's own order once it is connected."""
    parsed = _parse_action(params)
    _check_state(params, status)
    return parsed


def refusal(params, status=IDLE) -> str:
    try:
        plan(params, status)
    except _Refused as r:
        return r.reason
    return "accepted"


class TestWhitelist(unittest.TestCase):
    def test_unknown_or_missing_action_is_unsupported(self):
        for params in (None, {}, {"action": "G28"}, {"action": "raw_gcode", "script": "M84"}, {"action": 1},
                       {"action": ["home"]}, {"action": "HOME"}, "home"):
            self.assertEqual(refusal(params), "unsupported_action", params)

    def test_every_plain_action_maps_to_its_named_command(self):
        expect = {"home": "G28", "center": "MOVE_TO_CENTER", "motors_off": "M84", "load_filament": "LOAD_FILAMENT",
                  "load_more": "_CONTINUE_LOAD_FILAMENT", "unload_filament": "UNLOAD_FILAMENT",
                  "cooldown": "TURN_OFF_HEATERS", "qgl": "QUAD_GANTRY_LEVEL", "bed_mesh": "BED_MESH_CALIBRATE",
                  "clean_nozzle": "CLEAN_NOZZLE", "emergency_stop": "/printer/emergency_stop",
                  "firmware_restart": "/printer/firmware_restart"}
        for action, script in expect.items():
            self.assertEqual(plan({"action": action}, IDLE)[:2], (action, script))
        self.assertEqual(plan({"action": "qgl"}, IDLE)[2], "quad_gantry_level")
        self.assertEqual(plan({"action": "load_more"}, IDLE)[2], "gcode_macro _continue_load_filament")
        self.assertIsNone(plan({"action": "home"}, IDLE)[2])

    def test_unknown_command_still_unsupported_and_offline_adapter_validates_first(self):
        a = KlipperAdapter("p", "http://127.0.0.1:1")   # never polled
        self.assertEqual(a.send_command("klipper_action", {"action": "nope"}),
                         {"ok": False, "reason": "unsupported_action", "command": "klipper_action"})
        self.assertEqual(a.send_command("klipper_action", {"action": "home", "x": 1})["reason"], "bad_params")
        self.assertEqual(a.send_command("klipper_action", {"action": "home"})["reason"], "not_connected")
        self.assertEqual(a.send_command("klipper_action", {"action": "emergency_stop"})["reason"], "not_connected")


class TestParams(unittest.TestCase):
    def test_extra_or_missing_keys(self):
        self.assertEqual(refusal({"action": "home", "axes": "xy"}), "bad_params")
        self.assertEqual(refusal({"action": "jog", "axis": "x"}), "bad_params")
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1, "speed": 9000}), "bad_params")
        self.assertEqual(refusal({"action": "preheat", "nozzle": 200}), "bad_params")

    def test_jog_steps_are_exactly_the_set(self):
        middle = {**IDLE, "toolhead": {**IDLE["toolhead"], "position": [250.0, 250.0, 250.0, 0.0]}}
        for mm in (0.1, -0.1, 1, -1, 1.0, 10, -10, 50, -50.0):
            for axis in "xyz":
                self.assertEqual(refusal({"action": "jog", "axis": axis, "mm": mm}, middle), "accepted", (axis, mm))
        for mm in (0, 0.5, 5, 2, 100, -100, 0.01, 0.10000001, True, "10", None, float("nan"), float("inf")):
            self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": mm}), "bad_params", mm)
        for axis in ("X", "e", "xy", "", 1, None, ["x"], {"x": 1}):
            self.assertEqual(refusal({"action": "jog", "axis": axis, "mm": 1}), "bad_params", axis)

    def test_jog_script(self):
        self.assertEqual(plan({"action": "jog", "axis": "x", "mm": -10}, IDLE)[1], "G91\nG1 X-10 F6000\nG90")
        self.assertEqual(plan({"action": "jog", "axis": "y", "mm": 0.1}, IDLE)[1], "G91\nG1 Y0.1 F6000\nG90")
        self.assertEqual(plan({"action": "jog", "axis": "z", "mm": 1.0}, IDLE)[1], "G91\nG1 Z1 F600\nG90")

    def test_preheat(self):
        ok = lambda n, b: refusal({"action": "preheat", "nozzle": n, "bed": b})   # noqa: E731
        for n, b in ((0, 0), (300, 110), (215, 60)):
            self.assertEqual(ok(n, b), "accepted")
        for n, b in ((301, 60), (-1, 60), (200, 111), (200, -1), (200.0, 60), (200, 60.5), (True, 60), ("200", 60),
                     (None, 60)):
            self.assertEqual(ok(n, b), "bad_params", (n, b))
        self.assertEqual(plan({"action": "preheat", "nozzle": 215, "bed": 60}, IDLE)[1], "M104 S215\nM140 S60")

    def test_light(self):
        self.assertEqual(plan({"action": "light", "on": True}, IDLE), ("light", "mainled_on", "gcode_macro mainled_on"))
        self.assertEqual(plan({"action": "light", "on": False}, IDLE), ("light", "mainled_off", "gcode_macro mainled_off"))
        for on in (1, 0, "true", None):
            self.assertEqual(refusal({"action": "light", "on": on}), "bad_params", on)

    def test_z_offset(self):
        printing = state("printing")
        for d in (0.1, -0.1, 0.01, -0.025, 0.005):
            self.assertEqual(refusal({"action": "z_offset", "delta": d}, printing), "accepted", d)
        for d in (0, 0.0, 0.1000001, -0.11, 1, True, "0.01", None, float("nan"), float("inf")):
            self.assertEqual(refusal({"action": "z_offset", "delta": d}, printing), "bad_params", d)
        self.assertEqual(plan({"action": "z_offset", "delta": -0.025}, printing)[1],
                         "SET_GCODE_OFFSET Z_ADJUST=-0.025 MOVE=1")

    def test_speed_and_fan(self):
        printing = state("printing")
        for pct in (10, 100, 300):
            self.assertEqual(refusal({"action": "speed", "pct": pct}, printing), "accepted")
        for pct in (9, 301, 0, 100.0, True, "100", None):
            self.assertEqual(refusal({"action": "speed", "pct": pct}, printing), "bad_params", pct)
        self.assertEqual(plan({"action": "speed", "pct": 150}, printing)[1], "M220 S150")
        for pct, s in ((0, 0), (1, 3), (50, 127), (100, 255)):
            self.assertEqual(plan({"action": "fan", "pct": pct}, printing)[1], f"M106 S{s}")
        for pct in (-1, 101, 50.5, True, "50"):
            self.assertEqual(refusal({"action": "fan", "pct": pct}, printing), "bad_params", pct)


class TestStateRules(unittest.TestCase):
    IDLE_ONLY = [{"action": a} for a in ("home", "center", "motors_off", "load_filament", "load_more", "unload_filament",
                                         "qgl", "bed_mesh", "clean_nozzle", "cooldown")] + [
        {"action": "jog", "axis": "x", "mm": 1}, {"action": "preheat", "nozzle": 200, "bed": 60}]
    PRINT_ONLY = [{"action": "z_offset", "delta": 0.01}, {"action": "speed", "pct": 100}, {"action": "fan", "pct": 50}]
    ANY = [{"action": "light", "on": True}, {"action": "emergency_stop"}, {"action": "firmware_restart"}]

    def test_moves_heat_and_filament_are_refused_during_a_print(self):
        for s in ("printing", "paused"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "printer_busy", (s, params))
        for s in ("standby", "complete", "cancelled", "error"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "accepted", (s, params))

    def test_tuning_only_during_a_print(self):
        for s in ("standby", "complete", "cancelled", "error"):
            for params in self.PRINT_ONLY:
                self.assertEqual(refusal(params, state(s)), "not_printing", (s, params))
        for s in ("printing", "paused"):
            for params in self.PRINT_ONLY:
                self.assertEqual(refusal(params, state(s)), "accepted", (s, params))

    def test_light_and_the_stops_run_in_any_state(self):
        for s in ("standby", "printing", "paused", "error"):
            for params in self.ANY:
                self.assertEqual(refusal(params, state(s)), "accepted", (s, params))

    def test_no_print_stats(self):
        self.assertEqual(refusal({"action": "home"}, {}), "printer_stale")
        self.assertEqual(refusal({"action": "light", "on": True}, {"print_stats": {}}), "printer_stale")
        self.assertEqual(refusal({"action": "emergency_stop"}, {}), "accepted")   # the way out never waits on a reading


class TestJogLimits(unittest.TestCase):
    def test_not_homed(self):
        for homed in ("", "xy", None):
            st = {**IDLE, "toolhead": {**IDLE["toolhead"], "homed_axes": homed}}
            self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": 1}, st), "not_homed", homed)
        st = {**IDLE, "toolhead": {**IDLE["toolhead"], "homed_axes": "xy"}}
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, st), "accepted")
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, {"print_stats": {"state": "standby"}}), "not_homed")

    def test_out_of_range_both_ends_and_the_exact_edge(self):
        jog = lambda axis, mm: refusal({"action": "jog", "axis": axis, "mm": mm})   # noqa: E731
        self.assertEqual(jog("x", -50), "accepted")          # 100 -> 50
        self.assertEqual(jog("z", -10), "accepted")          # 10 -> 0, the floor is -5
        self.assertEqual(jog("z", -50), "out_of_range")      # 10 -> -40
        edge = {**IDLE, "toolhead": {**IDLE["toolhead"], "position": [450.0, 499.9, -5.0, 0.0]}}
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 50}, edge), "accepted")       # exactly 500
        self.assertEqual(refusal({"action": "jog", "axis": "y", "mm": 0.1}, edge), "accepted")      # 499.9 + 0.1
        self.assertEqual(refusal({"action": "jog", "axis": "y", "mm": 1}, edge), "out_of_range")
        self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": -0.1}, edge), "out_of_range")

    def test_unreadable_limits_are_stale(self):
        st = {**IDLE, "toolhead": {"homed_axes": "xyz", "position": [1.0, 2.0, 3.0, 0.0]}}
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, st), "printer_stale")


class Moonraker:
    """Threaded stub: GET /printer/objects/query answers `status`, GET /printer/objects/list `objects` (each list GET is
    recorded in `gets`; `objects_http` != 200 fails it); every POST recorded as (path, script or None) and answered by
    `reply(path, script)` -> (status, body). `hold` blocks any script listed in `held` until it is set."""

    def __init__(self, objects=SV08_OBJECTS):
        self.objects = list(objects)
        self.objects_http = 200
        self.status = dict(IDLE)
        self.posts: list[tuple[str, str | None]] = []
        self.gets: list[str] = []
        self.hold = threading.Event()
        self.held: set[str] = set()
        self.reply = lambda path, script: (200, {"result": "ok"})
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, doc):
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/printer/objects/query?"):
                    return self._send(200, {"result": {"status": outer.status}})
                outer.gets.append(self.path)
                self._send(outer.objects_http, {"result": {"objects": outer.objects}})

            def do_POST(self):
                parts = urllib.parse.urlsplit(self.path)
                script = urllib.parse.parse_qs(parts.query).get("script", [None])[0]
                outer.posts.append((parts.path, script))
                if script in outer.held:
                    outer.hold.wait(10)
                self._send(*outer.reply(parts.path, script))

            def log_message(self, *_a, **_k):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()


class TestWire(unittest.TestCase):
    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(IDLE))   # one fresh poll, no thread

    def act(self, **params):
        return self.a.send_command("klipper_action", params)

    def test_short_action_waits_for_its_answer_and_sends_exactly_the_script(self):
        self.assertEqual(self.act(action="jog", axis="z", mm=-1), {"ok": True, "command": "klipper_action"})
        self.assertEqual(self.act(action="preheat", nozzle=215, bed=60)["ok"], True)
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "G91\nG1 Z-1 F600\nG90"),
                                         ("/printer/gcode/script", "M104 S215\nM140 S60")])
        self.assertEqual(self.mr.gets, [])   # built-in commands never ask for the objects list

    def test_klipper_refusing_a_jog_maps_its_words_and_puts_g90_back(self):
        self.mr.reply = lambda path, script: ((400, {"error": {"message": "Move out of range: 510.000 0.000 0.000 [0.000]"}})
                                              if script and "G1" in script else (200, {"result": "ok"}))
        self.assertEqual(self.act(action="jog", axis="x", mm=10)["reason"], "out_of_range")
        self.assertEqual(self.mr.posts[-1], ("/printer/gcode/script", "G90"))
        self.mr.reply = lambda path, script: (400, {"error": {"message": "Must home axis first: 0.000 0.000 0.000"}})
        self.assertEqual(self.act(action="jog", axis="x", mm=10)["reason"], "not_homed")

    def test_missing_macro_or_module_is_unsupported_on_printer(self):
        self.mr.objects = ["toolhead", "gcode_macro mainled_on"]
        for params in ({"action": "load_filament"}, {"action": "load_more"}, {"action": "unload_filament"},
                       {"action": "center"}, {"action": "qgl"}, {"action": "bed_mesh"}, {"action": "clean_nozzle"},
                       {"action": "light", "on": False}):
            self.assertEqual(self.act(**params)["reason"], "unsupported_on_printer", params)
        self.assertEqual(self.mr.posts, [])
        self.assertEqual(len(self.mr.gets), 1)   # cached
        self.assertEqual(self.act(action="light", on=True)["ok"], True)
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "mainled_on")])

    def test_unreadable_state_or_objects_list_runs_nothing(self):
        a = KlipperAdapter("p2", "http://127.0.0.1:1")
        a._record_poll(dict(IDLE))
        self.assertIn(a.send_command("klipper_action", {"action": "home"})["reason"], ("unreachable", "timeout"))
        self.mr.objects_http = 500
        self.assertEqual(self.act(action="qgl")["reason"], "klipper_error")
        self.assertEqual(self.mr.posts, [])

    def test_a_stale_poll_is_not_connected(self):
        self.a._last_poll_ok_at = time.monotonic() - 600.0
        self.assertEqual(self.act(action="light", on=True)["reason"], "not_connected")
        self.assertEqual(self.act(action="emergency_stop")["reason"], "not_connected")
        self.assertEqual(self.mr.posts, [])

    def test_long_action_returns_promptly_and_holds_the_printer_until_it_ends(self):
        self.mr.held = {"LOAD_FILAMENT"}
        t0 = time.monotonic()
        out = self.act(action="load_filament")
        self.assertLess(time.monotonic() - t0, klipper.LONG_ACTION_GRACE_SEC + 1.0)
        self.assertEqual(out, {"ok": True, "detail": "started", "command": "klipper_action"})
        self.assertEqual(self.a._running_action, "load_filament")
        self.assertEqual(self.act(action="home")["reason"], "action_running")
        self.assertEqual(self.act(action="light", on=True)["reason"], "action_running")
        self.assertEqual(self.act(action="emergency_stop")["ok"], True)          # the way out is never held
        self.assertIn(("/printer/emergency_stop", None), self.mr.posts)
        self.mr.hold.set()
        self.assertTrue(_wait(lambda: self.a._running_action is None))
        self.assertEqual(self.act(action="home")["ok"], True)

    def test_long_action_refused_at_once_reports_the_reason(self):
        self.mr.reply = lambda path, script: (400, {"error": {"message": "Printer is shutdown"}})
        self.assertEqual(self.act(action="qgl"), {"ok": False, "reason": "klipper_error", "command": "klipper_action"})
        self.assertIsNone(self.a._running_action)

    def test_long_action_failing_later_goes_to_the_log(self):
        self.mr.held = {"G28"}
        self.mr.reply = lambda path, script: (400, {"error": {"message": "Endstop x still triggered after retract"}})
        with self.assertLogs("makeros_hub.printers.klipper", "WARNING") as logs:
            self.assertEqual(self.act(action="home")["detail"], "started")
            self.mr.hold.set()
            self.assertTrue(_wait(lambda: self.a._running_action is None))
        self.assertTrue(any("gcode_error" in line and "Endstop" in line for line in logs.output))

    def test_firmware_restart_drops_the_objects_cache(self):
        self.mr.held = {"LOAD_FILAMENT"}
        self.act(action="load_filament")
        self.mr.hold.set()
        _wait(lambda: self.a._running_action is None)
        self.assertEqual(self.act(action="firmware_restart")["ok"], True)
        self.assertIn(("/printer/firmware_restart", None), self.mr.posts)
        self.act(action="unload_filament")
        self.assertEqual(len(self.mr.gets), 2)

    def test_state_is_read_fresh_not_from_the_poll(self):
        # the poll (setUp) says standby at x=100; the printer has since started a print / moved to x=495
        self.mr.status = state("printing")
        self.assertEqual(self.act(action="home")["reason"], "printer_busy")
        self.assertEqual(self.act(action="speed", pct=120)["ok"], True)
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "M220 S120")])
        self.mr.status = {**IDLE, "toolhead": {**IDLE["toolhead"], "position": [495.0, 200.0, 10.0, 0.0]}}
        self.assertEqual(self.act(action="jog", axis="x", mm=10)["reason"], "out_of_range")
        self.assertEqual(self.act(action="emergency_stop")["ok"], True)


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestReadings(unittest.TestCase):
    def status(self, **objects):
        return _build_status("p", {"print_stats": {"state": "standby"}, **objects}, "connected")

    def test_the_query_asks_for_the_new_objects_url_encoded(self):
        for part in ("&toolhead=homed_axes,position,axis_minimum,axis_maximum", "&gcode_move=speed_factor,homing_origin",
                     "&fan=speed", "&fan_generic%20fan0=speed", "&output_pin%20main_led=value"):
            self.assertIn(part, klipper._QUERY_OBJECTS)
        self.assertNotIn(" ", klipper._QUERY_OBJECTS)

    def test_sv08_shaped_readings(self):
        out = self.status(toolhead={"homed_axes": "xyz", "position": [250.004, 249.996, 10.1234, 3.2]},
                          gcode_move={"speed_factor": 1.25, "homing_origin": [0.0, 0.0, -0.0251, 0.0]},
                          fan={"speed": None}, **{"fan_generic fan0": {"speed": 0.502},
                                                   "output_pin main_led": {"value": 1.0}})
        self.assertEqual(out["toolhead"], {"homed": "xyz", "x": 250.0, "y": 250.0, "z": 10.12})
        self.assertEqual((out["speedPct"], out["zOffset"], out["fanPct"], out["light"]), (125, -0.025, 50, True))

    def test_a_printer_with_fan_reads_fan_and_an_off_light_is_false(self):
        out = self.status(fan={"speed": 1.0}, **{"fan_generic fan0": {"speed": 0.0}, "output_pin main_led": {"value": 0}})
        self.assertEqual((out["fanPct"], out["light"]), (100, False))

    def test_absent_objects_are_omitted(self):
        out = self.status(toolhead={"homed_axes": "", "position": None}, gcode_move={"speed_factor": None},
                          fan={"speed": None}, **{"output_pin main_led": {"value": None}})
        self.assertEqual(out["toolhead"], {"homed": ""})
        for key in ("speedPct", "zOffset", "fanPct", "light"):
            self.assertNotIn(key, out)
        self.assertNotIn("toolhead", self.status())
        self.assertNotIn("toolhead", _build_status("p", {"toolhead": {"homed_axes": "xyz"}}, "offline"))

    def test_the_push_carries_them_and_an_idle_jog_is_sent(self):
        row = {"printerId": "p", "connectionState": "connected", "sampledAt": 1, "state": "idle",
               "toolhead": {"homed": "xyz", "x": 1.0, "y": 2.0, "z": 3.0}, "light": True, "speedPct": 100,
               "zOffset": 0.0, "fanPct": 0}
        first = telemetry.project(row)
        for key in ("toolhead", "light", "speedPct", "zOffset", "fanPct"):
            self.assertEqual(first[key], row[key])
        moved = telemetry.project({**row, "sampledAt": 2, "toolhead": {"homed": "xyz", "x": 11.0, "y": 2.0, "z": 3.0}})
        self.assertEqual(telemetry.delta(first, moved)["toolhead"]["x"], 11.0)
        self.assertEqual(telemetry.delta(first, telemetry.project({**row, "sampledAt": 2, "light": False}))["light"], False)


if __name__ == "__main__":
    unittest.main()
