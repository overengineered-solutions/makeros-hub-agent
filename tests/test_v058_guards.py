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
            # over the bound, each job keeps its LAST report (its current state), never just "the newest entries"
            many = []
            for i in range(report_outbox.MAX_REPORTS + 10):
                many += [{"queueJobId": "old", "state": "uploading"}, {"queueJobId": f"j{i}", "state": "printing"}]
            many.append({"queueJobId": "old", "state": "completed"})
            self.assertTrue(report_outbox.save(many, p))
            loaded = report_outbox.load(p)
            self.assertLessEqual(len(loaded), report_outbox.MAX_REPORTS)
            self.assertEqual([r for r in loaded if r["queueJobId"] == "old"], [{"queueJobId": "old", "state": "completed"}])


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


class TestRecoveryAndSeqOnProgress(unittest.TestCase):
    def test_guarded_resend_and_adapter_pending_resend_report_uploading_not_silence_or_busy(self):
        class Started:
            def __init__(self):
                self.calls = 0

            def pending_queue_job_ids(self):
                return ["q1"]

            def start_print(self, *a, **k):
                self.calls += 1; return {"ok": True}

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "aaaaaaaa").mkdir(); (Path(d) / "aaaaaaaa" / "p.3mf").write_bytes(b"3mf")
            m = PrinterManager(); ad = Started(); m._adapters["p1"] = ad
            asg = {"queueJobId": "q1", "printerId": "p1", "submissionUid": "aaaaaaaa", "fileName": "p.3mf", "plate": 1, "useAms": True, "amsMapping": [0], "assignmentSeq": 5}
            rep = m.dispatch_assignments([asg], d)
            self.assertEqual((ad.calls, [(r["state"], r["assignmentSeq"]) for r in rep]), (0, [("uploading", 5)]))
            rep = m.dispatch_assignments([asg], d)
            self.assertEqual([(r["state"], r["assignmentSeq"]) for r in rep], [("uploading", 5)])

    def test_progress_reports_carry_the_assignment_seq_and_ambiguity_or_idle_never_latch_the_printer(self):
        from makeros_hub.printers.queue_progress import QueueProgressTracker
        t = QueueProgressTracker()
        t.record_dispatch("q1", [], now=0.0, task_name="part", assignment_seq=4)
        self.assertEqual(t.pending_queue_job_ids(), ["q1"])
        self.assertEqual(t.collect([], "RUNNING", now=1.0), [{"queueJobId": "q1", "state": "printing", "assignmentSeq": 4}])
        two = [{"jobKey": "a", "status": "done", "filename": "part"}, {"jobKey": "b", "status": "done", "filename": "part"}]
        self.assertEqual(t.collect(two, "IDLE", now=2.0), [{"queueJobId": "q1", "state": "held", "reason": "ambiguous_queue_correlation", "assignmentSeq": 4}])
        self.assertEqual(t.pending_queue_job_ids(), [])
        t.record_dispatch("q2", [], now=10.0, assignment_seq=6)
        t.collect([], "RUNNING", now=11.0)
        self.assertEqual(t.collect([], "IDLE", now=12.0), [])
        late = t.collect([], "IDLE", now=12.0 + t._start_timeout_sec + 1)
        self.assertEqual(late, [{"queueJobId": "q2", "state": "held", "reason": "outcome_unknown", "assignmentSeq": 6}])
        self.assertEqual(t.pending_queue_job_ids(), [])
        t.record_dispatch("q3", [], now=20.0, assignment_seq=9)
        u = QueueProgressTracker(); u.load_state(t.to_state(), now=21.0, now_wall=t.to_state()["dispatches"][0]["dispatched_wall"] + 1)
        self.assertEqual(u.collect([], "RUNNING", now=22.0), [{"queueJobId": "q3", "state": "printing", "assignmentSeq": 9}])

    def test_direct_spool_print_never_routes_a_required_filament_to_255(self):
        units = [{"unit": 0, "raw": {"id": "0"}, "trays": []}]
        vt = {"material": "PETG", "colorHex": "000000FF"}
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_print_trays({"use_ams": False, "ams_mapping": [255]}, [{"slot": 1, "type": "PETG", "color": "000000"}], units, vt)
        self.assertEqual(tt.translate_print_trays({"use_ams": False, "ams_mapping": [254]}, [{"slot": 1, "type": "PETG", "color": "000000"}], units, vt)["ams_mapping"], [254])


class TestProgressReportsDurableBeforeStatePop(unittest.TestCase):
    def test_the_hook_runs_while_the_dispatch_is_still_on_disk(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf"):
            a = adapter()
            kw = dict(plate=1, use_ams=True, ams_mapping=[5], raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]}, required_filaments=REQ_PLA)
            self.assertTrue(a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", assignment_seq=3, **kw)["ok"])
            a._data = {"print": {**RAW_STATE["print"], "gcode_state": "RUNNING"}}
            seen = []

            def hook(report):
                on_disk = _json.loads((Path(d) / "p1.json").read_text())
                seen.append((report, [x["queueJobId"] for x in on_disk["dispatches"]]))

            reports = a.collect_queue_progress(on_report=hook)
            self.assertEqual(reports, [{"queueJobId": "q1", "state": "printing", "assignmentSeq": 3}])
            self.assertEqual(seen, [({"queueJobId": "q1", "state": "printing", "assignmentSeq": 3}, ["q1"])])   # still on disk at hook time
            # the manager passes the hook down and still forgets a completed/held job
            m = PrinterManager(); m._adapters["p1"] = a; m._remember_dispatched_queue_job("q1")
            a._data = {"print": {**RAW_STATE["print"], "gcode_state": "IDLE"}}
            got = []
            with mock.patch.object(a._queue_progress, "collect", return_value=[{"queueJobId": "q1", "state": "completed", "assignmentSeq": 3}]):
                out = m.collect_queue_progress(on_report=got.append)
            self.assertEqual((out, got), ([{"queueJobId": "q1", "state": "completed", "assignmentSeq": 3}], [{"queueJobId": "q1", "state": "completed", "assignmentSeq": 3}]))
            self.assertNotIn("q1", m._dispatched_queue_jobs)


class TestDurabilityFailures(unittest.TestCase):
    def test_a_failed_outbox_write_keeps_the_dispatch_and_regenerates_the_report_next_beat(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf"):
            a = adapter()
            kw = dict(plate=1, use_ams=True, ams_mapping=[5], raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]}, required_filaments=REQ_PLA)
            self.assertTrue(a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", assignment_seq=2, **kw)["ok"])
            a._data = {"print": {**RAW_STATE["print"], "gcode_state": "RUNNING"}}

            def failing(report):
                raise OSError("disk full")

            self.assertEqual(a.collect_queue_progress(on_report=failing), [])
            self.assertEqual(a.pending_queue_job_ids(), ["q1"])                                  # still tracked in memory…
            self.assertEqual([x["queueJobId"] for x in _json.loads((Path(d) / "p1.json").read_text())["dispatches"]], ["q1"])   # …and on disk
            got = []
            self.assertEqual(a.collect_queue_progress(on_report=got.append), [{"queueJobId": "q1", "state": "printing", "assignmentSeq": 2}])   # regenerated

    def test_uploading_not_durable_leaves_the_manager_guard_unset(self):
        class Adapter:
            def start_print(self, *a, **k):
                return {"ok": True}

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "aaaaaaaa").mkdir(); (Path(d) / "aaaaaaaa" / "p.3mf").write_bytes(b"3mf")
            m = PrinterManager(); m._adapters["p1"] = Adapter()
            asg = {"queueJobId": "q1", "printerId": "p1", "submissionUid": "aaaaaaaa", "fileName": "p.3mf", "plate": 1, "useAms": True, "amsMapping": [0], "assignmentSeq": 1}

            def failing(report):
                raise OSError("disk full")

            m.dispatch_assignments([asg], d, on_report=failing)
            self.assertNotIn("q1", m._dispatched_queue_jobs)
            ok = []
            m.dispatch_assignments([asg], d, on_report=ok.append)
            self.assertIn("q1", m._dispatched_queue_jobs)
            self.assertEqual([r["state"] for r in ok], ["uploading"])

    def test_save_bounds_the_live_list_and_fails_on_an_unwritable_dir(self):
        with tempfile.TemporaryDirectory() as d:
            live = [{"queueJobId": str(i), "state": "printing"} for i in range(report_outbox.MAX_REPORTS + 50)]
            self.assertTrue(report_outbox.save(live, Path(d) / "q.json"))
            self.assertEqual(len(live), report_outbox.MAX_REPORTS)
            self.assertFalse(report_outbox.save(live, Path(d) / "nope" / "deeper" / "q.json") if False else report_outbox.save(live, Path("/proc/q.json")))


class TestSeqKeyedIdempotency(unittest.TestCase):
    def test_a_new_assignment_of_the_same_job_supersedes_the_stale_guard_and_dispatch(self):
        class Adapter:
            def __init__(self):
                self.calls = []; self.pending = []

            def pending_queue_jobs(self):
                return list(self.pending)

            def start_print(self, *a, **k):
                self.calls.append(k.get("assignment_seq")); return {"ok": True}

        with tempfile.TemporaryDirectory() as d, mock.patch.object(__import__("makeros_hub.printers.manager", fromlist=["x"]), "DISPATCHED_STATE_PATH", Path(d) / "dispatched.json"):
            (Path(d) / "aaaaaaaa").mkdir(); (Path(d) / "aaaaaaaa" / "p.3mf").write_bytes(b"3mf")
            m = PrinterManager(); ad = Adapter(); m._adapters["p1"] = ad
            base = {"queueJobId": "q1", "printerId": "p1", "submissionUid": "aaaaaaaa", "fileName": "p.3mf", "plate": 1, "useAms": True, "amsMapping": [0]}
            self.assertEqual([r["state"] for r in m.dispatch_assignments([dict(base, assignmentSeq=1)], d)], ["uploading"])
            self.assertEqual([r["state"] for r in m.dispatch_assignments([dict(base, assignmentSeq=1)], d)], ["uploading"])   # same assignment: recovery, no start
            self.assertEqual(ad.calls, [1])
            self.assertEqual([r["state"] for r in m.dispatch_assignments([dict(base, assignmentSeq=2)], d)], ["uploading"])   # NEW assignment: starts again
            self.assertEqual(ad.calls, [1, 2])
            # the persisted guard carries the seq (v2 format) and survives a reload
            m2 = PrinterManager()
            self.assertTrue(m2._guard_matches("q1", 2)); self.assertFalse(m2._guard_matches("q1", 3)); self.assertTrue(m2._guard_matches("q1", None))
            # the adapter-pending recovery path is seq-aware too: a pending dispatch for seq 2 answers seq 2, not seq 3
            m3 = PrinterManager(); ad3 = Adapter(); ad3.pending = [{"queueJobId": "q1", "assignmentSeq": 2}]; m3._adapters["p1"] = ad3
            m3._forget_dispatched_queue_job("q1")
            self.assertEqual([r["state"] for r in m3.dispatch_assignments([dict(base, assignmentSeq=2)], d)], ["uploading"]); self.assertEqual(ad3.calls, [])
            self.assertEqual([r["state"] for r in m3.dispatch_assignments([dict(base, assignmentSeq=3)], d)], ["uploading"]); self.assertEqual(ad3.calls, [3])

    def test_adapter_supersedes_a_stale_dispatch_on_a_new_seq_but_is_idempotent_on_the_same(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bambu_module, "QUEUE_PROGRESS_DIR", Path(d)), \
                mock.patch.object(bambu_module, "TERMINAL_JOBS_DIR", Path(d)), mock.patch.object(bambu_send, "upload_3mf") as up:
            a = adapter()
            kw = dict(plate=1, use_ams=True, ams_mapping=[5], raw_print={"command": "project_file", "use_ams": True, "ams_mapping": [5]}, required_filaments=REQ_PLA)
            self.assertTrue(a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", assignment_seq=1, **kw)["ok"])
            self.assertEqual(a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", assignment_seq=1, **kw), {"ok": True, "already_dispatched": True})
            r = a.start_print("/tmp/x.3mf", "x.3mf", queue_job_id="q1", assignment_seq=2, **kw)
            self.assertEqual(r, {"ok": True})                                                    # a new assignment: started again
            self.assertEqual((up.call_count, len(a._client.published)), (2, 2))
            self.assertEqual(a.pending_queue_jobs(), [{"queueJobId": "q1", "assignmentSeq": 2}])
