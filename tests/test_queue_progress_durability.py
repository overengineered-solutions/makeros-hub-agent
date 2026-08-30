"""v0.54: the queue↔print correlation survives a restart (owner rule: nothing that matters lives only in RAM)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub.printers.queue_progress import QueueProgressTracker


class TestDurableQueueProgress(unittest.TestCase):
    def test_state_round_trip_keeps_dispatches_baselines_and_links(self):
        t = QueueProgressTracker()
        t.record_dispatch("q1", [{"jobKey": "old-1"}], now=100.0)
        t.collect([{"jobKey": "old-1"}], "RUNNING", now=101.0)          # → printing reported
        state = json.loads(json.dumps(t.to_state()))
        u = QueueProgressTracker()
        u.load_state(state, now=5.0, now_wall=state["dispatches"][0]["dispatched_wall"] + 30)
        self.assertEqual(u.to_state()["dispatches"][0]["queueJobId"], "q1")
        self.assertTrue(u.to_state()["dispatches"][0]["reported_printing"])
        self.assertEqual(u.to_state()["dispatches"][0]["baselineKeys"], ["old-1"])

    def test_print_that_ended_while_the_box_was_down_is_completed_for_its_job(self):
        t = QueueProgressTracker()
        t.record_dispatch("q1", [{"jobKey": "old-1"}], now=0.0)
        u = QueueProgressTracker()
        u.load_state(t.to_state(), now=1000.0)
        # after the restart JobTracker recovers the terminal job from the printer's own task id (jobs.py _emit_recovered)
        reports = u.collect([{"jobKey": "old-1"}, {"jobKey": "task-77", "status": "done"}], "FINISH", now=1001.0)
        self.assertEqual(reports, [{"queueJobId": "q1", "state": "printing"}, {"queueJobId": "q1", "state": "completed", "printerJobKey": "task-77"}])

    def test_rehydrated_printing_job_with_idle_printer_and_no_terminal_is_held_not_completed(self):
        t = QueueProgressTracker(start_timeout_sec=60)
        t.record_dispatch("q1", [], now=0.0)
        t.collect([], "RUNNING", now=1.0)                                # it was printing before the restart
        u = QueueProgressTracker(start_timeout_sec=60)
        u.load_state(t.to_state(), now=1000.0)
        self.assertEqual(u.collect([], "IDLE", now=1010.0), [])          # first observation at 1010: the patience window starts HERE, not at boot
        self.assertEqual(u.collect([], "IDLE", now=1061.0), [])
        self.assertEqual(u.collect([], "IDLE", now=1071.0), [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart"}])

    def test_garbage_state_is_ignored(self):
        u = QueueProgressTracker()
        u.load_state("nope"); u.load_state({"dispatches": [1, {"queueJobId": 5}], "linked": [None]})
        self.assertEqual(u.to_state(), {"dispatches": [], "linked": []})


class TestAdapterPersistence(unittest.TestCase):
    def test_bambu_adapter_writes_and_rehydrates_its_queue_progress_file(self):
        from makeros_hub.printers import bambu as bambu_module
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)):
            a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
            with a._lock:
                a._queue_progress.record_dispatch("q9", [])
                a._save_queue_progress()
            path = Path(d) / "p1.json"
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())["dispatches"][0]["queueJobId"], "q9")
            b = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
            self.assertEqual(b._queue_progress.to_state()["dispatches"][0]["queueJobId"], "q9")


if __name__ == "__main__":
    unittest.main()


class TestV054Fixes(unittest.TestCase):
    def test_silence_after_restart_is_not_idle(self):
        t = QueueProgressTracker(start_timeout_sec=60)
        t.record_dispatch("q1", [], now=0.0)
        t.collect([], "RUNNING", now=1.0)
        u = QueueProgressTracker(start_timeout_sec=60)
        u.load_state(t.to_state(), now=1000.0)
        self.assertEqual(u.collect([], None, now=2000.0), [])           # no report yet → no timeout, ever
        self.assertEqual(u.collect([], "IDLE", now=2000.0), [])         # first observed idle: patience window starts
        self.assertEqual(u.collect([], "IDLE", now=2061.0), [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart"}])

    def test_the_print_already_running_at_dispatch_is_never_our_terminal(self):
        t = QueueProgressTracker()
        t.record_dispatch("q1", [], now=0.0, task_name="mine.3mf", active_key="task_S1_41")
        u = QueueProgressTracker()
        u.load_state(t.to_state(), now=100.0)
        # after the restart the OLD print (task 41) is recovered as terminal — it must not complete q1
        self.assertEqual(u.collect([{"jobKey": "task_S1_41", "status": "done", "filename": "theirs.3mf"}], "FINISH", now=101.0), [])
        # a recovered terminal naming a different file is not ours either
        self.assertEqual(u.collect([{"jobKey": "task_S1_42", "status": "done", "filename": "other.3mf"}], "FINISH", now=102.0), [])
        # ours: right name
        self.assertEqual(u.collect([{"jobKey": "task_S1_43", "status": "done", "filename": "mine.3mf"}], "FINISH", now=103.0),
                         [{"queueJobId": "q1", "state": "printing"}, {"queueJobId": "q1", "state": "completed", "printerJobKey": "task_S1_43"}])

    def test_discard_dispatch_forgets_a_failed_publish(self):
        t = QueueProgressTracker()
        t.record_dispatch("q1", [], now=0.0)
        t.discard_dispatch("q1")
        self.assertEqual(t.to_state()["dispatches"], [])

    def test_job_tracker_pending_terminals_survive_a_restart(self):
        from makeros_hub.printers.jobs import JobTracker
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p1.json"
            a = JobTracker("p1", "S1", state_path=path)
            a._buffer({"jobKey": "task_S1_7", "printerId": "p1", "status": "done"})
            b = JobTracker("p1", "S1", state_path=path)
            self.assertEqual([j["jobKey"] for j in b.pending()], ["task_S1_7"])
            b.ack(["task_S1_7"])
            c = JobTracker("p1", "S1", state_path=path)
            self.assertEqual(c.pending(), [])


class TestV054Round2(unittest.TestCase):
    def test_recovered_terminal_without_a_filename_is_not_linked_when_we_know_ours(self):
        t = QueueProgressTracker()
        t.record_dispatch("q1", [], now=0.0, task_name="mine.3mf")
        self.assertEqual(t.collect([{"jobKey": "task_S1_9", "status": "done"}], "FINISH", now=1.0), [])
        self.assertEqual(t.collect([{"jobKey": "task_S1_10", "status": "done", "filename": "mine.3mf"}], "FINISH", now=2.0)[-1]["printerJobKey"], "task_S1_10")

    def test_start_is_refused_when_the_dispatch_cannot_be_persisted(self):
        from makeros_hub.printers import bambu as bambu_module
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d) / "nope.json"):
            (Path(d) / "nope.json").write_text("a file where a directory must be")   # mkdir will fail → save fails
            a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
            with a._lock:
                a._queue_progress.record_dispatch("q1", [])
                self.assertFalse(a._save_queue_progress())

