"""v0.58 (audit 2026-08-30): per-pass printer reservation, idempotent duplicate dispatch, the durable report outbox,
assignmentSeq in every assignment-derived report body, and the external-spool identity gate."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub import agent as agent_module, report_outbox
from makeros_hub.printers import bambu as bambu_module, bambu_send, tray_translate as tt
from makeros_hub.printers.manager import PrinterManager


class _Info:
    rc = 0


class FakeClient:
    def __init__(self):
        self.published = []

    def is_connected(self):
        return True

    def publish(self, topic, payload):
        self.published.append(payload)
        return _Info()


RAW_STATE = {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFL99", "remain": 50}]}]},
                       "vt_tray": {"id": "254", "tray_type": "PETG", "tray_color": "000000FF", "remain": 30, "state": 11}}}
REQ_PLA = [{"slot": 1, "type": "PLA", "color": "FFFFFF"}]


def adapter():
    a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
    a._client = FakeClient(); a._connack = "ok"; a._data = RAW_STATE; a._last_report_at = time.monotonic()
    return a


class TestAdapterInFlightGuard(unittest.TestCase):
    def test_same_job_twice_is_idempotent_and_a_second_job_is_refused_while_the_first_is_unresolved(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf") as up:
            a = adapter()
            kw = dict(plate=1, use_ams=True, ams_mapping=[5], raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]}, required_filaments=REQ_PLA)
            self.assertTrue(a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", **kw)["ok"])
            r = a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", **kw)
            self.assertEqual(r, {"ok": True, "already_dispatched": True})
            # the manager still reports 'uploading' for the idempotent re-send (the cloud's sent_at stamp is harmless)
            class Re:
                def start_print(self, *a, **k):
                    return {"ok": True, "already_dispatched": True}
            (Path(d) / "cccccccc").mkdir(); (Path(d) / "cccccccc" / "p.3mf").write_bytes(b"3mf")
            mm = PrinterManager(); mm._adapters["p1"] = Re()
            rep = mm.dispatch_assignments([{"queueJobId": "q1", "printerId": "p1", "submissionUid": "cccccccc", "fileName": "p.3mf", "plate": 1, "useAms": True, "amsMapping": [0], "assignmentSeq": 4}], d)
            self.assertEqual([(r["state"], r.get("assignmentSeq")) for r in rep], [("uploading", 4)])
            self.assertEqual((up.call_count, len(a._client.published)), (1, 1))            # nothing uploaded/published twice
            self.assertEqual(a.start_print("/tmp/y.3mf", "y.3mf", queue_job_id="q2", **kw), {"ok": False, "reason": "printer_busy"})


class TestPerPassReservation(unittest.TestCase):
    def test_two_assignments_for_one_printer_in_one_beat_start_only_the_first(self):
        class Adapter:
            calls = []

            def start_print(self, local_path, file_name, **kwargs):
                Adapter.calls.append(kwargs["queue_job_id"]); return {"ok": True}

        with tempfile.TemporaryDirectory() as d:
            for uid in ("aaaaaaaa", "bbbbbbbb"):
                (Path(d) / uid).mkdir(); (Path(d) / uid / "p.3mf").write_bytes(b"3mf")
            m = PrinterManager(); m._adapters["p1"] = Adapter()
            base = {"printerId": "p1", "fileName": "p.3mf", "plate": 1, "useAms": True, "amsMapping": [0]}
            reports = m.dispatch_assignments([dict(base, queueJobId="q1", submissionUid="aaaaaaaa", assignmentSeq=3),
                                              dict(base, queueJobId="q2", submissionUid="bbbbbbbb", assignmentSeq=1)], d)
            self.assertEqual(Adapter.calls, ["q1"])
            self.assertEqual([r["state"] for r in reports], ["uploading", "held"])
            # the hook saw each report the moment it existed (durable before the pass returned)
            seen = []
            m2 = PrinterManager(); m2._adapters["p1"] = Adapter(); Adapter.calls.clear()
            m2.dispatch_assignments([dict(base, queueJobId="q3", submissionUid="aaaaaaaa", assignmentSeq=9)], d, on_report=seen.append)
            self.assertEqual([(r["state"], r["assignmentSeq"]) for r in seen], [("uploading", 9)])
            self.assertEqual(reports[0]["assignmentSeq"], 3)                                  # uploading names its assignment too
            self.assertEqual((reports[1]["reason"], reports[1]["assignmentSeq"]), ("printer_busy", 1))


class TestReporterBody(unittest.TestCase):
    def test_assignment_seq_reaches_the_post_body(self):
        cfg = mock.Mock(); cfg.queue_status_url = "https://cloud/api/print/hub/queue-status"
        sent = []
        resp = mock.Mock(status=200, body={"ok": True})
        with mock.patch.object(agent_module, "post_json", side_effect=lambda url, body, **kw: (sent.append(body), resp)[1]):
            reporter = agent_module.make_queue_status_reporter(cfg, "cred")
            reporter({"queueJobId": "q1", "state": "held", "reason": "printer_busy", "assignmentSeq": 7})
            reporter({"queueJobId": "q2", "state": "uploading", "assignmentSeq": 2})
            reporter({"queueJobId": "q3", "state": "printing"})
        self.assertEqual([b.get("assignmentSeq") for b in sent], [7, 2, None])


class TestReportOutbox(unittest.TestCase):
    def test_round_trip_is_atomic_bounded_and_tolerant(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "queue-reports.json"
            self.assertEqual(report_outbox.load(p), [])
            reports = [{"queueJobId": "q1", "state": "uploading", "assignmentSeq": 1}, {"queueJobId": "q2", "state": "held", "reason": "x"}]
            self.assertTrue(report_outbox.save(reports, p))
            self.assertEqual(report_outbox.load(p), reports)
            self.assertFalse(list(Path(d).glob("*.tmp")))
            p.write_text("{not json")
            self.assertEqual(report_outbox.load(p), [])
            self.assertTrue(report_outbox.save([{"queueJobId": str(i), "state": "printing"} for i in range(report_outbox.MAX_REPORTS + 5)], p))
            self.assertEqual(len(report_outbox.load(p)), report_outbox.MAX_REPORTS)


class TestExternalSpoolIdentity(unittest.TestCase):
    def test_external_spool_prints_must_match_the_holder(self):
        units = [{"unit": 0, "raw": {"id": "0"}, "trays": [{"slot": 0, "material": "PLA", "colorHex": "FFFFFFFF"}]}]
        vt = {"material": "PETG", "colorHex": "000000FF"}
        out = tt.translate_print_trays({"use_ams": False, "ams_mapping": [254]}, [{"slot": 1, "type": "PETG", "color": "000000"}], units, vt)
        self.assertEqual(out["ams_mapping"], [254])
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_print_trays({"use_ams": False, "ams_mapping": [254]}, REQ_PLA, units, vt)
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_mapping([254], REQ_PLA, tt.live_trays(units, vt))
        self.assertEqual(tt.translate_mapping([254], [{"slot": 1, "type": "PETG", "color": "000000"}], tt.live_trays(units, vt)), [254])
        with self.assertRaises(tt.TrayTranslationError):   # the second external holder (255) cannot be verified → refuse
            tt.translate_mapping([255], [{"slot": 1, "type": "PETG", "color": "000000"}], tt.live_trays(units, vt))
