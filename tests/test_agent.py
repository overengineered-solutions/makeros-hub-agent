"""Stdlib-only tests (unittest, so the repo has zero test deps either).

  python3 -m unittest discover -s tests
"""

import hashlib
import json
import queue
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from makeros_hub import diagnostics, http
from makeros_hub.agent import (
    QUEUE_STATUS_DROP,
    QUEUE_STATUS_RETRY,
    QUEUE_STATUS_SENT,
    _TailscaleRuntimeState,
    _drain_vprinter_submissions,
    _enqueue_vprinter_submission,
    _flush_queue_status_reports,
    _maybe_retry_tailscale_config,
    _pull_config,
    _reconcile_tailscale_config,
    _run_pending_probes,
    heartbeat_payload,
    make_queue_status_reporter,
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
    def test_drain_ok_true_removes_job(self):
        cfg = Config(cloud_url="https://host.example/")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
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

    def test_drain_ok_false_rejected_is_terminal(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        _enqueue_vprinter_submission(q, _captured_job())

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(200, {"ok": False, "state": "rejected", "reason": "not eligible"})

        submitted = _drain_vprinter_submissions(q, cfg, "cred", model="N1", poster=poster)

        self.assertEqual(submitted, 0)
        self.assertTrue(q.empty())

    def test_drain_transport_error_reenqueues_and_records_vprinter_diagnostic(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        diag = diagnostics.Diagnostics(enable_network=False)
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            raise http.TransportError("network down")

        submitted = _drain_vprinter_submissions(
            q,
            cfg,
            "cred",
            model="N1",
            diagnostics=diag,
            poster=poster,
        )

        self.assertEqual(submitted, 0)
        self.assertEqual(q.get_nowait().submission_uid, job.submission_uid)
        self.assertIn("vp-submit transport", diag.errors.snapshot()["vprinter"]["message"])

    def test_drain_non_200_reenqueues(self):
        cfg = Config(cloud_url="https://host.example")
        q: queue.Queue[CapturedJob] = queue.Queue(maxsize=4)
        job = _captured_job()
        _enqueue_vprinter_submission(q, job)

        def poster(_url, _payload, *, bearer, timeout):
            return http.Response(503, {"error": "try later"})

        _drain_vprinter_submissions(q, cfg, "cred", model="N1", poster=poster)

        self.assertEqual(q.get_nowait().submission_uid, job.submission_uid)

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
