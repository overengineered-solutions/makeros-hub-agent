"""v0.53: a hub restart (OTA) mid-print must never re-dispatch or interrupt a job. Three guards: the dispatched-job guard
survives a restart; a printer that reports PREPARE/RUNNING/PAUSE is never sent another file; the OTA itself waits."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub.printers import manager as manager_module
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.update import should_defer_update


class FakeAdapter:
    def __init__(self, gcode_state="IDLE", state="idle"):
        self.calls = []
        self.gcode_state, self.state = gcode_state, state

    def status(self):
        return {"printerId": "p1", "connectionState": "connected", "gcodeState": self.gcode_state, "state": self.state}

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def _assignment():
    return {"queueJobId": "q1", "printerId": "p1", "submissionUid": "abcdef12", "fileName": "part.3mf", "plate": 1}


class TestOtaSafety(unittest.TestCase):
    def _spool(self, d):
        f = Path(d) / "abcdef12" / "part.3mf"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"3mf")

    def test_busy_printer_is_never_sent_another_file(self):
        for gs, st in (("PREPARE", "idle"), ("RUNNING", "printing"), ("PAUSE", "paused")):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(manager_module, "DISPATCHED_STATE_PATH", Path(d) / "disp.json"):
                self._spool(d)
                m = PrinterManager()
                fake = FakeAdapter(gs, st)
                m._adapters["p1"] = fake
                reports = m.dispatch_assignments([_assignment()], d)
                self.assertEqual(reports, [{"queueJobId": "q1", "state": "held", "reason": "printer_busy"}], gs)
                self.assertEqual(fake.calls, [])
                self.assertEqual(m.busy_printers(), ["p1"])

    def test_dispatched_guard_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(manager_module, "DISPATCHED_STATE_PATH", Path(d) / "disp.json"):
            self._spool(d)
            first = PrinterManager()
            a = FakeAdapter()
            first._adapters["p1"] = a
            first.dispatch_assignments([_assignment()], d)
            self.assertEqual(len(a.calls), 1)
            # "restart": a brand-new manager (fresh memory) with the same state file — the cloud re-sends the same
            # 'assigned' job because the printer has not reported RUNNING yet (it is heating: PREPARE ≈ idle)
            second = PrinterManager()
            b = FakeAdapter()
            second._adapters["p1"] = b
            second.dispatch_assignments([_assignment()], d)
            self.assertEqual(b.calls, [], "the same queueJobId must not be re-uploaded/re-started after a restart")
            # a terminal progress report forgets the guard → a genuinely NEW assignment with that id could dispatch again
            second._forget_dispatched_queue_job("q1")
            third = PrinterManager()
            c = FakeAdapter()
            third._adapters["p1"] = c
            third.dispatch_assignments([_assignment()], d)
            self.assertEqual(len(c.calls), 1)

    def test_guard_survives_a_backwards_clock(self):
        import json, time
        with tempfile.TemporaryDirectory() as d, mock.patch.object(manager_module, "DISPATCHED_STATE_PATH", Path(d) / "disp.json"):
            self._spool(d)
            (Path(d) / "disp.json").write_text(json.dumps({"q1": time.time() + 3600}), encoding="utf-8")   # stamped an hour "ahead"
            m = PrinterManager()
            a = FakeAdapter()
            m._adapters["p1"] = a
            m.dispatch_assignments([_assignment()], d)
            self.assertEqual(a.calls, [], "a future-stamped entry must still block the re-send")

    def test_ota_defers_while_any_printer_is_busy(self):
        self.assertTrue(should_defer_update([{"gcodeState": "IDLE"}, {"gcodeState": "PREPARE"}]))
        self.assertTrue(should_defer_update([{"state": "paused"}]))
        self.assertFalse(should_defer_update([{"gcodeState": "FINISH", "state": "idle"}, {"connectionState": "offline"}]))
        self.assertFalse(should_defer_update(None))


if __name__ == "__main__":
    unittest.main()
