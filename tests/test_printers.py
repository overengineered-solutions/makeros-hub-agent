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


class TestSummarizeShape(unittest.TestCase):
    def test_redacts_values_keeps_keys(self):
        merged = {"print": {"gcode_state": "RUNNING", "hms": [1, 2], "nozzle_temper": 210}}
        shape = bambu_parse.summarize_shape(merged)
        self.assertEqual(shape["values"], "[redacted]")
        self.assertIn("gcode_state", shape["printKeys"])
        self.assertEqual(shape["arrayLengths"]["hms"], 2)
        # No actual telemetry values leak into the shape summary.
        self.assertNotIn("210", str(shape))


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
