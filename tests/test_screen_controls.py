"""v0.76: the shared touchscreen's Klipper actions (klipper_action) and the readings shown beside them; v0.79: the rules
that protect the printer from a tap. The planner is pure (_parse_action, then _check_state); the wire path runs against a
threaded stdlib Moonraker stub that records every request."""

import http.server
import json
import threading
import time
import unittest
import urllib.parse

from makeros_hub import telemetry
from makeros_hub.printers import klipper
from makeros_hub.printers.klipper import KlipperAdapter, _build_status, _check_state, _parse_action, _Refused

# An SV08-shaped idle printer, homed at Z10 (where its homing_override ends), the nozzle cold, Sovol's load variable 250.
IDLE = {"print_stats": {"state": "standby"},
        "toolhead": {"homed_axes": "xyz", "position": [100.0, 200.0, 10.0, 0.0],
                     "axis_minimum": [0.0, 0.0, -5.0, 0.0], "axis_maximum": [500.0, 500.0, 500.0, 0.0]},
        "extruder": {"temperature": 24.0, "target": 0.0}, "heater_bed": {"temperature": 24.0, "target": 0.0},
        "gcode_macro _global_var": {"load_filament_extruder_temp": 250}}
HOT = {**IDLE, "extruder": {"temperature": 214.0, "target": 215.0}}
SV08_OBJECTS = ["gcode_macro MOVE_TO_CENTER", "gcode_macro LOAD_FILAMENT", "gcode_macro _CONTINUE_LOAD_FILAMENT",
                "gcode_macro UNLOAD_FILAMENT", "gcode_macro mainled_on", "gcode_macro mainled_off",
                "gcode_macro CLEAN_NOZZLE", "quad_gantry_level", "bed_mesh", "toolhead", "output_pin main_led"]
VAR = "SET_GCODE_VARIABLE MACRO=_global_var VARIABLE=load_filament_extruder_temp VALUE="


def state(s: str, base=IDLE) -> dict:
    return {**base, "print_stats": {"state": s}}


def at(x, y, z, base=IDLE, **toolhead) -> dict:
    return {**base, "toolhead": {**base["toolhead"], "position": [x, y, z, 0.0], **toolhead}}


def plan(params, status=IDLE, proven=True, z_tuned=0.0):
    """_parse_action, then _check_state: the adapter's own order once it is connected. -> (action, script, need)."""
    action, script, need = _parse_action(params)
    return action, _check_state(params, script, status, home_proven=proven, z_tuned=z_tuned), need


def refusal(params, status=IDLE, proven=True, z_tuned=0.0) -> str:
    try:
        plan(params, status, proven, z_tuned)
    except _Refused as r:
        return r.reason
    return "accepted"


class TestWhitelist(unittest.TestCase):
    def test_unknown_or_missing_action_is_unsupported(self):
        for params in (None, {}, {"action": "G28"}, {"action": "raw_gcode", "script": "M84"}, {"action": 1},
                       {"action": ["home"]}, {"action": "HOME"}, "home"):
            self.assertEqual(refusal(params), "unsupported_action", params)

    def test_every_plain_action_maps_to_its_named_command(self):
        # v0.79: Home ends 10 mm up; QGL re-homes Z after levelling
        expect = {"home": "G28\nG90\nG1 Z10 F600", "center": "MOVE_TO_CENTER", "motors_off": "M84",
                  "load_more": "_CONTINUE_LOAD_FILAMENT", "cooldown": "TURN_OFF_HEATERS",
                  "qgl": "QUAD_GANTRY_LEVEL\nG28 Z", "bed_mesh": "BED_MESH_CALIBRATE", "clean_nozzle": "CLEAN_NOZZLE",
                  "emergency_stop": "/printer/emergency_stop", "firmware_restart": "/printer/firmware_restart"}
        for action, script in expect.items():
            self.assertEqual(plan({"action": action}, HOT)[:2], (action, script))
        self.assertEqual(plan({"action": "qgl"}, IDLE)[2], "quad_gantry_level")
        self.assertEqual(plan({"action": "load_more"}, HOT)[2], "gcode_macro _continue_load_filament")
        self.assertEqual(plan({"action": "load_filament", "material": "PLA"})[2], "gcode_macro load_filament")
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
        self.assertEqual(refusal({"action": "preheat"}), "bad_params")
        self.assertEqual(refusal({"action": "load_filament"}), "bad_params")      # v0.79: names its material
        self.assertEqual(refusal({"action": "load_more", "material": "PLA"}), "bad_params")

    def test_jog_steps_are_exactly_the_set(self):
        middle = at(250.0, 250.0, 250.0)
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

    def test_preheat_names_a_material_and_holds_the_nozzle_at_standby(self):
        # v0.79 (the operator's rule): the bed for the material, the nozzle at 140 until the print's start G-code raises it
        self.assertEqual(plan({"action": "preheat", "material": "PLA"})[1], "M104 S140\nM140 S60")
        self.assertEqual(plan({"action": "preheat", "material": "PETG"})[1], "M104 S140\nM140 S70")
        for material in ("ABS", "TPU", "pla", "", None, 215, ["PLA"]):
            self.assertEqual(refusal({"action": "preheat", "material": material}), "bad_params", material)
        self.assertEqual(refusal({"action": "preheat", "nozzle": 215, "bed": 60}), "bad_params")   # v0.76's numbers

    def test_every_temperature_stays_under_the_caps(self):
        # the Giga's Q3 caps, nozzle 250 and bed 100; a load needs at least MIN_EXTRUDE_C
        for material, (nozzle, bed) in klipper.MATERIALS.items():
            self.assertTrue(klipper.MIN_EXTRUDE_C <= nozzle <= 250 and 0 < bed <= 100, material)
        self.assertEqual(set(klipper.MATERIALS), {"PLA", "PETG"})
        self.assertLess(klipper.NOZZLE_STANDBY_C, klipper.MIN_EXTRUDE_C)

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

    def test_z_offset_total_per_print_is_capped(self):
        # v0.79: each nudge is at most 0.1, but twenty of them no longer push the nozzle 2 mm into the plate
        printing, z = state("printing"), {"action": "z_offset", "delta": -0.05}
        self.assertEqual(refusal(z, printing, z_tuned=-0.25), "accepted")              # to -0.30, the limit
        self.assertEqual(refusal(z, printing, z_tuned=-0.3), "z_offset_limit")
        self.assertEqual(refusal({"action": "z_offset", "delta": 0.05}, printing, z_tuned=-0.3), "accepted")  # back up
        self.assertEqual(refusal({"action": "z_offset", "delta": 0.1}, printing, z_tuned=0.25), "z_offset_limit")

    def test_speed_and_fan(self):
        printing = state("printing")
        for pct in (10, 100, 150):
            self.assertEqual(refusal({"action": "speed", "pct": pct}, printing), "accepted")
        for pct in (9, 151, 300, 0, 100.0, True, "100", None):   # v0.79: capped at 150 % (the Giga's Q9)
            self.assertEqual(refusal({"action": "speed", "pct": pct}, printing), "bad_params", pct)
        self.assertEqual(plan({"action": "speed", "pct": 150}, printing)[1], "M220 S150")
        for pct, s in ((0, 0), (1, 3), (50, 127), (100, 255)):
            self.assertEqual(plan({"action": "fan", "pct": pct}, printing)[1], f"M106 S{s}")
        for pct in (-1, 101, 50.5, True, "50"):
            self.assertEqual(refusal({"action": "fan", "pct": pct}, printing), "bad_params", pct)


class TestStateRules(unittest.TestCase):
    IDLE_ONLY = [{"action": a} for a in ("home", "center", "motors_off", "qgl", "bed_mesh", "clean_nozzle", "cooldown")] + [
        {"action": "jog", "axis": "x", "mm": 1}, {"action": "preheat", "material": "PLA"}]
    FILAMENT = [{"action": "load_filament", "material": "PLA"}, {"action": "unload_filament", "material": "PETG"},
                {"action": "load_more"}]
    PRINT_ONLY = [{"action": "z_offset", "delta": 0.01}, {"action": "speed", "pct": 100}, {"action": "fan", "pct": 50}]
    ANY = [{"action": "light", "on": True}, {"action": "emergency_stop"}, {"action": "firmware_restart"}]

    def test_moves_and_heat_are_refused_during_a_print(self):
        for s in ("printing", "paused"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "printer_busy", (s, params))
        for s in ("standby", "complete", "cancelled", "error"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "accepted", (s, params))

    def test_filament_moves_run_idle_or_paused_never_while_printing(self):
        # v0.79 (report D5): a runout pause was a dead end
        for params in self.FILAMENT:
            self.assertEqual(refusal(params, state("printing", HOT)), "printer_busy", params)
            for s in ("paused", "standby", "complete", "cancelled", "error"):
                self.assertEqual(refusal(params, state(s, HOT)), "accepted", (s, params))

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

    def test_homing_waits_for_a_calibrated_probe_and_a_bed_under_90(self):
        # v0.79 (report D10, D9): on Barnes a G28 would calibrate and SAVE_CONFIG (a Klipper restart); the eddy probe left
        # its range at a 100 C bed
        uncalibrated = {**IDLE, "probe": {"is_calibrated": False}}
        for a in ("home", "center", "qgl", "bed_mesh", "clean_nozzle"):
            self.assertEqual(refusal({"action": a}, uncalibrated), "probe_uncalibrated", a)
            self.assertEqual(refusal({"action": a}, {**IDLE, "probe": {"is_calibrated": True}}), "accepted", a)
            self.assertEqual(refusal({"action": a}, {**IDLE, "heater_bed": {"temperature": 90.0, "target": 0}}),
                             "bed_too_hot", a)
            self.assertEqual(refusal({"action": a}, {**IDLE, "heater_bed": {"temperature": 40.0, "target": 100}}),
                             "bed_too_hot", a)   # heating there: it would pass 90 mid-probe
            self.assertEqual(refusal({"action": a}, {**IDLE, "heater_bed": {"temperature": 89.0, "target": 70}}),
                             "accepted", a)
        self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": 1}, uncalibrated), "accepted")   # no probing
        self.assertEqual(refusal({"action": "home"}, {k: v for k, v in IDLE.items() if k != "heater_bed"}), "accepted")


class TestJogLimits(unittest.TestCase):
    def test_only_after_a_home_this_hub_saw_finish(self):
        # v0.79: homed_axes alone counts the SV08's guessed Z (homing_override set_position_z) and the Giga's boot home
        self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": 1}, IDLE, proven=False), "not_homed")
        for homed in ("", "xy", None):
            st = at(100.0, 200.0, 10.0, homed_axes=homed)
            self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, st), "not_homed", homed)
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, {"print_stats": {"state": "standby"}}), "not_homed")
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, at(100.0, 200.0, 10.0, homed_axes="XYZ")),
                         "accepted")

    def test_z_never_goes_under_the_floor(self):
        # v0.79 (report D1): the floor was axis_minimum (-10 on the SV08), so a jog after a home could drive the nozzle
        # into the plate; now JOG_Z_FLOOR_MM (2), or position_min when that is higher
        jog = lambda mm, st=IDLE: refusal({"action": "jog", "axis": "z", "mm": mm}, st)   # noqa: E731
        self.assertEqual(jog(-1), "accepted")                         # 10 -> 9
        self.assertEqual(jog(-10), "z_floor")                         # 10 -> 0
        self.assertEqual(jog(-1, at(100.0, 200.0, 3.0)), "accepted")  # 3 -> 2, exactly the floor
        self.assertEqual(jog(-0.1, at(100.0, 200.0, 2.0)), "z_floor")
        self.assertEqual(jog(1, at(100.0, 200.0, 0.0)), "accepted")   # up is always the way out
        high_floor = at(100.0, 200.0, 10.0, axis_minimum=[0.0, 0.0, 5.0, 0.0])
        self.assertEqual(jog(-10, high_floor), "z_floor")
        self.assertEqual(jog(-1, at(100.0, 200.0, 6.0, high_floor)), "accepted")
        self.assertEqual(jog(-10, at(100.0, 200.0, 250.0)), "accepted")

    def test_sideways_only_clear_of_the_plate(self):
        # v0.79 (report D2): no X/Y move with the nozzle under the floor (its clips, the SV08's wiper brush)
        low = at(100.0, 200.0, 1.0)
        for axis in "xy":
            self.assertEqual(refusal({"action": "jog", "axis": axis, "mm": 1}, low), "lift_first", axis)
            self.assertEqual(refusal({"action": "jog", "axis": axis, "mm": 1}, at(100.0, 200.0, 2.0)), "accepted", axis)

    def test_out_of_range_both_ends_and_the_exact_edge(self):
        jog = lambda axis, mm: refusal({"action": "jog", "axis": axis, "mm": mm})   # noqa: E731
        self.assertEqual(jog("x", -50), "accepted")          # 100 -> 50
        self.assertEqual(jog("z", 50), "accepted")
        edge = at(450.0, 499.9, 499.9)
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 50}, edge), "accepted")       # exactly 500
        self.assertEqual(refusal({"action": "jog", "axis": "y", "mm": 0.1}, edge), "accepted")      # 499.9 + 0.1
        self.assertEqual(refusal({"action": "jog", "axis": "y", "mm": 1}, edge), "out_of_range")
        self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": 1}, edge), "out_of_range")
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": -10}, at(5.0, 200.0, 10.0)), "out_of_range")

    def test_unreadable_limits_are_stale(self):
        st = {**IDLE, "toolhead": {"homed_axes": "xyz", "position": [1.0, 2.0, 3.0, 0.0]}}
        self.assertEqual(refusal({"action": "jog", "axis": "x", "mm": 1}, st), "printer_stale")


class TestFilament(unittest.TestCase):
    def test_load_and_unload_at_the_materials_temperature_then_the_printers_value_back(self):
        # v0.79 (report D4): Sovol's LOAD_FILAMENT heated every material to 250 and left it there; its own M109 waits
        self.assertEqual(plan({"action": "load_filament", "material": "PLA"})[1],
                         f"{VAR}215\nLOAD_FILAMENT\n{VAR}250")
        self.assertEqual(plan({"action": "unload_filament", "material": "PETG"})[1],
                         f"{VAR}240\nUNLOAD_FILAMENT\n{VAR}250")
        mine = {**IDLE, "gcode_macro _global_var": {"load_filament_extruder_temp": 230.0}}
        self.assertTrue(plan({"action": "load_filament", "material": "PLA"}, mine)[1].endswith(f"{VAR}230"))

    def test_a_printer_whose_load_temperature_cannot_be_set_is_refused(self):
        for status in ({k: v for k, v in IDLE.items() if k != "gcode_macro _global_var"},
                       {**IDLE, "gcode_macro _global_var": {}}):
            for a in ("load_filament", "unload_filament"):
                self.assertEqual(refusal({"action": a, "material": "PLA"}, status), "unsupported_on_printer", a)

    def test_lift_before_an_idle_load_or_unload_when_low_and_truly_homed(self):
        # the Giga's Q6; the SV08's unload pushes 30 mm before it pulls back
        low = at(100.0, 200.0, 0.4)
        for a in ("load_filament", "unload_filament"):
            self.assertTrue(plan({"action": a, "material": "PLA"}, low)[1].startswith(f"G90\nG1 Z10 F600\n{VAR}215\n"), a)
            for status, proven in ((low, False), (state("paused", low), True), (IDLE, True),
                                   (at(100.0, 200.0, 0.4, homed_axes="xy"), True),
                                   (at(100.0, 200.0, 0.4, axis_maximum=[500.0, 500.0, 8.0, 0.0]), True)):
                self.assertTrue(plan({"action": a, "material": "PLA"}, status, proven)[1].startswith(VAR), (a, status))

    def test_load_more_waits_for_the_heat(self):
        # the Giga's Q1: set to a filament temperature, within 5 C of it, never under 170
        more = lambda t, target: refusal({"action": "load_more"}, {**IDLE, "extruder": {"temperature": t, "target": target}})  # noqa: E731
        self.assertEqual(more(214.0, 215.0), "accepted")
        self.assertEqual(more(210.0, 215.0), "accepted")
        self.assertEqual(more(209.0, 215.0), "nozzle_heating")
        self.assertEqual(more(200.0, 0.0), "nozzle_cold")       # hot but its heater is off: cooling
        self.assertEqual(more(140.0, 140.0), "nozzle_cold")     # the standby
        self.assertEqual(more(None, None), "nozzle_cold")
        self.assertEqual(refusal({"action": "load_more"}, state("paused", HOT)), "accepted")


class Moonraker:
    """Threaded stub: GET /printer/objects/query answers `status`, GET /printer/objects/list `objects` (each list GET is
    recorded in `gets`; `objects_http` != 200 fails it); every POST recorded as (path, script or None) and answered by
    `reply(path, script)` -> (status, body). `hold` blocks any script with a line listed in `held` until it is set."""

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
                if script and outer.held & set(script.split("\n")):
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
        self.a._home_proven = True        # as after a Home from here (TestHome covers how it is earned)

    def act(self, **params):
        return self.a.send_command("klipper_action", params)

    def test_short_action_waits_for_its_answer_and_sends_exactly_the_script(self):
        self.assertEqual(self.act(action="jog", axis="z", mm=-1), {"ok": True, "command": "klipper_action"})
        self.assertEqual(self.act(action="preheat", material="PLA")["ok"], True)
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "G91\nG1 Z-1 F600\nG90"),
                                         ("/printer/gcode/script", "M104 S140\nM140 S60")])
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
        self.mr.status = dict(HOT)
        for params in ({"action": "load_filament", "material": "PLA"}, {"action": "load_more"},
                       {"action": "unload_filament", "material": "PLA"},
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
        out = self.act(action="load_filament", material="PLA")
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
        self.act(action="load_filament", material="PLA")
        self.mr.hold.set()
        _wait(lambda: self.a._running_action is None)
        self.assertEqual(self.act(action="firmware_restart")["ok"], True)
        self.assertIn(("/printer/firmware_restart", None), self.mr.posts)
        self.act(action="unload_filament", material="PLA")
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


class TestHome(unittest.TestCase):
    """v0.79: a jog trusts only a full home this hub saw finish, never lost since."""

    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(IDLE))

    def act(self, **params):
        return self.a.send_command("klipper_action", params)

    def finish(self, **params):
        out = self.act(**params)
        self.assertTrue(_wait(lambda: self.a._running_action is None))
        return out

    def test_klippers_homed_axes_alone_is_not_a_home(self):
        self.assertEqual(self.act(action="jog", axis="z", mm=-1)["reason"], "not_homed")
        self.assertEqual(self.a.status()["toolhead"]["homed"], "")   # the screen says Home first
        self.assertEqual(self.mr.posts, [])

    def test_a_finished_home_earns_it_and_any_unhomed_reading_ends_it(self):
        self.assertTrue(self.finish(action="home")["ok"])
        self.assertEqual(self.mr.posts[-1], ("/printer/gcode/script", "G28\nG90\nG1 Z10 F600"))
        self.assertEqual(self.a.status()["toolhead"]["homed"], "xyz")
        self.assertTrue(self.act(action="jog", axis="z", mm=-1)["ok"])
        self.mr.status = at(100.0, 200.0, 10.0, homed_axes="")   # motors off from the printer's own screen
        self.assertEqual(self.act(action="jog", axis="z", mm=-1)["reason"], "not_homed")
        self.mr.status = dict(IDLE)                               # then homed_axes back without a home seen
        self.assertEqual(self.act(action="jog", axis="z", mm=-1)["reason"], "not_homed")

    def test_a_failed_home_or_an_unhomed_poll_leaves_it_unproven(self):
        self.mr.reply = lambda path, script: (400, {"error": {"message": "Endstop x still triggered after retract"}})
        self.assertFalse(self.finish(action="home")["ok"])
        self.assertFalse(self.a._home_proven)
        self.a._home_proven = True
        self.a._after_poll(at(100.0, 200.0, 10.0, homed_axes="xy"))
        self.assertFalse(self.a._home_proven)

    def test_motors_off_the_stops_and_the_probing_actions_end_it_qgl_and_centre_earn_it(self):
        for action, earns in (("motors_off", False), ("emergency_stop", False), ("firmware_restart", False),
                              ("bed_mesh", False), ("clean_nozzle", False), ("qgl", True), ("center", True)):
            self.a._home_proven = True
            self.finish(action=action)
            self.assertEqual(self.a._home_proven, earns, action)
        self.assertIn(("/printer/gcode/script", "QUAD_GANTRY_LEVEL\nG28 Z"), self.mr.posts)

    def test_a_failed_poll_ends_it(self):
        a = KlipperAdapter("p2", "http://127.0.0.1:1")   # nothing listens: the first poll fails
        a._home_proven = True
        stop = threading.Event()
        t = threading.Thread(target=a._poll_loop, args=(stop,), daemon=True)
        t.start()
        self.assertTrue(_wait(lambda: a._consecutive_failures >= 1))
        stop.set()
        t.join(5)
        self.assertFalse(a._home_proven)

    def test_an_idle_load_lifts_only_after_a_proven_home(self):
        self.mr.status = at(100.0, 200.0, 0.4)
        self.finish(action="load_filament", material="PLA")
        self.assertEqual(self.mr.posts[-1][1], f"{VAR}215\nLOAD_FILAMENT\n{VAR}250")
        self.a._home_proven = True
        self.finish(action="unload_filament", material="PETG")
        self.assertEqual(self.mr.posts[-1][1], f"G90\nG1 Z10 F600\n{VAR}240\nUNLOAD_FILAMENT\n{VAR}250")


class TestStandby(unittest.TestCase):
    """v0.79, the Giga's Q2: nothing waits hot after a filament change."""

    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(IDLE))

    def standby(self, minutes, target, s="standby"):
        """Run the poll's standby step for a filament move `minutes` ago; -> what it sent, and whether it ended."""
        fil_at = time.monotonic() - minutes * 60
        self.a._fil_at = fil_at
        self.mr.posts.clear()
        self.a._after_poll({**state(s), "extruder": {"temperature": target, "target": target}})
        return [p[1] for p in self.mr.posts], self.a._fil_at is None

    def test_down_to_140_at_10_min_and_off_at_30(self):
        self.assertEqual(self.standby(9, 215.0), ([], False))
        self.assertEqual(self.standby(11, 215.0), (["M104 S140"], False))
        self.assertEqual(self.standby(20, 140.0), ([], False))     # already there
        self.assertEqual(self.standby(31, 140.0), (["M104 S0"], True))
        self.assertEqual(self.standby(31, 0.0), ([], True))
        self.assertEqual(self.standby(11, 215.0, "complete"), (["M104 S140"], False))

    def test_a_print_ends_it_and_a_pause_never_cools(self):
        # the SV08's RESUME does not reheat: a paused nozzle stays at the print's temperature
        self.assertEqual(self.standby(45, 220.0, "paused"), ([], False))
        self.assertEqual(self.standby(5, 220.0, "printing"), ([], True))
        self.assertEqual(self.standby(45, 220.0, "printing"), ([], True))

    def test_not_while_a_long_action_runs_and_a_failed_send_ends_it(self):
        self.a._running_action = "load_filament"
        self.assertEqual(self.standby(11, 215.0), ([], False))
        self.a._running_action = None
        self.mr.reply = lambda path, script: (400, {"error": {"message": "Printer is shutdown"}})
        self.assertEqual(self.standby(11, 215.0), (["M104 S140"], True))

    def test_filament_moves_start_it_and_a_new_heat_plan_ends_it(self):
        self.mr.status = dict(HOT)
        for params in ({"action": "load_filament", "material": "PLA"}, {"action": "load_more"},
                       {"action": "unload_filament", "material": "PLA"}):
            self.a._fil_at = None
            self.a.send_command("klipper_action", params)
            self.assertTrue(_wait(lambda: self.a._running_action is None))
            self.assertIsNotNone(self.a._fil_at, params)
        for params in ({"action": "preheat", "material": "PLA"}, {"action": "cooldown"}, {"action": "emergency_stop"}):
            self.a._fil_at = time.monotonic()
            self.assertTrue(self.a.send_command("klipper_action", params)["ok"], params)
            self.assertIsNone(self.a._fil_at, params)


class TestZTune(unittest.TestCase):
    def test_the_nudges_add_up_per_print_and_start_again_after_it(self):
        mr = Moonraker()
        self.addCleanup(mr.close)
        mr.status = state("printing")
        a = KlipperAdapter("p1", mr.url)
        a._record_poll(state("printing"))
        nudge = lambda: a.send_command("klipper_action", {"action": "z_offset", "delta": -0.1})   # noqa: E731
        self.assertEqual([nudge()["ok"] for _ in range(3)], [True, True, True])
        self.assertEqual(nudge()["reason"], "z_offset_limit")
        a._after_poll(state("paused"))
        self.assertEqual(nudge()["reason"], "z_offset_limit")
        a._after_poll(state("complete"))
        self.assertTrue(nudge()["ok"])


if __name__ == "__main__":
    unittest.main()
