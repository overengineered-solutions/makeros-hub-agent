"""B3 (v0.56): the adapter translates trays from its LIVE AMS state before anything is uploaded, and refuses
(spool_mismatch) when a required spool is not loaded — nothing reaches the printer."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub.printers import bambu as bambu_module
from makeros_hub.printers import bambu_send


class _Info:
    rc = 0


class FakeClient:
    def __init__(self):
        self.published: list[str] = []

    def is_connected(self):
        return True

    def publish(self, topic, payload):
        self.published.append(payload)
        return _Info()


RAW_STATE = {"print": {"ams": {"ams": [{"id": "0", "tray": [
    {"id": "0", "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG00", "remain": 50},
    {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFL99", "remain": 50},
]}]}}}


class TestDispatchOrder(unittest.TestCase):
    def _adapter(self, d: str):
        a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
        a._client = FakeClient()
        a._connack = "ok"
        a._data = RAW_STATE
        a._last_report_at = time.monotonic()   # a FRESH report: the AMS mirror rides only on connected status
        return a

    def test_refuses_before_upload_when_a_required_spool_is_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf") as up:
            a = self._adapter(d)
            r = a.start_print("/tmp/x.3mf", "x.3mf", plate=1, use_ams=True, ams_mapping=[5], queue_job_id="q1",
                              raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]},
                              required_filaments=[{"slot": 1, "type": "PLA", "color": "FF00FF"}])
            self.assertFalse(r["ok"])
            self.assertTrue(r["reason"].startswith("spool_mismatch: needs PLA #FF00FF"))
            up.assert_not_called()
            self.assertEqual(a._client.published, [])
            self.assertEqual(a._queue_progress.to_state()["dispatches"], [])   # nothing recorded for a print we did not start

    def test_translates_then_uploads_then_publishes(self):
        import json
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf") as up:
            a = self._adapter(d)
            r = a.start_print("/tmp/x.3mf", "x.3mf", plate=1, use_ams=True, ams_mapping=[5, 9], queue_job_id="q1",
                              raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5, 9], "ams_mapping2": [-1, 9]},
                              required_filaments=[{"slot": 1, "type": "PLA", "color": "FFFFFF", "idx": "GFL99"},
                                                  {"slot": 2, "type": "PETG", "color": "000000"}])
            self.assertTrue(r["ok"], r)
            up.assert_called_once()
            sent = json.loads(a._client.published[0])["print"]
            self.assertEqual(sent["ams_mapping"], [1, 0])
            self.assertEqual(sent["ams_mapping2"], [-1, 0])
            self.assertIs(sent["use_ams"], True)

    def test_stale_report_is_a_refusal_not_a_guess(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf") as up:
            a = self._adapter(d)
            a._last_report_at = time.monotonic() - 3600
            r = a.start_print("/tmp/x.3mf", "x.3mf", plate=1, use_ams=True, ams_mapping=[5], queue_job_id="q1",
                              raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]},
                              required_filaments=[{"slot": 1, "type": "PLA", "color": "FFFFFF"}])
            self.assertEqual(r, {"ok": False, "reason": "spool_mismatch: printer has not reported its trays recently"})
            up.assert_not_called()

    def test_without_requirements_the_replay_is_untouched(self):
        import json
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf"):
            a = self._adapter(d)
            r = a.start_print("/tmp/x.3mf", "x.3mf", plate=1, use_ams=True, ams_mapping=[5], queue_job_id="q1",
                              raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]})
            self.assertTrue(r["ok"], r)
            self.assertEqual(json.loads(a._client.published[0])["print"]["ams_mapping"], [5])
