"""Tests for queue assignment dispatch in PrinterManager."""

import tempfile
import unittest
from pathlib import Path

from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.queue_progress import QueueProgressTracker


class FakeAdapter:
    def __init__(self, result=None):
        self.result = result or {"ok": True}
        self.calls = []

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append((Path(local_path), file_name, kwargs))
        return dict(self.result)


class FakeProgressAdapter(FakeAdapter):
    def __init__(self):
        super().__init__({"ok": True})
        self.progress = QueueProgressTracker()
        self.gcode_state = "IDLE"
        self.pending = []
        self.now = 0.0

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append((Path(local_path), file_name, kwargs))
        self.progress.record_dispatch(kwargs["queue_job_id"], self.pending, now=self.now)
        return {"ok": True}

    def collect_queue_progress(self):
        return self.progress.collect(self.pending, self.gcode_state, now=self.now)


def assignment(**overrides):
    data = {
        "queueJobId": "q1",
        "printerId": "p1",
        "submissionUid": "abcdef12",
        "fileName": "part.3mf",
        "plate": 2,
        "useAms": True,
        "amsMapping": [0, 1],
    }
    data.update(overrides)
    return data


class TestDispatchAssignments(unittest.TestCase):
    def test_file_present_starts_print_and_reports_uploading_only(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter({"ok": True})
            manager._adapters["p1"] = fake

            reports = manager.dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "uploading"}],
        )
        self.assertEqual(len(fake.calls), 1)
        local_path, file_name, kwargs = fake.calls[0]
        self.assertEqual(local_path, spool_file)
        self.assertEqual(file_name, "part.3mf")
        self.assertEqual(kwargs["plate"], 2)
        self.assertIs(kwargs["use_ams"], True)
        self.assertEqual(kwargs["ams_mapping"], [0, 1])
        self.assertEqual(kwargs["queue_job_id"], "q1")

    def test_file_missing_reports_held(self):
        with tempfile.TemporaryDirectory() as d:
            manager = PrinterManager()
            fake = FakeAdapter()
            manager._adapters["p1"] = fake

            reports = manager.dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "file_not_found"}],
        )
        self.assertEqual(fake.calls, [])

    def test_unknown_printer_reports_held(self):
        with tempfile.TemporaryDirectory() as d:
            reports = PrinterManager().dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "printer_unavailable"}],
        )

    def test_idempotent_second_assignment_does_not_restart(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter()
            manager._adapters["p1"] = fake

            first = manager.dispatch_assignments([assignment()], d)
            second = manager.dispatch_assignments([assignment()], d)

        self.assertEqual([r["state"] for r in first], ["uploading"])
        self.assertEqual(second, [])
        self.assertEqual(len(fake.calls), 1)

    def test_start_failure_reports_uploading_then_held(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter({"ok": False, "reason": "upload_failed"})

            reports = manager.dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [
                {"queueJobId": "q1", "state": "uploading"},
                {"queueJobId": "q1", "state": "held", "reason": "upload_failed"},
            ],
        )

    def test_start_failure_does_not_suppress_retry(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter({"ok": False, "reason": "start_command_failed"})
            manager._adapters["p1"] = fake

            first = manager.dispatch_assignments([assignment()], d)
            fake.result = {"ok": True}
            second = manager.dispatch_assignments([assignment()], d)

        self.assertEqual([r["state"] for r in first], ["uploading", "held"])
        self.assertEqual(second, [{"queueJobId": "q1", "state": "uploading"}])
        self.assertEqual(len(fake.calls), 2)

    def test_bad_submission_uid_reports_bad_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter()

            reports = manager.dispatch_assignments([assignment(submissionUid="../bad")], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "bad_assignment"}],
        )

    def test_bad_file_name_reports_bad_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter()

            reports = manager.dispatch_assignments([assignment(fileName="nested/part.3mf")], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "bad_assignment"}],
        )

    def test_collect_queue_progress_printing_once_then_completed(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeProgressAdapter()
            manager._adapters["p1"] = fake

            self.assertEqual(
                manager.dispatch_assignments([assignment()], d),
                [{"queueJobId": "q1", "state": "uploading"}],
            )
            fake.gcode_state = "RUNNING"
            self.assertEqual(
                manager.collect_queue_progress(),
                [{"queueJobId": "q1", "state": "printing"}],
            )
            self.assertEqual(manager.collect_queue_progress(), [])
            fake.gcode_state = "FINISH"
            fake.pending = [{"jobKey": "task_SER_real", "status": "done"}]

            reports = manager.collect_queue_progress()

        self.assertEqual(
            reports,
            [
                {
                    "queueJobId": "q1",
                    "state": "completed",
                    "printerJobKey": "task_SER_real",
                }
            ],
        )

    def test_collect_queue_progress_failed_terminal_holds_queue_job(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeProgressAdapter()
            manager._adapters["p1"] = fake

            manager.dispatch_assignments([assignment()], d)
            fake.pending = [{"jobKey": "task_SER_failed", "status": "failed"}]

            reports = manager.collect_queue_progress()

        self.assertEqual(
            reports,
            [
                {
                    "queueJobId": "q1",
                    "state": "held",
                    "printerJobKey": "task_SER_failed",
                    "reason": "print_failed",
                }
            ],
        )

    def test_collect_queue_progress_completed_without_observed_printing_synthesizes_printing(self):
        # A print that ran AND finished entirely between heartbeats (we never
        # saw RUNNING) must still emit printing→completed, since the cloud
        # rejects uploading→completed. A 'held' terminal does NOT get this.
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeProgressAdapter()
            manager._adapters["p1"] = fake

            manager.dispatch_assignments([assignment()], d)  # uploading only
            # No RUNNING ever observed; jump straight to a done terminal job.
            fake.gcode_state = "FINISH"
            fake.pending = [{"jobKey": "task_SER_fast", "status": "done"}]

            reports = manager.collect_queue_progress()

        self.assertEqual(
            reports,
            [
                {"queueJobId": "q1", "state": "printing"},
                {"queueJobId": "q1", "state": "completed", "printerJobKey": "task_SER_fast"},
            ],
        )

    def test_collect_queue_progress_start_timeout_idle_holds_queue_job(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeProgressAdapter()
            manager._adapters["p1"] = fake

            manager.dispatch_assignments([assignment()], d)
            fake.gcode_state = "IDLE"
            fake.now = 121.0

            reports = manager.collect_queue_progress()

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "start_not_observed"}],
        )


class FakeCommandAdapter:
    def __init__(self, result=None, raises=False):
        self.result = result or {"ok": True}
        self.raises = raises
        self.calls = []

    def send_command(self, command, params=None):
        self.calls.append((command, params))
        if self.raises:
            raise RuntimeError("boom")
        return dict(self.result)


def command(**overrides):
    data = {"requestId": "r1", "printerId": "p1", "command": "pause"}
    data.update(overrides)
    return data


class TestDispatchCommands(unittest.TestCase):
    def test_ok_command_reports_ok(self):
        manager = PrinterManager()
        fake = FakeCommandAdapter({"ok": True})
        manager._adapters["p1"] = fake
        reports = manager.dispatch_commands([command()])
        self.assertEqual(reports, [{"requestId": "r1", "command": "pause", "status": "ok"}])
        self.assertEqual(fake.calls, [("pause", None)])

    def test_failure_reports_failed_with_detail(self):
        manager = PrinterManager()
        manager._adapters["p1"] = FakeCommandAdapter({"ok": False, "reason": "not_connected"})
        reports = manager.dispatch_commands([command(command="stop")])
        self.assertEqual(
            reports,
            [{"requestId": "r1", "command": "stop", "status": "failed", "detail": "not_connected"}],
        )

    def test_unknown_printer_reports_failed_unavailable(self):
        reports = PrinterManager().dispatch_commands([command()])
        self.assertEqual(
            reports,
            [
                {
                    "requestId": "r1",
                    "command": "pause",
                    "status": "failed",
                    "detail": "printer_unavailable",
                }
            ],
        )

    def test_adapter_exception_reports_failed(self):
        manager = PrinterManager()
        manager._adapters["p1"] = FakeCommandAdapter(raises=True)
        reports = manager.dispatch_commands([command()])
        self.assertEqual(
            reports,
            [{"requestId": "r1", "command": "pause", "status": "failed", "detail": "exception"}],
        )

    def test_malformed_or_non_list_inputs_are_skipped(self):
        manager = PrinterManager()
        fake = FakeCommandAdapter()
        manager._adapters["p1"] = fake
        # a non-dict, a dict missing requestId/command — both dropped; the valid one runs
        reports = manager.dispatch_commands(["notadict", {"printerId": "p1"}, command()])
        self.assertEqual(reports, [{"requestId": "r1", "command": "pause", "status": "ok"}])
        self.assertEqual(manager.dispatch_commands(None), [])
        self.assertEqual(fake.calls, [("pause", None)])


if __name__ == "__main__":
    unittest.main()
