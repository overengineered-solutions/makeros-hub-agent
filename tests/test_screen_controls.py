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
        "stepper_enable": {"steppers": {"stepper_x": True, "stepper_y": True, "stepper_z": True}},
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


def plan(params, status=IDLE, proven=True, z_tuned=0.0, xy=False):
    """_parse_action, then _check_state: the adapter's own order once it is connected. -> (action, script, need)."""
    action, script, need = _parse_action(params)
    return action, _check_state(params, script, status, home_proven=proven, z_tuned=z_tuned, xy_proven=xy), need


def refusal(params, status=IDLE, proven=True, z_tuned=0.0, xy=False) -> str:
    try:
        plan(params, status, proven, z_tuned, xy)
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
        self.assertIn(a.send_command("klipper_action", {"action": "emergency_stop"})["reason"], ("unreachable", "timeout"))


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
                # v0.85: a jog down in Z is at most JOG_Z_DOWN_MAX_MM (1) a tap; up and sideways keep every step
                want = "z_down_step" if axis == "z" and mm < -1 else "accepted"
                self.assertEqual(refusal({"action": "jog", "axis": axis, "mm": mm}, middle), want, (axis, mm))
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
    PRINT_ONLY = [{"action": "z_offset", "delta": 0.01}, {"action": "speed", "pct": 100}]
    # v0.85: the part fan joins them (the Controls tab turns it on or off on an idle printer)
    ANY = [{"action": "light", "on": True}, {"action": "emergency_stop"}, {"action": "firmware_restart"}, {"action": "fan", "pct": 50}]

    def test_moves_and_heat_are_refused_during_a_print(self):
        for s in ("printing", "paused"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "printer_busy", (s, params))
        for s in ("standby", "complete", "cancelled", "error"):
            for params in self.IDLE_ONLY:
                self.assertEqual(refusal(params, state(s)), "accepted", (s, params))

    def test_a_held_print_is_a_print_and_cool_down_lets_it_go(self):
        # v0.82: an error stop the printer holds keeps its part and its place; only Cool down ends it from here
        held = {**state("error", HOT), "gcode_macro _pstation_hold": {"active": True}}
        for params in self.IDLE_ONLY:
            want = "accepted" if params["action"] == "cooldown" else "printer_busy"
            self.assertEqual(refusal(params, held), want, params)
        for params in self.FILAMENT + self.ANY:
            self.assertEqual(refusal(params, held), "accepted", params)   # a repair, the light, the stops
        self.assertEqual(refusal({"action": "home"}, {**held, "gcode_macro _pstation_hold": {"active": None}}), "accepted")
        self.assertIn("&gcode_macro%20_pstation_hold=active", klipper._ACTION_QUERY)

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
        self.assertEqual(jog(-1, at(100.0, 200.0, 2.5)), "z_floor")   # 2.5 -> 1.5
        self.assertEqual(jog(-1, at(100.0, 200.0, 3.0)), "accepted")  # 3 -> 2, exactly the floor
        self.assertEqual(jog(-0.1, at(100.0, 200.0, 2.0)), "z_floor")
        self.assertEqual(jog(1, at(100.0, 200.0, 0.0)), "accepted")   # up is always the way out
        high_floor = at(100.0, 200.0, 5.5, axis_minimum=[0.0, 0.0, 5.0, 0.0])
        self.assertEqual(jog(-1, high_floor), "z_floor")
        self.assertEqual(jog(-1, at(100.0, 200.0, 6.0, high_floor)), "accepted")
        self.assertEqual(jog(-10, at(100.0, 200.0, 250.0)), "z_down_step")   # v0.85: down is at most 1 mm a tap

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
        self.assertEqual(self.mr.posts, [])
        self.assertTrue(self.act(action="emergency_stop")["ok"])
        self.assertEqual(self.mr.posts, [("/printer/emergency_stop", None)])
        self.a._last_poll_ok_at = None
        self.assertTrue(self.act(action="emergency_stop")["ok"])

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

    def test_the_heater_hold_only_when_the_printer_says_it_holds(self):
        self.assertIn("&gcode_macro%20_pstation_hold=active,minutes,ceiling_minutes,since", klipper._QUERY_OBJECTS)
        held = lambda **h: self.status(**{"gcode_macro _pstation_hold": h})
        self.assertEqual(held(active=True, minutes=12, ceiling_minutes=2880)["hold"], {"minutes": 12, "ceilingMinutes": 2880})
        self.assertEqual(held(active=True, minutes=1, ceiling_minutes=2880, since=92026.0239)["hold"],
                         {"minutes": 1, "ceilingMinutes": 2880, "since": 92026.0})
        self.assertNotIn("hold", held(active=False, minutes=12, ceiling_minutes=2880))
        self.assertNotIn("hold", held(active=None, minutes=None, ceiling_minutes=None))   # a printer without the hold
        self.assertNotIn("hold", held(active=True, minutes=3000, ceiling_minutes=2880))
        self.assertNotIn("hold", held(active=True, minutes=True, ceiling_minutes=2880))
        self.assertTrue(held(active=False, minutes=0, ceiling_minutes=2880)["heaterHold"])
        self.assertNotIn("heaterHold", held(active=None, minutes=None, ceiling_minutes=None))

    def test_a_klipper_error_carries_its_own_words(self):
        error = lambda **ps: _build_status("p", {"print_stats": {"state": "error", **ps}}, "connected")
        self.assertEqual(error(message="Extrude only move too long (-642.480mm vs 300.000mm)\nSee the 'max_extrude_only_distance' "
                               "config option for details")["errorReason"][:52], "Extrude only move too long (-642.480mm vs 300.000mm)")
        self.assertNotIn("errorReason", error(message=""))
        self.assertNotIn("errorReason", _build_status("p", {"print_stats": {"state": "paused", "message": "x"}}, "connected"))

    def test_a_hold_starting_or_ending_is_sent_at_once_for_a_printer_in_error(self):
        row = {"printerId": "p", "connectionState": "connected", "sampledAt": 1, "state": "error"}
        before = telemetry.project(row)
        held = telemetry.project({**row, "sampledAt": 2, "hold": {"minutes": 0, "ceilingMinutes": 2880}})
        self.assertEqual(telemetry.delta(before, held)["hold"], {"minutes": 0, "ceilingMinutes": 2880})
        self.assertIsNone(telemetry.delta(held, telemetry.project({**row, "sampledAt": 3}))["hold"])

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


class TestFans(unittest.TestCase):
    """v0.79 (the sv08 report's question 6): staff get the SV08's auxiliary (fan2) and exhaust (fan3) fans."""

    def test_whole_percent_any_state(self):
        self.assertEqual(plan({"action": "aux_fan", "pct": 50}), ("aux_fan", "SET_FAN_SPEED FAN=fan2 SPEED=0.5",
                                                                   "fan_generic fan2"))
        self.assertEqual(plan({"action": "exhaust_fan", "pct": 100})[1:], ("SET_FAN_SPEED FAN=fan3 SPEED=1",
                                                                           "fan_generic fan3"))
        self.assertEqual(plan({"action": "exhaust_fan", "pct": 0})[1], "SET_FAN_SPEED FAN=fan3 SPEED=0")
        for s in ("standby", "printing", "paused", "complete"):
            self.assertEqual(refusal({"action": "aux_fan", "pct": 30}, state(s)), "accepted", s)
        for pct in (-1, 101, 50.5, True, "50", None):
            self.assertEqual(refusal({"action": "aux_fan", "pct": pct}), "bad_params", pct)
        self.assertEqual(refusal({"action": "aux_fan"}), "bad_params")

    def test_refused_on_a_printer_without_them(self):
        mr = Moonraker(objects=["toolhead", "fan_generic fan2"])
        self.addCleanup(mr.close)
        a = KlipperAdapter("p1", mr.url)
        a._record_poll(dict(IDLE))
        self.assertEqual(a.send_command("klipper_action", {"action": "exhaust_fan", "pct": 50})["reason"],
                         "unsupported_on_printer")
        self.assertTrue(a.send_command("klipper_action", {"action": "aux_fan", "pct": 50})["ok"])
        self.assertEqual(mr.posts, [("/printer/gcode/script", "SET_FAN_SPEED FAN=fan2 SPEED=0.5")])


PAUSED = {**state("paused"), "extruder": {"temperature": 219.0, "target": 220.0}}
PRINTING = {**state("printing"), "extruder": {"temperature": 219.0, "target": 220.0}}
RESUME_HEAT = SV08_OBJECTS + ["gcode_macro _pstation_resume_heat"]


class TestPauseWatch(unittest.TestCase):
    """v0.79 (the sv08 report's D6): a paused print that sits is told to the shop at 30 min; at 60 min the nozzle drops to
    140 (the bed is left on), only on a printer whose RESUME heats back."""

    def setUp(self):
        self.mr = Moonraker(objects=RESUME_HEAT)
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(PRINTING))
        self.a._after_poll(PRINTING)

    def paused_for(self, minutes, reading=PAUSED):
        self.a._pause["at"] = time.monotonic() - minutes * 60
        self.a._record_poll(dict(reading))
        self.a._after_poll(reading)

    def test_the_print_temperature_is_kept_and_the_pause_is_timed_from_when_it_was_seen(self):
        self.assertEqual(self.a._print_target, 220.0)
        self.a._after_poll({**PAUSED, "extruder": {"temperature": 140.0, "target": 140.0}})
        self.assertEqual(self.a._print_target, 220.0)       # paused: what it printed at, not what it cooled to
        self.assertIsNotNone(self.a._pause)
        self.a._after_poll(state("complete"))
        self.assertIsNone(self.a._pause)
        self.assertIsNone(self.a._print_target)

    def test_at_30_min_the_status_carries_the_fault_and_the_log_hears_it_once(self):
        self.a._after_poll(PAUSED)
        self.paused_for(29)
        self.assertNotIn("printFault", self.a.status())
        with self.assertLogs("makeros_hub.printers.klipper", "WARNING") as logs:
            self.paused_for(31)
            self.paused_for(32)
        self.assertEqual(sum("paused 3" in line for line in logs.output), 1)
        self.assertEqual(self.a.status()["printFault"], "paused_long")
        self.assertEqual(self.mr.posts, [])                 # nothing sent at 30 min
        self.a._record_poll(dict(PRINTING))
        self.a._after_poll(PRINTING)                        # resumed
        self.assertNotIn("printFault", self.a.status())

    def test_at_60_min_the_nozzle_goes_to_140_once_and_the_bed_stays(self):
        self.a._after_poll(PAUSED)
        self.paused_for(59)
        self.assertEqual(self.mr.posts, [])
        self.paused_for(61)
        self.paused_for(70)
        self.assertEqual(self.mr.posts, [("/printer/gcode/script",
                                          "SET_GCODE_VARIABLE MACRO=_pstation_resume_heat VARIABLE=nozzle VALUE=220\n"
                                          "M104 S140")])

    def test_never_on_a_printer_that_holds_its_own_heat(self):
        held = {**PAUSED, "gcode_macro _pstation_hold": {"active": False, "minutes": 0, "ceiling_minutes": 2880}}
        self.a._after_poll(held)
        self.paused_for(61, held)
        self.assertEqual(self.mr.posts, [])                 # v0.82: its hold turns the nozzle off itself
        self.assertEqual(self.a.status()["printFault"], "paused_long")   # the shop is still told

    def test_never_on_a_printer_whose_resume_does_not_heat_back_nor_below_140(self):
        self.mr.objects = list(SV08_OBJECTS)                # Sovol's RESUME as shipped
        self.a._after_poll(PAUSED)
        with self.assertLogs("makeros_hub.printers.klipper", "WARNING") as logs:
            self.paused_for(61)
        self.assertTrue(any("does not heat back" in line for line in logs.output))
        self.assertEqual(self.mr.posts, [])
        self.mr.objects = list(RESUME_HEAT)
        self.a._objects = None
        self.a._after_poll(state("complete"))
        self.a._after_poll({**PAUSED, "extruder": {"temperature": 140.0, "target": 140.0}})
        self.paused_for(61, {**PAUSED, "extruder": {"temperature": 140.0, "target": 140.0}})
        self.assertEqual(self.mr.posts, [])


class TestResume(unittest.TestCase):
    """v0.79 (D6): a Resume from the site heats back to the print's temperature and waits; Sovol's RESUME never does."""

    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(PAUSED))

    def resume(self, reading, print_target=None):
        self.mr.status = reading
        self.a._print_target = print_target
        out = self.a.send_command("resume")
        self.assertTrue(_wait(lambda: self.a._running_action is None))
        return out

    def test_hot_resumes_as_before(self):
        out = self.resume(PAUSED, 220.0)
        self.assertEqual(out, {"ok": True, "command": "resume", "result": "ok"})
        self.assertEqual(self.mr.posts, [("/printer/print/resume", None)])

    def test_cooled_heats_back_to_the_print_temperature_and_waits_first(self):
        out = self.resume({**PAUSED, "extruder": {"temperature": 141.0, "target": 140.0}}, 220.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["command"], "resume")
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "M109 S220\nRESUME")])

    def test_after_a_hub_restart_the_printers_saved_target_is_used(self):
        reading = {**PAUSED, "extruder": {"temperature": 25.0, "target": 0.0},
                   "gcode_macro _pstation_resume_heat": {"nozzle": 230.0}}
        self.assertTrue(self.resume(reading)["ok"])
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "M109 S230\nRESUME")])

    def test_what_the_hub_saw_it_print_at_wins_over_a_left_over_printer_value(self):
        reading = {**PAUSED, "extruder": {"temperature": 140.0, "target": 140.0},
                   "gcode_macro _pstation_resume_heat": {"nozzle": 240.0}}   # from a PETG print cancelled while paused
        self.assertTrue(self.resume(reading, 215.0)["ok"])
        self.assertEqual(self.mr.posts, [("/printer/gcode/script", "M109 S215\nRESUME")])

    def test_cold_with_nothing_known_is_refused(self):
        out = self.resume({**PAUSED, "extruder": {"temperature": 140.0, "target": 140.0}})
        self.assertEqual(out, {"ok": False, "reason": "nozzle_cold", "command": "resume"})
        self.assertEqual(self.mr.posts, [])

    def test_not_paused_goes_to_moonraker_as_before(self):
        self.resume(dict(PRINTING), 220.0)
        self.assertEqual(self.mr.posts, [("/printer/print/resume", None)])


# v0.85: the printer page's Controls tab (operator, 2026-10-26: "we nee da tab for movement and commands where i can do stops
# and stuff"). Giga-shaped: X/Y home to the back-right corner (axis_maximum 815.5, 805.5), Z to 1020, no Sovol park variable.
GIGA = {**IDLE, "toolhead": {"homed_axes": "xyz", "position": [359.9, 392.0, 10.0, 0.0],
                             "axis_minimum": [-5.0, -1.0, -8.75, 0.0], "axis_maximum": [815.5, 805.5, 1020.0, 0.0]},
        "gcode_macro _global_var": {}}
# What the Controls tab sends (pstation src/lib/printer-controls.ts): each must be in the list the heartbeat reports.
TAB = [{"action": "home"}, {"action": "home_xy"}, {"action": "home_z"}, {"action": "jog", "axis": "x", "mm": 10},
       {"action": "jog", "axis": "z", "mm": -1}, {"action": "park"}, {"action": "nozzle_temp", "to": "PLA"},
       {"action": "nozzle_temp", "to": "off"}, {"action": "bed_temp", "to": "PETG"}, {"action": "bed_temp", "to": "off"},
       {"action": "cooldown"}, {"action": "fan", "pct": 100}, {"action": "fan", "pct": 0}, {"action": "motors_off"},
       {"action": "emergency_stop"}, {"action": "firmware_restart"}]


class TestControlsTab(unittest.TestCase):
    def test_the_heartbeat_lists_exactly_the_actions_this_hub_runs(self):
        from makeros_hub.agent import heartbeat_payload
        self.assertEqual(heartbeat_payload()["klipperActions"], sorted(klipper._ACTION_PARAMS))
        self.assertLessEqual({p["action"] for p in TAB}, set(klipper.KLIPPER_ACTIONS))

    def test_anything_off_the_list_is_refused_before_any_state_is_read(self):
        for params in ({"action": "gcode", "script": "G28"}, {"action": "G28 X Y"}, {"action": "home_all"},
                       {"action": "move_to", "x": 1, "y": 2}, {"action": "set_temp", "heater": "extruder", "target": 300},
                       {"action": "M112"}, {"action": "park", "x": 0}, {"action": "nozzle_temp", "to": 300},
                       {"action": "nozzle_temp", "to": "ABS"}, {"action": "bed_temp", "to": "PLA", "zone": 2},
                       {"action": "nozzle_temp"}):
            self.assertIn(refusal(params, {}), ("unsupported_action", "bad_params"), params)
        a = KlipperAdapter("p", "http://127.0.0.1:1")   # never polled: validation comes first
        self.assertEqual(a.send_command("klipper_action", {"action": "gcode", "script": "M84"})["reason"], "unsupported_action")
        self.assertEqual(a.send_command("gcode", {"script": "M84"})["reason"], "unsupported_command")

    def test_the_new_actions_send_exactly_their_named_commands(self):
        self.assertEqual(plan({"action": "home_xy"}, GIGA)[1], "G28 X Y")
        self.assertEqual(plan({"action": "home_z"}, GIGA, xy=True)[1], "G28 Z\nG90\nG1 Z10 F600")
        for to, nozzle, bed in (("PLA", 215, 60), ("PETG", 240, 70), ("off", 0, 0)):
            self.assertEqual(plan({"action": "nozzle_temp", "to": to})[1], f"M104 S{nozzle}")
            self.assertEqual(plan({"action": "bed_temp", "to": to})[1], f"M140 S{bed}")   # the Giga's M140 sets all four zones
        self.assertEqual(plan({"action": "fan", "pct": 100})[1], "M106 S255")   # idle: on and off from the tab
        self.assertEqual(plan({"action": "fan", "pct": 0})[1], "M106 S0")

    def test_every_motion_and_heat_is_refused_during_a_print_or_a_pause_and_while_held(self):
        held = {**state("error"), "gcode_macro _pstation_hold": {"active": True}}
        for s in (state("printing"), state("paused"), held):
            for params in TAB:
                a = params["action"]
                if a in ("emergency_stop", "firmware_restart", "fan") or (a == "cooldown" and s is held):
                    continue   # the way out, the fan (the screen's tune), and Cool down letting a hold go
                self.assertEqual(refusal(params, s, xy=True), "printer_busy", (s["print_stats"], params))

    def test_a_jog_or_park_needs_a_home_this_hub_saw_and_a_readable_printer(self):
        for params in ({"action": "jog", "axis": "x", "mm": 1}, {"action": "jog", "axis": "z", "mm": 1}, {"action": "park"}):
            self.assertEqual(refusal(params, GIGA, proven=False), "not_homed", params)
            self.assertEqual(refusal(params, at(1.0, 2.0, 10.0, GIGA, homed_axes="xy")), "not_homed", params)
            self.assertEqual(refusal(params, {}), "printer_stale", params)                       # no state in the read
            self.assertEqual(refusal(params, {"print_stats": {"state": "standby"}, "toolhead": {"homed_axes": "xyz"}}),
                             "printer_stale", params)                                            # no position or limits

    def test_a_stale_poll_only_allows_emergency_stop(self):
        mr = Moonraker()
        self.addCleanup(mr.close)
        a = KlipperAdapter("p1", mr.url)
        a._record_poll(dict(GIGA))
        a._home_proven = a._xy_proven = True
        a._last_poll_ok_at = time.monotonic() - klipper.STALE_SEC - 1
        for params in TAB:
            result = a.send_command("klipper_action", params)
            if params["action"] == "emergency_stop":
                self.assertTrue(result["ok"])
            else:
                self.assertEqual(result["reason"], "not_connected", params)
        self.assertEqual(mr.posts, [("/printer/emergency_stop", None)])

    def test_down_in_z_is_one_millimetre_at_most(self):
        high = at(400.0, 400.0, 400.0, GIGA)
        for mm in (-10, -50):
            self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": mm}, high), "z_down_step", mm)
        for mm in (-1, -0.1, 0.1, 1, 10, 50):
            self.assertEqual(refusal({"action": "jog", "axis": "z", "mm": mm}, high), "accepted", mm)
        self.assertEqual(klipper.JOG_Z_DOWN_MAX_MM, 1.0)

    def test_home_z_travels_on_an_xy_home_this_hub_saw(self):
        self.assertEqual(refusal({"action": "home_z"}, GIGA), "home_xy_first")                       # Klipper's homed_axes alone
        self.assertEqual(refusal({"action": "home_z"}, at(1.0, 2.0, 3.0, GIGA, homed_axes="z"), xy=True), "home_xy_first")
        self.assertEqual(refusal({"action": "home_z"}, at(1.0, 2.0, 3.0, GIGA, homed_axes="xy"), xy=True), "accepted")
        self.assertEqual(refusal({"action": "home_z"}, {**GIGA, "heater_bed": {"temperature": 95.0}}, xy=True), "bed_too_hot")

    def test_park_lifts_first_then_goes_back_right_on_the_giga(self):
        # from Z10: up 40 to Z50 (PARK_Z_MIN_MM), then X/Y 15 mm in from the back-right corner
        self.assertEqual(plan({"action": "park"}, GIGA)[1], "G91\nG1 Z40 F600\nG90\nG1 X800.5 Y790.5 F6000")
        # high already: 10 more, never down
        self.assertEqual(plan({"action": "park"}, at(359.9, 392.0, 410.37, GIGA))[1],
                         "G91\nG1 Z10 F600\nG90\nG1 X800.5 Y790.5 F6000")
        # near the top: capped 1 mm under axis_maximum, and no lift at all from there
        self.assertEqual(plan({"action": "park"}, at(1.0, 1.0, 1015.0, GIGA))[1], "G91\nG1 Z4 F600\nG90\nG1 X800.5 Y790.5 F6000")
        self.assertEqual(plan({"action": "park"}, at(1.0, 1.0, 1019.5, GIGA))[1], "G90\nG1 X800.5 Y790.5 F6000")
        # from under the jog floor it still goes up first (park is the way off the plate)
        self.assertTrue(plan({"action": "park"}, at(100.0, 100.0, 0.4, GIGA))[1].startswith("G91\nG1 Z49.6 F600\n"))

    def test_park_uses_the_printers_own_park_when_it_names_one(self):
        sv08 = {**IDLE, "gcode_macro _global_var": {"load_filament_extruder_temp": 250,
                                                     "cancel_park": {"x": -2, "y": 501, "z": 10, "e": 1}},
                "toolhead": {**IDLE["toolhead"], "axis_minimum": [-11.0, -1.0, -10.0, 0.0], "axis_maximum": [502.0, 505.0, 505.0, 0.0]}}
        self.assertEqual(plan({"action": "park"}, sv08)[1], "G91\nG1 Z40 F600\nG90\nG1 X-2 Y501 F6000")
        self.assertIn("cancel_park", klipper._ACTION_QUERY)
        outside = {**sv08, "gcode_macro _global_var": {"cancel_park": {"x": 900, "y": 501}}}
        self.assertEqual(refusal({"action": "park"}, outside), "out_of_range")
        junk = {**sv08, "gcode_macro _global_var": {"cancel_park": {"x": "left", "y": 501}}}
        self.assertEqual(plan({"action": "park"}, junk)[1], "G91\nG1 Z40 F600\nG90\nG1 X487 Y490 F6000")   # back-right


class TestXYHome(unittest.TestCase):
    """v0.85: Home X/Y earns the X/Y home Home Z needs; Home Z on it earns the full home a jog needs; both end as v0.79's did."""

    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.mr.status = dict(GIGA)
        self.a = KlipperAdapter("p1", self.mr.url)
        self.a._record_poll(dict(GIGA))

    def finish(self, **params):
        out = self.a.send_command("klipper_action", params)
        self.assertTrue(_wait(lambda: self.a._running_action is None))
        return out

    def test_xy_then_z_earns_the_home_and_the_status_says_which(self):
        self.assertEqual(self.a.status()["toolhead"]["homed"], "")
        self.assertEqual(self.finish(action="home_z")["reason"], "home_xy_first")
        self.assertTrue(self.finish(action="home_xy")["ok"])
        self.assertEqual(self.a.status()["toolhead"]["homed"], "xy")
        self.assertEqual(self.finish(action="jog", axis="x", mm=1)["reason"], "not_homed")   # Z is not trusted yet
        self.assertTrue(self.finish(action="home_z")["ok"])
        self.assertEqual(self.a.status()["toolhead"]["homed"], "xyz")
        self.assertTrue(self.finish(action="jog", axis="x", mm=1)["ok"])
        self.assertEqual([p[1] for p in self.mr.posts], ["G28 X Y", "G28 Z\nG90\nG1 Z10 F600", "G91\nG1 X1 F6000\nG90"])

    def test_motors_off_a_stop_an_unhomed_reading_or_another_homing_action_end_it(self):
        for action in ("motors_off", "emergency_stop", "firmware_restart", "bed_mesh"):
            self.a._home_proven = self.a._xy_proven = True
            self.finish(action=action)
            self.assertFalse(self.a._home_proven or self.a._xy_proven, action)
        self.a._home_proven = self.a._xy_proven = True
        self.a._after_poll(at(1.0, 2.0, 3.0, GIGA, homed_axes="yz"))
        self.assertFalse(self.a._home_proven or self.a._xy_proven)
        self.a._xy_proven = True
        self.a._after_poll(at(1.0, 2.0, 3.0, GIGA, homed_axes="xy"))   # Z lost, X/Y kept
        self.assertTrue(self.a._xy_proven)
        self.a._xy_proven = True
        self.finish(action="home_z")                                    # homes Z alone: keeps X/Y
        self.assertTrue(self.a._xy_proven)

    def test_a_refused_park_puts_g90_back(self):
        self.a._home_proven = True
        self.mr.reply = lambda path, script: ((400, {"error": {"message": "Move out of range: 0 0 1030 [0]"}})
                                              if script and script.startswith("G91") else (200, {"result": "ok"}))
        self.assertEqual(self.finish(action="park")["reason"], "out_of_range")
        self.assertEqual(self.mr.posts[-1], ("/printer/gcode/script", "G90"))


def test_command_answers_go_back_at_once():
    # v0.85 (operator, 2026-10-26: "i think the 30 second updates might need to go waway"): after a batch of commands the loop
    # checks in again at once with their results, instead of waiting out the heartbeat interval
    import inspect
    from makeros_hub import agent
    src = inspect.getsource(agent.run)
    after = src[src.index("_keep_command_results(command_reports)"):]
    assert after.index("wake_event.set()") < after.index("elif resp.status == 401")


# v0.86 (operator, 2026-10-26): an honest shutdown, the stop point a recovery needs, and the re-home over a print. The Giga's
# stop of 2026-10-26 (reports/giga-mcu-shutdown-2026-10-26.md): toolhead (359.929, 392.005) Z 410.37, byte 35,710,026 of
# 36,803,071, layer 1233 of 1307, fan 189, nozzle 215, all four zones 60, relative extrusion, offset -1.85, mesh default.
SHUTDOWN_MSG = "MCU 'mcu' shutdown: Rescheduled timer in the past\nThis generally occurs when the micro-controller has been"
STOPPED = {**GIGA, "print_stats": {"state": "paused", "filename": "tditto_combinatory_form.gcode", "info": {"current_layer": 1233, "total_layer": 1307}},
           "webhooks": {"state": "shutdown", "state_message": SHUTDOWN_MSG},
           "toolhead": {**GIGA["toolhead"], "position": [359.929, 392.005, 410.37, 0.0]},
           "virtual_sdcard": {"file_position": 35710026, "file_size": 36803071, "progress": 0.9703},
           "gcode_move": {"gcode_position": [359.9, 392.0, 417.05, 0.0], "homing_origin": [0.0, 0.0, -1.85, 0.0], "absolute_extrude": False,
                          "speed_factor": 1.0, "extrude_factor": 1.0},
           "extruder": {"temperature": 190.0, "target": 215.0}, "heater_bed": {"temperature": 40.0, "target": 60.0},
           "heater_generic heater_bed1": {"target": 60.0}, "heater_generic heater_bed2": {"target": 60.0}, "heater_generic heater_bed3": {"target": 60.0},
           "output_pin fan1": {"value": 189.0}, "bed_mesh": {"profile_name": "default"},
           "exclude_object": {"objects": [{"name": "form", "polygon": [[300, 300], [520, 300], [520, 640], [300, 640]]}]},
           "gcode_macro _pstation_hold": {"active": True, "minutes": 4, "ceiling_minutes": 2880}}
GIGA_CORNERS = {"fl": [40.0, 40.0], "fr": [775.0, 40.0], "bl": [40.0, 765.0], "br": [775.0, 765.0]}
STOP = klipper._stop_point(STOPPED, 1761507636000)


def restarted(**toolhead):   # after FIRMWARE_RESTART: standby, Elegoo's boot home faked at 110, 110, 0, the corners macro in
    return {**GIGA, "print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
            "gcode_macro _ps_corners": {**GIGA_CORNERS, "xy_extra_lift": 0.0},
            "gcode_move": {"homing_origin": [0.0, 0.0, 0.0, 0.0]},
            "toolhead": {**GIGA["toolhead"], "position": [110.0, 110.0, 0.0, 0.0], **toolhead}}


def rehome(params, status, **kw):
    action, script, _ = _parse_action(params)
    return _check_state(params, script, status, **{"stop": STOP, **kw})


def rehome_refusal(params, status, **kw):
    try:
        rehome(params, status, **kw)
    except _Refused as r:
        return r.reason
    return "accepted"


class TestShutdownIsHonest(unittest.TestCase):
    def test_a_shutdown_reads_as_an_error_with_every_heater_off_and_no_hold(self):
        out = _build_status("p", STOPPED, "connected")
        self.assertEqual(out["state"], "error")   # never "paused"
        self.assertEqual(out["klipperShutdown"], "MCU 'mcu' shutdown: Rescheduled timer in the past")
        self.assertEqual(out["errorReason"], out["klipperShutdown"])
        self.assertEqual((out["nozzleTargetC"], out["bedTargetC"]), (0.0, 0.0))   # Klipper cut them; it still reports 215 and 60
        self.assertNotIn("hold", out)
        self.assertEqual(_build_status("p", {**STOPPED, "webhooks": {"state": "ready"}}, "connected")["state"], "paused")

    def test_the_stop_point_holds_what_the_recovery_wrote_down_by_hand(self):
        self.assertEqual({k: STOP[k] for k in ("x", "y", "z", "gcodeZ", "zOffset", "fileByte", "fileSize", "layer", "file", "mesh")},
                         {"x": 359.929, "y": 392.005, "z": 410.37, "gcodeZ": 417.05, "zOffset": -1.85, "fileByte": 35710026,
                          "fileSize": 36803071, "layer": [1233, 1307], "file": "tditto_combinatory_form.gcode", "mesh": "default"})
        self.assertEqual((STOP["nozzleTargetC"], STOP["bedTargetsC"], STOP["fanPin"], STOP["absoluteExtrude"]),
                         (215.0, {"heater_bed": 60.0, "heater_bed1": 60.0, "heater_bed2": 60.0, "heater_bed3": 60.0}, 189.0, False))
        self.assertEqual((STOP["shutdown"], STOP["reason"], STOP["progressPct"]), (True, "MCU 'mcu' shutdown: Rescheduled timer in the past", 97.0))
        self.assertEqual(STOP["objects"], [{"name": "form", "box": [300.0, 300.0, 520.0, 640.0]}])
        self.assertEqual(klipper._stop_point({}, 1), {"at": 1, "shutdown": False})   # only what Klipper gave

    def test_nothing_but_the_two_stops_is_sent_into_a_shutdown(self):
        for params in TAB + [{"action": "rehome_xy"}, {"action": "move_corner", "corner": "fl"}, {"action": "restore_bed"}]:
            want = "accepted" if params["action"] in ("emergency_stop", "firmware_restart") else "klipper_shutdown"
            self.assertEqual(rehome_refusal(params, STOPPED, xy_proven=True, home_proven=True), want, params)

    def test_the_wire_carries_them(self):
        for key in ("klipperShutdown", "stopPoint", "corners", "rehome"):
            self.assertIn(key, telemetry._WIRE_KEYS)
            self.assertIn(key, telemetry._TRANSITION_KEYS)


class TestStopWatch(unittest.TestCase):
    def setUp(self):
        self.mr = Moonraker()
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p-stop", self.mr.url)

    def poll(self, status):
        self.mr.status = status
        self.a._record_poll(dict(status))
        self.a._after_poll(dict(status))

    def test_recorded_once_on_the_stop_kept_through_a_restart_cleared_when_a_print_runs(self):
        self.poll({**STOPPED, "webhooks": {"state": "ready"}, "print_stats": {"state": "printing", "filename": "f.gcode"}})
        self.assertIsNone(self.a.status().get("stopPoint"))
        self.poll(STOPPED)
        sp = self.a.status()["stopPoint"]
        self.assertEqual((sp["z"], sp["fileByte"], sp["layer"]), (410.37, 35710026, [1233, 1307]))
        self.assertEqual(klipper._load_json(self.a._stop_path)["fileByte"], 35710026)   # on disk
        self.mr.status = {**STOPPED, "virtual_sdcard": {"file_position": 1}}
        self.a._after_poll(dict(self.mr.status))
        self.assertEqual(self.a.status()["stopPoint"]["fileByte"], 35710026)            # read once, not again
        self.assertEqual(KlipperAdapter("p-stop", self.mr.url)._stop_pt["z"], 410.37)    # a hub restart keeps it
        self.poll(restarted())                                                         # the firmware restart forgets the print; the record stays
        self.assertEqual(self.a.status()["stopPoint"]["z"], 410.37)
        self.poll({**restarted(), "print_stats": {"state": "printing", "filename": "remainder.gcode"}})
        self.assertIsNone(self.a.status().get("stopPoint"))
        self.assertIsNone(klipper._load_json(self.a._stop_path))

    def test_an_idle_error_with_no_print_records_nothing(self):
        self.poll({**restarted(), "print_stats": {"state": "error", "filename": ""}})
        self.assertIsNone(self.a.status().get("stopPoint"))

    def test_no_long_pause_alert_inside_a_shutdown(self):
        self.poll(STOPPED)
        self.a._pause = {"at": time.monotonic() - klipper.PAUSE_ALERT_SEC - 5, "alerted": True, "cooled": False}
        self.assertNotIn("printFault", self.a.status())


class TestRehomeRules(unittest.TestCase):
    def test_without_a_stop_point_nothing_is_offered(self):
        for params in ({"action": "rehome_xy"}, {"action": "move_corner", "corner": "fl"}, {"action": "home_z_corner", "corner": "fl"},
                       {"action": "restore_bed"}):
            self.assertEqual(rehome_refusal(params, restarted(), stop=None, xy_proven=True, z_rec=True), "no_stop_point", params)

    def test_home_xy_over_the_print_lifts_from_the_recorded_height_and_never_down(self):
        self.assertEqual(rehome({"action": "rehome_xy"}, restarted()), "PS_REHOME_XY Z=410.37 LIFT=30")
        near_top = {**STOP, "z": 1000.0}
        self.assertEqual(rehome({"action": "rehome_xy"}, restarted(), stop=near_top), "PS_REHOME_XY Z=1000 LIFT=19")
        self.assertEqual(rehome_refusal({"action": "rehome_xy"}, restarted(), stop={**STOP, "z": 1016.0}), "no_room_to_lift")
        # a home this hub trusts wins over the record, and must still clear the print's top by the minimum
        self.assertEqual(rehome({"action": "rehome_xy"}, restarted(position=[1.0, 1.0, 430.0, 0.0]), home_proven=True), "PS_REHOME_XY Z=430 LIFT=30")
        self.assertEqual(rehome_refusal({"action": "rehome_xy"}, restarted(position=[1.0, 1.0, 300.0, 0.0]), home_proven=True), "below_print")
        for z in (0.0, 2.0, 410.37, 700.0, 1014.0):   # whatever Z is, the lift is up
            out = rehome({"action": "rehome_xy"}, restarted(), stop={**STOP, "z": z})
            self.assertGreater(float(out.split("LIFT=")[1]), 0, z)

    def test_a_corner_only_on_that_home_above_the_print_and_x_y_only(self):
        st = restarted(position=[815.0, 805.0, 440.37, 0.0])
        self.assertEqual(rehome({"action": "move_corner", "corner": "fl"}, st, xy_proven=True, z_rec=True), "G90\nG1 X40 Y40 F6000")
        self.assertEqual(rehome_refusal({"action": "move_corner", "corner": "fl"}, st, xy_proven=False, z_rec=True), "not_homed")
        self.assertEqual(rehome_refusal({"action": "move_corner", "corner": "fl"}, st, xy_proven=True), "z_unknown")
        low = restarted(position=[815.0, 805.0, 414.0, 0.0])   # under the print's top + 5
        self.assertEqual(rehome_refusal({"action": "move_corner", "corner": "fl"}, low, xy_proven=True, z_rec=True), "below_print")
        bare = {**st, "gcode_macro _ps_corners": {}}
        self.assertEqual(rehome_refusal({"action": "move_corner", "corner": "fl"}, bare, xy_proven=True, z_rec=True), "unsupported_on_printer")
        self.assertEqual(rehome_refusal({"action": "move_corner", "corner": "xx"}, st, xy_proven=True, z_rec=True), "bad_params")

    def test_z_homes_only_at_the_corner_the_nozzle_went_to_and_is_still_at(self):
        at_fl = restarted(position=[40.0, 40.0, 440.37, 0.0])
        ok = {"xy_proven": True, "z_rec": True, "corner_at": {"name": "fl", "x": 40.0, "y": 40.0}}
        self.assertEqual(rehome({"action": "home_z_corner", "corner": "fl"}, at_fl, **ok), "PS_HOME_Z_HERE LIFT_TO=430.37")
        self.assertEqual(rehome_refusal({"action": "home_z_corner", "corner": "fl"}, at_fl, xy_proven=True, z_rec=True), "not_at_corner")
        self.assertEqual(rehome_refusal({"action": "home_z_corner", "corner": "br"}, at_fl, **ok), "not_at_corner")   # nobody confirmed br
        moved = restarted(position=[41.0, 40.0, 440.37, 0.0])
        self.assertEqual(rehome_refusal({"action": "home_z_corner", "corner": "fl"}, moved, **ok), "not_at_corner")
        self.assertEqual(rehome_refusal({"action": "home_z_corner", "corner": "fl"}, {**at_fl, "probe": {"is_calibrated": False}}, **ok), "probe_uncalibrated")
        self.assertEqual(rehome_refusal({"action": "home_z_corner", "corner": "fl"}, {**at_fl, "heater_bed": {"temperature": 95.0}}, **ok), "bed_too_hot")
        high = restarted(position=[40.0, 40.0, 1010.0, 0.0])   # a tall print: the lift ends 1 mm under the top
        self.assertEqual(rehome({"action": "home_z_corner", "corner": "fl"}, high, stop={**STOP, "z": 1000.0}, **ok), "PS_HOME_Z_HERE LIFT_TO=1019")

    def test_the_bed_goes_back_to_the_prints_own_zones_capped(self):
        self.assertEqual(rehome({"action": "restore_bed"}, restarted()).splitlines(),
                         [f"SET_HEATER_TEMPERATURE HEATER={z} TARGET=60" for z in ("heater_bed", "heater_bed1", "heater_bed2", "heater_bed3")])
        hot = {**STOP, "bedTargetsC": {"heater_bed": 120.0, "heater_bed1": 0.0}}
        self.assertEqual(rehome({"action": "restore_bed"}, restarted(), stop=hot), "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=100")
        self.assertEqual(rehome_refusal({"action": "restore_bed"}, restarted(), stop={**STOP, "bedTargetsC": {}}), "nothing_to_restore")
        self.assertEqual(rehome_refusal({"action": "restore_bed"}, {**restarted(), "print_stats": {"state": "printing"}}), "printer_busy")


class TestRehomeOverTheWire(unittest.TestCase):
    """The three steps through the adapter: each answers, the status says how far it got, and a corner nobody confirmed is
    never homed at."""

    def setUp(self):
        self.mr = Moonraker(objects=SV08_OBJECTS + ["gcode_macro ps_rehome_xy", "gcode_macro _ps_corners", "gcode_macro ps_home_z_here"])
        self.addCleanup(self.mr.close)
        self.a = KlipperAdapter("p-rehome", self.mr.url)
        self.a._stop_pt = dict(STOP)
        self.mr.status = restarted()
        self.a._record_poll(dict(self.mr.status))

    def act(self, **params):
        out = self.a.send_command("klipper_action", params)
        self.assertTrue(_wait(lambda: self.a._running_action is None))
        return out

    def test_xy_then_a_confirmed_corner_then_z_and_the_status_follows(self):
        self.assertTrue(self.act(action="rehome_xy")["ok"])
        self.assertEqual(self.a.status()["rehome"], {"stage": "xy"})
        self.assertEqual(self.act(action="jog", axis="x", mm=1)["reason"], "not_homed")    # Z from a record: corners only
        self.mr.status = restarted(position=[815.0, 805.0, 440.37, 0.0])
        self.assertTrue(self.act(action="move_corner", corner="fl")["ok"])
        self.assertEqual(self.a.status()["rehome"], {"stage": "corner", "corner": "fl"})
        self.mr.status = restarted(position=[40.0, 40.0, 440.37, 0.0])
        self.assertEqual(self.act(action="home_z_corner", corner="fr")["reason"], "not_at_corner")
        self.assertTrue(self.act(action="move_corner", corner="fl")["ok"])
        self.assertTrue(self.act(action="home_z_corner", corner="fl")["ok"])
        self.assertEqual(self.a.status()["rehome"], {"stage": "z"})
        self.assertEqual([p[1] for p in self.mr.posts], ["PS_REHOME_XY Z=410.37 LIFT=30", "G90\nG1 X40 Y40 F6000", "G90\nG1 X40 Y40 F6000",
                                                         "PS_HOME_Z_HERE LIFT_TO=430.37"])
        self.assertTrue(self.act(action="jog", axis="x", mm=1)["ok"])                       # X, Y and Z known again

    def test_any_other_action_or_a_move_between_the_corner_and_the_answer_cancels_it(self):
        self.act(action="rehome_xy")
        self.mr.status = restarted(position=[40.0, 40.0, 440.37, 0.0])
        self.act(action="move_corner", corner="fl")
        self.act(action="fan", pct=0)
        self.assertEqual(self.act(action="home_z_corner", corner="fl")["reason"], "not_at_corner")
        self.act(action="move_corner", corner="fl")
        self.a._after_poll(restarted(position=[60.0, 40.0, 440.37, 0.0]))                   # moved by hand or by the screen
        self.assertEqual(self.act(action="home_z_corner", corner="fl")["reason"], "not_at_corner")

    def test_a_failed_corner_home_puts_the_g_code_offset_back(self):
        self.act(action="rehome_xy")
        self.mr.status = {**restarted(position=[40.0, 40.0, 440.37, 0.0]), "gcode_move": {"homing_origin": [0.0, 0.0, -1.85, 0.0]}}
        self.act(action="move_corner", corner="fl")
        self.mr.reply = lambda path, script: ((400, {"error": {"message": "No trigger on z after full movement"}})
                                              if script and script.startswith("PS_HOME_Z_HERE") else (200, {"result": "ok"}))
        self.a.send_command("klipper_action", {"action": "home_z_corner", "corner": "fl"})
        self.assertTrue(_wait(lambda: self.a._running_action is None and self.mr.posts[-1][1] == "SET_GCODE_OFFSET X=0 Y=0 MOVE=0"))
        self.assertNotEqual(self.a.status().get("rehome", {}).get("stage"), "z")

    def test_without_the_printers_macros_it_is_refused(self):
        self.mr.objects = list(SV08_OBJECTS)
        self.assertEqual(self.act(action="rehome_xy")["reason"], "unsupported_on_printer")
        self.assertEqual(self.mr.posts, [])


if __name__ == "__main__":
    unittest.main()
