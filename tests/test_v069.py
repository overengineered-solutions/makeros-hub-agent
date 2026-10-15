"""v0.69: the heartbeat's camera additions, the operator's forced update, and nothing lost on a mid-print restart.

The restart tests build a NEW adapter / manager on the same state directories (tests/conftest.py gives each test its
own), which is exactly what a hub restart is: a new process reading what the old one wrote. Fakes only — no printer,
no site, no network."""

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub import agent, http, report_outbox
from makeros_hub.config import Config
from makeros_hub.printers import bambu as bambu_module, bambu_send, manager as manager_module
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

    def test_results_waiting_for_a_heartbeat_are_sent_after_a_restart_and_cleared_on_its_200(self):
        report_outbox.save([{"requestId": "r9", "command": "ams_dry", "status": "ok"}], report_outbox.COMMAND_RESULTS_PATH)
        self.assertEqual(report_outbox.load(report_outbox.COMMAND_RESULTS_PATH), [])   # a command row is not a queue report
        bodies = []
        stop = threading.Event()

        def post(url, body, **_kw):
            bodies.append(body)
            stop.set()
            return http.Response(status=200, body={"hubId": "h", "configVersion": "v", "nextPollMs": 30000, "applied": 0})

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
                mock.patch.object(agent, "post_json", side_effect=post):
            agent.run(Config(cloud_url="https://host.example", heartbeat_sec=1), _stop_event=stop, _install_signals=False,
                      _outbox=mock.Mock(load_all=lambda: []))
        self.assertEqual(bodies[0]["commandResults"], [{"requestId": "r9", "command": "ams_dry", "status": "ok"}])
        self.assertEqual(report_outbox.load(report_outbox.COMMAND_RESULTS_PATH, report_outbox.COMMAND_KEYS), [])


if __name__ == "__main__":
    unittest.main()
