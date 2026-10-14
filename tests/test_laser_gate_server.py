"""Laser gate HTTP server, config and static boundaries, contract G12-G13 (and G6's not_calibrated answer)."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import laser_gate_fixtures as fx
from laser_gate.gate import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "laser_gate"


class ServerBase(unittest.TestCase):
    def running(self, fake="new", **over):
        if fake == "new":
            fake = fx.FakeController()
            self.addCleanup(fake.close)
        gate = fx.gate_for(fake, **over)
        if fake:
            self.addCleanup(fake.assert_clean, gate.reads)  # G11 holds here too
        srv = fx.Running(gate)
        self.addCleanup(srv.close)
        return srv


class Http(ServerBase):
    def test_g12_wrong_peer_is_403_before_the_body_is_read(self):
        srv = self.running(allowed_peer="127.0.0.2")
        # headers promise 100 kB and no body follows: a server that read the body would hang, not answer
        reply = srv.raw(b"POST /check?maxPowerPct=50 HTTP/1.1\r\nHost: x\r\nContent-Length: 100000\r\n"
                        b"Authorization: Bearer " + fx.TOKEN.encode() + b"\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 403"), reply)
        self.assertIn(b'{"error": "forbidden"}', reply)

    def test_g12_token(self):
        srv = self.running()
        self.assertEqual(srv.call("GET", "/status", token=None), (401, {"error": "unauthorized"}))
        self.assertEqual(srv.call("GET", "/status", token="t" * 39 + "u"), (401, {"error": "unauthorized"}))
        self.assertEqual(srv.call("GET", "/status", headers={"Authorization": "Basic " + fx.TOKEN}, token=None)[0], 401)

    def test_g12_routes_methods_and_length(self):
        srv = self.running()
        self.assertEqual(srv.call("GET", "/check"), (405, {"error": "method_not_allowed"}))
        self.assertEqual(srv.call("POST", "/status", b""), (405, {"error": "method_not_allowed"}))
        self.assertEqual(srv.call("DELETE", "/store"), (405, {"error": "method_not_allowed"}))
        self.assertEqual(srv.call("POST", "/start", b""), (404, {"error": "not_found"}))
        reply = srv.raw(b"POST /check?maxPowerPct=50 HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                        + fx.TOKEN.encode() + b"\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 411"), reply)
        reply = srv.raw(b"POST /check?maxPowerPct=50 HTTP/1.1\r\nHost: x\r\nContent-Length: 5000000\r\nAuthorization: "
                        b"Bearer " + fx.TOKEN.encode() + b"\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 413"), reply)
        self.assertIn(b'"limit": 4194304', reply)

    def test_check_endpoint(self):
        srv = self.running()
        code, v = srv.call("POST", "/check?maxPowerPct=50", fx.square())
        self.assertEqual((code, v["verdict"], v["maxPowerPct"], v["widthMm"]), (200, "checked", 50, 20.0))
        code, v = srv.call("POST", "/check?maxPowerPct=20", fx.square())
        self.assertEqual((code, v["verdict"], v["code"]), (200, "refused", "power_over_cap"))
        self.assertEqual(set(v), {"verdict", "code", "reason", "offset", "sha256", "sizeBytes"})
        for bad in ("", "?maxPowerPct=0", "?maxPowerPct=101", "?maxPowerPct=5.5", "?maxPowerPct=50&maxPowerPct=60"):
            with self.subTest(query=bad):
                self.assertEqual(srv.call("POST", "/check" + bad, fx.square()),
                                 (400, {"error": "bad_request", "detail": "maxPowerPct"}))

    def test_g6_check_answers_not_calibrated_without_a_minimum(self):
        srv = self.running(min_power_pct=0.0)
        self.assertEqual(srv.call("POST", "/check?maxPowerPct=50", fx.square()),
                         (503, {"error": "not_calibrated", "detail": "min_power_pct is not set (T5)"}))

    def test_g12_status_up_and_down(self):
        fake = fx.FakeController(count=12)
        self.addCleanup(fake.close)
        code, s = self.running(fake).call("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual({k: s[k] for k in ("gateVersion", "controller", "statusWord", "moving", "fileCount",
                                            "storeEnabled", "calibrated", "busy")},
                         {"gateVersion": "0.1.0", "controller": "up", "statusWord": 0x08000000, "moving": False,
                          "fileCount": 12, "storeEnabled": True, "calibrated": {"minPowerPct": True, "fileCountAddr": True},
                          "busy": False})
        self.assertRegex(s["seenAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(all(p.startswith(b"\xDA\x00") for p in fake.got))
        code, s = self.running(None, file_count_addr="").call("GET", "/status")
        self.assertEqual((code, s["controller"], s["statusWord"], s["moving"], s["fileCount"], s["seenAt"]),
                         (200, "down", None, None, None, None))
        self.assertEqual(s["calibrated"], {"minPowerPct": True, "fileCountAddr": False})


class StartupConfig(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.d)]))
        self.token = self.d / "token"
        self.token.write_text("x" * 43 + "\n")
        self.token.chmod(0o600)

    def write(self, extra=""):
        cfg = self.d / "config.toml"
        cfg.write_text((PKG / "config.example.toml").read_text().replace(
            'token_file = "/etc/laser-gate/token"', f'token_file = "{self.token}"') + extra)
        return cfg

    def serve(self, cfg):
        return subprocess.run([sys.executable, "-m", "laser_gate", "serve", "--config", str(cfg)], cwd=ROOT,
                              capture_output=True, text=True, timeout=20)

    def test_example_config_loads(self):
        cfg, token = load_config(self.write())
        self.assertEqual((cfg.store_enabled, cfg.file_count, cfg.min_power_pct, cfg.listen_addr),
                         (False, None, 0.0, ("127.0.0.1", 7650)))
        self.assertEqual(token, "x" * 43)

    def test_g12_a_0644_token_file_refuses_to_start(self):
        self.token.chmod(0o644)
        r = self.serve(self.write())
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("readable by group or others", r.stderr)

    def test_bad_configs_refuse_to_start(self):
        cases = {"surprise = 1\n": "unknown config key", 'listen = "7650"\n': "listen must be host:port",
                 'listen = "0.0.0.0:7650"\n': "must not be 0.0.0.0", 'controller_ip = "laser.local"\n': "IPv4",
                 'allowed_peer = "::1"\n': "IPv4", "store_enabled = 1\n": "store_enabled must be bool",
                 'file_count_addr = "zz"\n': "file_count_addr", "min_power_pct = 120\n": "min_power_pct"}
        for extra, msg in cases.items():
            with self.subTest(extra=extra.strip()):
                text = self.write().read_text()
                key = extra.split(" =")[0]
                lines = [ln for ln in text.splitlines() if not ln.startswith(key + " ")]
                cfg = self.d / "case.toml"
                cfg.write_text("\n".join(lines) + "\n" + extra)
                with self.assertRaises(ConfigError) as e:
                    load_config(cfg)
                self.assertIn(msg, str(e.exception))
        self.token.write_text("short\n")
        with self.assertRaises(ConfigError):
            load_config(self.write())
        os.unlink(self.token)
        r = self.serve(self.write())
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing to start", r.stderr)


class Static(unittest.TestCase):
    def test_g13_stdlib_only_and_nothing_from_the_hub(self):
        for f in PKG.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for n in names:
                    with self.subTest(file=f.name, module=n):
                        self.assertIn(n.split(".")[0], sys.stdlib_module_names)

    def test_g13_the_panel_port_number_appears_nowhere_in_the_package(self):
        panel_port = str(50200 + 7)  # built, so a repo-wide grep for the number finds nothing, this test included
        hits = [str(f) for f in PKG.rglob("*") if f.is_file() and "__pycache__" not in f.parts
                and panel_port in f.read_text(errors="replace")]
        self.assertEqual(hits, [])

    def test_g13_the_hub_never_mentions_the_gate(self):
        hub = [ROOT / "install.sh", ROOT / "update.sh", *(ROOT / "makeros_hub").rglob("*.py")]
        for f in hub:
            text = f.read_text(errors="replace")
            with self.subTest(file=str(f.relative_to(ROOT))):
                self.assertNotIn("laser_gate", text)
                self.assertNotIn("laser-gate", text)

    def test_the_firewall_loads_at_boot_whether_or_not_the_gate_runs(self):
        def unit(name):
            out = {}
            for line in (PKG / name).read_text().splitlines():
                if "=" in line and not line.startswith(("#", "[")):
                    k, v = line.split("=", 1)
                    out.setdefault(k, []).extend(v.split())
            return out

        fw = unit("laser-gate-firewall.service")
        self.assertEqual(fw["DefaultDependencies"], ["no"])
        self.assertIn("network-pre.target", fw["Before"])
        self.assertIn("network-pre.target", fw["Wants"])
        self.assertIn("nftables.service", fw["After"])  # Debian's nftables.conf flushes the ruleset when it loads
        self.assertIn("nftables.service", fw["PartOf"])
        self.assertIn("sysinit.target", fw["WantedBy"])
        self.assertEqual((fw["Type"], fw["RemainAfterExit"]), (["oneshot"], ["yes"]))
        self.assertEqual(fw["ExecStart"], ["/usr/sbin/nft", "-f", "/etc/laser-gate/laser-gate.nft"])
        self.assertNotIn("ExecStop", fw)  # stopping the unit leaves the rules loaded
        gate = unit("laser-gate.service")
        self.assertIn("laser-gate-firewall.service", gate["Requires"])
        self.assertIn("laser-gate-firewall.service", gate["After"])
        self.assertFalse([c for c in gate.get("ExecStartPre", []) if "nft" in c])  # one loader only
        install = (PKG / "install.sh").read_text()
        self.assertIn("systemctl enable laser-gate-firewall.service laser-gate.service", install)
        self.assertIn('"$HERE/laser-gate-firewall.service" /etc/systemd/system/laser-gate-firewall.service', install)

    def test_g13_install_script_parses(self):
        r = subprocess.run(["bash", "-n", str(PKG / "install.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_check_prints_the_check_json(self):
        with tempfile.TemporaryDirectory() as d:
            rd, cfg = Path(d) / "square.rd", Path(d) / "c.toml"
            rd.write_bytes(fx.square())
            cfg.write_text((PKG / "config.example.toml").read_text().replace("min_power_pct = 0", "min_power_pct = 10"))
            r = subprocess.run([sys.executable, "-m", "laser_gate", "check", str(rd), "--max-power", "50",
                                "--config", str(cfg)], cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["verdict"], "checked")
            r = subprocess.run([sys.executable, "-m", "laser_gate", "check", str(rd), "--max-power", "50"], cwd=ROOT,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (3, "not_calibrated"))
            r = subprocess.run([sys.executable, "-m", "laser_gate", "learn", d], cwd=ROOT, capture_output=True,
                               text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("square.rd: checked 20.0 x 20.0 mm, 1 layer(s)", r.stdout)


if __name__ == "__main__":
    unittest.main()
