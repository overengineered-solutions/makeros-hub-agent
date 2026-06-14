"""Stdlib-only tests (unittest, so the repo has zero test deps either).

  python3 -m unittest discover -s tests
"""

import hashlib
import json
import queue
import signal
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from makeros_hub import diagnostics, http
from makeros_hub.agent import (
    QUEUE_STATUS_DROP,
    QUEUE_STATUS_RETRY,
    QUEUE_STATUS_SENT,
    _TailscaleRuntimeState,
    _VirtualPrinterRuntimeState,
    _drain_vprinter_submissions,
    _enqueue_vprinter_submission,
    _flush_queue_status_reports,
    _install_shutdown_signal_handlers,
    _make_vprinter_capture_handler,
    _maybe_retry_tailscale_config,
    _maybe_retry_vprinter_config,
    _pull_config,
    _rehydrate_vprinter_submissions,
    _reconcile_tailscale_config,
    _request_shutdown,
    _restore_shutdown_signal_handlers,
    _run_pending_probes,
    heartbeat_payload,
    make_queue_status_reporter,
    run,
)
from makeros_hub.config import Config
from makeros_hub.vprinter.capture import CapturedJob


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _captured_job(uid: str = "uid-1", filename: str = "part.3mf") -> CapturedJob:
    return CapturedJob(
        member_id="member-1",
        filename=filename,
        file_path=Path(filename),
        sha256="a" * 64,
        size=123,
        ams_mapping=[0],
        use_ams=True,
        required_filaments=[{"slot": 0, "type": "PLA"}],
        submitted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
        submission_uid=uid,
    )


class _FakeVPrinterOutbox:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.persisted = []
        self.removed = []
        self.load_count = 0

    def persist(self, job):
        self.persisted.append(job)

    def remove(self, submission_uid):
        self.removed.append(submission_uid)

    def load_all(self):
        self.load_count += 1
        return list(self.jobs)


class TestHttpParse(unittest.TestCase):
    def test_parses_json_object(self):
        r = http._parse(200, b'{"ok": true, "hubId": "h1"}')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["hubId"], "h1")

    def test_empty_body_is_empty_dict(self):
        self.assertEqual(http._parse(200, b"").body, {})

    def test_non_json_raises(self):
        with self.assertRaises(http.TransportError):
            http._parse(200, b"<html>not json</html>")

    def test_json_array_raises(self):
        with self.assertRaises(http.TransportError):
            http._parse(200, b"[1, 2, 3]")


class TestConfigUrls(unittest.TestCase):
    def test_endpoint_urls(self):
        cfg = Config(cloud_url="https://host.example/")
        self.assertEqual(cfg.enroll_url, "https://host.example/api/print/hub/enroll")
        self.assertEqual(cfg.heartbeat_url, "https://host.example/api/print/hub/heartbeat")
        self.assertEqual(cfg.config_url, "https://host.example/api/print/hub/config")
        self.assertEqual(cfg.queue_status_url, "https://host.example/api/print/hub/queue-status")
        self.assertEqual(cfg.vp_submit_url, "https://host.example/api/print/hub/vp-submit")


class TestHeartbeatPayload(unittest.TestCase):
    def test_shape(self):
        p = heartbeat_payload()
        # The contract the cloud Zod-parses: liveness self-report + empty
        # printer/job arrays (the printer adapter fills these in PR 5).
        for key in ("agentVersion", "os", "hostname", "uptimeSec", "printers", "jobs"):
            self.assertIn(key, p)
        self.assertEqual(p["printers"], [])
        self.assertEqual(p["jobs"], [])

    def test_tailscale_fields_omit_absent_values(self):
        p = heartbeat_payload(
            tailscale_status={
                "tailscaleIp": "100.64.0.10",
                "tailscaleHostname": "hub-one",
                "tailscaleStatus": "connected",
                "tailscaleStatusReason": None,
            }
        )
        self.assertEqual(p["tailscaleIp"], "100.64.0.10")
        self.assertEqual(p["tailscaleHostname"], "hub-one")
        self.assertEqual(p["tailscaleStatus"], "connected")
        self.assertNotIn("tailscaleStatusReason", p)

    def test_probe_results_are_included_when_present(self):
        p = heartbeat_payload(
            probe_results=[
                {
                    "requestId": "r1",
                    "name": "disk-free",
                    "status": "ok",
                    "exitCode": 0,
                    "output": "ok",
                    "durationMs": 1,
                    "truncated": False,
                }
            ]
        )

        self.assertEqual(p["probeResults"][0]["requestId"], "r1")
        self.assertEqual(p["probeResults"][0]["name"], "disk-free")

    def test_pending_probe_response_becomes_next_payload(self):
        runner = mock.Mock(
            return_value={
                "name": "disk-free",
                "status": "ok",
                "exitCode": 0,
                "output": "ok",
                "durationMs": 1,
                "truncated": False,
            }
        )

        results = _run_pending_probes([{"requestId": "r1", "name": "disk-free"}], runner=runner)
        payload = heartbeat_payload(probe_results=results)

        runner.assert_called_once_with("disk-free")
        self.assertEqual(payload["probeResults"][0]["requestId"], "r1")
        self.assertEqual(payload["probeResults"][0]["status"], "ok")

    def test_pending_probe_validation_ignores_malformed_and_unknown(self):
        runner = mock.Mock()

        results = _run_pending_probes(
            [
                {"requestId": "bad1", "name": "; rm -rf /"},
                {"requestId": "bad2", "name": 123},
                "bad3",
            ],
            runner=runner,
        )

        self.assertEqual(results, [])
        runner.assert_not_called()

    def test_probe_runner_exception_does_not_break_heartbeat_payload(self):
        secret = "tskey-auth-secret"

        def runner(_name):
            raise RuntimeError(f"boom {secret}")

        results = _run_pending_probes([{"requestId": "r1", "name": "disk-free"}], runner=runner)
        payload = heartbeat_payload(probe_results=results)

        self.assertEqual(payload["probeResults"][0]["status"], "error")
        self.assertNotIn(secret, json.dumps(payload))
        self.assertIn("[redacted]", json.dumps(payload))


class TestConfigDownTailscale(unittest.TestCase):
    def test_pull_config_reconciles_tailscale_block(self):
        class Manager:
            config_version = None

            def __init__(self):
                self.reconciled = None

            def reconcile(self, printers, version):
                self.reconciled = (printers, version)
                self.config_version = version

            def statuses(self):
                return []

        cfg = Config(cloud_url="https://host.example")
        manager = Manager()
        tailscale_cfg = {"enabled": False}
        tailscale_status = {
            "tailscaleIp": None,
            "tailscaleHostname": None,
            "tailscaleStatus": "disabled",
            "tailscaleStatusReason": None,
        }
        reconciler = mock.Mock(return_value=tailscale_status)
        with mock.patch(
            "makeros_hub.agent.get_json",
            return_value=http.Response(200, {"printers": [], "version": "v1", "tailscale": tailscale_cfg}),
        ):
            returned = _pull_config(cfg, "cred", manager, tailscale_reconciler=reconciler)

        self.assertEqual(manager.reconciled, ([], "v1"))
        reconciler.assert_called_once_with(tailscale_cfg)
        self.assertEqual(returned, tailscale_status)

    def test_pull_config_reconciler_raise_returns_sanitized_error(self):
        class Manager:
            config_version = None

            def reconcile(self, printers, version):
                self.config_version = version

            def statuses(self):
                return []

        key = "tskey-secret"
        tailscale_cfg = {"enabled": True, "authKey": key, "hostname": "hub-one"}

        def reconciler(_tailscale_cfg):
            raise RuntimeError(f"boom {key}")

        cfg = Config(cloud_url="https://host.example")
        with mock.patch(
            "makeros_hub.agent.get_json",
            return_value=http.Response(200, {"printers": [], "version": "v1", "tailscale": tailscale_cfg}),
        ), self.assertLogs("makeros-hub", level="ERROR") as logs:
            returned = _pull_config(cfg, "cred", Manager(), tailscale_reconciler=reconciler)

        self.assertEqual(returned["tailscaleStatus"], "error")
        self.assertEqual(returned["tailscaleStatusReason"], "tailscale reconcile failed")
        self.assertNotIn(key, json.dumps(returned))
        self.assertNotIn(key, "\n".join(logs.output))
        payload = heartbeat_payload(tailscale_status=returned)
        self.assertNotIn(key, json.dumps(payload))

    def test_reconcile_tailscale_config_sanitizes_returned_reason(self):
        key = "tskey-secret"

        def reconciler(_tailscale_cfg):
            return {
                "tailscaleIp": None,
                "tailscaleHostname": None,
                "tailscaleStatus": "error",
                "tailscaleStatusReason": f"bad auth {key}",
            }

        with self.assertLogs("makeros-hub", level="ERROR") as logs:
            returned = _reconcile_tailscale_config(
                {"enabled": True, "authKey": key},
                reconciler,
            )

        self.assertIn("[redacted]", returned["tailscaleStatusReason"])
        self.assertNotIn(key, json.dumps(returned))
        self.assertNotIn(key, "\n".join(logs.output))

    def test_enabled_not_connected_retries_on_later_beat_with_backoff(self):
        state = _TailscaleRuntimeState()
        state.remember_config({"enabled": True, "authKey": "tskey-secret", "hostname": "hub-one"})
        state.record_reconcile_status({"tailscaleStatus": "joining"}, now=0)
        reconciler = mock.Mock(return_value={"tailscaleStatus": "joining"})

        self.assertIsNone(_maybe_retry_tailscale_config(state, 29, reconciler))
        self.assertEqual(reconciler.call_count, 0)

        returned = _maybe_retry_tailscale_config(state, 30, reconciler)

        self.assertEqual(returned["tailscaleStatus"], "joining")
        reconciler.assert_called_once_with(state.config)
        self.assertEqual(state.next_retry_at, 90)
        self.assertLessEqual(state.retry_delay_sec, 300)


class TestVirtualPrinterStartRetry(unittest.TestCase):
    def test_enabled_start_failure_retries_without_config_change(self):
        cfg = mock.Mock()
        state = _VirtualPrinterRuntimeState()

        class Manager:
            def __init__(self):
                self.calls = 0
                self.model = None

            def reconcile_sync(self, config):
                self.calls += 1
                self.reconciled = config
                if self.calls == 2:
                    self.model = "N1"

            def current_model(self):
                return self.model

        manager = Manager()
        state.remember_config(cfg)
        state.record_reconcile_attempt(manager.current_model(), now=0)

        self.assertFalse(_maybe_retry_vprinter_config(state, manager, now=29))
        self.assertEqual(manager.calls, 0)

        self.assertTrue(_maybe_retry_vprinter_config(state, manager, now=30))
        self.assertEqual(manager.calls, 1)
        self.assertIs(manager.reconciled, cfg)

        self.assertTrue(_maybe_retry_vprinter_config(state, manager, now=90))
        self.assertEqual(manager.calls, 2)
        self.assertEqual(manager.current_model(), "N1")
        self.assertEqual(state.next_retry_at, 0.0)

    def test_disabled_vprinter_config_does_not_arm_retry(self):
        state = _VirtualPrinterRuntimeState()
        # A disabled config block (non-None) must NOT arm the retry timer — else
        # needs_retry() (keyed on "current_model is None") would re-reconcile a
        # deliberately-stopped VP every heartbeat.
        state.remember_config(mock.Mock(enabled=False))
        self.assertIsNone(state.config)
        self.assertFalse(state.needs_retry(current_model=None))
        self.assertFalse(state.retry_due(now=10_000.0, current_model=None))


class TestConfigDownVirtualPrinter(unittest.TestCase):
    def test_pull_config_reconciles_virtual_printer_block(self):
        class Manager:
            config_version = None

            def reconcile(self, printers, version):
                self.config_version = version

            def statuses(self):
                return []

        class VirtualPrinterManager:
            def __init__(self):
                self.reconciled = None

            def reconcile_sync(self, config):
                self.reconciled = config

        vp_manager = VirtualPrinterManager()
        cfg = Config(cloud_url="https://host.example")
        with mock.patch(
            "makeros_hub.agent.get_json",
            return_value=http.Response(
                200,
                {
                    "printers": [],
                    "version": "v1",
                    "virtualPrinter": {
                        "enabled": True,
                        "serial": "SER123",
                        "model": "N1",
                        "name": "VP A1",
                        "fw": "01.08.00.00",
                        "bindIp": "100.64.0.10",
                        "members": [
                            {
                                "accessCodeSha256": _code_hash("12345678"),
                                "memberId": "m1",
                            }
                        ],
                        "pool": [{"material": "PLA", "color": "FFFFFFFF"}],
                    },
                },
            ),
        ):
            _pull_config(
                cfg,
                "cred",
                Manager(),
                tailscale_reconciler=mock.Mock(return_value={"tailscaleStatus": "disabled"}),
                virtual_printer_manager=vp_manager,
            )

        self.assertEqual(vp_manager.reconciled.serial, "SER123")
        self.assertEqual(vp_manager.reconciled.members[0].member_id, "m1")
        self.assertEqual(vp_manager.reconciled.members[0].access_code_sha256, _code_hash("12345678"))

    def test_pull_config_disables_virtual_printer_when_block_absent(self):
        class Manager:
            config_version = None

            def reconcile(self, printers, version):
                self.config_version = version

            def statuses(self):
                return []

        vp_manager = mock.Mock()
        cfg = Config(cloud_url="https://host.example")
        with mock.patch(
            "makeros_hub.agent.get_json",
            return_value=http.Response(200, {"printers": [], "version": "v1"}),
        ):
            _pull_config(
                cfg,
                "cred",
                Manager(),
                tailscale_reconciler=mock.Mock(return_value={"tailscaleStatus": "disabled"}),
                virtual_printer_manager=vp_manager,
            )

        vp_manager.reconcile_sync.assert_called_once_with(None)

    def test_pull_config_accepts_snake_case_virtual_printer_fallback(self):
        class Manager:
            config_version = None

            def reconcile(self, printers, version):
                self.config_version = version

            def statuses(self):
                return []

        vp_manager = mock.Mock()
        cfg = Config(cloud_url="https://host.example")
        with mock.patch(
            "makeros_hub.agent.get_json",
            return_value=http.Response(
                200,
                {
                    "printers": [],
                    "version": "v1",
                    "virtual_printer": {
                        "enabled": True,
                        "serial": "SER123",
                        "model": "N1",
                        "name": "VP A1",
                        "fw": "01.08.00.00",
                        "bind_ip": "100.64.0.10",
                        "members": [
                            {
                                "access_code_sha256": _code_hash("12345678"),
                                "member_id": "m1",
                            }
                        ],
                    },
                },
            ),
        ):
            _pull_config(
                cfg,
                "cred",
                Manager(),
                tailscale_reconciler=mock.Mock(return_value={"tailscaleStatus": "disabled"}),
                virtual_printer_manager=vp_manager,
            )

        reconciled = vp_manager.reconcile_sync.call_args.args[0]
        self.assertEqual(reconciled.serial, "SER123")
        self.assertEqual(reconciled.members[0].member_id, "m1")


class TestQueueStatusOutbox(unittest.TestCase):
    def test_flush_drops_deterministic_4xx_and_continues(self):
        reports = [
            {"queueJobId": "q1", "state": "completed"},
            {"queueJobId": "q2", "state": "completed"},
            {"queueJobId": "q3", "state": "completed"},
        ]

        def reporter(report):
            if report["queueJobId"] == "q2":
                return QUEUE_STATUS_DROP
            return QUEUE_STATUS_SENT

        self.assertEqual(_flush_queue_status_reports(reporter, reports), [])

    def test_flush_retains_5xx_retry_and_later_reports(self):
        reports = [
            {"queueJobId": "q1", "state": "completed"},
            {"queueJobId": "q2", "state": "completed"},
            {"queueJobId": "q3", "state": "completed"},
        ]

        def reporter(report):
            if report["queueJobId"] == "q2":
                return QUEUE_STATUS_RETRY
            return QUEUE_STATUS_SENT

        self.assertEqual(_flush_queue_status_reports(reporter, reports), reports[1:])

    def test_reporter_drops_4xx_and_retries_5xx_or_transport(self):
        cfg = Config(cloud_url="https://host.example")
        reporter = make_queue_status_reporter(cfg, "cred")
        report = {"queueJobId": "q1", "state": "held", "reason": "bad_assignment"}

        with mock.patch(
            "makeros_hub.agent.post_json",
            return_value=http.Response(409, {"error": "bad transition"}),
        ):
            self.assertEqual(reporter(report), QUEUE_STATUS_DROP)
        with mock.patch(
            "makeros_hub.agent.post_json",
            return_value=http.Response(503, {"error": "try later"}),
        ):
            self.assertEqual(reporter(report), QUEUE_STATUS_RETRY)
        with mock.patch(
            "makeros_hub.agent.post_json",
            side_effect=http.TransportError("network down"),
        ):
            self.assertEqual(reporter(report), QUEUE_STATUS_RETRY)


class TestVirtualPrinterSubmissionOutbox(unittest.TestCase):
    def test_capture_handler_persists_before_enqueue(self):
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        observed_qsizes = []

        class FakeOutbox(_FakeVPrinterOutbox):
            def persist(self, persisted_job):
                observed_qsizes.append(q.qsize())
                super().persist(persisted_job)

        outbox = FakeOutbox()
        handler = _make_vprinter_capture_handler(q, outbox)

        handler(job)

        self.assertEqual(observed_qsizes, [0])
        self.assertEqual(outbox.persisted, [job])
        self.assertIs(q.get_nowait(), job)

    def test_capture_handler_persist_failure_still_enqueues(self):
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()

        class FailingOutbox(_FakeVPrinterOutbox):
            def persist(self, persisted_job):
                raise OSError("disk full")

        handler = _make_vprinter_capture_handler(q, FailingOutbox())
        # A durability-write failure must NEVER drop the job — it falls back to
        # the in-memory queue so it still submits this session.
        handler(job)
        self.assertIs(q.get_nowait(), job)

    def test_rehydrate_enqueues_loaded_outbox_jobs(self):
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        jobs = [_captured_job("uid-1", "one.3mf"), _captured_job("uid-2", "two.3mf")]
        outbox = _FakeVPrinterOutbox(jobs)

        count = _rehydrate_vprinter_submissions(q, outbox)

        self.assertEqual(count, 2)
        self.assertEqual(outbox.load_count, 1)
        self.assertEqual([job.submission_uid for job in list(q.queue)], ["uid-1", "uid-2"])

    def test_drain_ok_true_removes_job(self):
        cfg = Config(cloud_url="https://host.example/")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)
        calls = []

        def poster(url, payload, *, bearer, timeout):
            calls.append((url, payload, bearer, timeout))
            return http.Response(200, {"ok": True, "jobId": "job-1", "state": "queued"})

        submitted = _drain_vprinter_submissions(
            q,
            cfg,
            "cred",
            model="3DPrinter-X1-Carbon",
            outbox=outbox,
            poster=poster,
        )

        self.assertEqual(submitted, 1)
        self.assertTrue(q.empty())
        self.assertEqual(calls[0][0], "https://host.example/api/print/hub/vp-submit")
        self.assertEqual(calls[0][2], "cred")
        # Short per-submit timeout so a slow cloud can't stall the heartbeat.
        self.assertEqual(calls[0][3], 8)
        self.assertEqual(calls[0][1]["hubSubmissionUid"], job.submission_uid)
        self.assertEqual(calls[0][1]["memberId"], "member-1")
        self.assertEqual(calls[0][1]["printerModel"], "3DPrinter-X1-Carbon")
        self.assertEqual(outbox.removed, [job.submission_uid])

    def test_drain_ok_false_rejected_is_terminal(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(200, {"ok": False, "state": "rejected", "reason": "not eligible"})

        submitted = _drain_vprinter_submissions(
            q,
            cfg,
            "cred",
            model="N1",
            outbox=outbox,
            poster=poster,
        )

        self.assertEqual(submitted, 0)
        self.assertTrue(q.empty())
        self.assertEqual(outbox.removed, [job.submission_uid])

    def test_drain_transport_error_reenqueues_and_records_vprinter_diagnostic(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        diag = diagnostics.Diagnostics(enable_network=False)
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            raise http.TransportError("network down")

        submitted = _drain_vprinter_submissions(
            q,
            cfg,
            "cred",
            model="N1",
            diagnostics=diag,
            outbox=outbox,
            poster=poster,
        )

        self.assertEqual(submitted, 0)
        retried = q.get_nowait()
        self.assertEqual(retried.submission_uid, job.submission_uid)
        self.assertEqual(retried.attempts, 1)
        self.assertEqual(outbox.persisted, [retried])
        self.assertEqual(outbox.removed, [])
        self.assertIn("vp-submit transport", diag.errors.snapshot()["vprinter"]["message"])

    def test_drain_deterministic_400_deadletters_without_reenqueue(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(400, {"error": "payload_shape_mismatch"})

        with self.assertLogs("makeros-hub", level="WARNING") as logs:
            _drain_vprinter_submissions(
                q,
                cfg,
                "cred",
                model="N1",
                outbox=outbox,
                poster=poster,
            )

        self.assertTrue(q.empty())
        self.assertEqual(outbox.removed, [job.submission_uid])
        self.assertIn("vprinter.submit.deadletter", "\n".join(logs.output))

    def test_drain_recoverable_4xx_retries_not_deadletter(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(401, {"error": "unauthorized"})

        _drain_vprinter_submissions(q, cfg, "cred", model="N1", poster=poster)
        # 401 is a recoverable hub-auth CONTEXT error (e.g. a rotated credential),
        # not a job-intrinsic rejection — retry under the cap, never lose the job.
        retried = q.get_nowait()
        self.assertEqual(retried.submission_uid, job.submission_uid)
        self.assertEqual(retried.attempts, 1)

    def test_drain_503_retries_with_attempt_increment(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(503, {"error": "try later"})

        _drain_vprinter_submissions(q, cfg, "cred", model="N1", outbox=outbox, poster=poster)

        retried = q.get_nowait()
        self.assertEqual(retried.submission_uid, job.submission_uid)
        self.assertEqual(retried.attempts, 1)
        self.assertEqual(outbox.persisted, [retried])
        self.assertEqual(outbox.removed, [])

    def test_drain_max_attempts_deadletters_retryable_failure(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = replace(_captured_job(), attempts=9)
        outbox = _FakeVPrinterOutbox()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(503, {"error": "try later"})

        with self.assertLogs("makeros-hub", level="WARNING") as logs:
            _drain_vprinter_submissions(
                q,
                cfg,
                "cred",
                model="N1",
                outbox=outbox,
                poster=poster,
            )

        self.assertTrue(q.empty())
        self.assertEqual(outbox.removed, [job.submission_uid])
        self.assertIn("vprinter.submit.deadletter", "\n".join(logs.output))

    def test_enqueue_full_queue_drops_oldest(self):
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=2)
        _enqueue_vprinter_submission(q, _captured_job("uid-1", "one.3mf"))
        _enqueue_vprinter_submission(q, _captured_job("uid-2", "two.3mf"))
        _enqueue_vprinter_submission(q, _captured_job("uid-3", "three.3mf"))

        self.assertEqual([job.submission_uid for job in list(q.queue)], ["uid-2", "uid-3"])

    def test_drain_without_active_model_drops_pending_jobs(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        _enqueue_vprinter_submission(q, _captured_job())

        _drain_vprinter_submissions(q, cfg, "cred", model=None)

        self.assertTrue(q.empty())


class TestRunLoopShutdown(unittest.TestCase):
    def test_signal_handlers_set_shutdown_event_and_restore_previous_handlers(self):
        stop_event = threading.Event()
        installed = {}
        previous_handlers = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }

        def fake_signal(sig, handler):
            installed[sig] = handler

        with mock.patch("makeros_hub.agent.signal.getsignal", side_effect=previous_handlers.get), mock.patch(
            "makeros_hub.agent.signal.signal", side_effect=fake_signal
        ):
            previous = _install_shutdown_signal_handlers(stop_event)
            installed[signal.SIGTERM](signal.SIGTERM, None)
            _restore_shutdown_signal_handlers(previous)

        self.assertTrue(stop_event.is_set())
        self.assertEqual(previous, previous_handlers)
        self.assertIs(installed[signal.SIGTERM], previous_handlers[signal.SIGTERM])
        self.assertIs(installed[signal.SIGINT], previous_handlers[signal.SIGINT])

    def test_heartbeat_transport_error_still_invokes_vprinter_drain(self):
        stop_event = threading.Event()
        drains = []

        class FakeIngest:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class FakeVirtualPrinterManager:
            def __init__(self, *args, **kwargs):
                pass

            def current_model(self):
                return "N1"

            def stop_sync(self):
                pass

        def drain(*_args, **kwargs):
            drains.append(kwargs.get("model"))
            stop_event.set()
            return 0

        with mock.patch("makeros_hub.agent.read_credential", return_value="cred"), mock.patch(
            "makeros_hub.agent._pull_config", return_value={"tailscaleStatus": "disabled"}
        ), mock.patch("makeros_hub.agent.IngestServer", FakeIngest), mock.patch(
            "makeros_hub.agent.VirtualPrinterManager", FakeVirtualPrinterManager
        ), mock.patch(
            "makeros_hub.agent.tailscale_binary_exists", return_value=False
        ), mock.patch(
            "makeros_hub.agent.post_json", side_effect=http.TransportError("network down")
        ), mock.patch(
            "makeros_hub.agent._drain_vprinter_submissions_safely", side_effect=drain
        ):
            result = run(
                Config(cloud_url="https://host.example", heartbeat_sec=1),
                _stop_event=stop_event,
                _install_signals=False,
                _outbox=_FakeVPrinterOutbox(),
            )

        self.assertEqual(result, 0)
        self.assertGreaterEqual(len(drains), 1)
        self.assertEqual(drains[0], "N1")

    def test_run_rehydrates_outbox_before_first_drain(self):
        stop_event = threading.Event()
        _request_shutdown(stop_event, signal.SIGTERM)
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        outbox = _FakeVPrinterOutbox([job])
        drain_seen = []

        class FakeIngest:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class FakeVirtualPrinterManager:
            def __init__(self, *args, **kwargs):
                pass

            def current_model(self):
                return "N1"

            def stop_sync(self):
                pass

        def drain(submission_queue, *_args, **_kwargs):
            drain_seen.append([queued.submission_uid for queued in list(submission_queue.queue)])
            return 0

        with mock.patch("makeros_hub.agent.read_credential", return_value="cred"), mock.patch(
            "makeros_hub.agent._pull_config", return_value={"tailscaleStatus": "disabled"}
        ), mock.patch("makeros_hub.agent.IngestServer", FakeIngest), mock.patch(
            "makeros_hub.agent.VirtualPrinterManager", FakeVirtualPrinterManager
        ), mock.patch(
            "makeros_hub.agent._drain_vprinter_submissions_safely", side_effect=drain
        ):
            result = run(
                Config(cloud_url="https://host.example", heartbeat_sec=1),
                _stop_event=stop_event,
                _install_signals=False,
                _submission_queue=q,
                _outbox=outbox,
            )

        self.assertEqual(result, 0)
        self.assertEqual(outbox.load_count, 1)
        self.assertEqual(drain_seen[0], [job.submission_uid])

    def test_run_does_not_rehydrate_outbox_without_vprinter_model(self):
        stop_event = threading.Event()
        _request_shutdown(stop_event, signal.SIGTERM)
        outbox = _FakeVPrinterOutbox([_captured_job()])

        class FakeIngest:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class FakeVirtualPrinterManager:
            def __init__(self, *args, **kwargs):
                pass

            def current_model(self):
                return None

            def stop_sync(self):
                pass

        with mock.patch("makeros_hub.agent.read_credential", return_value="cred"), mock.patch(
            "makeros_hub.agent._pull_config", return_value={"tailscaleStatus": "disabled"}
        ), mock.patch("makeros_hub.agent.IngestServer", FakeIngest), mock.patch(
            "makeros_hub.agent.VirtualPrinterManager", FakeVirtualPrinterManager
        ), mock.patch(
            "makeros_hub.agent._drain_vprinter_submissions_safely", return_value=0
        ):
            result = run(
                Config(cloud_url="https://host.example", heartbeat_sec=1),
                _stop_event=stop_event,
                _install_signals=False,
                _outbox=outbox,
            )

        self.assertEqual(result, 0)
        self.assertEqual(outbox.load_count, 0)

    def test_shutdown_signal_event_runs_final_vprinter_drain(self):
        stop_event = threading.Event()
        _request_shutdown(stop_event, signal.SIGTERM)
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        _enqueue_vprinter_submission(q, job)
        submitted = []

        class FakeIngest:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class FakeVirtualPrinterManager:
            def __init__(self, *args, **kwargs):
                pass

            def current_model(self):
                return "N1"

            def stop_sync(self):
                pass

        def poster(url, payload, *, bearer, timeout):
            submitted.append((url, payload, bearer, timeout))
            return http.Response(200, {"ok": True, "jobId": "job-1"})

        with mock.patch("makeros_hub.agent.read_credential", return_value="cred"), mock.patch(
            "makeros_hub.agent._pull_config", return_value={"tailscaleStatus": "disabled"}
        ), mock.patch("makeros_hub.agent.IngestServer", FakeIngest), mock.patch(
            "makeros_hub.agent.VirtualPrinterManager", FakeVirtualPrinterManager
        ), mock.patch("makeros_hub.agent.post_json", side_effect=poster):
            result = run(
                Config(cloud_url="https://host.example", heartbeat_sec=1),
                _stop_event=stop_event,
                _install_signals=False,
                _submission_queue=q,
                _outbox=_FakeVPrinterOutbox(),
            )

        self.assertEqual(result, 0)
        self.assertTrue(q.empty())
        self.assertEqual(submitted[0][1]["hubSubmissionUid"], job.submission_uid)


class TestEnroll(unittest.TestCase):
    def test_enroll_writes_credential_on_200(self):
        from makeros_hub import enroll as enroll_mod

        cfg = Config(cloud_url="https://host.example")
        with mock.patch.object(
            enroll_mod, "read_credential", return_value=None
        ), mock.patch.object(
            enroll_mod, "post_json",
            return_value=http.Response(200, {"hubId": "h1", "credential": "secret-cred"}),
        ), mock.patch.object(enroll_mod, "write_credential") as wc:
            hub_id = enroll_mod.enroll(cfg, "the-token")
        self.assertEqual(hub_id, "h1")
        wc.assert_called_once_with("secret-cred")

    def test_enroll_consumed_token_exits(self):
        from makeros_hub import enroll as enroll_mod

        cfg = Config(cloud_url="https://host.example")
        with mock.patch.object(
            enroll_mod, "read_credential", return_value=None
        ), mock.patch.object(
            enroll_mod, "post_json",
            return_value=http.Response(410, {"error": "token_consumed"}),
        ):
            with self.assertRaises(SystemExit):
                enroll_mod.enroll(cfg, "used-token")


class TestPersistCloudUrl(unittest.TestCase):
    def test_replaces_placeholder_and_preserves_other_lines(self):
        from makeros_hub import config as cfg_mod

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(
                '# comment\ncloud_url = "https://your-makeros-host.example"\nheartbeat_sec = 30\n',
                encoding="utf-8",
            )
            with mock.patch.object(cfg_mod, "CONFIG_PATH", p):
                cfg_mod.persist_cloud_url("https://www.makeros.net")
            txt = p.read_text(encoding="utf-8")
            self.assertIn('cloud_url = "https://www.makeros.net"', txt)
            self.assertNotIn("your-makeros-host.example", txt)
            self.assertIn("heartbeat_sec = 30", txt)  # other lines preserved
            self.assertIn("# comment", txt)

    def test_appends_when_absent(self):
        from makeros_hub import config as cfg_mod

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text("heartbeat_sec = 30\n", encoding="utf-8")
            with mock.patch.object(cfg_mod, "CONFIG_PATH", p):
                cfg_mod.persist_cloud_url("https://www.makeros.net")
            txt = p.read_text(encoding="utf-8")
            self.assertIn('cloud_url = "https://www.makeros.net"', txt)
            self.assertIn("heartbeat_sec = 30", txt)


if __name__ == "__main__":
    unittest.main()
