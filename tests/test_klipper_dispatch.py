"""Klipper queue dispatch end-to-end against a fake Moonraker on loopback: start_print (model gate, live idle check,
durable dispatch record, streamed upload, explicit start), collect_queue_progress, persistence across a restart, and the
manager path. Response shapes follow Moonraker's source: objects/query and print/start are wrapped in "result"; the
upload answers HTTP 201 with an UNWRAPPED {item, print_started, print_queued, action}."""

import hashlib
import http.server
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from makeros_hub.multipart import boundary_from_content_type, parse_multipart
from makeros_hub.printers import klipper
from makeros_hub.printers.klipper import KlipperAdapter
from makeros_hub.printers.manager import PrinterManager
from test_gcode_meta import sv08_gcode

MODEL = "Sovol SV08 MAX"
JOB = "0123abcd-0000-4000-8000-000000000001"
OTHER_JOB = "fedcba98-0000-4000-8000-000000000002"
UID = "a" * 32


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        return   # the start-timeout case writes to a client that already gave up


class FakeMoonraker:
    def __init__(self):
        self.klippy = "ready"
        self.idle = "Ready"          # idle_timeout.state: "Printing" while klippy runs commands (a macro, or a print)
        self.state = "standby"
        self.filename = ""
        self.duration = 0.0
        self.print_started_at = None  # wall clock; total_duration = now - this, as klippy computes it while printing
        self.total = None             # a frozen total_duration (klippy freezes it when a print ends)
        self.start_raw = None         # bytes written verbatim as the start POST's reply (a broken answer)
        self.upload_status, self.upload_body = 201, None
        self.start_status, self.start_body, self.start_delay = 200, {"result": "ok"}, 0.0
        self.start_begins_printing = True
        self.uploads, self.starts, self.queries = [], [], 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if not self.path.startswith("/printer/objects/query"):
                    return self._send(404, {"error": {"message": "Not Found"}})
                outer.queries += 1
                self._send(200, {"result": {"status": outer.status()}})

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                if url.path == "/server/files/upload":
                    form = parse_multipart(body, boundary_from_content_type(self.headers.get("Content-Type")))
                    outer.uploads.append(form)
                    stored = f"{form.fields.get('path')}/{form.file.filename}"
                    return self._send(outer.upload_status, outer.upload_body or {
                        "item": {"path": stored, "root": "gcodes", "size": len(form.file.data), "permissions": "rw"},
                        "print_started": False, "print_queued": False, "action": "create_file"})
                if url.path == "/printer/print/start":
                    name = urllib.parse.parse_qs(url.query)["filename"][0]
                    outer.starts.append(name)
                    time.sleep(outer.start_delay)
                    if outer.start_status == 200 and outer.start_begins_printing:
                        outer.state, outer.filename, outer.duration = "printing", name, 0.0
                        outer.print_started_at = time.time()
                    if outer.start_raw is not None:
                        self.wfile.write(outer.start_raw)
                        self.close_connection = True
                        return None
                    return self._send(outer.start_status, outer.start_body)
                self._send(404, {"error": {"message": "Not Found"}})

            def log_message(self, *_args, **_kw):
                return

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def status(self):
        total = self.total if self.total is not None else (
            time.time() - self.print_started_at if self.print_started_at is not None else 0.0)
        out = {"webhooks": {"state": self.klippy},
               "print_stats": {"state": self.state, "filename": self.filename, "print_duration": self.duration,
                               "total_duration": total}}
        if self.idle is not None:
            out["idle_timeout"] = {"state": self.idle}
        return out

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class KlipperCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMoonraker()
        self.addCleanup(self.fake.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spool = Path(tmp.name)
        self.gcode = sv08_gcode().encode()
        (self.spool / UID).mkdir()
        self.file = self.spool / UID / "cube.gcode"
        self.file.write_bytes(self.gcode)
        self.diag = mock.Mock()
        self.adapter = self.make()

    def make(self, model=MODEL, url=None, pid="k1"):
        return KlipperAdapter(pid, url or self.fake.url, diagnostics=self.diag, model=model)

    def start(self, job=JOB, seq=3, adapter=None):
        return (adapter or self.adapter).start_print(self.file, "cube.gcode", queue_job_id=job, assignment_seq=seq,
                                                     plate=1, use_ams=True, ams_mapping=[0], required_filaments=[{"type": "PLA"}])

    def poll(self, adapter=None):
        (adapter or self.adapter)._record_poll(self.fake.status())


class TestStartPrint(KlipperCase):
    def test_a_file_sliced_for_another_model_never_leaves_the_hub(self):
        for model in ("Sovol SV08", None):
            with self.subTest(model=model):
                self.assertEqual(self.start(adapter=self.make(model=model)), {"ok": False, "reason": "model_mismatch"})
        self.assertEqual((self.fake.uploads, self.fake.queries), ([], 0))

    def test_a_printer_that_is_not_between_prints_is_refused(self):
        cases = [("ready", "printing", "Ready", "printer_busy: printing"), ("ready", "paused", "Ready", "printer_busy: printing"),
                 ("ready", "error", "Ready", "printer_busy: klipper_error"),
                 ("shutdown", "standby", "Ready", "printer_busy: klippy_shutdown"),
                 # a QGL / bed-mesh macro from the screen: print_stats says standby, klippy is busy — a start would queue
                 # behind it, time out, and run later untracked
                 ("ready", "standby", "Printing", "printer_busy: klippy_busy"),
                 ("ready", "complete", "Printing", "printer_busy: klippy_busy"),
                 ("ready", "standby", None, "printer_stale: shape_mismatch")]   # cannot rule a macro out
        for klippy, state, idle, reason in cases:
            with self.subTest(klippy=klippy, state=state, idle=idle):
                self.fake.klippy, self.fake.state, self.fake.idle = klippy, state, idle
                self.assertEqual(self.start(), {"ok": False, "reason": reason})
        self.assertEqual(self.fake.uploads, [])
        self.assertEqual(self.adapter.pending_queue_jobs(), [])

    def test_an_unreachable_printer_is_stale(self):
        out = self.start(adapter=self.make(url="http://127.0.0.1:1"))
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"].startswith("printer_stale: "), out)

    def test_happy_path_uploads_then_starts_explicitly(self):
        self.assertEqual(self.start(), {"ok": True})
        (form,) = self.fake.uploads
        self.assertEqual(form.fields["root"], "gcodes")
        self.assertEqual(form.fields["path"], "makeros")
        self.assertEqual(form.fields["checksum"], hashlib.sha256(self.gcode).hexdigest())
        self.assertNotIn("print", form.fields)   # with print=true Moonraker may job-queue it or swallow a failed start
        self.assertEqual(form.file.filename, "0123abcd_cube.gcode")
        self.assertEqual(form.file.data, self.gcode)
        self.assertEqual(self.fake.starts, ["makeros/0123abcd_cube.gcode"])
        self.assertEqual(self.adapter.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 3}])

    def test_the_start_names_the_path_moonraker_stored(self):
        self.fake.upload_body = {"item": {"path": "makeros/stored.gcode", "root": "gcodes"}, "action": "create_file"}
        self.assertEqual(self.start(), {"ok": True})
        self.assertEqual(self.fake.starts, ["makeros/stored.gcode"])
        self.assertEqual(self.adapter._queue_progress.to_state()["dispatches"][0]["taskName"], "makeros/stored.gcode")

    def test_nothing_is_uploaded_when_the_dispatch_cannot_be_persisted(self):
        with mock.patch.object(klipper, "write_json_durably", return_value=False):
            self.assertEqual(self.start(), {"ok": False, "reason": "state_persist_failed"})
        self.assertEqual(self.fake.uploads, [])
        self.assertEqual(self.adapter.pending_queue_jobs(), [])

    def test_a_refused_upload_forgets_the_dispatch(self):
        self.fake.upload_body = {"error": {"message": "File is loaded, upload not permitted"}}
        for status in (403, 422, 500):
            with self.subTest(status=status):
                self.fake.upload_status = status
                self.assertEqual(self.start(), {"ok": False, "reason": "upload_failed"})
                self.assertEqual(self.adapter.pending_queue_jobs(), [])
        self.assertEqual(self.fake.starts, [])
        self.assertIn("HTTP 403 File is loaded", self.diag.record.call_args_list[0].args[1])

    def test_a_refused_start_forgets_the_dispatch(self):
        self.fake.start_status, self.fake.start_body = 400, {"error": {"message": "Printer is busy"}}
        result = self.start()
        self.assertEqual((result["ok"], result["reason"]), (False, "start_refused"))
        self.assertEqual(result["startEvidence"]["commandReply"], "refused")
        self.assertEqual(self.adapter.pending_queue_jobs(), [])

    def test_an_unanswered_start_keeps_the_dispatch_for_telemetry(self):
        self.fake.start_delay = 1.0
        with mock.patch.object(klipper, "HTTP_CONTROL_TIMEOUT_SEC", 0.2):
            self.assertEqual(self.start(), {"ok": True})
        self.assertEqual(self.adapter.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 3}])

    def test_a_broken_start_reply_keeps_the_dispatch_for_telemetry(self):
        # BadStatusLine (an http.client.HTTPException, not an OSError) and a truncated 200 body: the start may have reached
        # klippy, so the dispatch must outlive it exactly like an unanswered start
        for raw in (b"garbage\r\n\r\n", b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"res'):
            with self.subTest(raw=raw):
                adapter = self.make(pid=f"k-{len(raw)}")
                self.fake.state, self.fake.start_raw = "standby", raw
                self.assertEqual(self.start(adapter=adapter), {"ok": True})
                self.assertEqual(adapter.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 3}])

    def test_a_resent_assignment_is_not_uploaded_twice(self):
        self.assertEqual(self.start(), {"ok": True})
        self.assertEqual(self.start(), {"ok": True, "already_dispatched": True})
        self.assertEqual(self.start(seq=None), {"ok": True, "already_dispatched": True})
        self.assertEqual(len(self.fake.uploads), 1)

    def test_another_pending_job_blocks_a_second_start(self):
        self.assertEqual(self.start(), {"ok": True})
        self.assertEqual(self.start(job=OTHER_JOB), {"ok": False, "reason": "printer_busy: dispatch_pending"})
        self.assertEqual(len(self.fake.uploads), 1)

    def test_a_new_assignment_of_the_same_job_supersedes_the_old_record(self):
        self.assertEqual(self.start(seq=1), {"ok": True})
        self.fake.state = "standby"   # the first start never took; the cloud re-queued and assigned again
        self.assertEqual(self.start(seq=2), {"ok": True})
        self.assertEqual(len(self.fake.uploads), 2)
        self.assertEqual(self.adapter.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 2}])


class TestCollectQueueProgress(KlipperCase):
    def test_printing_then_complete_carry_the_printer_job_key(self):
        self.start()
        self.poll()
        key = self.adapter._job_tracker.active_key()
        self.assertTrue(key.startswith("fp_"))
        self.assertEqual(self.adapter.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "printing", "assignmentSeq": 3, "printerJobKey": key}])
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll()
        self.assertEqual(self.adapter.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "completed", "printerJobKey": key, "assignmentSeq": 3}])
        (job,) = self.adapter.pending_jobs()
        self.assertEqual((job["jobKey"], job["status"], job["filename"]), (key, "done", "makeros/0123abcd_cube.gcode"))
        self.assertEqual(self.adapter.pending_queue_jobs(), [])

    def test_a_cancel_or_an_error_is_held(self):
        for end, reason in (("cancelled", "print_cancelled"), ("error", "print_failed")):
            with self.subTest(end=end):
                adapter = self.make(pid=f"k-{end}")
                self.fake.state = "standby"
                self.start(adapter=adapter)
                self.poll(adapter)
                self.assertEqual(adapter.collect_queue_progress()[0]["state"], "printing")
                self.fake.state = end
                self.poll(adapter)
                (report,) = adapter.collect_queue_progress()
                self.assertEqual((report["state"], report["reason"], report["assignmentSeq"]), ("held", reason, 3))

    def test_a_print_that_never_starts_is_held_after_the_timeout(self):
        self.fake.start_begins_printing = False
        self.start()
        self.adapter._queue_progress._start_timeout_sec = 0
        self.poll()
        (report,) = self.adapter.collect_queue_progress()
        evidence = report.pop("startEvidence")
        self.assertEqual(evidence["commandReply"], "accepted")
        self.assertIsInstance(evidence["sampledAt"], int)
        self.assertEqual(report, {"queueJobId": JOB, "state": "held", "reason": "start_not_observed", "assignmentSeq": 3})

    def test_another_file_running_is_never_this_jobs_print(self):
        # the start never reached klippy; a member starts a walk-up print at the screen inside the start window
        self.fake.start_begins_printing = False
        self.start()
        self.fake.state, self.fake.filename, self.fake.print_started_at = "printing", "walkup_vase.gcode", time.time()
        self.poll()
        self.assertEqual(self.adapter.collect_queue_progress(), [])
        self.adapter._queue_progress._start_timeout_sec = 0
        (report,) = self.adapter.collect_queue_progress()
        evidence = report.pop("startEvidence")
        self.assertEqual(evidence["commandReply"], "accepted")
        self.assertIsInstance(evidence["sampledAt"], int)
        self.assertEqual(report, {"queueJobId": JOB, "state": "held", "reason": "start_not_observed", "assignmentSeq": 3})

    def test_the_printing_report_never_carries_another_prints_key(self):
        # ours runs and finishes between two beats, then a walk-up print starts before the next collect: the synthesized
        # 'printing' must not carry the walk-up's key (active_key's filename guard), and 'completed' carries ours
        self.start()
        self.poll()
        ours = self.adapter._job_tracker.active_key()
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll()
        self.fake.state, self.fake.filename, self.fake.duration = "printing", "walkup_vase.gcode", 0.0
        self.poll()
        self.assertNotEqual(self.adapter._job_tracker.active_key(), ours)
        self.assertEqual(self.adapter.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "printing", "assignmentSeq": 3},
                          {"queueJobId": JOB, "state": "completed", "printerJobKey": ours, "assignmentSeq": 3}])

    def test_a_print_paused_before_its_first_poll_still_carries_its_key(self):
        self.start()
        self.fake.state = "paused"
        self.poll()
        key = self.adapter._job_tracker.active_key()
        self.assertTrue(key)
        self.assertEqual(self.adapter.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "printing", "assignmentSeq": 3, "printerJobKey": key}])

    def test_a_key_that_is_not_durable_is_never_stamped(self):
        # a restart could not find it again, and the cloud would then refuse the real terminal; without a key the cloud's
        # time-window match still links it
        self.start()
        with mock.patch.object(klipper, "write_json_durably", return_value=False):
            self.poll()
        self.assertEqual(self.adapter.collect_queue_progress(), [{"queueJobId": JOB, "state": "printing", "assignmentSeq": 3}])
        key = self.adapter._job_tracker.active_key()
        self.poll()   # the disk is back: the next observation saves it
        reborn = self.make()
        self.poll(reborn)
        self.assertEqual(reborn._job_tracker.active_key(), key)

    def test_silence_is_not_idle(self):
        self.fake.start_begins_printing = False
        self.start()
        self.adapter._queue_progress._start_timeout_sec = 0
        self.assertEqual(self.adapter.collect_queue_progress(), [])   # never polled
        self.poll()
        self.adapter._last_poll_ok_at = time.monotonic() - klipper.STALE_SEC - 1
        self.assertEqual(self.adapter.collect_queue_progress(), [])   # polled, but not freshly
        self.assertEqual(self.adapter.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 3}])

    def test_a_report_that_is_not_durable_rolls_back(self):
        self.start()
        self.poll()

        def outbox_down(_report):
            raise OSError("disk full")

        self.assertEqual(self.adapter.collect_queue_progress(on_report=outbox_down), [])
        self.assertEqual([r["state"] for r in self.adapter.collect_queue_progress()], ["printing"])


class TestDurability(KlipperCase):
    def test_a_poll_in_flight_at_stop_records_nothing_and_the_replacement_reports_the_print(self):
        # codex review of release-0.68.0: a poll still in flight after stop() saved print A after the replacement loaded
        # the state file, and the replacement's next save dropped A. Now a stopped adapter never records or saves.
        self.fake.state, self.fake.filename = "printing", "makeros/a_cube.gcode"
        self.poll()
        self.fake.state, self.fake.duration = "complete", 3600.0
        finished, release, in_flight = self.fake.status(), threading.Event(), threading.Event()

        def slow_fetch(*_a, **_kw):
            in_flight.set()
            release.wait(5)
            return finished

        with mock.patch.object(self.adapter, "_fetch_once", side_effect=slow_fetch):
            self.adapter.start()
            self.assertTrue(in_flight.wait(2))
            self.adapter.stop()
            reborn = self.make()                  # what manager._reconcile_klipper builds next, on the same state file
            release.set()
            self.adapter._thread.join(2)
        self.assertFalse(self.adapter._thread.is_alive())
        self.assertEqual(self.adapter.pending_jobs(), [])          # the late poll was dropped, not applied
        self.poll(reborn)                         # the printer still shows A finished
        self.fake.state, self.fake.filename, self.fake.duration = "printing", "makeros/b_other.gcode", 0.0
        self.poll(reborn)                         # then B begins, and the replacement saves its state
        self.assertEqual([j["endReason"] for j in reborn.pending_jobs()], ["lost"])
        self.assertEqual([j["endReason"] for j in self.make().pending_jobs()], ["lost"])   # and on disk

    def test_a_restarted_adapter_rehydrates_the_dispatch_and_the_terminal_jobs(self):
        self.start()
        self.poll()
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll()
        key = self.adapter.pending_jobs()[0]["jobKey"]
        reborn = self.make()   # an update / power cut: same state dirs, a new process
        self.assertEqual(reborn.pending_queue_jobs(), [{"queueJobId": JOB, "assignmentSeq": 3}])
        self.assertEqual([j["jobKey"] for j in reborn.pending_jobs()], [key])
        self.poll(reborn)      # the printer still shows the same finished print
        self.assertEqual([j["jobKey"] for j in reborn.pending_jobs()], [key])
        reports = reborn.collect_queue_progress()
        self.assertEqual([(r["state"], r.get("printerJobKey")) for r in reports], [("printing", None), ("completed", key)])

    def test_a_restart_mid_print_keeps_the_job_key(self):
        # an OTA / config re-pull / power blip of the hub mid-print: the cloud holds hub_job_ref = the key from 'printing',
        # so the terminal after the restart must carry that same key or the queue job is never completed
        self.start()
        self.poll()
        (printing,) = self.adapter.collect_queue_progress()
        key = printing["printerJobKey"]
        reborn = self.make()
        self.fake.duration = 1200.0
        self.poll(reborn)
        self.assertEqual(reborn._job_tracker.active_key(), key)
        again = self.make()   # restarted twice: still the same print
        self.poll(again)
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll(again)
        self.assertEqual([(j["jobKey"], j["status"]) for j in again.pending_jobs()], [(key, "done")])
        self.assertEqual(again.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "completed", "printerJobKey": key, "assignmentSeq": 3}])

    def test_a_print_that_finished_while_the_hub_was_down_keeps_its_key(self):
        self.start()
        self.poll()
        key = self.adapter._job_tracker.active_key()
        self.fake.state, self.fake.duration = "complete", 3600.0
        reborn = self.make()
        self.poll(reborn)
        self.assertEqual([(j["jobKey"], j["status"], j["endReason"]) for j in reborn.pending_jobs()], [(key, "cancelled", "lost")])

    def test_a_print_that_ended_while_the_hub_was_down_is_reported_lost_once(self):
        # A terminal reached during downtime cannot prove which run of the same file it ended. Keep the key, fail closed.
        self.start()
        self.poll()
        (printing,) = self.adapter.collect_queue_progress()
        key = printing["printerJobKey"]
        self.fake.state, self.fake.duration = "complete", 3600.0
        reborn = self.make()
        self.poll(reborn)
        self.assertEqual([(j["jobKey"], j["status"], j["endReason"]) for j in reborn.pending_jobs()], [(key, "cancelled", "lost")])
        self.assertEqual(reborn.collect_queue_progress(),
                         [{"queueJobId": JOB, "state": "held", "reason": "outcome_unknown", "printerJobKey": key, "assignmentSeq": 3, "failure": {"kind": "lost"}}])
        reborn.ack_jobs([key])
        for _ in range(2):
            again = self.make()
            self.poll(again)
            self.assertEqual((again.pending_jobs(), again.collect_queue_progress()), ([], []))

    def test_a_print_that_ended_while_the_hub_was_down_ends_when_the_printer_says(self):
        # saved an hour ago as running (started then); it ran 10 minutes; the hub comes back now
        now = time.time()
        state = {"pending": [], "lastTerminal": None,
                 "active": {"key": "fp_saved", "name": "makeros/0123abcd_cube.gcode", "startedWall": now - 3600, "printStart": now - 3600}}
        (klipper.TERMINAL_JOBS_DIR / "k9.json").write_text(json.dumps(state), encoding="utf-8")
        self.fake.state, self.fake.filename, self.fake.duration, self.fake.total = "complete", "makeros/0123abcd_cube.gcode", 590.0, 600.0
        reborn = self.make(pid="k9")
        self.poll(reborn)
        (job,) = reborn.pending_jobs()
        self.assertEqual((job["jobKey"], job["status"], job["endReason"]), ("fp_saved", "cancelled", "lost"))
        self.assertAlmostEqual(job["printTimeSeconds"], 600, delta=2)
        self.assertEqual(job["endedAt"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3000)))

    def test_a_different_print_after_the_restart_gets_a_new_key(self):
        self.start()
        self.fake.print_started_at = time.time() - 3600   # it has been printing for an hour
        self.poll()
        key = self.adapter._job_tracker.active_key()
        for change in ("restarted", "standby"):
            with self.subTest(change=change):
                if change == "restarted":   # the same file printed again from the screen while the hub was down
                    self.fake.print_started_at = time.time() - 60
                else:                       # reset after it ended: outcome unknown, nothing invented
                    self.fake.state, self.fake.filename = "standby", ""
                time.sleep(0.002)   # keys carry the observation's millisecond: a fast box observed both in one (codex review)
                reborn = self.make(pid="k1")
                self.poll(reborn)
                self.assertNotEqual(reborn._job_tracker.active_key(), key)
                self.assertEqual(reborn.pending_jobs(), [])

    def test_a_restart_onto_a_finished_printer_does_not_report_the_print_twice(self):
        self.fake.state, self.fake.filename = "printing", "makeros/x_cube.gcode"
        self.poll()
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll()
        self.adapter.ack_jobs([j["jobKey"] for j in self.adapter.pending_jobs()])
        for _ in range(2):
            reborn = self.make()
            self.poll(reborn)
            self.assertEqual(reborn.pending_jobs(), [])
        self.fake.duration = 1800.0   # the same file printed again while the hub was down: that IS a new print
        reborn = self.make()
        self.poll(reborn)
        self.assertEqual([j["status"] for j in reborn.pending_jobs()], ["done"])


class TestManagerKlipper(KlipperCase):
    def manager(self, model=MODEL):
        m = PrinterManager(diagnostics=self.diag)
        self.addCleanup(m.stop_all)
        m.reconcile([{"id": "k1", "vendor": "klipper", "moonrakerUrl": self.fake.url, "model": model}], version="v1")
        return m

    def test_the_cloud_model_reaches_the_adapter_and_an_edit_rebuilds_it(self):
        m = self.manager()
        first = m._adapters["k1"]
        self.assertEqual(first._model, MODEL)
        m.reconcile([{"id": "k1", "vendor": "klipper", "moonrakerUrl": self.fake.url, "model": MODEL}], version="v2")
        self.assertIs(m._adapters["k1"], first)
        m.reconcile([{"id": "k1", "vendor": "klipper", "moonrakerUrl": self.fake.url, "model": "Sovol SV08"}], version="v3")
        self.assertIsNot(m._adapters["k1"], first)
        self.assertEqual(m._adapters["k1"]._model, "Sovol SV08")

    @mock.patch.object(KlipperAdapter, "start")
    def test_a_model_edit_rebuild_reports_an_unacked_finished_print_once(self, _start):
        # This case feeds samples explicitly; a background HTTP sample can otherwise replay an older state.
        # the old adapter's unacked job is rescued AND the rebuilt one reloads it from disk: one jobKey, one row
        m = self.manager()
        self.fake.state, self.fake.filename = "printing", "makeros/x_cube.gcode"
        self.poll(m._adapters["k1"])
        self.fake.state, self.fake.duration = "complete", 3600.0
        self.poll(m._adapters["k1"])
        (job,) = m.pending_jobs()
        m.reconcile([{"id": "k1", "vendor": "klipper", "moonrakerUrl": self.fake.url, "model": "Sovol SV08"}], version="v2")
        self.assertEqual([j["jobKey"] for j in m.pending_jobs()], [job["jobKey"]])
        m.ack_jobs([job["jobKey"]])
        self.assertEqual(m.pending_jobs(), [])

    def test_a_queue_state_file_of_the_wrong_shape_never_stops_the_agent(self):
        # valid JSON, wrong shapes: each one raised TypeError in the adapter constructor, outside the manager's handler
        path = klipper.QUEUE_PROGRESS_DIR / "k1.json"
        good = {"queueJobId": JOB, "dispatched_wall": time.time(), "baselineKeys": ["fp_a"], "taskName": "makeros/x.gcode"}
        for state in ({"dispatches": 17}, {"dispatches": [good], "linked": 17},
                      {"dispatches": [{**good, "baselineKeys": 17}, {**good, "queueJobId": OTHER_JOB}]}):
            path.write_text(json.dumps(state), encoding="utf-8")
            m = self.manager()
            self.assertIn("k1", m._adapters)
            kept = [d["queueJobId"] for d in m._adapters["k1"]._queue_progress.to_state()["dispatches"]]
            self.assertEqual(kept, [] if state["dispatches"] == 17 else [d["queueJobId"] for d in state["dispatches"]])
            m.stop_all()

    def test_an_assignment_goes_uploading_then_printing(self):
        m = self.manager()
        assignment = {"queueJobId": JOB, "printerId": "k1", "submissionUid": UID, "fileName": "cube.gcode", "assignmentSeq": 3}
        self.assertEqual(m.dispatch_assignments([assignment], self.spool), [{"queueJobId": JOB, "state": "uploading", "assignmentSeq": 3}])
        adapter = m._adapters["k1"]
        self.poll(adapter)
        (report,) = m.collect_queue_progress()
        self.assertEqual((report["state"], report["assignmentSeq"]), ("printing", 3))
        self.assertEqual(report["printerJobKey"], adapter._job_tracker.active_key())


if __name__ == "__main__":
    unittest.main()
