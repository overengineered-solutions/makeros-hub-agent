"""Tests for queue assignment dispatch in PrinterManager."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.queue_progress import QueueProgressTracker


def _write_3mf_with_objects(path: Path, plate: int, objects: list[tuple[int, str]]) -> None:
    """Stage a 3MF whose slice_info.config carries skippable objects on `plate`."""
    obj_xml = "".join(f'<object identify_id="{i}" name="{n}"/>' for i, n in objects)
    slice_info = (
        '<?xml version="1.0"?><config><plate>'
        f'<metadata key="index" value="{plate}"/>'
        '<metadata key="label_object_enabled" value="true"/>'
        f"{obj_xml}</plate></config>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr(f"Metadata/plate_{plate}.gcode", "; sliced")   # a sliced plate holds its G-code (v0.80.1 checks it)


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

    def test_uploading_report_carries_parsed_objects(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            # default assignment() plate is 2 — stage objects on plate 2.
            _write_3mf_with_objects(spool_file, plate=2, objects=[(286, "Cube"), (287, "Sphere")])
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter({"ok": True})

            reports = manager.dispatch_assignments([assignment()], d)

        uploading = next(r for r in reports if r["state"] == "uploading")
        self.assertEqual(uploading["objects"], [{"id": 286, "name": "Cube"}, {"id": 287, "name": "Sphere"}])

    def test_uploading_report_omits_objects_when_not_skippable(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"not a real 3mf")  # parser returns [] (no raise)
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter({"ok": True})

            reports = manager.dispatch_assignments([assignment()], d)

        uploading = next(r for r in reports if r["state"] == "uploading")
        self.assertNotIn("objects", uploading)

    def test_plate_the_file_does_not_hold_is_refused_before_the_printer_is_told(self):
        # v0.80.1 (2026-10-22): an OrcaSlicer 2.4.2 single-plate export of plate 2 holds only plate_2.gcode; sent plate 1, the
        # printer took the command, stopped on its screen and the hub reported start_not_observed two minutes later, six times
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            _write_3mf_with_objects(spool_file, plate=2, objects=[])
            manager = PrinterManager()
            fake = FakeAdapter({"ok": True})
            manager._adapters["p1"] = fake

            refused = manager.dispatch_assignments([assignment(plate=1, assignmentSeq=4)], d)
            self.assertEqual(refused, [{"queueJobId": "q1", "state": "held", "assignmentSeq": 4,
                                        "reason": "plate_missing: the file has no plate 1; it holds plate 2"}])
            self.assertEqual(fake.calls, [])   # nothing uploaded, no start published

            # the plate it holds starts as before
            self.assertEqual(manager.dispatch_assignments([assignment(plate=2, assignmentSeq=5)], d),
                             [{"queueJobId": "q1", "state": "uploading", "assignmentSeq": 5}])
            self.assertEqual(fake.calls[0][2]["plate"], 2)

            # a zip with no sliced plate at all (an unsliced project) is refused too; a file that is not a zip is not this
            # check's business (a Klipper .gcode): it goes on to the adapter
            with zipfile.ZipFile(spool_file, "w") as zf:
                zf.writestr("3D/3dmodel.model", "<model/>")
            self.assertEqual(manager.dispatch_assignments([assignment(queueJobId="q2", plate=1)], d)[0]["reason"],
                             "plate_missing: the file has no plate 1; it holds no sliced plate")
            spool_file.write_bytes(b"G28\n")
            self.assertEqual(manager.dispatch_assignments([assignment(queueJobId="q3", plate=1)], d)[0]["state"], "uploading")

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
        # v0.58 (codex): a guarded re-send is never silent — it answers 'uploading' again (idempotent for the cloud)
        self.assertEqual(second, [{"queueJobId": "q1", "state": "uploading"}])
        self.assertEqual(len(fake.calls), 1)

    def test_start_failure_reports_held_only(self):
        with tempfile.TemporaryDirectory() as d:
            spool_file = Path(d) / "abcdef12" / "part.3mf"
            spool_file.parent.mkdir()
            spool_file.write_bytes(b"3mf")
            manager = PrinterManager()
            manager._adapters["p1"] = FakeAdapter({"ok": False, "reason": "upload_failed"})

            reports = manager.dispatch_assignments([assignment()], d)

        self.assertEqual(
            reports,
            [{"queueJobId": "q1", "state": "held", "reason": "upload_failed"},
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

        self.assertEqual([r["state"] for r in first], ["held"])
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

    def test_redelivered_requestid_is_not_republished(self):
        # Cloud delivery is one-shot, but defense-in-depth: a redelivered
        # requestId must NOT re-publish (a second ams_dry would restart the
        # dryer). The duplicate still re-reports ok so the cloud can close it.
        manager = PrinterManager()
        fake = FakeCommandAdapter({"ok": True})
        manager._adapters["p1"] = fake
        first = manager.dispatch_commands([command(requestId="r9", command="ams_dry")])
        second = manager.dispatch_commands([command(requestId="r9", command="ams_dry")])
        self.assertEqual(first, [{"requestId": "r9", "command": "ams_dry", "status": "ok"}])
        self.assertEqual(second, [{"requestId": "r9", "command": "ams_dry", "status": "ok"}])
        self.assertEqual(len(fake.calls), 1)  # published exactly once

    def test_failed_command_is_not_deduped_so_a_resend_can_retry(self):
        manager = PrinterManager()
        fake = FakeCommandAdapter({"ok": False, "reason": "not_connected"})
        manager._adapters["p1"] = fake
        manager.dispatch_commands([command(requestId="r10", command="pause")])
        fake.result = {"ok": True}
        second = manager.dispatch_commands([command(requestId="r10", command="pause")])
        self.assertEqual(second, [{"requestId": "r10", "command": "pause", "status": "ok"}])
        self.assertEqual(len(fake.calls), 2)  # retried because the first failed

    def test_caps_per_heartbeat_and_rate_limits_the_excess(self):
        # R6.7: the cloud caps delivery at 5; a flood (here 18) means a buggy/
        # compromised control plane. Execute the first 16, fail the rest as
        # rate_limited (so the cloud still closes them out).
        manager = PrinterManager()
        fake = FakeCommandAdapter({"ok": True})
        manager._adapters["p1"] = fake
        cmds = [command(requestId=f"r{i}") for i in range(18)]
        reports = manager.dispatch_commands(cmds)

        self.assertEqual(len(reports), 18)
        self.assertEqual(len(fake.calls), 16)  # only the first 16 actually published
        ok = [r for r in reports if r["status"] == "ok"]
        rate_limited = [r for r in reports if r.get("detail") == "rate_limited"]
        self.assertEqual(len(ok), 16)
        self.assertEqual(len(rate_limited), 2)
        self.assertTrue(all(r["status"] == "failed" for r in rate_limited))


if __name__ == "__main__":
    unittest.main()
