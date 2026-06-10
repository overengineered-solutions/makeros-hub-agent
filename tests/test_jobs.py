"""Stdlib-only tests for the pure JobTracker — terminal-job detection over
simulated Bambu report sequences. Run: python3 -m unittest discover -s tests"""

import unittest

from makeros_hub.printers.jobs import JobTracker, decode_active_material


def report(gcode_state=None, subtask=None, task_id=None, ams=None, vt_tray=None):
    p = {}
    if gcode_state is not None:
        p["gcode_state"] = gcode_state
    if subtask is not None:
        p["subtask_name"] = subtask
    if task_id is not None:
        p["task_id"] = task_id
    if ams is not None:
        p["ams"] = ams
    if vt_tray is not None:
        p["vt_tray"] = vt_tray
    return {"print": p}


AMS_PLA_SLOT0 = {
    "tray_now": "0",
    "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}, {}, {}, {}]}],
}


class TestHappyPath(unittest.TestCase):
    def test_running_to_finish_emits_done_with_metadata(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="bracket.3mf", task_id="998877", ams=AMS_PLA_SLOT0), now=1000.0)
        t.observe(report("RUNNING"), now=2000.0)  # mid-print delta
        t.observe(report("FINISH"), now=6400.0)

        jobs = t.pending()
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["jobKey"], "task_998877")  # printer-supplied id wins
        self.assertEqual(j["printerId"], "p1")
        self.assertEqual(j["filename"], "bracket.3mf")
        self.assertEqual(j["materialKey"], "PLA")
        self.assertEqual(j["printTimeSeconds"], 5400)
        self.assertIn("+00:00", j["startedAt"])  # ISO with offset (cloud DTO)
        self.assertIn("+00:00", j["endedAt"])

    def test_running_to_failed_emits_failed(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="x.3mf"), now=10.0)
        t.observe(report("FAILED"), now=20.0)
        self.assertEqual(t.pending()[0]["status"], "failed")

    def test_pause_keeps_job_active(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="x.3mf"), now=10.0)
        t.observe(report("PAUSE"), now=20.0)
        self.assertEqual(t.pending(), [])  # still in flight
        t.observe(report("RUNNING"), now=30.0)
        t.observe(report("FINISH"), now=40.0)
        jobs = t.pending()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["printTimeSeconds"], 30)  # one job, 10→40


class TestEdges(unittest.TestCase):
    def test_idle_noise_emits_nothing(self):
        t = JobTracker("p1", "SER1")
        for now, state in [(1, "IDLE"), (2, "PREPARE"), (3, "IDLE")]:
            t.observe(report(state), now=float(now))
        self.assertEqual(t.pending(), [])

    def test_missed_ending_closes_as_cancelled(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="x.3mf"), now=10.0)
        t.observe(report("IDLE"), now=99.0)  # FINISH/FAILED frame was missed
        jobs = t.pending()
        self.assertEqual(jobs[0]["status"], "cancelled")  # visible, never billed

    def test_name_change_while_running_closes_old_opens_new(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="first.3mf"), now=10.0)
        t.observe(report("RUNNING", subtask="second.3mf"), now=500.0)
        t.observe(report("FINISH"), now=900.0)
        jobs = t.pending()
        self.assertEqual([j["status"] for j in jobs], ["cancelled", "done"])
        self.assertEqual(jobs[0]["filename"], "first.3mf")
        self.assertEqual(jobs[1]["filename"], "second.3mf")

    def test_late_task_id_upgrades_fingerprint_key(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="x.3mf"), now=10.0)  # no task_id yet
        t.observe(report("RUNNING", task_id="42"), now=20.0)  # arrives late
        t.observe(report("FINISH"), now=30.0)
        self.assertEqual(t.pending()[0]["jobKey"], "task_42")

    def test_fingerprint_key_when_printer_gives_no_id(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="x.3mf", task_id="0"), now=10.0)  # '0' = none
        t.observe(report("FINISH"), now=20.0)
        self.assertTrue(t.pending()[0]["jobKey"].startswith("fp_"))

    def test_ack_clears_only_confirmed_keys(self):
        t = JobTracker("p1", "SER1")
        t.observe(report("RUNNING", subtask="a.3mf", task_id="1"), now=1.0)
        t.observe(report("FINISH"), now=2.0)
        t.observe(report("RUNNING", subtask="b.3mf", task_id="2"), now=3.0)
        t.observe(report("FINISH"), now=4.0)
        self.assertEqual(len(t.pending()), 2)
        t.ack(["task_1"])
        remaining = t.pending()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["jobKey"], "task_2")


class TestDecodeActiveMaterial(unittest.TestCase):
    def test_ams_slot(self):
        self.assertEqual(decode_active_material({"print": {"ams": AMS_PLA_SLOT0}}), "PLA")

    def test_external_spool(self):
        merged = {"print": {"ams": {"tray_now": "254"}, "vt_tray": {"tray_type": "PETG"}}}
        self.assertEqual(decode_active_material(merged), "PETG")

    def test_none_and_unknown(self):
        self.assertIsNone(decode_active_material({"print": {"ams": {"tray_now": "255"}}}))
        self.assertIsNone(decode_active_material({"print": {}}))
        # slot index out of range
        merged = {"print": {"ams": {"tray_now": "7", "ams": [{"tray": [{}]}]}}}
        self.assertIsNone(decode_active_material(merged))


if __name__ == "__main__":
    unittest.main()
