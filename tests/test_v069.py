"""v0.69: the heartbeat's camera additions, the operator's forced update, and nothing lost on a mid-print restart.

The restart tests build a NEW adapter / manager on the same state directories (tests/conftest.py gives each test its
own), which is exactly what a hub restart is: a new process reading what the old one wrote. Fakes only — no printer,
no site, no network."""

import json
import os
import socket
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub import agent, http, report_outbox, update
from makeros_hub.config import Config
from makeros_hub.printers import bambu as bambu_module, bambu_camera, bambu_send, camera, manager as manager_module
from makeros_hub.printers.camera import CameraScheduler
from makeros_hub.printers.jobs import JobTracker, _iso
from makeros_hub.printers.manager import PrinterManager

SHA = "a" * 40
UID = "cccccccc"
ASSIGNMENT = {"queueJobId": "q1", "printerId": "p1", "submissionUid": UID, "fileName": "cube.3mf", "plate": 1, "assignmentSeq": 7}


class _Info:
    rc = 0


class FakeClient:
    def __init__(self):
        self.published = []

    def is_connected(self):
        return True

    def subscribe(self, *_a, **_k):
        pass

    def publish(self, _topic, payload):
        self.published.append(payload)
        return _Info()


class _Msg:
    def __init__(self, doc):
        self.payload = json.dumps(doc).encode()


class _Crash(BaseException):
    """The process dying at that line: nothing after it runs, nothing catches it."""


def boot(pid="p1"):
    """What a hub (re)start builds: a new adapter on the same state files, MQTT connected, no report yet."""
    a = bambu_module.BambuAdapter(printer_id=pid, host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
    a._client = FakeClient()
    a._connack = "ok"
    return a


def report(a, **print_fields):
    a._on_message(None, None, _Msg({"print": print_fields}))


def report_later(a, seconds, **print_fields):
    """A report `seconds` after now on the wall clock the tracker reads (e.g. a LAN print still saying "0" 5 min in)."""
    with mock.patch.object(bambu_module.time, "time", return_value=time.time() + seconds):
        report(a, **print_fields)


class TestHeartbeatAdditions(unittest.TestCase):
    def test_camera_want_is_capped_and_a_non_list_means_an_older_site(self):
        self.assertEqual(agent._camera_want({"cameraWant": ["a", 3, None, "b"]}), {"a", "b"})
        self.assertEqual(len(agent._camera_want({"cameraWant": [f"p{i}" for i in range(100)]})), 64)
        self.assertIsNone(agent._camera_want({"cameraWant": "a"}))
        self.assertIsNone(agent._camera_want({}))
        self.assertEqual(agent._camera_want({"cameraWant": []}), set())   # a new site wanting nothing is not "old"

    def test_payload_carries_camera_stats_under_1kb_and_pi_metrics_carry_tick_and_cpu_count(self):
        s = CameraScheduler()
        s.stats["fail"] = {r: 999999 for r in ("timeout", "liveview-off", "auth-fail", "unreachable", "tls-error", "no-ffmpeg",
                                              "ffmpeg-arg", "bad-jpeg", "frozen", "too-large", "unknown")}
        s.stats["push"] = {k: 999999 for k in ("200", "404", "409", "413", "415", "422", "500", "503", "net", "unsent")}
        p = agent.heartbeat_payload(camera_stats=agent._camera_stats(s), pi_metrics=agent._collect_pi_metrics(1234))
        self.assertLess(len(json.dumps(p["cameraStats"])), 1024)
        self.assertEqual({"breakerOpen", "ffSlots", "childCpuMs", "notStarted", "ioMs", "ffMs"} - set(p["cameraStats"]), set())
        self.assertEqual(p["piMetrics"]["tickWallMs"], 1234)
        self.assertGreaterEqual(p["piMetrics"]["cpuCount"], 1)
        self.assertNotIn("cameraStats", agent.heartbeat_payload())

    def test_mqtt_connect_stamps_the_status_and_it_is_absent_before_the_first_connect(self):
        a = bambu_module.BambuAdapter(printer_id="p1", host="127.0.0.1", serial="S1", access_code="x", model="A1 mini")
        self.assertNotIn("mqttConnectedAtMs", a.status())
        before = int(time.time() * 1000)
        a._on_connect(FakeClient(), None, None, 0)
        stamp = a.status()["mqttConnectedAtMs"]
        self.assertGreaterEqual(stamp, before)
        time.sleep(0.002)
        a._on_connect(FakeClient(), None, None, 0)     # a reconnect (power-cycle) moves it
        self.assertGreater(a.status()["mqttConnectedAtMs"], stamp)
        a._on_connect(FakeClient(), None, None, 5)     # a refused connect does not
        self.assertEqual(a._connack, "fail")


class TestForcedUpdate(unittest.TestCase):
    BUSY = [{"printerId": "p1", "gcodeState": "RUNNING", "state": "printing"}]

    def step(self, body, statuses=BUSY, wait_idle=True):
        calls = []
        out = agent._ota_step(body, statuses, wait_idle=wait_idle, updater=lambda *a: calls.append(a) or True)
        return out, calls

    def test_default_defers_while_printing(self):
        self.assertEqual(self.step({"targetVersion": "v9.0.0", "targetVersionSha256": SHA}), ("deferred", []))

    def test_a_force_for_this_target_updates_while_printing(self):
        out, calls = self.step({"targetVersion": "v9.0.0", "targetVersionSha256": SHA, "targetForceSha256": SHA})
        self.assertEqual(out, "forced")
        self.assertEqual(calls, [(agent.__version__, "v9.0.0", SHA)])   # still through maybe_update: cooldown, cap, sha

    def test_the_site_0141_response_forces_this_target(self):
        # the exact shape pstation 0141's heartbeat route answers once the operator pressed the button (route.ts: sent only
        # while target_agent_force_sha equals the armed sha). Each side's tests used its own name before (codex review).
        body = {"hubId": "h", "nextPollMs": 30000, "targetVersion": "v9.0.0", "targetVersionSha256": SHA, "targetVersionForce": True}
        out, calls = self.step(body)
        self.assertEqual((out, calls), ("forced", [(agent.__version__, "v9.0.0", SHA)]))

    def test_a_force_for_another_target_or_malformed_is_ignored(self):
        for force in ("b" * 40, True, "yes", None, SHA.upper()):
            with self.subTest(force=force):
                body = {"targetVersion": "v9.0.0", "targetVersionSha256": SHA, "targetForceSha256": force}
                self.assertEqual(self.step(body), ("deferred", []))
        for flag in ("true", 1, False, None, [True]):
            with self.subTest(flag=flag):
                body = {"targetVersion": "v9.0.0", "targetVersionSha256": SHA, "targetVersionForce": flag}
                self.assertEqual(self.step(body), ("deferred", []))
        # a force without a valid target sha is no force (content trust is checked first, never bypassed)
        self.assertEqual(self.step({"targetVersion": "v9.0.0", "targetVersionSha256": "nope", "targetForceSha256": "nope"}),
                         ("deferred", []))
        self.assertEqual(self.step({"targetVersion": "v9.0.0", "targetVersionSha256": "nope", "targetVersionForce": True}),
                         ("deferred", []))

    def test_idle_or_wait_idle_off_launches_without_a_force_and_an_old_target_is_nothing(self):
        self.assertEqual(self.step({"targetVersion": "v9.0.0", "targetVersionSha256": SHA}, statuses=[{"gcodeState": "IDLE"}])[0], "launched")
        self.assertEqual(self.step({"targetVersion": "v9.0.0", "targetVersionSha256": SHA}, wait_idle=False)[0], "launched")
        self.assertEqual(self.step({"targetVersion": "v0.1.0", "targetVersionSha256": SHA, "targetForceSha256": SHA}), ("none", []))


class TestUpdateAttemptRecordIsDurable(unittest.TestCase):
    def test_the_rename_of_the_attempt_record_is_fsynced_with_its_directory(self):
        # codex review: without the directory fsync a power cut can lose the attempt, and the cap no longer holds
        synced = []
        real = os.fsync

        def spy(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            real(fd)

        with mock.patch.object(update.os, "fsync", side_effect=spy):
            update._write_state({"target": "v9.0.0", "sha": SHA, "at": 1.0, "attempts": 1})
        self.assertEqual(synced, [False, True])
        self.assertEqual(json.loads(update.STATE_PATH.read_text())["attempts"], 1)


class _Dispatch(unittest.TestCase):
    def setUp(self):
        model = mock.patch.object(bambu_module, "_sliced_model_matches", return_value=True)
        model.start()
        self.addCleanup(model.stop)
        upload = mock.patch.object(bambu_send, "upload_3mf")
        self.upload = upload.start()
        self.addCleanup(upload.stop)
        self.spool = Path(manager_module.DISPATCHED_STATE_PATH).parent / "spool"
        (self.spool / UID).mkdir(parents=True)
        (self.spool / UID / "cube.3mf").write_bytes(b"3mf")

    def start(self, a):
        return a.start_print(self.spool / UID / "cube.3mf", "cube.3mf", queue_job_id="q1", assignment_seq=7)

    def printing(self, task_id="0"):
        """Hub process #1: dispatch q1, see it print, report 'printing'. Returns (key, startedAt) of the running print."""
        a = boot()
        report(a, gcode_state="IDLE")
        self.assertEqual(self.start(a), {"ok": True})
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id=task_id)
        self.assertEqual(a.collect_queue_progress(), [{"queueJobId": "q1", "state": "printing", "assignmentSeq": 7}])
        return a._jobs.active_key(), a._jobs._active["startedAt"]


class TestPrintFinishedWhileTheHubWasDown(_Dispatch):
    def age_saved_print(self, hours_since_start, hours_since_eta):
        """The hub was down: move the saved running print's start and ETA into the past (wall clock)."""
        path = bambu_module.TERMINAL_JOBS_DIR / "p1.json"
        raw = json.loads(path.read_text())
        (entry,) = [e for e in raw if "hubState" in e]
        started = entry["hubState"]["active"]["startedAt"] = time.time() - hours_since_start * 3600
        entry["hubState"]["active"]["etaAt"] = time.time() - hours_since_eta * 3600
        path.write_text(json.dumps(raw))
        return started

    def test_a_re_print_of_the_same_file_never_completes_the_saved_prints_queue_job(self):
        # codex re-review: dropping the saved key was not enough; queue progress matched the re-print's terminal by
        # filename and reported q1 'completed' with ANOTHER print's key
        self.printing(task_id="0")
        self.age_saved_print(9, 8)                           # down 8 h; the saved print should have ended 8 h ago
        queue_file = next(bambu_module.QUEUE_PROGRESS_DIR.glob("*.json"))
        before = queue_file.read_text()
        b = boot()
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="0")    # a re-print of the same file
        # the re-print carries its own mark in the terminal file's one atomic write: no second file has to land with it
        # (codex re-review of 9ac3ff7: a crash between two writes left the old job linkable)
        self.assertEqual(queue_file.read_text(), before)
        self.assertEqual(b.collect_queue_progress(), [])
        c = boot()                                           # and another restart before it ends
        report(c, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="0")
        self.assertEqual(len(c.pending_jobs()), 1)           # the re-print is still reported, as its own print
        c._queue_progress._start_timeout_sec = 0
        self.assertEqual(c.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])

    def test_no_print_after_a_drop_completes_the_old_job_until_the_hub_dispatches_again(self):
        # codex review of 135cb83: only the first replacement was marked, so a second walk-up of the same file, started
        # before the queue saw the printer idle, completed the old job with its own key
        self.printing(task_id="0")
        self.age_saved_print(9, 8)
        b = boot()
        for name in ("cube", "cube"):                        # the re-print, then another walk-up of the same file
            report(b, gcode_state="RUNNING", subtask_name=name, task_id="0")
            report(b, gcode_state="FINISH", subtask_name=name, task_id="0")
        self.assertEqual([j.get("notDispatched") for j in b.pending_jobs()], [True, True])
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])
        # the hub's next dispatch ends the marking: its own print completes it
        (self.spool / UID / "next.3mf").write_bytes(b"3mf")
        self.assertEqual(b.start_print(self.spool / UID / "next.3mf", "next.3mf", queue_job_id="q2", assignment_seq=8), {"ok": True})
        c = boot()                                           # even across a restart right after the dispatch
        report(c, gcode_state="RUNNING", subtask_name="next", task_id="0")
        report(c, gcode_state="FINISH", subtask_name="next", task_id="0")
        self.assertEqual([r["state"] for r in c.collect_queue_progress()], ["printing", "completed"])

    def test_another_members_finished_reprint_found_on_return_is_never_the_old_jobs_completion(self):
        # codex review of e69e4b1: Alice's cube (task 77) was saved; Bob re-printed cube as task 78 during the outage; the
        # hub came back onto FINISH/78 and the recovered record completed Alice's job with Bob's print
        self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="78")
        (job,) = b.pending_jobs()
        self.assertEqual((job["jobKey"], job.get("notDispatched")), ("task_S1_78", True))
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])

    def test_a_new_dispatch_whose_mark_clear_cannot_be_saved_is_not_started(self):
        # codex review of e69e4b1: the clear failed silently, the start went out, and after a restart the dispatched print
        # inherited the mark and its job was held
        self.printing(task_id="0")
        b = boot()
        report(b, gcode_state="IDLE")                        # reset during the outage: dropped, marking on
        b._queue_progress._start_timeout_sec = 0
        b.collect_queue_progress()                           # q1 held
        (self.spool / UID / "next.3mf").write_bytes(b"3mf")
        with mock.patch.object(JobTracker, "_save_pending", return_value=False):
            got = b.start_print(self.spool / UID / "next.3mf", "next.3mf", queue_job_id="q2", assignment_seq=8)
        self.assertEqual(got, {"ok": False, "reason": "state_persist_failed"})
        self.assertEqual((b._client.published, b.pending_queue_jobs(), b._jobs._mark_next), ([], [], True))
        self.assertEqual(b.start_print(self.spool / UID / "next.3mf", "next.3mf", queue_job_id="q2", assignment_seq=8),
                         {"ok": True})                       # the disk is back: the cloud's re-send starts it
        self.assertFalse(b._jobs._mark_next)

    def test_a_task_id_that_turns_up_after_a_name_only_adoption_and_differs_drops_it(self):
        # codex review of 3b59933: Alice's task 77 adopted by name from a report without an id; Bob's task 78 then showed
        # up (as a later delta, or straight on its FINISH) and completed Alice's job under her key
        for later in ("RUNNING", "FINISH"):
            with self.subTest(later=later):
                key, _ = self.printing(task_id="77")
                b = boot()
                report(b, gcode_state="RUNNING", subtask_name="cube")
                self.assertIsNone(b._jobs.active_key())      # a saved print with an id waits for the id, not the name
                report(b, gcode_state=later, subtask_name="cube", task_id="78")
                if later == "RUNNING":
                    self.assertEqual(b._jobs.active_key(), "task_S1_78")
                    report(b, gcode_state="FINISH", subtask_name="cube", task_id="78")
                (job,) = b.pending_jobs()
                self.assertEqual((job["jobKey"], job.get("notDispatched")), ("task_S1_78", True))
                b._queue_progress._start_timeout_sec = 0
                self.assertEqual(b.collect_queue_progress(), [{"queueJobId": "q1", "state": "held",
                                                               "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])
                for f in bambu_module.TERMINAL_JOBS_DIR.glob("*"):
                    f.unlink()
                for f in bambu_module.QUEUE_PROGRESS_DIR.glob("*"):
                    f.unlink()

    def test_a_fingerprinted_print_whose_task_id_shows_up_after_the_restart_is_reported_once(self):
        # codex review of 3b59933: closed under its fingerprint, acked, then re-emitted under task_S1_77 on the next
        # restart onto the same FINISH: one physical print, two terminal rows
        key, _ = self.printing(task_id="0")
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual([j["jobKey"] for j in b.pending_jobs()], [key])
        b.ack_jobs([key])
        c = boot()
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual(c.pending_jobs(), [])

    def test_a_task_id_that_arrives_after_a_fingerprint_close_dedupes_the_next_restart(self):
        # codex review of 921c14a: closed under its fingerprint on a FINISH without an id, then a delta named task 77; the
        # next restart onto that FINISH emitted task_S1_77 as a second print
        # (a print whose reports left the id OUT; one that said "0" has no id, and a later id is another print's)
        t = JobTracker("p6", "S6", state_path=self.PATH.with_name("p6.json"))
        t.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube"}}, 1000.0)
        t.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 2000.0)
        t.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube", "task_id": "77"}}, 2001.0)
        (job,) = t.pending()
        t.ack([job["jobKey"]])
        u = JobTracker("p6", "S6", state_path=self.PATH.with_name("p6.json"))
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube", "task_id": "77"}}, 3000.0)
        self.assertEqual(u.pending(), [])

    def test_a_learned_task_id_is_an_exact_identity_past_the_eta_cutoff(self):
        # codex review of 921c14a: fingerprinted print, its task id learned while paused, then an outage past the ETA cutoff:
        # the matching id must keep it (and its start), not drop it as a possible re-print
        key, _ = self.printing(task_id="0")
        b = boot()
        report(b, gcode_state="PAUSE", subtask_name="cube", task_id="77")
        self.assertEqual(b._jobs.active_key(), key)
        started = self.age_saved_print(9, 8)
        c = boot()
        report(c, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="77")
        (job,) = c.pending_jobs()
        self.assertEqual((job["startedAt"], job.get("notDispatched")), (_iso(started), None))
        self.assertEqual(c.collect_queue_progress()[-1]["state"], "completed")

    def test_a_print_found_finished_after_a_watched_task_id_change_is_reported_as_its_own(self):
        # codex review of c3ccb8b: the hub watched task 77, another print (78) ran and finished unseen, and FINISH/78
        # closed 77 but never reported 78
        for reconnect in (True, False):
            with self.subTest(reconnect=reconnect):
                key, _ = self.printing(task_id="77")
                a = boot()
                report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
                report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77", mc_percent=40)
                if reconnect:                                # MQTT dropped: 77 must be confirmed again, like after a restart
                    a._on_disconnect(None, None, 0)
                    a._on_connect(a._client, None, None, 0)
                report(a, gcode_state="FINISH", subtask_name="cube", task_id="78")
                expected = [] if reconnect else [(key, "cancelled", None)]   # unknown is not invented; a watched gap is
                self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in a.pending_jobs()],
                                 expected + [("task_S1_78", "done", True)])
                a._queue_progress._start_timeout_sec = 0
                self.assertEqual([(r["state"], r.get("reason")) for r in a.collect_queue_progress()],
                                 [("held", "outcome_unknown_after_restart" if reconnect else "print_cancelled")])
                for f in list(bambu_module.TERMINAL_JOBS_DIR.glob("*")) + list(bambu_module.QUEUE_PROGRESS_DIR.glob("*")):
                    f.unlink()

    def test_a_reconnect_needs_a_fresh_identity_before_it_settles_the_running_print(self):
        # codex review of e8134f8: Alice's task 77 cached, Bob's 78 ran and finished during the drop, and a bare FINISH
        # delta merged with the cached identity closed Alice's print as done and completed her job
        self.printing(task_id="77")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        a._on_disconnect(None, None, 0)
        a._on_connect(a._client, None, None, 0)
        report(a, gcode_state="FINISH")
        self.assertEqual(a.pending_jobs(), [])               # undecided
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j.get("notDispatched")) for j in a.pending_jobs()], [("task_S1_78", True)])
        # a short blip on the same print just carries on, and completes its job
        for f in list(bambu_module.TERMINAL_JOBS_DIR.glob("*")) + list(bambu_module.QUEUE_PROGRESS_DIR.glob("*")):
            f.unlink()
        key, _ = self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        b._on_disconnect(None, None, 0)
        b._on_connect(b._client, None, None, 0)
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual([(j["jobKey"], j["status"]) for j in b.pending_jobs()], [(key, "done")])
        self.assertEqual(b.collect_queue_progress()[-1]["state"], "completed")

    def test_a_reconnect_with_nothing_running_never_re_emits_an_acked_print(self):
        # codex review of 1e116ed: closed under its fingerprint, acked, then the reconnect's pushall named task 77 and the
        # re-armed recovery emitted the same print again under task_S1_77 (a print whose reports left the id out)
        key, _ = self.printing(task_id=None)
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube")
        report(a, gcode_state="FINISH", subtask_name="cube")
        a.ack_jobs([key])
        self.reconnect(a)
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual(a.pending_jobs(), [])

    def test_a_saved_lan_print_is_not_adopted_by_a_same_name_print_with_an_id(self):
        # codex review of 708dff1: Alice's saved LAN cube said task "0"; Bob's cube (task 78) finished during the outage;
        # on return the name matched, Alice's job completed under her key with Bob's ending, and 78 became her alias
        self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report_later(a, 400, gcode_state="RUNNING", subtask_name="cube", task_id="0")   # 'no id' is settled now
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual([(j["jobKey"], j.get("notDispatched")) for j in b.pending_jobs()], [("task_S1_78", True)])
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual([r.get("reason") for r in b.collect_queue_progress()], ["outcome_unknown_after_restart"])

    def test_a_lan_re_dispatch_keeps_its_no_id_so_a_later_id_is_another_print(self):
        # codex review of 708dff1: the second LAN dispatch's seed dropped the "0" ids its deltas never resend, so another
        # cube (task 78) finished during a later drop was aliased to it and never reported
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="0")
        a.collect_queue_progress()
        a.ack_jobs([key])
        time.sleep(0.002)
        self.assertEqual(a.start_print(self.spool / UID / "cube.3mf", "cube.3mf", queue_job_id="q2", assignment_seq=8), {"ok": True})
        report(a, gcode_state="RUNNING")
        report_later(a, 400, gcode_state="RUNNING", mc_percent=50)   # still the seeded "0" 5 min in: a LAN print
        report_later(a, 401, gcode_state="FINISH")
        (second,) = a.pending_jobs()
        a.collect_queue_progress()
        a.ack_jobs([second["jobKey"]])
        self.reconnect(a)
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual([j["jobKey"] for j in a.pending_jobs()], ["task_S1_78"])

    def test_after_a_reconnect_a_new_id_after_a_lan_print_is_another_print(self):
        # codex review of a599650: a LAN cube (task "0") closed and acked, another cube (task 78) ran and finished during the
        # drop, and the name match aliased 78 to the old fingerprint: never reported, then suppressed for good
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report_later(a, 400, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report_later(a, 401, gcode_state="FINISH", subtask_name="cube", task_id="0")
        a.ack_jobs([key])
        self.reconnect(a)
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual([j["jobKey"] for j in a.pending_jobs()], ["task_S1_78"])

    def test_a_task_id_landing_after_the_first_running_frame_never_splits_the_dispatched_print(self):
        # the guard on the 5-minute rule: the hub's start sends "0" and the printer's real id may land a few frames later
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report_later(a, 20, gcode_state="RUNNING", subtask_name="cube", task_id="4242")
        report_later(a, 3600, gcode_state="FINISH", subtask_name="cube", task_id="4242")
        self.assertEqual([(j["status"], j.get("notDispatched")) for j in a.pending_jobs()], [("done", None)])
        self.assertEqual(a.collect_queue_progress()[-1]["state"], "completed")

    def test_a_crash_right_after_the_cancellation_keeps_the_replacements_fence(self):
        # codex review of 213d644: the cancellation's save landed before the fence was set; a power cut between the two
        # recovered task 78 unmarked, ahead of Alice's 'held', and the site's time fallback billed Alice with it
        key, _ = self.printing(task_id="77")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        with mock.patch.object(JobTracker, "_save_active", side_effect=_Crash()), self.assertRaises(_Crash):
            report(a, gcode_state="RUNNING", subtask_name="bob", task_id="78")
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in b.pending_jobs()],
                         [(key, "cancelled", None), ("task_S1_78", "done", True)])

    def test_a_distinct_print_that_finished_during_a_reconnect_gap_is_recovered(self):
        # codex review of 213d644: 77 finished and was acked; 78 ran and finished while MQTT was down; after the reconnect
        # nothing reported it
        key, _ = self.printing(task_id="77")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="77")
        a.ack_jobs([key])
        a._on_disconnect(None, None, 0)
        a._on_connect(a._client, None, None, 0)
        report(a, gcode_state="FINISH")                      # a bare delta first: judged on the report that names it
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in a.pending_jobs()],
                         [("task_S1_78", "done", None)])
        a._on_disconnect(None, None, 0)                      # and the same print on screen across another drop: nothing
        a._on_connect(a._client, None, None, 0)
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual(len(a.pending_jobs()), 1)

    def reconnect(self, a):
        a._on_disconnect(None, None, 0)
        a._on_connect(a._client, None, None, 0)

    def test_after_a_reconnect_another_name_is_never_aliased_to_the_print_just_closed(self):
        # codex review of 95d4abe: the late-id alias ran before the reconnect judgement and took Bob's task 78 as the id
        # of Alice's fingerprinted cube, so Bob's print was never reported
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="0")
        a.ack_jobs([key])
        self.reconnect(a)
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([j["jobKey"] for j in a.pending_jobs()], ["task_S1_78"])

    def test_a_name_before_the_task_id_never_spends_the_reconnect_judgement(self):
        # codex review of 95d4abe: FINISH/bob/task "0" first, then a delta with task 78: the first report used up the
        # judgement although nothing could be recovered without the id
        key, _ = self.printing(task_id="77")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="77")
        a.ack_jobs([key])
        self.reconnect(a)
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="0")
        report(a, gcode_state="FINISH", task_id="78")
        self.assertEqual([j["jobKey"] for j in a.pending_jobs()], ["task_S1_78"])

    def test_the_next_dispatch_never_inherits_the_finished_prints_cached_identity(self):
        # codex review of 95d4abe: Bob's first delta (gcode_state only) opened under Alice's cached task 77; his real name
        # and task 78 then read as a contradiction, fenced his own dispatch and held q2
        key, _ = self.printing(task_id="77")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual([r["state"] for r in a.collect_queue_progress()], ["completed"])
        a.ack_jobs([key])
        (self.spool / UID / "bob.3mf").write_bytes(b"3mf")
        self.assertEqual(a.start_print(self.spool / UID / "bob.3mf", "bob.3mf", queue_job_id="q2", assignment_seq=8), {"ok": True})
        report(a, gcode_state="RUNNING")
        report(a, gcode_state="RUNNING", subtask_name="bob", task_id="78")
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in a.pending_jobs()],
                         [("task_S1_78", "done", None)])
        self.assertEqual([r["state"] for r in a.collect_queue_progress()], ["printing", "completed"])

    def test_a_message_without_a_state_after_a_reconnect_never_spends_the_judgement(self):
        # codex review of 61c12f8: a get_version answer ahead of the pushall fed the cached IDLE to the tracker, which
        # disarmed the reconnect judgement; Bob's finished task 78 in the pushall was then never reported
        a = boot()
        report(a, gcode_state="IDLE")
        self.reconnect(a)
        a._on_message(None, None, _Msg({"info": {"command": "get_version", "module": []}}))
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([j["jobKey"] for j in a.pending_jobs()], ["task_S1_78"])

    def test_a_same_file_lan_re_dispatch_keeps_its_name_through_identity_less_deltas(self):
        # codex review of 61c12f8: clearing the cached identity at the dispatch also cleared the name, and a LAN re-print of
        # the same file never resends an unchanged subtask_name, so its ending had no filename and q2 was held
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="0")
        self.assertEqual([r["state"] for r in a.collect_queue_progress()], ["completed"])
        a.ack_jobs([key])
        time.sleep(0.002)   # a fingerprint carries its start's millisecond (codex review: a fast box saw both in one)
        self.assertEqual(a.start_print(self.spool / UID / "cube.3mf", "cube.3mf", queue_job_id="q2", assignment_seq=8), {"ok": True})
        report(a, gcode_state="RUNNING")
        report(a, gcode_state="FINISH")
        (job,) = a.pending_jobs()
        self.assertEqual((job["filename"], job.get("notDispatched")), ("cube", None))
        self.assertEqual([r["state"] for r in a.collect_queue_progress()], ["printing", "completed"])

    def test_a_start_refused_after_its_upload_leaves_the_observed_print_untouched(self):
        # codex review of b715c89: the name was seeded before the final busy check; Bob's local print, started during
        # Alice's upload, was then reported cancelled and a phantom Alice print done
        a = boot()
        report(a, gcode_state="IDLE")
        self.upload.side_effect = lambda *_a, **_k: report(a, gcode_state="RUNNING", subtask_name="bob-part", task_id="0")
        self.assertEqual(self.start(a), {"ok": False, "reason": "printer_busy"})
        report(a, gcode_state="RUNNING", mc_percent=50)
        report(a, gcode_state="FINISH")
        self.assertEqual([(j["status"], j["filename"]) for j in a.pending_jobs()], [("done", "bob-part")])

    def test_another_real_name_decides_a_saved_print_with_an_id_without_waiting(self):
        # codex review of b715c89: Alice's saved task 77 waited for an id that Bob's local LAN print (task "0") never sends,
        # so Bob went untracked to his end and no terminal was reported
        self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="RUNNING", subtask_name="bob-part", task_id="0", gcode_file="Metadata/plate_1.gcode")
        report(b, gcode_state="FINISH", subtask_name="bob-part", task_id="0")
        self.assertEqual([(j["filename"], j["status"], j.get("notDispatched")) for j in b.pending_jobs()],
                         [("bob-part", "done", True)])

    def test_a_same_file_finish_without_the_saved_prints_id_waits_for_the_id(self):
        # codex review of 1e116ed: Alice's cube (task 77) saved; Bob's cube (task 78) finished during the outage; the first
        # fresh report (FINISH, cube, no id) completed Alice's job, and task 78 was ignored
        self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="cube")
        self.assertEqual(b.pending_jobs(), [])
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="78")
        self.assertEqual([(j["jobKey"], j.get("notDispatched")) for j in b.pending_jobs()], [("task_S1_78", True)])
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual([r.get("reason") for r in b.collect_queue_progress()], ["outcome_unknown_after_restart"])

    def test_another_prints_name_on_a_finish_frame_splits_it_off(self):
        # codex review of 1e116ed: the name split ran only on RUNNING, so FINISH/bob/78 closed Alice's fingerprinted cube as
        # done with Bob's ending and completed her job
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in a.pending_jobs()],
                         [(key, "cancelled", None), ("task_S1_78", "done", True)])
        self.assertEqual([(r["state"], r.get("reason")) for r in a.collect_queue_progress()], [("held", "print_cancelled")])

    def test_a_generic_plate_path_never_disproves_the_saved_real_name(self):
        # codex review of e8134f8: a LAN cube finished during the restart; the first report (FINISH, task "0",
        # gcode_file only) mismatched the saved name and dropped the print, so it was never reported
        key, _ = self.printing(task_id="0")
        b = boot()
        report(b, gcode_state="FINISH", task_id="0", gcode_file="Metadata/plate_1.gcode")
        self.assertEqual(b.pending_jobs(), [])               # undecided
        report(b, gcode_state="FINISH", task_id="0", gcode_file="Metadata/plate_1.gcode", subtask_name="cube")
        self.assertEqual([(j["jobKey"], j["status"]) for j in b.pending_jobs()], [(key, "done")])
        self.assertEqual(b.collect_queue_progress()[-1]["state"], "completed")

    def test_a_real_name_change_with_a_new_task_id_is_another_print_even_on_a_fingerprint(self):
        # codex review of e8134f8: the alias learned from Bob's report (task 78) hid the name change, so Bob was taken for
        # Alice's fingerprinted cube
        key, _ = self.printing(task_id="0")
        a = boot()
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="RUNNING", subtask_name="bob", task_id="78")
        self.assertEqual(a._jobs.active_key(), "task_S1_78")
        report(a, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j["status"], j.get("notDispatched")) for j in a.pending_jobs()],
                         [(key, "cancelled", None), ("task_S1_78", "done", True)])
        self.assertEqual([(r["state"], r.get("reason")) for r in a.collect_queue_progress()], [("held", "print_cancelled")])

    def test_the_real_name_replaces_a_fallback_name_under_the_same_task_id(self):
        # codex review of c3ccb8b: opened from a report with task 77 and gcode_file only; the later subtask_name was kept
        # out, so FINISH carried the wrong filename and the queue job was held instead of completed
        a = boot()
        report(a, gcode_state="IDLE")
        self.assertEqual(self.start(a), {"ok": True})
        report(a, gcode_state="RUNNING", task_id="77", gcode_file="Metadata/plate_1.gcode")
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="77")
        (job,) = a.pending_jobs()
        self.assertEqual((job["jobKey"], job["filename"]), ("task_S1_77", "cube"))
        self.assertEqual([r["state"] for r in a.collect_queue_progress()], ["printing", "completed"])

    def test_a_bare_finish_delta_never_decides_which_print_finished(self):
        # codex review of 26528c0: {"gcode_state": "FINISH"} alone adopted and closed Alice's saved task 77 as done; the
        # full report naming Bob's task 78 that followed was ignored, and Alice's job completed under her key
        key, _ = self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="FINISH")
        self.assertEqual(b.pending_jobs(), [])               # undecided: nothing closed, nothing invented
        report(b, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual([(j["jobKey"], j.get("notDispatched")) for j in b.pending_jobs()], [("task_S1_78", True)])
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])
        c = JobTracker("p5", "S5", state_path=self.PATH.with_name("p5.json"))   # and when the full report IS the print
        c.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", "task_id": "77"}}, 1000.0)
        d = JobTracker("p5", "S5", state_path=self.PATH.with_name("p5.json"))
        d.observe({"print": {"gcode_state": "FINISH"}}, 2000.0)
        d.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube", "task_id": "77"}}, 2001.0)
        self.assertEqual([(j["jobKey"], j["status"]) for j in d.pending()], [("task_S5_77", "done")])

    def test_a_replacement_print_waits_until_the_old_job_is_held_and_that_report_delivered(self):
        # codex review of 26528c0: the site's heartbeat matches an unbound terminal to the printer's 'printing' job by
        # time, so Bob's marked print arriving before Alice's 'held' could still complete and bill Alice's job
        self.printing(task_id="77")
        b = boot()
        m = PrinterManager()
        m._adapters["p1"] = b
        report(b, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual(m.pending_jobs(), [])                 # q1 still unresolved on this printer: held back
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual([r["state"] for r in m.collect_queue_progress()], ["held"])
        self.assertEqual([j["jobKey"] for j in m.pending_jobs()], ["task_S1_78"])
        # an undelivered queue report holds it back in the run loop too
        sent = []

        def post(url, body, stop):
            if "jobs" in body:
                sent.append([j["jobKey"] for j in body["jobs"]])
            stop.set()
            return http.Response(status=200, body=TestCommandsSurviveARestart.OK)

        report_outbox.save([{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart"}])
        TestCommandsSurviveARestart.one_beat(post, mock.patch.object(PrinterManager, "pending_jobs",
                                                                     return_value=b.pending_jobs()))
        self.assertEqual(sent[0], [])

    def test_a_torn_down_adapter_never_releases_a_held_back_print_unfenced(self):
        # codex review of 02dc878: a config pull without the access code tore the adapter down, the rescue moved Bob's
        # held-back print into the orphans, and the next heartbeat sent it while Alice's job was still 'printing'
        self.printing(task_id="77")
        b = boot()
        m = PrinterManager()
        m._adapters["p1"] = b
        report(b, gcode_state="FINISH", subtask_name="bob", task_id="78")
        m._stop("p1")
        self.assertEqual(m.pending_jobs(), [])
        c = boot()                                           # the access code is back: rebuilt from the same files
        m._adapters["p1"] = c
        report(c, gcode_state="FINISH", subtask_name="bob", task_id="78")
        self.assertEqual(m.pending_jobs(), [])               # still fenced behind q1
        c._queue_progress._start_timeout_sec = 0
        self.assertEqual([r["state"] for r in m.collect_queue_progress()], ["held"])
        self.assertEqual([j["jobKey"] for j in m.pending_jobs()], ["task_S1_78"])

    def test_a_new_dispatch_settles_a_saved_print_no_report_has_named(self):
        self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="FINISH")                      # undecided
        b._queue_progress._start_timeout_sec = 0
        b.collect_queue_progress()                           # q1 held after its idle timeout
        (self.spool / UID / "next.3mf").write_bytes(b"3mf")
        self.assertEqual(b.start_print(self.spool / UID / "next.3mf", "next.3mf", queue_job_id="q2", assignment_seq=8), {"ok": True})
        report(b, gcode_state="RUNNING", subtask_name="next", task_id="90")
        report(b, gcode_state="FINISH", subtask_name="next", task_id="90")
        self.assertEqual([(j["jobKey"], j.get("notDispatched")) for j in b.pending_jobs()], [("task_S1_90", None)])
        self.assertEqual([r["state"] for r in b.collect_queue_progress()], ["printing", "completed"])

    def test_a_learned_task_id_beats_a_fallback_name_while_the_hub_watches(self):
        # codex review of 02dc878: the in-session same-id check looked at the key only, not the learned alias
        self.printing(task_id="0")
        b = boot()
        report(b, gcode_state="PAUSE", subtask_name="cube", task_id="77")   # learns the alias
        c = boot()                                           # its first report: the id, and only the fallback name
        report(c, gcode_state="RUNNING", task_id="77", gcode_file="Metadata/plate_1.gcode")
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="77")
        # one print, done (RUNNING upgrades a fingerprint key to the task key, as since v0.4x), never cancelled + a new one
        self.assertEqual([(j["jobKey"], j["status"], j["filename"]) for j in c.pending_jobs()], [("task_S1_77", "done", "cube")])
        self.assertEqual(c.collect_queue_progress()[-1]["state"], "completed")

    def test_the_task_id_beats_a_partial_reports_fallback_file_name(self):
        # codex review of 135cb83: the first report after the restart carried task 77 and gcode_file only; the name
        # mismatch dropped the real dispatch's print, reopened it with a new start and marked it notDispatched
        key, started = self.printing(task_id="77")
        b = boot()
        report(b, gcode_state="RUNNING", task_id="77", gcode_file="Metadata/plate_1.gcode")
        self.assertEqual(b._jobs.active_key(), key)
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="77", gcode_file="Metadata/plate_1.gcode")
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        (job,) = b.pending_jobs()
        self.assertEqual((job["jobKey"], job["startedAt"], job.get("notDispatched")), (key, _iso(started), None))
        self.assertEqual(b.collect_queue_progress()[-1], {"queueJobId": "q1", "state": "completed", "printerJobKey": key,
                                                          "assignmentSeq": 7})
        # and while the hub watches: a name flip under the same task id is not a second print
        t = JobTracker("p9", "S9")
        t.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", "task_id": "5"}}, 1000.0)
        t.observe({"print": {"gcode_state": "RUNNING", "gcode_file": "Metadata/plate_1.gcode", "task_id": "5"}}, 1001.0)
        self.assertEqual((t.pending(), t.active_key()), ([], "task_S9_5"))

    def test_a_dropped_walk_up_print_never_costs_the_finished_dispatch_its_completion(self):
        # codex re-review of 9ac3ff7: q1 finished (its record on disk), a walk-up print started at the screen, the hub
        # restarted onto IDLE; invalidating every pre-restart dispatch held q1 although its own ending was right there
        key, _ = self.printing(task_id="0")
        a = boot()                                           # (the same process as printing() in effect: state is on disk)
        report(a, gcode_state="RUNNING", subtask_name="cube", task_id="0")
        report(a, gcode_state="FINISH", subtask_name="cube", task_id="0")
        report(a, gcode_state="RUNNING", subtask_name="walk-up", task_id="0")
        b = boot()                                           # down before the next beat; the walk-up was cancelled
        report(b, gcode_state="IDLE")
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "completed", "printerJobKey": key, "assignmentSeq": 7}])

    def test_the_printers_own_task_id_keeps_the_saved_print_however_long_the_outage(self):
        # codex re-review: the ETA cutoff discarded a print whose task id matched (paused through the outage)
        key, _ = self.printing(task_id="77")
        started = self.age_saved_print(9, 8)
        b = boot()
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="77")
        self.assertEqual(b._jobs.active_key(), key)
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual(b.pending_jobs()[0]["startedAt"], _iso(started))   # the saved start, not the hub's return
        self.assertEqual(b.collect_queue_progress()[-1]["printerJobKey"], key)


    def test_reported_once_to_the_right_queue_job_for_a_lan_print_without_a_task_id(self):
        # LAN prints carry task_id "0". Before v0.69 the running print lived only in RAM: after the restart nothing was
        # reported, and the queue job was held as outcome_unknown_after_restart for a human.
        key, started = self.printing(task_id="0")
        b = boot()                                           # the hub comes back; the printer finished meanwhile
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="0")
        (job,) = b.pending_jobs()
        self.assertEqual((job["jobKey"], job["status"], job["filename"]), (key, "done", "cube"))
        self.assertEqual(job["startedAt"], time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(started))))
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "completed", "printerJobKey": key, "assignmentSeq": 7}])
        b.ack_jobs([key])                                    # the site's 200
        for _ in range(2):                                   # and restarts after that, onto the still-FINISH printer
            c = boot()
            report(c, gcode_state="FINISH", subtask_name="cube", task_id="0")
            self.assertEqual((c.pending_jobs(), c.collect_queue_progress()), ([], []))

    def test_reported_once_with_a_printer_task_id_and_never_again_after_the_ack(self):
        key, _ = self.printing(task_id="77")
        self.assertEqual(key, "task_S1_77")
        b = boot()
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual([(j["jobKey"], j["status"]) for j in b.pending_jobs()], [(key, "done")])
        self.assertIn("startedAt", b.pending_jobs()[0])     # closed as the observed print, not a bare recovery
        self.assertEqual(b.collect_queue_progress()[-1]["printerJobKey"], key)
        b.ack_jobs([key])
        c = boot()
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual(c.pending_jobs(), [])               # v0.68 re-sent it on every restart (the site deduped)

    def test_a_restart_mid_print_keeps_one_key_and_its_start(self):
        key, started = self.printing()
        b = boot()
        report(b, gcode_state="RUNNING", subtask_name="cube", task_id="0", mc_percent=60)
        self.assertEqual(b._jobs.active_key(), key)
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="0")
        (job,) = b.pending_jobs()
        self.assertEqual(job["jobKey"], key)
        self.assertEqual(job["startedAt"], time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(started))))

    def test_a_saved_print_the_printer_no_longer_shows_is_dropped_not_invented(self):
        key, _ = self.printing()
        b = boot()
        report(b, gcode_state="IDLE")                        # reset while the hub was down: the outcome is unknown
        self.assertEqual((b.pending_jobs(), b._jobs.active_key()), ([], None))
        b._queue_progress._start_timeout_sec = 0
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "outcome_unknown_after_restart", "assignmentSeq": 7}])
        key2, _ = self.printing()                            # and another file running after a restart is another print
        c = boot()
        report(c, gcode_state="RUNNING", subtask_name="other", task_id="0")
        self.assertNotIn(c._jobs.active_key(), (None, key2))

    @property
    def PATH(self):
        return bambu_module.TERMINAL_JOBS_DIR / "p1.json"   # read per test: conftest points it at the test's own dir

    @staticmethod
    def v068_jobs(path):
        """v0.68's JobTracker._load_pending, verbatim: what a rollback release reads from this file."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [j for j in raw if isinstance(j, dict) and isinstance(j.get("jobKey"), str)][-500:]

    def run_cube(self, t, at=1000.0, **extra):
        t.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", **extra}}, at)

    def test_the_terminal_file_keeps_the_v068_format_and_a_close_is_one_write(self):
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t)
        self.assertEqual(self.v068_jobs(self.PATH), [])                    # the saved print is not a job to v0.68
        t.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 2000.0)
        self.assertEqual(self.v068_jobs(self.PATH), t.pending())           # a rollback still reads exactly the jobs
        (state,) = [e["hubState"] for e in json.loads(self.PATH.read_text()) if "hubState" in e]
        self.assertEqual((state["active"], state["lastKey"]), (None, t.pending()[0]["jobKey"]))   # the same write cleared it
        u = JobTracker("p1", "S1", state_path=self.PATH)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 3000.0)
        self.assertEqual(len(u.pending()), 1)

    def test_a_print_that_ended_unseen_ends_at_the_printers_last_eta_not_when_the_hub_came_back(self):
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t, 1000.0, mc_remaining_time=120)                    # ETA 8200
        self.run_cube(t, 4600.0, mc_remaining_time=70)                     # slower than planned: ETA 8800
        on_disk = self.PATH.read_text()
        for at, left in ((4601.0, 70), (4660.0, 69), (4720.0, 68)):        # the estimate ticking down: no rewrite
            self.run_cube(t, at, mc_remaining_time=left)
        self.assertEqual(self.PATH.read_text(), on_disk)
        u = JobTracker("p1", "S1", state_path=self.PATH)                   # down at 4720, back at 30000 (an 8 h outage)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube", "mc_remaining_time": 0}}, 30000.0)
        (job,) = u.pending()
        self.assertEqual((job["startedAt"], job["endedAt"], job["printTimeSeconds"]), (_iso(1000.0), _iso(8800.0), 7800))
        # back BEFORE the ETA (it finished early): never an end in the hub's future
        v = JobTracker("p2", "S2", state_path=self.PATH.with_name("p2.json"))
        v.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", "mc_remaining_time": 600}}, 1000.0)
        w = JobTracker("p2", "S2", state_path=self.PATH.with_name("p2.json"))
        w.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 5000.0)
        self.assertEqual((w.pending()[0]["endedAt"], w.pending()[0]["printTimeSeconds"]), (_iso(5000.0), 4000))

    def test_with_no_eta_an_unseen_end_sends_no_duration_and_a_seen_end_is_exact(self):
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t)
        u = JobTracker("p1", "S1", state_path=self.PATH)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 30000.0)
        (job,) = u.pending()
        self.assertEqual(job["endedAt"], _iso(30000.0))                   # the site needs an end to bill; the hub has no better
        self.assertNotIn("printTimeSeconds", job)                          # but 29000 s of outage is not print time
        x = JobTracker("p3", "S3", state_path=self.PATH.with_name("p3.json"))
        x.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", "mc_remaining_time": 5}}, 1000.0)
        x.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 1900.0)   # the hub saw it end
        self.assertEqual((x.pending()[0]["endedAt"], x.pending()[0]["printTimeSeconds"]), (_iso(1900.0), 900))
        # the previous print's mc_remaining_time=0 still merged in on the first RUNNING frame is no ETA
        y = JobTracker("p4", "S4", state_path=self.PATH.with_name("p4.json"))
        y.observe({"print": {"gcode_state": "RUNNING", "subtask_name": "cube", "mc_remaining_time": 0}}, 1000.0)
        z = JobTracker("p4", "S4", state_path=self.PATH.with_name("p4.json"))
        z.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 30000.0)
        self.assertEqual(z.pending()[0]["endedAt"], _iso(30000.0))
        self.assertNotIn("printTimeSeconds", z.pending()[0])

    def test_a_same_file_print_running_long_past_the_saved_eta_is_not_taken_for_the_saved_print(self):
        # codex review: A (LAN, task "0") was saved with an hour to go; during an 8 h outage A finished and a member
        # started B of the same file. Booking B under A's key closed A's queue job with B and a nine-hour duration.
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t, 1000.0, mc_remaining_time=60)                    # ETA 4600
        key_a = t.active_key()
        u = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(u, 30000.0, mc_remaining_time=40)
        self.assertNotIn(u.active_key(), (None, key_a))                   # B is its own print
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 32400.0)
        self.assertNotIn(key_a, [j["jobKey"] for j in u.pending()])      # A is never closed with B's ending
        # a hotfix restart (any length short of the saved ETA + 30 min) keeps the running print
        v = JobTracker("p2", "S2", state_path=self.PATH.with_name("p2.json"))
        self.run_cube(v, 1000.0, mc_remaining_time=60)
        w = JobTracker("p2", "S2", state_path=self.PATH.with_name("p2.json"))
        self.run_cube(w, 1090.0, mc_remaining_time=59)
        self.assertEqual(w.active_key(), v.active_key())

    def test_a_long_pause_keeps_its_eta_current_so_a_restart_during_it_keeps_the_print(self):
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t, 1000.0, mc_remaining_time=30)                    # ETA 2800
        for at in range(1100, 20001, 100):                                # filament runout: paused for 5 h, hub up
            t.observe({"print": {"gcode_state": "PAUSE", "subtask_name": "cube", "mc_remaining_time": 25}}, float(at))
        u = JobTracker("p1", "S1", state_path=self.PATH)                  # the operator forces a hotfix mid-pause
        u.observe({"print": {"gcode_state": "PAUSE", "subtask_name": "cube", "mc_remaining_time": 25}}, 20090.0)
        self.assertEqual(u.active_key(), t.active_key())

    def test_a_failed_save_is_retried_by_the_next_report_not_forgotten(self):
        # codex review: one failed write was remembered as saved, so the unchanged reports after it never wrote again and a
        # restart onto FINISH lost the LAN print entirely
        t = JobTracker("p1", "S1", state_path=self.PATH)
        with mock.patch("makeros_hub.printers.jobs.os.replace", side_effect=OSError("disk full")):
            self.run_cube(t, 1000.0)
        self.assertFalse(self.PATH.exists())
        self.run_cube(t, 1001.0)                                          # the same report again: the disk is back
        key = t.active_key()
        u = JobTracker("p1", "S1", state_path=self.PATH)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 9000.0)
        self.assertEqual([j["jobKey"] for j in u.pending()], [key])

    def test_after_a_rollback_reported_the_print_a_later_upgrade_never_brings_it_back(self):
        # codex review: v0.69 saves cube; the operator forces the rollback (v0.68's tree), which reports cube and a later
        # cube under its own keys and gets them acked; a later upgrade boots onto that later cube still showing FINISH.
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t)
        jobs = self.v068_jobs(self.PATH)                                   # v0.68 boots: no saved print in its eyes
        jobs.append({"jobKey": "fp_v068_cube", "printerId": "p1", "status": "done", "endedAt": _iso(2000.0)})
        self.PATH.write_text(json.dumps(jobs))                             # v0.68's _save_pending: json.dump(pending)
        self.PATH.write_text(json.dumps([]))                               # its ack of both cubes
        u = JobTracker("p1", "S1", state_path=self.PATH)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 9000.0)
        self.assertEqual((u.pending(), u.active_key()), ([], None))

    def test_a_rollback_that_reported_nothing_leaves_the_saved_print_to_be_reported_once(self):
        t = JobTracker("p1", "S1", state_path=self.PATH)
        self.run_cube(t)
        key = t.active_key()
        # v0.68 booted onto cube already FINISH with no task id: it emits nothing and so never rewrites the file
        u = JobTracker("p1", "S1", state_path=self.PATH)
        u.observe({"print": {"gcode_state": "FINISH", "subtask_name": "cube"}}, 9000.0)
        self.assertEqual([j["jobKey"] for j in u.pending()], [key])


class TestCameraFairnessAtCapacity(unittest.TestCase):
    """codex review of d73f5c6: with strict "urgent first", wanted / first-layer RTSP cameras at or over the ff lane's
    capacity starved every mid-print camera for as long as they stayed wanted (hours, for a dirty idle plate)."""

    def test_mid_print_cameras_still_get_photos_when_wanted_ones_fill_the_lane(self):
        cam = lambda pid: {"printerId": pid, "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}   # noqa: E731
        wanted, mid = [f"want{i}" for i in range(6)], ["mid0", "mid1"]
        targets = [cam(p) for p in wanted + mid]
        status = {p: {"state": "idle"} for p in wanted} | {p: {"state": "printing", "progressPct": 50} for p in mid}
        s = CameraScheduler()
        got = {p: [] for p in wanted + mid}
        for beat in range(30):
            now = beat * 40.0
            for t in s.plan(targets, status, now, set(wanted))[:4]:     # the ff lane's ~4 captures a beat, all accepted
                s.attempted(t["printerId"], now)
                s.accepted(t["printerId"], now, None)
                got[t["printerId"]].append(beat)
        for pid, beats in got.items():
            gaps = [b - a for a, b in zip([0] + beats, beats + [29])]
            with self.subTest(pid=pid):
                self.assertLessEqual(max(gaps), 4, beats)                 # never starved: a photo at least every 4 beats
        self.assertGreaterEqual(min(len(got[p]) for p in mid), 8)

    def test_dead_cameras_on_printing_printers_do_not_crowd_out_live_ones(self):
        cam = lambda pid: {"printerId": pid, "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}   # noqa: E731
        dead, live = [f"dead{i}" for i in range(12)], ["live0", "live1"]
        targets = [cam(p) for p in dead + live]
        status = {p: {"state": "printing", "progressPct": 50} for p in dead + live}
        s = CameraScheduler()
        got = {p: [] for p in live}
        for beat in range(30):
            now = beat * 40.0
            for t in s.plan(targets, status, now, set())[:4]:           # every dead capture eats a slot for the beat
                pid = t["printerId"]
                s.attempted(pid, now)
                if pid in got:
                    s.accepted(pid, now, None)
                    got[pid].append(beat)
                else:
                    s.failed(pid, now)
        for pid, beats in got.items():
            gaps = [b - a for a, b in zip([0] + beats, beats + [29])]
            with self.subTest(pid=pid):
                self.assertLessEqual(max(gaps[1:]), 4, beats)               # the 90 s tier, give or take a beat
        self.assertGreater(s.breaker_open(30 * 40.0), 0)

    def test_a_state_change_that_lost_the_race_stays_due_until_answered(self):
        # codex review: a printer photographed at t=0 finishes at t=40; cameras with no photo take every start; at t=80
        # the change was consumed and the 600 s idle tier hid it until t=600
        s = CameraScheduler()
        cam = lambda p: {"printerId": p, "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}   # noqa: E731
        targets = [cam("done")] + [cam(f"new{i}") for i in range(4)]
        s.plan(targets[:1], {"done": {"state": "printing", "progressPct": 50}}, 0.0)
        s.accepted("done", 0.0, None)
        idle = {t["printerId"]: {"state": "idle"} for t in targets}
        self.assertEqual(s.plan(targets, idle, 40.0)[0]["printerId"], "done")   # the change goes first...
        s.attempted("new0", 40.0)                                         # ...but say it was not started this beat
        self.assertEqual(s.plan(targets, idle, 80.0)[0]["printerId"], "done")   # still due, still first
        s.accepted("done", 80.0, None)
        self.assertNotIn("done", [t["printerId"] for t in s.plan(targets, idle, 120.0)])
        s.plan(targets, {**idle, "done": {"state": "printing"}}, 160.0)
        s.failed("done", 160.0)                                           # a real failure answers it: the breaker ranks it now
        self.assertFalse(s._cams["done"].change)

    def test_a_breaker_after_a_thousand_failures_does_not_overflow(self):
        s = CameraScheduler()
        t = {"printerId": "p", "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}
        s.plan([t], {"p": {"state": "printing"}}, 0.0)
        for _ in range(1100):                                             # ~37 h at the 120 s printing cap
            s.failed("p", 0.0)
        self.assertEqual(s._cams["p"].retry_at, 120.0)

    def test_a_straggling_capture_is_never_doubled_by_the_next_beat(self):
        # codex review: workers outlive the beat, so each beat's fresh executor stacked another capture on a stuck camera
        gate, calls = threading.Event(), []

        def stuck(t):
            calls.append(t["printerId"])
            gate.wait(5)
            return (None, "timeout", "")

        s = CameraScheduler()
        t = [{"printerId": "pi", "vendor": "klipper", "cameraSnapshotUrl": "http://pi/snap"}]
        st = {"pi": {"state": "idle"}}
        frames, fails = camera.collect_camera_frames(t, st, s, 0.0, capture_with_reason=stuck, budget=0.2)
        self.assertEqual([f["reason"] for f in fails], ["timeout"])       # started, outlived the beat
        s._cams["pi"].streak, s._cams["pi"].retry_at = 0, 0.0             # say the breaker let it straight back
        self.assertEqual(camera.collect_camera_frames(t, st, s, 40.0, capture_with_reason=stuck, budget=0.2), ([], []))
        self.assertEqual((calls, s.stats["stillRunning"]), (["pi"], 1))
        gate.set()
        for _ in range(100):
            if not s.running:
                break
            time.sleep(0.01)
        camera.collect_camera_frames(t, st, s, 80.0, capture_with_reason=stuck, budget=0.2)
        self.assertEqual(calls, ["pi", "pi"])                             # free again once its worker returned

    def test_a_trickling_snapshot_times_out_on_the_whole_transfer(self):
        class Trickle:
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read1(self, _n=-1):
                time.sleep(0.02)
                return b"\xff"                                          # a byte at a time: no socket timeout ever fires

        t0 = time.monotonic()
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=Trickle()):
            got = camera._http_snapshot_with_reason("http://pi/snap", timeout=0.2)
        self.assertEqual(got[:2], (None, "timeout"))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_an_a1_connect_shares_one_deadline_across_addresses(self):
        # codex review: socket.create_connection gives each resolved address the whole timeout (two dead ones: 10 s).
        # The fakes sit under create_connection (getaddrinfo + the socket class), so its own address loop is what runs.
        class DeadSock:
            def __init__(self, *_a):
                self.t = None

            def settimeout(self, t):
                self.t = t

            def connect(self, _sa):
                time.sleep(self.t)
                raise socket.timeout()

            def close(self):
                pass

        def resolve(host, port, *_a, **_k):
            ips = ["10.0.0.1", "10.0.0.2"] if host == "printer.local" else [host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

        t0 = time.monotonic()
        with mock.patch.object(socket, "getaddrinfo", side_effect=resolve), mock.patch.object(socket, "socket", DeadSock):
            r = bambu_camera.capture_frame_with_reason("printer.local", "code", timeout=0.3)
        self.assertEqual(r.reason, "unreachable")
        self.assertLess(time.monotonic() - t0, 0.45)

    def test_cameras_that_reconnect_every_beat_never_starve_the_healthy_ones(self):
        # codex review of 02dc878: four flapping RTSP printers reset their breaker and took the top rank every beat
        cam = lambda pid: {"printerId": pid, "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}   # noqa: E731
        flap, healthy = [f"flap{i}" for i in range(4)], ["ok0", "ok1"]
        targets = [cam(p) for p in flap + healthy]
        s = CameraScheduler()
        got = {p: 0 for p in healthy}
        for beat in range(20):
            now = beat * 40.0
            status = {p: {"state": "printing", "progressPct": 50, "mqttConnectedAtMs": beat} for p in flap}
            status |= {p: {"state": "printing", "progressPct": 50, "mqttConnectedAtMs": 1} for p in healthy}
            for t in s.plan(targets, status, now, set())[:4]:
                pid = t["printerId"]
                s.attempted(pid, now)
                if pid in got:
                    s.accepted(pid, now, None)
                    got[pid] += 1
                else:
                    s.failed(pid, now)
        self.assertGreaterEqual(min(got.values()), 6, got)                 # ~ every 3 beats on the 90 s tier

    def test_a_failed_transition_capture_is_retried_next_beat_whatever_the_tier(self):
        # codex review of 1e116ed: the print ended at t=40, that capture failed once, and the 600 s idle tier hid the
        # promised next-beat retry until t=600
        s = CameraScheduler()
        t = [{"printerId": "p", "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}]
        s.plan(t, {"p": {"state": "printing", "progressPct": 50}}, 0.0)
        s.accepted("p", 0.0, None)
        idle = {"p": {"state": "idle"}}
        self.assertEqual(len(s.plan(t, idle, 40.0)), 1)
        s.attempted("p", 40.0)
        s.failed("p", 40.0)
        self.assertEqual(len(s.plan(t, idle, 80.0)), 1)                   # the one free miss: retried next beat
        s.attempted("p", 80.0)
        s.failed("p", 80.0)                                               # the second opens the breaker
        self.assertEqual(s.plan(t, idle, 120.0), [])
        self.assertEqual(len(s.plan(t, idle, 141.0)), 1)                  # and the breaker paces it, not the tier

    def test_a_reconnect_keeps_a_failing_cameras_retry(self):
        # codex review of 213d644: the reconnect reset the streak, and with it the failing camera's due-ness, until t=600
        s = CameraScheduler()
        t = [{"printerId": "p", "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"}]
        st = lambda state, link: {"p": {"state": state, "progressPct": 50, "mqttConnectedAtMs": link}}   # noqa: E731
        s.plan(t, st("printing", 1), 0.0)
        s.accepted("p", 0.0, None)
        for now in (40.0, 80.0):
            self.assertEqual(len(s.plan(t, st("idle", 1), now)), 1)
            s.attempted("p", now)
            s.failed("p", now)
        self.assertEqual(len(s.plan(t, st("idle", 2), 120.0)), 1)         # the power-cycle: retried at once
        s.attempted("p", 120.0)
        s.accepted("p", 120.0, None)
        self.assertEqual(s.plan(t, st("idle", 3), 160.0), [])             # a healthy camera's reconnect costs no capture
        for now in (200.0, 240.0):                                        # failing again (streak 2, breaker 60 s)
            s.plan(t, st("idle", 3), now)
            s.attempted("p", now)
            s.failed("p", now)
        s.plan(t, st("idle", 4), 280.0)                                   # reconnect; its retry loses the race...
        self.assertEqual(len(s.plan(t, st("idle", 5), 320.0)), 1)         # ...and a second reconnect keeps it armed

    def test_a_state_change_still_goes_first(self):
        s = CameraScheduler()
        targets = [{"printerId": p, "vendor": "bambu", "model": "X1C", "host": "h", "accessCode": "c"} for p in ("a", "b")]
        idle = {"a": {"state": "idle"}, "b": {"state": "idle"}}
        for p in ("a", "b"):
            s.plan(targets, idle, 0.0)
            s.accepted(p, 0.0, None)
        s.plan(targets, idle, 1.0)
        order = [t["printerId"] for t in s.plan(targets, {"a": {"state": "idle"}, "b": {"state": "printing"}}, 5000.0)]
        self.assertEqual(order, ["b", "a"])                               # both overdue; b's print start wins


class TestRestartMidDispatchNeverStartsTwice(_Dispatch):
    def resend(self, b, *, state="RUNNING"):
        """Hub process #2: the cloud re-sends the still-'assigned' job every beat until it hears 'printing'."""
        m = PrinterManager()
        m._adapters["p1"] = b
        if state:
            report(b, gcode_state=state, subtask_name="cube")
        return [r["state"] for r in m.dispatch_assignments([ASSIGNMENT], self.spool)]

    def test_died_after_recording_the_dispatch_before_the_start_left(self):
        a = boot()
        report(a, gcode_state="IDLE")

        def die(*_a):
            raise _Crash()

        a._client.publish = die
        with self.assertRaises(_Crash):
            self.start(a)
        b = boot()
        self.assertEqual(self.resend(b, state="IDLE"), ["uploading"])
        self.assertEqual((b._client.published, self.upload.call_count), ([], 1))
        b._queue_progress._start_timeout_sec = 0           # it never started: held for a human, never started again
        self.assertEqual(b.collect_queue_progress(),
                         [{"queueJobId": "q1", "state": "held", "reason": "start_not_observed", "assignmentSeq": 7}])

    def test_died_after_the_start_left_before_any_report_or_guard_was_durable(self):
        m = PrinterManager()
        a = boot()
        report(a, gcode_state="IDLE")
        m._adapters["p1"] = a

        def die(_report):
            raise _Crash()

        with self.assertRaises(_Crash):
            m.dispatch_assignments([ASSIGNMENT], self.spool, on_report=die)
        self.assertEqual(len(a._client.published), 1)
        for state in (None, "PREPARE", "RUNNING"):           # before its first report, heating, printing
            with self.subTest(state=state):
                b = boot()
                self.assertEqual(self.resend(b, state=state), ["uploading"])
                self.assertEqual(b._client.published, [])
        self.assertEqual(self.upload.call_count, 1)

    def test_a_dispatch_resolved_after_the_restart_is_not_started_again_by_the_same_beats_re_send(self):
        # codex review of a599650 (present at v0.68): the start left, the hub died before its 'uploading' report or guard,
        # the print finished during the outage; back up, the beat's collect resolved and forgot it, and the site's re-sent
        # assignment (still 'assigned' there) started it a second time
        m = PrinterManager()
        a = boot()
        report(a, gcode_state="IDLE")
        m._adapters["p1"] = a

        def die(_report):
            raise _Crash()

        with self.assertRaises(_Crash):
            m.dispatch_assignments([ASSIGNMENT], self.spool, on_report=die)
        b = boot()
        again = PrinterManager()
        again._adapters["p1"] = b
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")   # it ran and finished while the hub was down
        self.assertEqual([r["state"] for r in again.collect_queue_progress()], ["printing", "completed"])
        self.assertEqual([r["state"] for r in again.dispatch_assignments([ASSIGNMENT], self.spool)], ["uploading"])
        self.assertEqual((b._client.published, self.upload.call_count), ([], 1))
        newer = {**ASSIGNMENT, "assignmentSeq": 8}                  # a re-queued job (a new seq) still dispatches
        self.assertEqual([r["state"] for r in again.dispatch_assignments([newer], self.spool)], ["uploading"])
        self.assertEqual(len(b._client.published), 1)

    def test_a_second_crash_before_the_resolved_guard_never_restarts_the_print(self):
        # codex review of 708dff1: the recovery's collect persisted the popped dispatch, then the hub died before the
        # manager's guard write; on the next boot neither existed and the site's unchanged assignment started it again
        m = PrinterManager()
        a = boot()
        report(a, gcode_state="IDLE")
        m._adapters["p1"] = a

        def die(_report):
            raise _Crash()

        with self.assertRaises(_Crash):
            m.dispatch_assignments([ASSIGNMENT], self.spool, on_report=die)
        b = boot()
        again = PrinterManager()
        again._adapters["p1"] = b
        report(b, gcode_state="FINISH", subtask_name="cube", task_id="77")
        real = PrinterManager._remember_dispatched_queue_job
        calls = []

        def remember_then_die(self_, *a_, **k_):
            calls.append(a_)
            if len(calls) > 1:
                raise _Crash()                                # the guard after the adapter's pop: never reached durably
            return real(self_, *a_, **k_)

        with mock.patch.object(PrinterManager, "_remember_dispatched_queue_job", remember_then_die), \
                self.assertRaises(_Crash):
            again.collect_queue_progress(on_report=lambda r: None)
        c = boot()
        third = PrinterManager()
        third._adapters["p1"] = c
        report(c, gcode_state="FINISH", subtask_name="cube", task_id="77")
        self.assertEqual([r["state"] for r in third.dispatch_assignments([ASSIGNMENT], self.spool)], ["uploading"])
        self.assertEqual((c._client.published, self.upload.call_count), ([], 1))

    def test_died_after_the_outbox_before_the_manager_guard(self):
        m = PrinterManager()
        a = boot()
        report(a, gcode_state="IDLE")
        m._adapters["p1"] = a
        outbox = []
        with mock.patch.object(PrinterManager, "_save_dispatched", side_effect=_Crash()), self.assertRaises(_Crash):
            m.dispatch_assignments([ASSIGNMENT], self.spool, on_report=outbox.append)
        self.assertEqual([r["state"] for r in outbox], ["uploading"])
        b = boot()
        self.assertEqual(self.resend(b), ["uploading"])
        self.assertEqual((len(a._client.published), b._client.published, self.upload.call_count), (1, [], 1))

    def test_a_reconnect_forgets_the_cached_state_until_the_printer_reports_again(self):
        # codex review: _data survives a reconnect, so a cached IDLE let a start through while the printer could have begun
        # a print at its own screen during the drop
        a = boot()
        report(a, gcode_state="IDLE")
        a._on_disconnect(None, None, 0)
        a._on_connect(a._client, None, None, 0)
        self.assertEqual(self.start(a), {"ok": False, "reason": "printer_stale: no printer state reported since the hub connected"})
        report(a, mc_percent=3)                                             # a partial report without a state is not enough
        self.assertFalse(self.start(a)["ok"])
        self.assertEqual(self.upload.call_count, 0)
        report(a, gcode_state="IDLE")
        self.assertEqual(self.start(a), {"ok": True})

    def test_a_fresh_busy_report_after_the_managers_check_still_stops_the_start(self):
        # codex re-review: the manager passed the cached IDLE, then the first fresh report (RUNNING, started at the screen
        # during the drop) set the freshness flag before start_print checked it
        a = boot()
        report(a, gcode_state="IDLE")
        a._on_disconnect(None, None, 0)
        a._on_connect(a._client, None, None, 0)
        report(a, gcode_state="RUNNING", subtask_name="walk-up")
        self.assertEqual(self.start(a), {"ok": False, "reason": "printer_busy"})
        self.assertEqual(self.upload.call_count, 0)
        b = boot()                                             # and a report landing DURING the upload
        report(b, gcode_state="IDLE")
        self.upload.side_effect = lambda *_a, **_k: report(b, gcode_state="PREPARE", subtask_name="walk-up")
        self.assertEqual(self.start(b), {"ok": False, "reason": "printer_busy"})
        self.assertEqual((b._client.published, b.pending_queue_jobs()), ([], []))   # nothing sent, no dispatch left behind

    def test_a_freshly_connected_printer_that_has_not_reported_is_never_started(self):
        a = boot()                                           # paho says connected; the printer's first report is not in
        self.assertEqual(self.start(a), {"ok": False, "reason": "printer_stale: no printer state reported since the hub connected"})
        self.assertEqual((a._client.published, self.upload.call_count, a.pending_queue_jobs()), ([], 0, []))


class TestCommandsSurviveARestart(unittest.TestCase):
    def test_a_redelivered_command_is_answered_without_running_it_again(self):
        calls = []

        class Printer:
            def send_command(self, command, params):
                calls.append(command)
                return {"ok": True}

        cmd = [{"requestId": "r1", "printerId": "p1", "command": "pause"}]
        m = PrinterManager()
        m._adapters["p1"] = Printer()
        self.assertEqual(m.dispatch_commands(cmd), [{"requestId": "r1", "command": "pause", "status": "ok"}])
        again = PrinterManager()                         # restarted; the result was lost; the site re-sends after 10 min
        again._adapters["p1"] = Printer()
        self.assertEqual(again.dispatch_commands(cmd), [{"requestId": "r1", "command": "pause", "status": "ok"}])
        self.assertEqual(calls, ["pause"])
        stale = json.loads(manager_module.DISPATCHED_COMMANDS_PATH.read_text())
        stale["r1"] -= manager_module.DISPATCHED_COMMANDS_TTL_SEC + 1   # past the site's hour, the row is closed there
        manager_module.DISPATCHED_COMMANDS_PATH.write_text(json.dumps(stale))
        self.assertEqual(PrinterManager()._dispatched_commands, {})

    @staticmethod
    def one_beat(post, *extra):
        """agent.run for one heartbeat: `post` answers it and sets the stop event it is given."""
        stop = threading.Event()

        class Ingest:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class VPs:
            def __init__(self, *a, **k):
                pass

            def current_model(self):
                return None

            def stop_sync(self):
                pass

        with mock.patch.object(agent, "read_credential", return_value="cred"), \
                mock.patch.object(agent, "_pull_config", return_value=None), \
                mock.patch.object(agent, "IngestServer", Ingest), mock.patch.object(agent, "VirtualPrinterManager", VPs), \
                mock.patch.object(agent, "tailscale_binary_exists", return_value=False), \
                mock.patch("makeros_hub.discovery.maybe_run_periodic_scan", return_value=False), \
                mock.patch("makeros_hub.discovery.get_cached_hits", return_value=[]), \
                mock.patch.object(agent, "post_json", side_effect=lambda url, body, **kw: post(url, body, stop)):
            for p in extra:
                p.start()
            try:
                agent.run(Config(cloud_url="https://host.example", heartbeat_sec=1), _stop_event=stop, _install_signals=False,
                          _outbox=mock.Mock(load_all=lambda: []))
            finally:
                for p in extra:
                    p.stop()

    OK = {"hubId": "h", "configVersion": "v", "nextPollMs": 30000, "applied": 0}

    def test_results_waiting_for_a_heartbeat_are_sent_after_a_restart_and_cleared_on_its_200(self):
        report_outbox.save([{"requestId": "r9", "command": "ams_dry", "status": "ok"}], report_outbox.COMMAND_RESULTS_PATH)
        self.assertEqual(report_outbox.load(report_outbox.COMMAND_RESULTS_PATH), [])   # a command row is not a queue report
        bodies = []

        def post(url, body, stop):
            bodies.append(body)
            stop.set()
            return http.Response(status=200, body=self.OK)

        self.one_beat(post)
        self.assertEqual(bodies[0]["commandResults"], [{"requestId": "r9", "command": "ams_dry", "status": "ok"}])
        self.assertEqual(report_outbox.load(report_outbox.COMMAND_RESULTS_PATH, report_outbox.COMMAND_KEYS), [])

    def test_a_backlog_past_one_heartbeat_keeps_what_was_not_sent(self):
        # codex review (present at v0.68.0 too): 257 terminal reports after an outage, 256 posted, all 257 acked -> the
        # 257th deleted unsent, a print never billed
        backlog = [{"jobKey": f"k{i}", "printerId": "p1", "status": "done", "endedAt": "2026-10-15T00:00:00+00:00"} for i in range(257)]
        acked, sent = [], []

        def post(url, body, stop):
            sent.extend(j["jobKey"] for j in body.get("jobs", []))
            stop.set()
            return http.Response(status=200, body=self.OK)

        self.one_beat(post, mock.patch.object(PrinterManager, "pending_jobs", return_value=backlog),
                      mock.patch.object(PrinterManager, "ack_jobs", side_effect=acked.extend))
        self.assertEqual(len(sent), 256)
        self.assertEqual(acked, sent)                                       # k256 stays buffered for the next beat

if __name__ == "__main__":
    unittest.main()
