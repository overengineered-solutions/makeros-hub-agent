import json
import logging
import os
import re
import subprocess
import unittest
from pathlib import Path

from makeros_hub import diagnostics, tailscale


def result(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        argv,
        returncode,
        stdout=stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"),
    )


class SequencedRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv!r}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestTailscaleReconcile(unittest.TestCase):
    def test_enabled_with_key_not_up_calls_setup_with_key_on_stdin_only(self):
        key = "tskey-secret"
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], 1, stderr="Tailscale is stopped."),
            result(["tailscale", "status"], 1, stderr="Tailscale is stopped."),
            result(["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up", "--hostname", "hub-one"]),
            result(["tailscale", "ip", "-4"], stdout="100.64.0.10\n"),
            result(
                ["tailscale", "status"],
                stdout="100.64.0.10 hub-one maker@example.com linux -\n",
            ),
        )

        status = tailscale.reconcile_tailscale(
            {"enabled": True, "authKey": key, "hostname": "hub-one"},
            runner,
        )

        setup_calls = [c for c in runner.calls if c["argv"][:3] == ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up"]]
        self.assertEqual(len(setup_calls), 1)
        self.assertEqual(setup_calls[0]["input"], key.encode("utf-8"))
        self.assertEqual(setup_calls[0]["argv"], ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up", "--hostname", "hub-one"])
        for call in runner.calls:
            self.assertNotIn(key, " ".join(call["argv"]))
        self.assertEqual(status["tailscaleStatus"], "connected")
        self.assertEqual(status["tailscaleIp"], "100.64.0.10")
        self.assertEqual(status["tailscaleHostname"], "hub-one")
        self.assertNotIn(key, json.dumps(status))

    def test_already_up_with_same_hostname_noops(self):
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], stdout="100.64.0.10\n"),
            result(
                ["tailscale", "status"],
                stdout="100.64.0.10 hub-one maker@example.com linux -\n",
            ),
            result(
                ["tailscale", "status", "--json"],
                stdout=json.dumps({"Self": {"AllowedIPs": ["100.64.0.10/32"], "RunSSH": False}}),
            ),
        )

        status = tailscale.reconcile_tailscale(
            {"enabled": True, "authKey": "tskey-secret", "hostname": "hub-one"},
            runner,
        )

        self.assertEqual(status["tailscaleStatus"], "connected")
        self.assertFalse([c for c in runner.calls if c["argv"][0] == "sudo"])

    def test_posture_drift_forces_reup(self):
        key = "tskey-secret"
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], stdout="100.64.0.10\n"),
            result(
                ["tailscale", "status"],
                stdout="100.64.0.10 hub-one maker@example.com linux -\n",
            ),
            result(
                ["tailscale", "status", "--json"],
                stdout=json.dumps({"Self": {"AllowedIPs": ["100.64.0.10/32", "10.0.0.0/24"]}}),
            ),
            result(["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up", "--hostname", "hub-one"]),
            result(["tailscale", "ip", "-4"], stdout="100.64.0.10\n"),
            result(
                ["tailscale", "status"],
                stdout="100.64.0.10 hub-one maker@example.com linux -\n",
            ),
        )

        status = tailscale.reconcile_tailscale(
            {"enabled": True, "authKey": key, "hostname": "hub-one"},
            runner,
        )

        setup_calls = [c for c in runner.calls if c["argv"][:3] == ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up"]]
        self.assertEqual(len(setup_calls), 1)
        self.assertEqual(setup_calls[0]["input"], key.encode("utf-8"))
        self.assertNotIn(key, " ".join(setup_calls[0]["argv"]))
        self.assertEqual(status["tailscaleStatus"], "connected")

    def test_disabled_when_up_calls_down(self):
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], stdout="100.64.0.10\n"),
            result(
                ["tailscale", "status"],
                stdout="100.64.0.10 hub-one maker@example.com linux -\n",
            ),
            result(["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "down"]),
        )

        status = tailscale.reconcile_tailscale({"enabled": False}, runner)

        self.assertEqual(status["tailscaleStatus"], "disabled")
        self.assertEqual(runner.calls[-1]["argv"], ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "down"])

    def test_runner_error_returns_sanitized_error_without_logging_secret(self):
        key = "tskey-secret"
        handler = ListHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            status = tailscale.reconcile_tailscale(
                {"enabled": True, "authKey": key, "hostname": "hub-one"},
                SequencedRunner(RuntimeError(f"boom {key}")),
            )
        finally:
            root.removeHandler(handler)

        self.assertEqual(status["tailscaleStatus"], "error")
        self.assertNotIn(key, json.dumps(status))
        self.assertNotIn(key, "\n".join(handler.messages))

    def test_setup_failure_reason_is_sanitized(self):
        key = "tskey-secret"
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], 1, stderr="Tailscale is stopped."),
            result(["tailscale", "status"], 1, stderr="Tailscale is stopped."),
            result(
                ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up", "--hostname", "hub-one"],
                1,
                stderr=f"bad auth {key}",
            ),
        )

        status = tailscale.reconcile_tailscale(
            {"enabled": True, "authKey": key, "hostname": "hub-one"},
            runner,
        )

        self.assertEqual(status["tailscaleStatus"], "error")
        self.assertIn("[redacted]", status["tailscaleStatusReason"])
        self.assertNotIn(key, json.dumps(status))

    def test_setup_failure_records_sticky_tailscale_diagnostic(self):
        key = "tskey-auth-supersecret"
        diag = diagnostics.Diagnostics(enable_network=False)
        old_default = diagnostics.get_default()
        diagnostics.set_default(diag)
        try:
            runner = SequencedRunner(
                result(["tailscale", "ip", "-4"], 1, stderr="Tailscale is stopped."),
                result(["tailscale", "status"], 1, stderr="Tailscale is stopped."),
                result(
                    ["sudo", tailscale.TAILSCALE_SETUP_SCRIPT, "up", "--hostname", "hub-one"],
                    1,
                    stderr=f"apt failed using {key}",
                ),
            )

            tailscale.reconcile_tailscale(
                {"enabled": True, "authKey": key, "hostname": "hub-one"},
                runner,
            )
        finally:
            diagnostics.set_default(old_default)

        snap = diag.errors.snapshot()
        self.assertIn("tailscale", snap)
        self.assertIn("tailscale up failed: apt failed", snap["tailscale"]["message"])
        self.assertNotIn(key, json.dumps(snap))


class TestTailscaleStatusParsing(unittest.TestCase):
    def test_parses_ip_and_matching_hostname_from_status_output(self):
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], stdout="100.64.0.20\n"),
            result(
                ["tailscale", "status"],
                stdout=(
                    "100.64.0.10 laptop maker@example.com macOS active; direct\n"
                    "100.64.0.20 hub-alpha maker@example.com linux -\n"
                    "# Health check:\n"
                    "#     - sample message\n"
                ),
            ),
        )

        status = tailscale.current_tailscale_status(runner)

        self.assertEqual(status["tailscaleStatus"], "connected")
        self.assertEqual(status["tailscaleIp"], "100.64.0.20")
        self.assertEqual(status["tailscaleHostname"], "hub-alpha")

    def test_ip_failure_is_disabled_status(self):
        runner = SequencedRunner(
            result(["tailscale", "ip", "-4"], 1, stderr="Tailscale is stopped."),
            result(["tailscale", "status"], 1, stderr="Tailscale is stopped."),
        )

        status = tailscale.current_tailscale_status(runner)

        self.assertEqual(status["tailscaleStatus"], "disabled")
        self.assertIn("not connected", status["tailscaleStatusReason"])

    def test_disabled_and_not_installed_skips_shellouts(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            raise AssertionError("runner should not be called")

        status = tailscale.current_tailscale_status(runner, enabled=False, installed=False)

        self.assertEqual(status, {})
        self.assertEqual(calls, [])


class TestTailscaleSetupHelper(unittest.TestCase):
    def test_dry_run_uses_auth_key_file_not_raw_key_arg(self):
        key = "tskey-secret"
        script = Path(__file__).resolve().parents[1] / "scripts" / "tailscale-setup.sh"
        env = dict(os.environ)
        env["MAKEROS_TAILSCALE_SETUP_DRY_RUN"] = "1"

        proc = subprocess.run(
            ["bash", str(script), "up", "--hostname", "hub-one"],
            input=f"{key}\n".encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

        output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("--auth-key=file:", output)
        self.assertNotIn("--authkey=", output)
        self.assertNotIn(key, output)
        match = re.search(r"--auth-key=file:(\S+)", output)
        self.assertIsNotNone(match)
        self.assertFalse(Path(match.group(1)).exists())


if __name__ == "__main__":
    unittest.main()
