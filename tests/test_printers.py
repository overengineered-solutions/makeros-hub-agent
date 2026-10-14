"""Stdlib-only tests for the printer layer. The pure parser (bambu_parse) is
fully covered without paho or a printer; the manager is covered on its non-paho
paths (klipper/incomplete/removal). Run: python3 -m unittest discover -s tests"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
        s = bambu_parse.normalize_status("p1", merged, connection_state="connected", model="N1")
        self.assertEqual(s["printerId"], "p1")
        self.assertEqual(s["connectionState"], "connected")
        self.assertEqual(s["state"], "printing")
        self.assertEqual(s["progressPct"], 42)
        self.assertEqual(s["nozzleTempC"], 210.4)
        self.assertEqual(s["bedTempC"], 60.0)
        self.assertEqual(s["jobName"], "bracket.3mf")
        self.assertEqual(s["etaMinutes"], 90)  # MINUTES, not seconds

    def test_s_obj_surfaces_as_skipped_objects(self):
        s = bambu_parse.normalize_status(
            "p", self._merged(gcode_state="RUNNING", s_obj=[286, 9, True, "x"]),
            connection_state="connected", model="N1",
        )
        # ints only (bool/str dropped); the cloud scopes display to active prints.
        self.assertEqual(s["skippedObjects"], [286, 9])

    def test_empty_or_absent_s_obj_omits_skipped_objects(self):
        for s_obj in ([], None):
            fields = {"gcode_state": "RUNNING"}
            if s_obj is not None:
                fields["s_obj"] = s_obj
            s = bambu_parse.normalize_status("p", self._merged(**fields), connection_state="connected", model="N1")
            self.assertNotIn("skippedObjects", s)

    def test_model_and_raw_gcode_state_ride_the_dto(self):
        # RC1: the VP live-mirror scopes each pool by status["model"] — it must come from the DTO, not a
        # test fixture. RC6: the cloud needs the RAW state (FINISH = part still on the bed) next to the
        # collapsed one. Both are additive keys pstation's hand-picking ingest ignores until it uses them.
        merged = self._merged(gcode_state="FINISH")
        s = bambu_parse.normalize_status("p", merged, connection_state="connected", model="A1 Mini")
        self.assertEqual(s["model"], "A1 Mini")
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["gcodeState"], "FINISH")
        off = bambu_parse.normalize_status("p", merged, connection_state="offline", model=" A1 Mini ")
        self.assertEqual(off["model"], "A1 Mini")  # identity, not telemetry: present while offline too
        self.assertNotIn("gcodeState", off)
        # model is REQUIRED at the call site (a forgotten kwarg fails loudly, never a silently unscoped
        # pool); None — a machine config-down left without a model — omits the key.
        with self.assertRaises(TypeError):
            bambu_parse.normalize_status("p", merged, connection_state="connected")  # type: ignore[call-arg]
        bare = bambu_parse.normalize_status("p", self._merged(gcode_state=""), connection_state="connected", model=None)
        self.assertNotIn("model", bare)
        self.assertNotIn("gcodeState", bare)

    def test_unidentified_spools_use_the_tray_state_rule_and_contiguous_units(self):
        ams = {
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        {"id": "0"},                                                        # empty: bare id
                        {"id": "1", "state": "9", "tray_type": "", "tray_color": "00000000"},  # SPOOL|STEADY, untyped
                        {"id": "2", "state": 1, "tray_type": ""},                           # inserted, still reading
                        {"id": "3", "tray_type": "", "tray_color": "00000000", "remain": -1},  # legacy report, no state
                    ],
                },
                "garbage",
                {"id": "128", "tray": [{"id": "0", "state": 3, "tray_type": "PLA"}, {"id": "1", "state": 3}]},
            ]
        }
        # An EMPTY external holder reports a full field set — never flagged without a state bit.
        print_obj = {"ams": ams, "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000", "remain": 0}}
        self.assertEqual(
            bambu_parse.build_unidentified_spools(print_obj),
            [{"unit": 0, "slot": 1}, {"unit": 0, "slot": 3}, {"unit": 1, "slot": 1}],  # raw id 128 -> unit 1
        )
        print_obj["vt_tray"]["state"] = 9
        self.assertEqual(bambu_parse.build_unidentified_spools(print_obj)[-1], {"slot": 254})
        s = bambu_parse.normalize_status("p", {"print": print_obj}, connection_state="connected", model="N1")
        self.assertEqual(len(s["unidentifiedSpools"]), 4)
        self.assertNotIn(
            "unidentifiedSpools",
            bambu_parse.normalize_status("p", {"print": print_obj}, connection_state="offline", model="N1"),
        )
        self.assertIsNone(bambu_parse.build_unidentified_spools({"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}}))
        self.assertIsNone(bambu_parse.build_unidentified_spools({"vt_tray": {"id": "254", "state": 9, "tray_type": "PLA"}}))

    def test_finish_and_failed_are_both_idle(self):
        # A finished OR failed job leaves the printer free again — a failed *job*
        # is not a printer *fault* (real faults surface via HMS). Bed-occupancy
        # safety for auto-routing is gated in the cloud, not by parking the
        # printer in "error".
        for gcode_state in ("FINISH", "FAILED"):
            self.assertEqual(
                bambu_parse.normalize_status(
                    "p", self._merged(gcode_state=gcode_state), connection_state="connected", model="N1"
                )["state"],
                "idle",
            )

    def test_omits_absent_fields_and_state_when_not_connected(self):
        s = bambu_parse.normalize_status("p", {"print": {}}, connection_state="connecting", model="N1")
        self.assertEqual(set(s.keys()), {"printerId", "connectionState", "model"})  # no null keys (strict DTO); model is identity
        self.assertNotIn("state", s)

    def test_error_reason_only_on_error(self):
        ok = bambu_parse.normalize_status("p", {"print": {}}, connection_state="connected", model="N1")
        self.assertNotIn("errorReason", ok)
        err = bambu_parse.normalize_status("p", {"print": {}}, connection_state="error", model="N1", error_reason="mqtt_auth_failed")
        self.assertEqual(err["errorReason"], "mqtt_auth_failed")

    def test_coerces_string_numbers_and_clamps_progress(self):
        s = bambu_parse.normalize_status(
            "p", {"print": {"nozzle_temper": "215.0", "mc_percent": 130}}, connection_state="connected", model="N1"
        )
        self.assertEqual(s["nozzleTempC"], 215.0)
        self.assertEqual(s["progressPct"], 100)  # clamped

    def test_gcode_file_fallback_for_job_name(self):
        s = bambu_parse.normalize_status(
            "p", {"print": {"gcode_file": "Metadata/plate_1.gcode"}}, connection_state="connected", model="N1"
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
        s = bambu_parse.normalize_status("p", merged, connection_state="connected", model="N1")
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
            "p", self._merged(gcode_state="IDLE", print_error=0), connection_state="connected", model="N1"
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
            with self.subTest(connection_state=state, model="N1"):
                s = bambu_parse.normalize_status("p", merged, connection_state=state, model="N1")
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
                    # `unit` is the ENUMERATION index (0 for the only unit), not
                    # the reported id "3" — the real id is preserved in `raw`.
                    "unit": 0,
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

    def test_build_ams_enumerates_high_unit_ids_keeping_raw(self):
        # The H2D's second AMS unit reports id 128 (the AMS-HT does too), which
        # exceeds the cloud DTO's unit cap (0-15) and would drop the WHOLE report
        # → printer stuck `pending`. build_ams emits the ENUMERATION index as
        # `unit` so it stays in range, while `raw.id` preserves the real firmware
        # id for the (deferred) per-nozzle H2D routing.
        print_obj = {
            "ams": {
                "ams": [
                    {"id": "0", "tray": [{"tray_type": "PLA"}]},
                    {"id": "128", "tray": [{"tray_type": "PETG"}]},
                ]
            }
        }
        units = bambu_parse.build_ams(print_obj)
        self.assertEqual([u["unit"] for u in units], [0, 1])
        self.assertEqual([u["raw"]["id"] for u in units], ["0", "128"])

    def test_build_ams_skips_garbage_without_crashing(self):
        self.assertIsNone(bambu_parse.build_ams({}))
        self.assertIsNone(bambu_parse.build_ams({"ams": {"ams": "bad"}}))
        self.assertEqual(
            bambu_parse.build_ams(
                {"ams": {"ams": ["bad", {"id": "not-int", "tray": [{}, "bad", {"remain": "x"}]}]}}
            ),
            [
                {
                    # Leading non-dict "bad" is skipped; the first ACCEPTED unit
                    # gets contiguous index 0 (not the raw array position 1).
                    "unit": 0,
                    "trays": [{"slot": 2}],
                    "raw": {"id": "not-int", "tray": [{}, "bad", {"remain": "x"}]},
                }
            ],
        )
        self.assertEqual(
            bambu_parse.build_ams({"ams": {"ams": [{"id": "2", "humidity": "4", "dry_time": "120"}]}}),
            [
                {
                    "unit": 0,  # enumeration index; raw.id keeps the reported "2"
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
                "tray_info_idx": "GFL99",
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
                "filamentId": "GFL99",
                "colorHex": "10203040",
                "remainPct": 55.5,
                "tagUid": "vt-uid",
            },
        )
        s = bambu_parse.normalize_status("p", {"print": print_obj}, connection_state="connected", model="N1")
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
        self.assertEqual(bambu_parse.build_active_tray({"ams": {"tray_now": 999}}), 999)  # AMS-HT range (512+) is real
        self.assertIsNone(bambu_parse.build_active_tray({"ams": {"tray_now": 4096}}))  # out of range
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
    def test_a_rebuilt_bambu_tracker_keeps_the_full_record_not_the_recovered_one(self):
        # codex review of release-0.68.0: a reloaded JobTracker's first FINISH re-emits a print it already holds
        # (_emit_recovered: no startedAt, no printTimeSeconds, endedAt = now). One row per key, the full one, for the
        # heartbeat AND queue progress (two rows were two candidates: held ambiguous_queue_correlation).
        from makeros_hub.printers.jobs import JobTracker

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gaudi.json"
            running = {"print": {"gcode_state": "RUNNING", "task_id": "77", "subtask_name": "cube"}}
            done = {"print": {"gcode_state": "FINISH", "task_id": "77", "subtask_name": "cube"}}
            first = JobTracker("gaudi", "TEST", state_path=path)
            first.observe(running, 1000.0)
            first.observe(done, 4600.0)
            reloaded = JobTracker("gaudi", "TEST", state_path=path)   # the rebuilt adapter's tracker
            reloaded.observe(done, 4660.0)
            (job,) = reloaded.pending()
            self.assertEqual((job["jobKey"], job["printTimeSeconds"]), ("task_TEST_77", 3600))
            (job,) = JobTracker("gaudi", "TEST", state_path=path).pending()   # and what it saved
            self.assertEqual(job["printTimeSeconds"], 3600)

            # a duplicate an older agent already saved: the load keeps the first
            poor = {"jobKey": "task_TEST_77", "printerId": "gaudi", "status": "done", "endedAt": "2026-10-14T00:00:00Z"}
            path.write_text(json.dumps([job, poor]))
            self.assertEqual(JobTracker("gaudi", "TEST", state_path=path).pending(), [job])

        # the manager merges the rescued orphan copy and an adapter's rows the same way: first per key
        class Adapter:
            def __init__(self, rows):
                self.rows = rows

            def pending_jobs(self):
                return list(self.rows)

            def stop(self):
                pass

        manager = PrinterManager()
        manager._adapters["gaudi"] = Adapter([job, poor])
        manager._stop("gaudi")
        manager._adapters["gaudi"] = Adapter([poor])
        self.assertEqual(manager.pending_jobs(), [job])

    def test_heartbeat_retries_failed_bambu_without_a_config_change(self):
        instances = []
        # Stands in for the adapter's DURABLE terminal-job buffer (JobTracker
        # persists per printer id): every rebuilt adapter re-reads the same
        # unacked job, exactly as the real one does after a teardown.
        durable_jobs = {"gaudi": [{"jobKey": "task_TEST_1", "printerId": "gaudi", "status": "done"}]}

        class FakeBambu:
            fail_start = False

            def __init__(self, printer_id, **config):
                self.printer_id = printer_id
                self.config = config
                self.state = "error"
                self.stopped = False
                instances.append(self)

            def start(self):
                if FakeBambu.fail_start:
                    raise OSError("printer unreachable")

            def stop(self):
                self.stopped = True

            def status(self):
                return {"printerId": self.printer_id, "connectionState": self.state}

            def pending_jobs(self):
                return list(durable_jobs.get(self.printer_id, []))

            def ack_jobs(self, keys):
                durable_jobs[self.printer_id] = [
                    j for j in durable_jobs.get(self.printer_id, []) if j["jobKey"] not in set(keys)
                ]

        module = types.ModuleType("makeros_hub.printers.bambu")
        module.BambuAdapter = FakeBambu
        printer = {"id": "gaudi", "vendor": "bambu", "host": "192.0.2.1", "serial": "TEST", "accessCode": "test-only", "model": "X1C"}
        with mock.patch.dict(sys.modules, {module.__name__: module}), mock.patch(
            "makeros_hub.printers.manager.time.monotonic", return_value=0
        ) as clock:
            manager = PrinterManager()
            manager.reconcile([printer], "unchanged")
            clock.return_value = 119
            manager.statuses()
            self.assertEqual(len(instances), 1)
            clock.return_value = 120
            manager.statuses()  # The heartbeat reads statuses; it does not re-fetch unchanged config.
            self.assertEqual(len(instances), 2)
            self.assertTrue(instances[0].stopped)
            self.assertEqual(instances[1].config["model"], "X1C")
            self.assertEqual(manager.config_version, "unchanged")
            manager.statuses()
            self.assertEqual(len(instances), 2)  # Still failing: bounded retry, not every heartbeat.

            # A retry whose start() RAISES leaves no adapter behind. The printer
            # must still be retried on the next window (it was stranded until an
            # operator edited the config) — and only on the next window.
            FakeBambu.fail_start = True
            clock.return_value = 240
            failed = manager.statuses()
            self.assertEqual(len(instances), 3)
            self.assertEqual(failed, [{"printerId": "gaudi", "connectionState": "error", "errorReason": "agent_start_failed"}])
            clock.return_value = 359
            manager.statuses()
            self.assertEqual(len(instances), 3)  # No hot loop between windows.

            # The start failure clears: the next window
            # rebuilds and the heartbeat reports the live connection, with the
            # 'agent_start_failed' row gone.
            FakeBambu.fail_start = False
            clock.return_value = 360
            manager.statuses()
            self.assertEqual(len(instances), 4)
            instances[3].state = "connected"
            self.assertEqual(manager.statuses(), [{"printerId": "gaudi", "connectionState": "connected"}])
            clock.return_value = 800
            manager.statuses()
            self.assertEqual(len(instances), 4)  # Leave a recovered connection alone.

            # Terminal (billable) evidence survived all four rebuilds — reported
            # once, not once per rebuild, and never dropped.
            self.assertEqual([j["jobKey"] for j in manager.pending_jobs()], ["task_TEST_1"])
            self.assertEqual(len(manager._orphan_jobs), 1)

            manager.ack_jobs(["task_TEST_1"])  # a confirmed send clears orphan + adapter
            self.assertEqual(manager.pending_jobs(), [])

            manager.reconcile([], "removed")
            self.assertEqual(manager.statuses(), [])
            self.assertEqual(len(instances), 4)  # Never resurrect a removed printer.
            self.assertEqual(manager._bambu_configs, {})  # the cached accessCode goes with it
            self.assertEqual(manager._stuck_retry_at, {})

            manager.reconcile([dict(printer, accessCode=None)], "incomplete")
            with mock.patch.object(manager, "_reconcile_bambu", wraps=manager._reconcile_bambu) as retry:
                self.assertEqual(manager.statuses()[0]["errorReason"], "incomplete_config")
                retry.assert_not_called()  # Incomplete config does not hot-loop.
            manager.reconcile([printer], "restored")
            self.assertEqual(len(instances), 5)
            manager.stop_all()
            clock.return_value = 1200
            self.assertEqual(manager.statuses(), [])
            self.assertEqual(len(instances), 5)
            self.assertEqual(manager._bambu_configs, {})
            self.assertEqual(manager._camera_meta, {})

    def test_klipper_with_moonraker_url_starts_polling(self):
        # The Klipper adapter is now real — given a URL it spins up a
        # polling thread + reports 'connecting' until the first poll lands.
        # We stop the manager at the end so the daemon thread doesn't
        # linger; the test point is the dispatch + initial state.
        m = PrinterManager()
        try:
            m.reconcile(
                [{"id": "k1", "vendor": "klipper", "moonrakerUrl": "http://127.0.0.1:1"}],
                version="v1",
            )
            statuses = {s["printerId"]: s for s in m.statuses()}
            # The adapter starts in 'connecting' until either the first poll
            # succeeds (won't — port 1 is closed) or the CONNECT_TIMEOUT
            # elapses + an error reason is captured. Either is acceptable.
            self.assertIn(statuses["k1"]["connectionState"], ("connecting", "error"))
            self.assertEqual(m.config_version, "v1")
        finally:
            m.reconcile([], version="v2")  # tears down the adapter cleanly

    def test_klipper_without_moonraker_url_is_incomplete(self):
        m = PrinterManager()
        m.reconcile([{"id": "k1", "vendor": "klipper"}], version="v1")
        statuses = {s["printerId"]: s for s in m.statuses()}
        self.assertEqual(statuses["k1"]["connectionState"], "error")
        self.assertEqual(statuses["k1"]["errorReason"], "incomplete_config")

    def test_incomplete_bambu_reports_incomplete_config(self):
        m = PrinterManager()
        m.reconcile(
            [{"id": "b1", "vendor": "bambu", "host": None, "serial": None, "accessCode": None}],
            version="v1",
        )
        statuses = {s["printerId"]: s for s in m.statuses()}
        self.assertEqual(statuses["b1"]["errorReason"], "incomplete_config")

    def test_stuck_error_adapter_rebuilds_only_past_backoff(self):
        # A wedged adapter (paho got a CONNACK auth-rejection it won't auto-retry)
        # must be rebuilt so a RECOVERED printer self-heals — but only once per
        # backoff window, and never for healthy / transiently-offline adapters.
        m = PrinterManager()

        class _FakeAdapter:
            def __init__(self, state):
                self._state = state

            def status(self):
                return {"connectionState": self._state}

        # No adapter yet → (re)build it.
        self.assertTrue(m._stuck_needs_retry("p1"))

        # Healthy → leave the live connection alone.
        m._adapters["p1"] = _FakeAdapter("connected")
        self.assertFalse(m._stuck_needs_retry("p1"))

        # Transient 'offline' → paho's reconnect handles it; don't churn.
        m._adapters["p1"] = _FakeAdapter("offline")
        self.assertFalse(m._stuck_needs_retry("p1"))

        # Hard 'error' + past the backoff (0.0) → rebuild.
        m._adapters["p1"] = _FakeAdapter("error")
        m._stuck_retry_at["p1"] = 0.0
        self.assertTrue(m._stuck_needs_retry("p1"))

        # Hard 'error' but still inside the backoff window → wait.
        m._stuck_retry_at["p1"] = 1e18
        self.assertFalse(m._stuck_needs_retry("p1"))

    def test_other_vendor_unsupported(self):
        m = PrinterManager()
        m.reconcile([{"id": "x1", "vendor": "other"}], version="v1")
        statuses = {s["printerId"]: s for s in m.statuses()}
        self.assertEqual(statuses["x1"]["errorReason"], "other_not_supported_yet")

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
