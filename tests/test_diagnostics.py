import json
import logging
import unittest
from unittest import mock

from makeros_hub import diagnostics
from makeros_hub.agent import heartbeat_payload


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestSubsystemErrorRing(unittest.TestCase):
    def test_redact_covers_auth_and_access_code_shapes(self):
        self.assertEqual(
            diagnostics.redact("tailscale authKey missing"),
            "tailscale authKey missing",
        )
        redacted = diagnostics.redact(
            "Authorization: Bearer hub-secret-token access code 12345678 authKey=tskey-auth-secret"
        )
        self.assertNotIn("hub-secret-token", redacted)
        self.assertNotIn("12345678", redacted)
        self.assertNotIn("tskey-auth-secret", redacted)
        self.assertEqual(redacted.count("[redacted]"), 3)

    def test_redact_defensively_covers_bambu_userinfo_and_access_code_is(self):
        redacted = diagnostics.redact(
            "MQTT refused: bblp:12345678@192.168.1.50; access code is 87654321"
        )

        self.assertNotIn("12345678", redacted)
        self.assertNotIn("87654321", redacted)
        self.assertIn("bblp:[redacted]@", redacted)

    def test_printer_record_extra_secret_redacts_bambu_access_code_forms(self):
        code = "12345678"
        messages = (
            f"MQTT refused: bblp:{code}@h",
            f"connect failed accessCode={code}",
            f"access code is {code}",
        )
        for message in messages:
            with self.subTest(message=message):
                ring = diagnostics.SubsystemErrorRing()
                ring.record("printers", message, extra_secrets=[code])

                recorded = ring.snapshot()["printers"]["message"]
                self.assertNotIn(code, recorded)
                self.assertIn("[redacted]", recorded)

    def test_sticky_per_subsystem_newest_only_and_redacted(self):
        ring = diagnostics.SubsystemErrorRing()
        secret = "tskey-auth-supersecret"

        ring.record("tailscale", "first error")
        ring.record("tailscale", f"newest installer stderr {secret} " + ("x" * 400))
        ring.record("printers", "printer status failed")

        snap = ring.snapshot()
        self.assertIn("newest installer stderr", snap["tailscale"]["message"])
        self.assertNotIn("first error", snap["tailscale"]["message"])
        self.assertEqual(snap["printers"]["message"], "printer status failed")
        self.assertNotIn(secret, json.dumps(snap))
        self.assertLessEqual(len(snap["tailscale"]["message"]), diagnostics.ERROR_MESSAGE_MAX)

    def test_recording_other_subsystem_does_not_clobber_tailscale(self):
        ring = diagnostics.SubsystemErrorRing()
        ring.record("tailscale", "real tailscale setup failure")
        ring.record("printers", "status read failed")

        self.assertEqual(
            ring.snapshot()["tailscale"]["message"],
            "real tailscale setup failure",
        )


class TestPresenceAndNetwork(unittest.TestCase):
    def tearDown(self):
        diagnostics.reset_caches()

    def test_binary_presence_shape(self):
        diagnostics.reset_caches()

        def fake_which(name):
            return f"/usr/bin/{name}" if name in ("python3", "git") else None

        with mock.patch.object(diagnostics.shutil, "which", side_effect=fake_which):
            presence = diagnostics.binary_presence()

        self.assertEqual(set(presence), set(diagnostics.BINARIES))
        self.assertTrue(all(isinstance(value, bool) for value in presence.values()))
        self.assertTrue(presence["python3"])
        self.assertFalse(presence["curl"])

    def test_network_reachability_all_bool_and_never_raises(self):
        diagnostics.reset_caches()
        calls = []

        def fake_connect(addr, timeout):
            calls.append((addr, timeout))
            if addr[0] == "tailscale.com":
                raise OSError("blocked")
            return FakeConn()

        with mock.patch.object(diagnostics.socket, "create_connection", side_effect=fake_connect):
            net = diagnostics.network_reachability(timeout=0.2, cloud_url="https://cloud.example/path")

        self.assertEqual(net, {"cloud": True, "tailscale_com": False, "pkgs_tailscale": True})
        self.assertTrue(all(isinstance(value, bool) for value in net.values()))
        self.assertIn((("cloud.example", 443), 0.2), calls)

        diagnostics.reset_caches()
        with mock.patch.object(diagnostics.socket, "create_connection", side_effect=OSError("down")):
            net = diagnostics.network_reachability(timeout=0.2, cloud_url="https://cloud.example")
        self.assertEqual(net, {"cloud": False, "tailscale_com": False, "pkgs_tailscale": False})


class TestLogRingHandler(unittest.TestCase):
    def test_captures_warning_error_redacts_and_wraps(self):
        handler = diagnostics.LogRingHandler()
        logger = logging.getLogger("tests.diagnostics.logring")
        old_level = logger.level
        old_propagate = logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.info("ignore me")
            for idx in range(20):
                logger.warning("warn %d Authorization: Bearer secret-token-%d", idx, idx)
            logger.error("final accessCode=12345678")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate

        entries = handler.snapshot()
        self.assertEqual(len(entries), 16)
        self.assertEqual(entries[0]["level"], "WARNING")
        self.assertIn("warn 5", entries[0]["message"])
        dumped = json.dumps(entries)
        self.assertNotIn("secret-token", dumped)
        self.assertNotIn("12345678", dumped)
        self.assertIn("[redacted]", dumped)


class TestCollectDiagnostics(unittest.TestCase):
    def tearDown(self):
        diagnostics.reset_caches()

    def test_collect_shape_and_recorded_auth_key_redacted(self):
        secret = "tskey-auth-supersecret"
        diag = diagnostics.Diagnostics(agent_version="0.9.3", enable_network=False)
        diag.record("tailscale", f"install failed with {secret}")

        payload = diag.collect_cheap_diagnostics()

        self.assertEqual(
            set(payload),
            {"systemFacts", "binaries", "network", "lastErrors", "recentLog"},
        )
        self.assertEqual(payload["systemFacts"]["agentVersion"], "0.9.3")
        self.assertEqual(payload["network"], diagnostics.NETWORK_DEFAULT)
        self.assertIn("tailscale", payload["lastErrors"])
        self.assertNotIn(secret, json.dumps(payload))

    def test_heartbeat_payload_includes_diagnostics(self):
        diag = diagnostics.Diagnostics(agent_version="0.9.3", enable_network=False)
        payload = heartbeat_payload(diagnostics=diag)

        self.assertIn("diagnostics", payload)
        self.assertEqual(payload["diagnostics"]["systemFacts"]["agentVersion"], "0.9.3")

    def test_heartbeat_payload_survives_binary_presence_failure(self):
        diag = diagnostics.Diagnostics(agent_version="0.9.3", enable_network=False)
        with mock.patch.object(diagnostics, "binary_presence", side_effect=RuntimeError("boom")):
            payload = heartbeat_payload(diagnostics=diag)

        self.assertEqual(payload["agentVersion"], "0.9.3")
        self.assertIn("diagnostics", payload)
        self.assertEqual(payload["diagnostics"]["systemFacts"]["agentVersion"], "0.9.3")
        self.assertEqual(payload["diagnostics"]["network"], diagnostics.NETWORK_DEFAULT)


if __name__ == "__main__":
    unittest.main()
