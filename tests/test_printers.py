"""Stdlib-only tests for the printer layer. The pure parser (bambu_parse) is
fully covered without paho or a printer; the manager is covered on its non-paho
paths (klipper/incomplete/removal). Run: python3 -m unittest discover -s tests"""

import unittest

from makeros_hub.printers import bambu_parse
from makeros_hub.printers.manager import PrinterManager


class TestMergeReport(unittest.TestCase):
    def test_deep_merges_partial_deltas(self):
        state = {}
        # pushall snapshot
        bambu_parse.merge_report(state, {"print": {"gcode_state": "RUNNING", "mc_percent": 10, "ams": {"tray_now": "0"}}})
        # a later delta touches only one nested field
        bambu_parse.merge_report(state, {"print": {"mc_percent": 55}})
        self.assertEqual(state["print"]["mc_percent"], 55)
        self.assertEqual(state["print"]["gcode_state"], "RUNNING")  # preserved
        self.assertEqual(state["print"]["ams"]["tray_now"], "0")  # nested preserved

    def test_scalar_and_list_overwrite(self):
        state = {"print": {"hms": [{"code": 1}]}}
        bambu_parse.merge_report(state, {"print": {"hms": []}})
        self.assertEqual(state["print"]["hms"], [])  # list replaced, not merged


class TestNormalizeStatus(unittest.TestCase):
    def _merged(self, **print_fields):
        return {"print": print_fields}

    def test_running_print_maps_to_printing_with_telemetry(self):
        merged = self._merged(
            gcode_state="RUNNING", mc_percent=42, nozzle_temper=210.4, bed_temper=60.0,
            subtask_name="bracket.3mf", mc_remaining_time=90,
        )
        s = bambu_parse.normalize_status("p1", merged, connection_state="connected")
        self.assertEqual(s["printerId"], "p1")
        self.assertEqual(s["connectionState"], "connected")
        self.assertEqual(s["state"], "printing")
        self.assertEqual(s["progressPct"], 42)
        self.assertEqual(s["nozzleTempC"], 210.4)
        self.assertEqual(s["bedTempC"], 60.0)
        self.assertEqual(s["jobName"], "bracket.3mf")
        self.assertEqual(s["etaMinutes"], 90)  # MINUTES, not seconds

    def test_finish_is_idle_failed_is_error(self):
        self.assertEqual(
            bambu_parse.normalize_status("p", self._merged(gcode_state="FINISH"), connection_state="connected")["state"],
            "idle",
        )
        self.assertEqual(
            bambu_parse.normalize_status("p", self._merged(gcode_state="FAILED"), connection_state="connected")["state"],
            "error",
        )

    def test_omits_absent_fields_and_state_when_not_connected(self):
        s = bambu_parse.normalize_status("p", {"print": {}}, connection_state="connecting")
        self.assertEqual(set(s.keys()), {"printerId", "connectionState"})  # no null keys (strict DTO)
        self.assertNotIn("state", s)

    def test_error_reason_only_on_error(self):
        ok = bambu_parse.normalize_status("p", {"print": {}}, connection_state="connected")
        self.assertNotIn("errorReason", ok)
        err = bambu_parse.normalize_status("p", {"print": {}}, connection_state="error", error_reason="mqtt_auth_failed")
        self.assertEqual(err["errorReason"], "mqtt_auth_failed")

    def test_coerces_string_numbers_and_clamps_progress(self):
        s = bambu_parse.normalize_status(
            "p", {"print": {"nozzle_temper": "215.0", "mc_percent": 130}}, connection_state="connected"
        )
        self.assertEqual(s["nozzleTempC"], 215.0)
        self.assertEqual(s["progressPct"], 100)  # clamped

    def test_gcode_file_fallback_for_job_name(self):
        s = bambu_parse.normalize_status(
            "p", {"print": {"gcode_file": "Metadata/plate_1.gcode"}}, connection_state="connected"
        )
        self.assertEqual(s["jobName"], "Metadata/plate_1.gcode")

    def test_connected_status_includes_ams_hms_and_print_error(self):
        merged = self._merged(
            gcode_state="RUNNING",
            ams={
                "tray_now": "2",
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PLA",
                                "tray_color": "AABBCCDD",
                                "remain": "45",
                            },
                            {},
                            {"id": "2", "tray_type": "PETG", "remain": 101},
                            {},
                        ],
                    }
                ],
            },
            hms=[{"attr": "1", "code": 1234}],
            print_error="7",
        )
        s = bambu_parse.normalize_status("p", merged, connection_state="connected")
        self.assertEqual(
            s["ams"],
            [
                {
                    "unit": 0,
                    "trays": [
                        {
                            "slot": 0,
                            "material": "PLA",
                            "colorHex": "AABBCCDD",
                            "remainPct": 45.0,
                        },
                        {"slot": 2, "material": "PETG", "remainPct": 100.0},
                    ],
                }
            ],
        )
        self.assertEqual(s["amsActiveTray"], 2)
        self.assertEqual(s["hms"], [{"attr": 1, "code": 1234}])
        self.assertEqual(s["printError"], 7)

    def test_connected_status_omits_absent_ams_hms_and_zero_print_error(self):
        s = bambu_parse.normalize_status(
            "p", self._merged(gcode_state="IDLE", print_error=0), connection_state="connected"
        )
        self.assertNotIn("ams", s)
        self.assertNotIn("amsActiveTray", s)
        self.assertNotIn("hms", s)
        self.assertNotIn("printError", s)

    def test_disconnected_status_never_includes_ams_hms_or_print_error(self):
        merged = self._merged(
            ams={"tray_now": "0", "ams": [{"id": "0", "tray": [{"tray_type": "PLA"}]}]},
            hms=[{"attr": 1, "code": 2}],
            print_error=9,
        )
        for state in ("offline", "error"):
            with self.subTest(connection_state=state):
                s = bambu_parse.normalize_status("p", merged, connection_state=state)
                self.assertNotIn("ams", s)
                self.assertNotIn("amsActiveTray", s)
                self.assertNotIn("hms", s)
                self.assertNotIn("printError", s)


class TestAmsHmsBuilders(unittest.TestCase):
    def test_build_ams_emits_units_and_trays_while_omitting_absent_fields(self):
        print_obj = {
            "ams": {
                "ams": [
                    {
                        "id": "3",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PLA",
                                "tray_color": "aabbccdd",
                                "remain": "87.5",
                            },
                            {},
                            {"id": "2"},
                            {"id": "3", "tray_type": "PETG", "remain": -10},
                        ],
                    }
                ]
            }
        }
        self.assertEqual(
            bambu_parse.build_ams(print_obj),
            [
                {
                    "unit": 3,
                    "trays": [
                        {
                            "slot": 0,
                            "material": "PLA",
                            "colorHex": "AABBCCDD",  # normalized to upper, 8-hex
                            "remainPct": 87.5,
                        },
                        {"slot": 2},
                        {"slot": 3, "material": "PETG", "remainPct": 0.0},
                    ],
                }
            ],
        )

    def test_build_ams_omits_invalid_color_and_caps_slots(self):
        # Non-8-hex colors are dropped; a malformed >4-tray unit only emits 0-3.
        print_obj = {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {"tray_type": "PLA", "tray_color": "GGGGGGGG"},  # non-hex
                            {"tray_type": "PETG", "tray_color": "FF0000FF11"},  # too long
                            {"tray_type": "TPU", "tray_color": "#00FF00FF"},  # leading # stripped
                            {"tray_type": "ABS"},
                            {"tray_type": "PA", "tray_color": "FFFFFFFF"},  # slot 4 — dropped
                        ],
                    }
                ]
            }
        }
        self.assertEqual(
            bambu_parse.build_ams(print_obj),
            [
                {
                    "unit": 0,
                    "trays": [
                        {"slot": 0, "material": "PLA"},
                        {"slot": 1, "material": "PETG"},
                        {"slot": 2, "material": "TPU", "colorHex": "00FF00FF"},
                        {"slot": 3, "material": "ABS"},
                    ],
                }
            ],
        )

    def test_build_ams_multi_unit_preserves_ids(self):
        print_obj = {
            "ams": {
                "ams": [
                    {"id": "0", "tray": [{"tray_type": "PLA"}]},
                    {"id": "1", "tray": [{"tray_type": "PETG"}]},
                ]
            }
        }
        self.assertEqual(
            [u["unit"] for u in bambu_parse.build_ams(print_obj)],
            [0, 1],
        )

    def test_build_ams_skips_garbage_without_crashing(self):
        self.assertIsNone(bambu_parse.build_ams({}))
        self.assertIsNone(bambu_parse.build_ams({"ams": {"ams": "bad"}}))
        self.assertEqual(
            bambu_parse.build_ams(
                {"ams": {"ams": ["bad", {"id": "not-int", "tray": [{}, "bad", {"remain": "x"}]}]}}
            ),
            [{"unit": 1, "trays": [{"slot": 2}]}],
        )

    def test_build_active_tray(self):
        self.assertEqual(bambu_parse.build_active_tray({"ams": {"tray_now": "5"}}), 5)
        self.assertEqual(bambu_parse.build_active_tray({"ams": {"tray_now": "63"}}), 63)  # max
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": 999}}))  # out of range
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": "-1"}}))  # negative
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": "254"}}))
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": "255"}}))
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": None}}))
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": "bad"}}))

    def test_to_int_rejects_non_integral(self):
        self.assertEqual(bambu_parse._to_int("42"), 42)
        self.assertEqual(bambu_parse._to_int("  -7 "), -7)
        self.assertEqual(bambu_parse._to_int(5.0), 5)
        self.assertIsNone(bambu_parse._to_int("1.9"))  # not silently truncated to 1
        self.assertIsNone(bambu_parse._to_int("1e3"))  # not silently widened to 1000
        self.assertIsNone(bambu_parse._to_int(1.9))
        self.assertIsNone(bambu_parse._to_int(float("inf")))
        self.assertIsNone(bambu_parse._to_int(True))

    def test_build_hms(self):
        self.assertEqual(
            bambu_parse.build_hms({"hms": [{"attr": "1", "code": 2}, "bad", {"attr": 3}]}),
            [{"attr": 1, "code": 2}],
        )
        self.assertIsNone(bambu_parse.build_hms({}))
        self.assertIsNone(bambu_parse.build_hms({"hms": []}))


class TestSummarizeShape(unittest.TestCase):
    def test_redacts_values_keeps_keys(self):
        merged = {"print": {"gcode_state": "RUNNING", "hms": [1, 2], "nozzle_temper": 210}}
        shape = bambu_parse.summarize_shape(merged)
        self.assertEqual(shape["values"], "[redacted]")
        self.assertIn("gcode_state", shape["printKeys"])
        self.assertEqual(shape["arrayLengths"]["hms"], 2)
        # No actual telemetry values leak into the shape summary.
        self.assertNotIn("210", str(shape))

    def test_counts_ams_units_and_trays_without_values(self):
        merged = {
            "print": {
                "ams": {
                    "tray_now": "0",
                    "ams": [
                        {"id": "0", "tray": [{"tray_type": "PLA"}, {}]},
                        {"id": "1", "tray": [{}, {}, {}, {}]},
                    ],
                },
                "hms": [{"attr": 1, "code": 2}],
            }
        }
        shape = bambu_parse.summarize_shape(merged)
        self.assertEqual(shape["arrayLengths"]["ams.units"], 2)
        self.assertEqual(shape["arrayLengths"]["ams.trays_total"], 6)
        self.assertEqual(shape["arrayLengths"]["hms"], 1)
        self.assertNotIn("PLA", str(shape))


class TestManagerNonPahoPaths(unittest.TestCase):
    def test_klipper_and_incomplete_bambu_report_clear_errors(self):
        m = PrinterManager()
        m.reconcile(
            [
                {"id": "k1", "vendor": "klipper", "moonrakerUrl": "http://x:7125"},
                {"id": "b1", "vendor": "bambu", "host": None, "serial": None, "accessCode": None},
            ],
            version="v1",
        )
        statuses = {s["printerId"]: s for s in m.statuses()}
        self.assertEqual(statuses["k1"]["connectionState"], "error")
        self.assertEqual(statuses["k1"]["errorReason"], "klipper_not_supported_yet")
        self.assertEqual(statuses["b1"]["errorReason"], "incomplete_config")
        self.assertEqual(m.config_version, "v1")

    def test_removing_a_printer_drops_its_status(self):
        m = PrinterManager()
        m.reconcile([{"id": "k1", "vendor": "klipper"}], version="v1")
        self.assertEqual(len(m.statuses()), 1)
        m.reconcile([], version="v2")  # printer removed in the admin UI
        self.assertEqual(m.statuses(), [])
        self.assertEqual(m.config_version, "v2")

    def test_teardown_rescues_unacked_jobs(self):
        # Codex finding: a config change rebuilds the adapter — its in-memory
        # job buffer must survive into the manager's orphan buffer until acked.
        class FakeAdapter:
            def __init__(self):
                self.jobs = [{"jobKey": "task_S_1", "printerId": "b1", "status": "done"}]
                self.stopped = False

            def pending_jobs(self):
                return list(self.jobs)

            def ack_jobs(self, keys):
                self.jobs = [j for j in self.jobs if j["jobKey"] not in set(keys)]

            def stop(self):
                self.stopped = True

        m = PrinterManager()
        fake = FakeAdapter()
        m._adapters["b1"] = fake
        m._fingerprints["b1"] = ("bambu", "h", "s", "c")

        m.reconcile([], version="v2")  # printer removed → adapter torn down
        self.assertTrue(fake.stopped)
        pending = m.pending_jobs()
        self.assertEqual([j["jobKey"] for j in pending], ["task_S_1"])  # rescued

        m.ack_jobs(["task_S_1"])  # confirmed send clears the orphan too
        self.assertEqual(m.pending_jobs(), [])


if __name__ == "__main__":
    unittest.main()
