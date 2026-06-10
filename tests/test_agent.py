"""Stdlib-only tests (unittest, so the repo has zero test deps either).

  python3 -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub import http
from makeros_hub.agent import (
    QUEUE_STATUS_DROP,
    QUEUE_STATUS_RETRY,
    QUEUE_STATUS_SENT,
    _flush_queue_status_reports,
    heartbeat_payload,
    make_queue_status_reporter,
)
from makeros_hub.config import Config


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


class TestHeartbeatPayload(unittest.TestCase):
    def test_shape(self):
        p = heartbeat_payload()
        # The contract the cloud Zod-parses: liveness self-report + empty
        # printer/job arrays (the printer adapter fills these in PR 5).
        for key in ("agentVersion", "os", "hostname", "uptimeSec", "printers", "jobs"):
            self.assertIn(key, p)
        self.assertEqual(p["printers"], [])
        self.assertEqual(p["jobs"], [])


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
