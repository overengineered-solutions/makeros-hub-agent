"""Stdlib-only tests for the printer layer. The pure parser (bambu_parse) is
fully covered without paho or a printer; the manager is covered on its non-paho
paths (klipper/incomplete/removal). Run: python3 -m unittest discover -s tests"""

import unittest

from makeros_hub import diagnostics
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

    def test_finish_and_failed_are_both_idle(self):
        # A finished OR failed job leaves the printer free again — a failed *job*
        # is not a printer *fault* (real faults surface via HMS). Bed-occupancy
        # safety for auto-routing is gated in the cloud, not by parking the
        # printer in "error".
        for gcode_state in ("FINISH", "FAILED"):
            self.assertEqual(
                bambu_parse.normalize_status(
                    "p", self._merged(gcode_state=gcode_state), connection_state="connected"
                )["state"],
                "idle",
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
                    "raw": {
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
                    },
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
            vt_tray={"tray_type": "PLA"},
        )
        for state in ("offline", "error"):
            with self.subTest(connection_state=state):
                s = bambu_parse.normalize_status("p", merged, connection_state=state)
                self.assertNotIn("ams", s)
                self.assertNotIn("amsActiveTray", s)
                self.assertNotIn("hms", s)
                self.assertNotIn("printError", s)
                self.assertNotIn("vtTray", s)


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
                    "raw": {
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
                    },
                }
            ],
        )

    def test_build_ams_extracts_rich_tray_unit_fields_and_scrubs_raw(self):
        cols = [
            "11223344",
            "#55667788",
            "bad",
            "99aabbcc",
            "01020304",
            "11111111",
            "22222222",
            "33333333",
            "44444444",
            "55555555",
        ]
        print_obj = {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "humidity": "3",
                        "humidity_raw": "44",
                        "temp": 25,
                        "dry_time": "120",
                        "ams_id": "AMS-SERIAL-123",
                        "chip_id": "CHIP-XYZ",
                        "serialNumber": "drop-me",
                        "access_code": "drop-me",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": " PLA ",
                                "tray_sub_brands": "PLA Marble",
                                "tray_info_idx": "GFA00",
                                "tray_color": "abcdef12",
                                "cols": cols,
                                "remain": "88.2",
                                "tag_uid": " UID123 ",
                                "nozzle_temp_min": "190.5",
                                "nozzle_temp_max": "230",
                                "ip_addr": "drop-me",
                                "token_value": "drop-me",
                            }
                        ],
                    }
                ]
            }
        }

        unit = bambu_parse.build_ams(print_obj)[0]
        self.assertEqual(unit["humidity"], 3.0)
        self.assertEqual(unit["temp"], 25.0)
        self.assertEqual(
            unit["trays"][0],
            {
                "slot": 0,
                "material": "PLA",
                "productName": "PLA Marble",
                "filamentId": "GFA00",
                "colorHex": "ABCDEF12",
                "colors": [
                    "11223344",
                    "55667788",
                    "99AABBCC",
                    "01020304",
                    "11111111",
                    "22222222",
                    "33333333",
                    "44444444",
                ],
                "remainPct": 88.2,
                "tagUid": "UID123",
                "nozzleTempMin": 190,
                "nozzleTempMax": 230,
            },
        )
        self.assertEqual(unit["humidityRaw"], 44.0)
        self.assertEqual(unit["dryTime"], 120)
        self.assertEqual(unit["raw"]["dry_time"], "120")
        self.assertNotIn("serialNumber", unit["raw"])
        self.assertNotIn("ams_id", unit["raw"])
        self.assertNotIn("chip_id", unit["raw"])
        self.assertNotIn("access_code", unit["raw"])
        self.assertNotIn("ip_addr", unit["raw"]["tray"][0])
        self.assertNotIn("token_value", unit["raw"]["tray"][0])

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
                    "raw": {
                        "id": "0",
                        "tray": [
                            {"tray_type": "PLA", "tray_color": "GGGGGGGG"},
                            {"tray_type": "PETG", "tray_color": "FF0000FF11"},
                            {"tray_type": "TPU", "tray_color": "#00FF00FF"},
                            {"tray_type": "ABS"},
                            {"tray_type": "PA", "tray_color": "FFFFFFFF"},
                        ],
                    },
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
            [
                {
                    "unit": 1,
                    "trays": [{"slot": 2}],
                    "raw": {"id": "not-int", "tray": [{}, "bad", {"remain": "x"}]},
                }
            ],
        )
        self.assertEqual(
            bambu_parse.build_ams({"ams": {"ams": [{"id": "2", "humidity": "4", "dry_time": "120"}]}}),
            [
                {
                    "unit": 2,
                    "trays": [],
                    "humidity": 4.0,
                    "dryTime": 120,
                    "raw": {"id": "2", "humidity": "4", "dry_time": "120"},
                }
            ],
        )

    def test_build_vt_tray_from_external_spool(self):
        print_obj = {
            "vt_tray": {
                "tray_type": "PLA",
                "tray_sub_brands": "PLA Marble",
                "tray_info_idx": "ignored-for-vt",
                "tray_color": "10203040",
                "remain": "55.5",
                "tag_uid": "vt-uid",
                "nozzle_temp_min": "190",
            }
        }
        self.assertEqual(
            bambu_parse.build_vt_tray(print_obj),
            {
                "material": "PLA",
                "productName": "PLA Marble",
                "colorHex": "10203040",
                "remainPct": 55.5,
                "tagUid": "vt-uid",
            },
        )
        s = bambu_parse.normalize_status("p", {"print": print_obj}, connection_state="connected")
        self.assertEqual(s["vtTray"], bambu_parse.build_vt_tray(print_obj))
        self.assertIsNone(bambu_parse.build_vt_tray({"vt_tray": {}}))
        self.assertIsNone(bambu_parse.build_vt_tray({"vt_tray": {"id": "0"}}))

    def test_build_ams_omits_oversized_raw_passthrough(self):
        unit = {"id": "0", "drying_blob": "x" * 9000, "tray": [{"tray_type": "PLA"}]}
        self.assertEqual(
            bambu_parse.build_ams({"ams": {"ams": [unit]}}),
            [{"unit": 0, "trays": [{"slot": 0, "material": "PLA"}]}],
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
        self.assertEqual(shape["arrayLengths"]["ams.trays_loaded"], 1)
        self.assertEqual(shape["arrayLengths"]["hms"], 1)
        self.assertEqual(shape["amsTrayKeys"], ["tray_type"])
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

    def test_adapter_status_error_records_with_printer_access_code_secret(self):
        class CapturingDiagnostics(diagnostics.Diagnostics):
            def __init__(self):
                super().__init__(enable_network=False)
                self.extra_secrets = None

            def record(self, subsystem, message, extra_secrets=None):
                self.extra_secrets = extra_secrets
                super().record(subsystem, message, extra_secrets=extra_secrets)

        class FakeAdapter:
            def status(self):
                raise RuntimeError("MQTT refused: adapter saw bare code 12345678")

        diag = CapturingDiagnostics()
        m = PrinterManager(diagnostics=diag)
        m._adapters["b1"] = FakeAdapter()
        m._fingerprints["b1"] = ("bambu", "h", "s", "12345678")

        statuses = m.statuses()

        self.assertEqual(statuses[0]["errorReason"], "agent_status_error")
        self.assertEqual(diag.extra_secrets, ["12345678"])
        self.assertNotIn("12345678", diag.errors.snapshot()["printers"]["message"])


if __name__ == "__main__":
    unittest.main()
