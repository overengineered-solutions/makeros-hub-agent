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


if __name__ == "__main__":
    unittest.main()
