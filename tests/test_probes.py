import subprocess
import unittest
from unittest import mock

from makeros_hub import probes


class TestRunProbe(unittest.TestCase):
    def test_journalctl_tail_uses_exact_fixed_argv(self):
        completed = subprocess.CompletedProcess(
            args=list(probes.PROBES["journalctl-tail"].argv),
            returncode=0,
            stdout="journal output",
            stderr="",
        )

        with mock.patch.object(probes.subprocess, "run", return_value=completed) as run:
            result = probes.run_probe("journalctl-tail")

        spec = probes.PROBES["journalctl-tail"]
        run.assert_called_once_with(
            ["journalctl", "-u", "makeros-hub", "-n", "200", "--no-pager"],
            shell=False,
            capture_output=True,
            timeout=spec.timeout,
            text=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["output"], "journal output")

    def test_unknown_and_injection_names_are_rejected_without_execution(self):
        with mock.patch.object(probes.subprocess, "run") as run:
            unknown = probes.run_probe("unknown")
            injected = probes.run_probe("; rm -rf /")

        self.assertEqual(unknown["status"], "rejected")
        self.assertEqual(injected["status"], "rejected")
        run.assert_not_called()

    def test_output_is_redacted(self):
        name = "_test-redaction"
        probes.PROBES[name] = probes.ProbeSpec(("fixed",), timeout=1.0, max_output_bytes=4096)
        completed = subprocess.CompletedProcess(
            args=["fixed"],
            returncode=0,
            stdout="Authorization: Bearer hub-secret-token bblp:12345678@printer",
            stderr=" accessCode=87654321",
        )
        try:
            with mock.patch.object(probes.subprocess, "run", return_value=completed):
                result = probes.run_probe(name)
        finally:
            del probes.PROBES[name]

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("hub-secret-token", result["output"])
        self.assertNotIn("12345678", result["output"])
        self.assertNotIn("87654321", result["output"])
        self.assertIn("[redacted]", result["output"])

    def test_output_is_truncated_to_max_output_bytes(self):
        name = "_test-truncation"
        probes.PROBES[name] = probes.ProbeSpec(("fixed",), timeout=1.0, max_output_bytes=10)
        completed = subprocess.CompletedProcess(
            args=["fixed"],
            returncode=0,
            stdout="x" * 100,
            stderr="",
        )
        try:
            with mock.patch.object(probes.subprocess, "run", return_value=completed):
                result = probes.run_probe(name)
        finally:
            del probes.PROBES[name]

        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].encode("utf-8")), 10)

    def test_timeout_returns_timeout_status(self):
        name = "_test-timeout"
        probes.PROBES[name] = probes.ProbeSpec(("fixed",), timeout=0.01, max_output_bytes=4096)
        timeout = subprocess.TimeoutExpired(
            cmd=["fixed"],
            timeout=0.01,
            output="partial stdout ",
            stderr="partial stderr",
        )
        try:
            with mock.patch.object(probes.subprocess, "run", side_effect=timeout):
                result = probes.run_probe(name)
        finally:
            del probes.PROBES[name]

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["exitCode"], None)
        self.assertIn("partial stdout", result["output"])
        self.assertIn("partial stderr", result["output"])

    def test_which_binaries_uses_diagnostics_without_subprocess(self):
        with mock.patch.object(probes, "binary_presence", return_value={"python3": True}), mock.patch.object(
            probes.subprocess,
            "run",
        ) as run:
            result = probes.run_probe("which-binaries")

        run.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exitCode"], 0)
        self.assertIn('"python3": true', result["output"])

    def test_agent_config_redacts_and_uses_no_subprocess(self):
        old_effective = probes._EFFECTIVE_CONFIG
        probes.set_effective_config(None)
        env = {
            "MAKEROS_HUB_CLOUD_URL": "https://cloud.example/path?token=topsecret",
            "MAKEROS_HUB_CREDENTIAL_VALUE": "credential-secret",
        }
        try:
            with mock.patch.dict("os.environ", env, clear=False), mock.patch.object(
                probes.subprocess,
                "run",
            ) as run:
                result = probes.run_probe("agent-config")
        finally:
            probes.set_effective_config(old_effective)

        run.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertIn("cloudUrl", result["output"])
        self.assertNotIn("topsecret", result["output"])
        self.assertNotIn("credential-secret", result["output"])


class TestCameraTestProbe(unittest.TestCase):
    """v0.41.0 'camera-test' probe — one-shot capture across eligible printers.

    Returns a JSON-encoded {rows: [...]} so the cloud admin can render an
    inline table with categorized per-printer outcomes. The probe is gated on
    a registered provider — if none is wired (test or freshly-started agent),
    it returns an empty {rows:[]} with an explanatory error rather than
    crashing the heartbeat."""

    def test_no_provider_registered_returns_empty_rows_and_error(self):
        old = probes._CAMERA_TARGETS_PROVIDER
        probes.set_camera_targets_provider(None)
        try:
            result = probes.run_probe("camera-test")
        finally:
            probes.set_camera_targets_provider(old)
        self.assertEqual(result["status"], "ok")
        import json
        body = json.loads(result["output"])
        self.assertEqual(body["rows"], [])
        self.assertIn("not registered", body["error"])

    def test_with_provider_iterates_and_categorizes_per_printer(self):
        # Provider yields three printers; we monkey-patch the capture call
        # to return success/auth-fail/timeout in turn so the table covers
        # the categorized-reason contract end-to-end.
        outcomes = [
            (b"\xff\xd8\xff\xe0body\xff\xd9", None, ""),  # p1: OK
            (None, "auth-fail", "401 Unauthorized"),  # p2: auth
            (None, "timeout", ""),  # p3: timeout
        ]
        targets = [
            {"printerId": "p1", "displayName": "Wade", "model": "P2S", "vendor": "bambu"},
            {"printerId": "p2", "displayName": "Moya", "model": "P2S", "vendor": "bambu"},
            {"printerId": "p3", "displayName": "Antoni", "model": "X1C", "vendor": "bambu"},
        ]
        iter_outcomes = iter(outcomes)

        def fake_capture(_t):
            return next(iter_outcomes)

        old = probes._CAMERA_TARGETS_PROVIDER
        probes.set_camera_targets_provider(lambda: targets)
        with mock.patch(
            "makeros_hub.printers.camera.capture_printer_frame_with_reason",
            fake_capture,
        ):
            try:
                result = probes.run_probe("camera-test")
            finally:
                probes.set_camera_targets_provider(old)
        import json
        body = json.loads(result["output"])
        rows = body["rows"]
        self.assertEqual([r["printerId"] for r in rows], ["p1", "p2", "p3"])
        self.assertEqual([r["ok"] for r in rows], [True, False, False])
        self.assertEqual([r["reason"] for r in rows], [None, "auth-fail", "timeout"])
        self.assertEqual(rows[0]["displayName"], "Wade")
        self.assertEqual(rows[0]["jpegBytes"], len(b"\xff\xd8\xff\xe0body\xff\xd9"))
        self.assertEqual(rows[1]["stderrTail"], "401 Unauthorized")

    def test_per_row_exception_is_isolated(self):
        targets = [
            {"printerId": "p1", "displayName": "Wade", "model": "P2S"},
            {"printerId": "p2", "displayName": "Moya", "model": "P2S"},
        ]
        calls = {"n": 0}

        def fake_capture(_t):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("camera exploded")
            return (b"\xff\xd8\xff\xe0body\xff\xd9", None, "")

        old = probes._CAMERA_TARGETS_PROVIDER
        probes.set_camera_targets_provider(lambda: targets)
        with mock.patch(
            "makeros_hub.printers.camera.capture_printer_frame_with_reason",
            fake_capture,
        ):
            try:
                result = probes.run_probe("camera-test")
            finally:
                probes.set_camera_targets_provider(old)
        import json
        body = json.loads(result["output"])
        rows = body["rows"]
        # First row failed (exception), second succeeded — neither sinks the probe.
        self.assertEqual(rows[0]["ok"], False)
        self.assertEqual(rows[0]["reason"], "unknown")
        self.assertEqual(rows[1]["ok"], True)


if __name__ == "__main__":
    unittest.main()
