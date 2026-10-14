"""Local-only proofs of root updater and cloud acknowledgement boundaries."""
import os
import runpy
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from makeros_hub import agent, http, config, enroll
from makeros_hub.config import Config


class SystemBoundaries(unittest.TestCase):
    def test_network_helper_restricts_verbs_interfaces_and_addresses(self):
        make_command = runpy.run_path(str(Path(__file__).parents[1] / "scripts/vp-network.py"))["command"]
        routes = [{"dst": "default", "dev": "eth0", "gateway": "192.168.1.1"}]
        addresses = [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.50", "prefixlen": 24}]}]
        self.assertEqual(make_command(["add", "198.18.10.2", "lo"], routes, addresses),
                         ["/sbin/ip", "addr", "add", "198.18.10.2/32", "dev", "lo"])
        self.assertEqual(make_command(["probe", "192.168.1.250", "eth0"], routes, addresses),
                         ["/usr/sbin/arping", "-c", "1", "-w", "1", "-I", "eth0", "192.168.1.250"])
        for args in [["add", "127.0.0.1", "lo"], ["del", "100.64.0.2", "tailscale0"],
                     ["add", "192.168.1.250/0", "eth0"], ["del", "192.168.1.1", "eth0"],
                     ["del", "192.168.1.50", "eth0"], ["add", "192.168.1.255", "eth0"],
                     ["add", "192.168.2.1", "eth0"], ["flush", "192.168.1.250", "eth0"],
                     ["add", "192.168.1.250", "eth0", "extra"]]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                make_command(args, routes, addresses)

    def test_failed_arp_probe_is_not_evidence_an_address_is_free(self):
        from makeros_hub.vprinter.ip_allocator import IpAllocator
        with tempfile.TemporaryDirectory() as d:
            for outcome in [subprocess.CompletedProcess([], 2), OSError("missing helper")]:
                runner = mock.Mock(return_value=outcome) if not isinstance(outcome, Exception) else mock.Mock(side_effect=outcome)
                allocator = IpAllocator(state_path=Path(d) / "bindings", run=runner)
                self.assertFalse(allocator._ip_is_free("192.168.1.250", "eth0"))

    def test_root_version_gate_refuses_downgrade_even_for_direct_callers(self):
        script = (Path(__file__).parents[1] / "update.sh").read_text()
        gate = script.split("<<'VERSION_GATE'\n")[1].split("\nVERSION_GATE")[0]
        for current, target, success in [("0.66.0", "v0.65.0", False), ("0.66.0", "v0.66.0", False),
                                         ("", "v0.67.0", False), ("0.66.0", "v0.67.0", True)]:
            result = subprocess.run(["python3", "-I", "-c", gate, current, target], capture_output=True)
            self.assertEqual(result.returncode == 0, success)

    def test_credential_replacement_is_private_atomic_and_nonempty(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "CREDENTIAL_PATH", Path(d) / "credential"):
            config.write_credential("original-synthetic")
            self.assertEqual(config.CREDENTIAL_PATH.stat().st_mode & 0o777, 0o600)
            for invalid in ("", "   ", None):
                with self.assertRaises(ValueError):
                    config.write_credential(invalid)
            with mock.patch.object(config.os, "replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
                config.write_credential("replacement-synthetic")
            self.assertEqual(config.read_credential(), "original-synthetic")
            self.assertEqual(list(Path(d).iterdir()), [config.CREDENTIAL_PATH])
            with mock.patch.object(enroll, "post_json", return_value=http.Response(200, {"hubId": "hub", "credential": ""})), \
                    mock.patch.object(enroll, "write_credential") as write, self.assertRaises(SystemExit):
                enroll.enroll(Config(cloud_url="https://cloud.invalid"), "fake", force=True)
            write.assert_not_called()

    def test_outbox_directory_flush_failure_is_not_success(self):
        from makeros_hub.vprinter.outbox import _fsync_dir
        with tempfile.TemporaryDirectory() as d, mock.patch("os.fsync", side_effect=OSError("disk error")), self.assertRaises(OSError):
            _fsync_dir(Path(d))

    def test_root_updater_ignores_service_writable_repository(self):
        # Execute the real selection/argument-validation prefix only: never clone,
        # detach, install, invoke sudo or touch the installed service.
        script = (Path(__file__).parents[1] / "update.sh").read_text().split("# Phase 1")[0]
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "config.toml"
            config.write_text('repo = "https://attacker.invalid/root.git"\n')
            result = subprocess.run(["sh", "-c", script + '\nprintf "%s" "$REPO"\n',
                                     "update-test", "v0.66.0", "a" * 40],
                                    env={**os.environ, "MAKEROS_HUB_CONFIG": str(config)},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "https://git.overengineeredsolutions.org/makeros-hub-agent.git")

    def test_bearer_transports_never_follow_redirects(self):
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET

            def log_message(self, *args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()

            do_POST = do_GET

        source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        servers = [target, source]
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{source.server_port}/redirect"
            self.assertEqual(http.get_json(url, bearer="synthetic-secret").status, 302)
            self.assertEqual(http.post_json(url, {}, bearer="synthetic-secret").status, 302)
            self.assertEqual(http.post_bytes(url, b"x", bearer="synthetic-secret"), 302)
            with tempfile.TemporaryDirectory() as d, self.assertRaises(http.TransportError):
                http.get_to_file(url, Path(d) / "file", bearer="synthetic-secret")
            self.assertEqual(received, [])
        finally:
            for s in servers:
                s.shutdown()
                s.server_close()

    def test_queue_reports_survive_auth_outages_and_unconfirmed_success(self):
        report = agent.make_queue_status_reporter(Config(cloud_url="https://cloud.invalid"), "test-only")
        for status, body in [(200, {}), (200, {"ok": "true"}), (204, {}), (401, {}), (403, {}), (429, {})]:
            with self.subTest(status=status, body=body), mock.patch.object(agent, "post_json", return_value=http.Response(status, body)):
                self.assertEqual(report({"queueJobId": "job", "state": "printing"}), agent.QUEUE_STATUS_RETRY)
        with mock.patch.object(agent, "post_json", return_value=http.Response(200, {"ok": True})):
            self.assertEqual(report({"queueJobId": "job", "state": "printing"}), agent.QUEUE_STATUS_SENT)

    def test_bad_config_keeps_existing_printers_running(self):
        manager = mock.Mock()
        for body in [{}, {"printers": [], "version": None}, {"printers": [None], "version": "v2"}]:
            with mock.patch.object(agent, "get_json", return_value=http.Response(200, body)):
                agent._pull_config(Config(cloud_url="https://cloud.invalid"), "test-only", manager)
        manager.reconcile.assert_not_called()

    def test_heartbeat_requires_acknowledgement_before_discarding_reports(self):
        good = {"hubId": "h1", "configVersion": "2", "nextPollMs": 30000, "applied": 0, "assignments": []}
        agent._validate_heartbeat(good)
        for bad in [{}, {**good, "nextPollMs": True}, {**good, "nextPollMs": float("inf")},
                    {**good, "applied": None}, {**good, "assignments": [None]}]:
            with self.subTest(body=bad), self.assertRaises(http.TransportError):
                agent._validate_heartbeat(bad)

    def test_upload_needs_real_job_receipt_and_distinguishes_hub_auth(self):
        submit = agent.make_cloud_submit(Config(cloud_url="https://cloud.invalid"), "test-only")
        cases = [(200, {}, "error"), (200, {"ok": True}, "error"), (200, {"ok": False}, "error"),
                 (401, {}, "error"), (200, {"ok": False, "reason": "invalid_token"}, "bad_token"),
                 (200, {"ok": False, "reason": "ineligible"}, "rejected"),
                 (200, {"ok": True, "jobId": "job-1"}, "queued")]
        for status, body, expected in cases:
            with self.subTest(status=status, body=body), mock.patch.object(agent, "post_json", return_value=http.Response(status, body)):
                result = submit(member_token="fake", submission_uid="uid", file_name="file", file_sha256="a" * 64,
                                file_size=1, print_now=False)
                self.assertEqual(result["status"], expected)

    def test_upload_body_carries_sliced_metadata_only_when_present(self):
        submit = agent.make_cloud_submit(Config(cloud_url="https://cloud.invalid"), "test-only")
        base = dict(member_token="fake", submission_uid="uid", file_name="cube.gcode", file_sha256="a" * 64, file_size=1, print_now=False)
        filaments = [{"slot": 1, "type": "PLA", "color": "FFFFFFFF", "usedG": 4.53}]
        with mock.patch.object(agent, "post_json", return_value=http.Response(200, {"ok": True, "jobId": "j"})) as post:
            submit(**base)
            submit(**base, printer_model=None, est_grams=None, required_filaments=[])
            submit(**base, printer_model="Sovol SV08 MAX", est_grams=5, required_filaments=filaments)
        bodies = [c.args[1] for c in post.call_args_list]
        legacy = {"memberToken", "hubSubmissionUid", "fileName", "fileSha256", "fileSizeBytes"}
        self.assertEqual(set(bodies[0]), legacy)
        self.assertEqual(set(bodies[1]), legacy)
        self.assertEqual(bodies[2]["printerModel"], "Sovol SV08 MAX")
        self.assertEqual(bodies[2]["estGrams"], 5)
        self.assertEqual(bodies[2]["requiredFilaments"], filaments)

    def test_config_requires_https_and_preserves_existing_file_on_replace_failure(self):
        for url in ['http://cloud.example', 'https://user:secret@cloud.example', 'https://cloud.example/path?token=secret', 'https://cloud.example\n']:
            with self.assertRaises(ValueError):
                Config(cloud_url=url)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, 'CONFIG_PATH', Path(d) / 'config.toml'):
            config.CONFIG_PATH.write_text('[camera]\nenabled = true\n')
            config.persist_cloud_url('https://cloud.example/')
            saved = config.CONFIG_PATH.read_text()
            self.assertTrue(saved.startswith('cloud_url = "https://cloud.example"\n'))
            with mock.patch.object(config.os, 'replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
                config.persist_cloud_url('https://new.example')
            self.assertEqual(config.CONFIG_PATH.read_text(), saved)

    def test_bad_or_unwritable_update_state_refuses_an_update(self):
        from makeros_hub import update
        with tempfile.TemporaryDirectory() as d, mock.patch.object(update, 'STATE_PATH', Path(d) / 'state.json'), mock.patch.object(update.subprocess, 'run') as run:
            for content in ['[]', '{"target":"v0.67.0","at":"wrong"}', 'broken json']:
                update.STATE_PATH.write_text(content)
                self.assertFalse(update.maybe_update('0.66.0', 'v0.67.0', 'a' * 40))
            update.STATE_PATH.unlink()
            with mock.patch.object(update, '_write_state', side_effect=OSError('disk full')):
                self.assertFalse(update.apply_update('v0.67.0', 'a' * 40))
            run.assert_not_called()

    def test_staged_install_restores_code_and_helpers_after_a_copy_or_start_failure(self):
        # Execute ONLY the real staging block. Fake service commands; every path is disposable.
        script = (Path(__file__).parents[1] / 'install.sh').read_text().split('# --- staged code install ---')[1].split('# --- end staged code install ---')[0]
        for failure in ['move', 'start', 'none']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                base = Path(d); dest = base / 'installed'; source = base / 'source'
                for parent, value in [(dest, 'old'), (source, 'new')]:
                    (parent / 'makeros_hub').mkdir(parents=True)
                    (parent / 'makeros_hub/__init__.py').write_text(value)
                    (parent / 'update.sh').write_text(value)
                    scripts = parent / 'scripts' if parent == source else parent
                    scripts.mkdir(exist_ok=True)
                    for name in ['tailscale-setup.sh', 'vp-network.py']:
                        (scripts / name).write_text(value)
                harness = r'''
set -euo pipefail
INSTALL_DIR="$1"; HERE="$2"; failure="$3"; failed=0
chown() { :; }
systemctl() {
  if [ "$1" = start ] && [ "$failure" = start ] && [ "$failed" = 0 ]; then failed=1; return 1; fi
  return 0
}
mv() {
  if [ "$failure" = move ] && [ "$failed" = 0 ] && [[ "$1" == */tailscale-setup.sh ]]; then failed=1; return 1; fi
  command mv "$@"
}
'''
                result = subprocess.run(['bash', '-c', harness + script, 'install-test', str(dest), str(source), failure], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, failure == 'none', result.stderr)
                expected = 'new' if failure == 'none' else 'old'
                self.assertEqual((dest / 'makeros_hub/__init__.py').read_text(), expected)
                for name in ['update.sh', 'tailscale-setup.sh', 'vp-network.py']:
                    self.assertEqual((dest / name).read_text(), expected)
