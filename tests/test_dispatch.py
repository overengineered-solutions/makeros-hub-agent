"""Tests for queue assignment dispatch in PrinterManager."""

import tempfile
import unittest
from pathlib import Path

from makeros_hub.printers.manager import PrinterManager


class FakeAdapter:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "jobKey": "task_SER_seq"}
        self.calls = []

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append((Path(local_path), file_name, kwargs))
        return dict(self.result)


def assignment(**overrides):
    data = {
        "queueJobId": "q1",
        "printerId": "p1",
        "submissionUid": "sub1",
        "fileName": "part.3mf",
        "plate": 2,
        "useAms": True,
        "amsMapping": [0, 1],
    }
    data.update(overrides)
    return data


class TestDispatchAssignments(unittest.TestCase):
    def test_file_present_starts_print_and_reports_uploading_then_printing(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "sub1" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter({"ok": True, "jobKey": "job123"})
            manager._adapters["p1"] = fake

            reports = manager.dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [
                {"queueJobId": "q1", "state": "uploading"},
                {"queueJobId": "q1", "state": "printing", "printerJobKey": "job123"},
            ],
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
            spool_file = Path(d) / "sub1" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            fake = FakeAdapter()
            manager._adapters["p1"] = fake

            first = manager.dispatch_assignments([assignment()], d)
            second = manager.dispatch_assignments([assignment()], d)

        self.assertEqual([r["state"] for r in first], ["uploading", "printing"])
        self.assertEqual(second, [])
        self.assertEqual(len(fake.calls), 1)

    def test_start_failure_reports_uploading_then_held(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "sub1" / "part.3mf"
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


if __name__ == "__main__":
    unittest.main()
