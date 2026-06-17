"""Tests for makeros_hub.lan_scan — Moonraker HTTP sweep.

Network-free: every test that needs HTTP responses passes a stub `probe_fn`
to `sweep_subnet`, and the few that exercise `probe_moonraker` directly
monkey-patch `urllib.request.urlopen`. Stdlib only — no requests/httpx.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from makeros_hub import lan_scan
from makeros_hub.lan_scan import (
    DiscoveryHit,
    enumerate_sweep_targets,
    is_moonraker_response,
    probe_moonraker,
    sweep_subnet,
)


class TestIsMoonrakerResponse(unittest.TestCase):
    def test_well_formed_response_recognised(self):
        body = {
            "result": {
                "klippy_state": "ready",
                "moonraker_version": "v0.9.3",
            }
        }
        self.assertTrue(is_moonraker_response(body))

    def test_shutdown_state_still_recognised(self):
        # We must accept ALL the documented klippy states (and unknown
        # future ones) so a new firmware doesn't break discovery.
        for state in ("shutdown", "error", "startup", "disconnected", "future_unknown"):
            with self.subTest(state=state):
                self.assertTrue(
                    is_moonraker_response({"result": {"klippy_state": state}})
                )

    def test_non_dict_input_rejected(self):
        self.assertFalse(is_moonraker_response([]))  # type: ignore[arg-type]
        self.assertFalse(is_moonraker_response("hello"))  # type: ignore[arg-type]
        self.assertFalse(is_moonraker_response(None))  # type: ignore[arg-type]

    def test_missing_result_envelope_rejected(self):
        self.assertFalse(is_moonraker_response({"klippy_state": "ready"}))

    def test_missing_klippy_state_rejected(self):
        # OctoPrint-style responses use different shapes; we must not match.
        self.assertFalse(
            is_moonraker_response({"result": {"moonraker_version": "v0.9.3"}})
        )

    def test_non_string_klippy_state_rejected(self):
        self.assertFalse(is_moonraker_response({"result": {"klippy_state": 7}}))


class TestEnumerateSweepTargets(unittest.TestCase):
    def test_slash_24_excludes_own_router_broadcast(self):
        targets = enumerate_sweep_targets(
            "192.168.1.0", "255.255.255.0", "192.168.1.50"
        )
        # Skips .0 (network), .1 (router heuristic), .50 (own), .255 (broadcast).
        self.assertNotIn("192.168.1.0", targets)
        self.assertNotIn("192.168.1.1", targets)
        self.assertNotIn("192.168.1.50", targets)
        self.assertNotIn("192.168.1.255", targets)
        # First and last sensible targets are present.
        self.assertIn("192.168.1.2", targets)
        self.assertIn("192.168.1.254", targets)
        self.assertEqual(len(targets), 252)  # 256 total - 4 skipped

    def test_caps_at_max_hosts(self):
        # A /16 would be 65k hosts; we cap at MAX_HOSTS_PER_SWEEP=510.
        targets = enumerate_sweep_targets(
            "10.0.0.0", "255.255.0.0", "10.0.0.50"
        )
        self.assertLessEqual(len(targets), lan_scan.MAX_HOSTS_PER_SWEEP)

    def test_returns_empty_on_invalid_network(self):
        self.assertEqual(enumerate_sweep_targets("not-an-ip", "255.255.255.0", "1.2.3.4"), [])
        self.assertEqual(enumerate_sweep_targets("192.168.1.0", "bad-mask", "1.2.3.4"), [])


class TestSweepSubnet(unittest.TestCase):
    """Orchestration tests — the per-IP probe is stubbed."""

    def test_collects_hits_from_stub_probe(self):
        # Stub probe returns Moonraker for any .2 or .47 address.
        def stub_probe(ip: str):
            if ip.endswith(".2") or ip.endswith(".47"):
                return DiscoveryHit(kind="moonraker", ip=ip, display_info={"klippyState": "ready"})
            return None

        hits = sweep_subnet(
            "192.168.1.0",
            "255.255.255.0",
            "192.168.1.50",
            probe_fn=stub_probe,
            parallelism=8,
            overall_budget_sec=5.0,
        )
        # .2 and .47 hit; .102, .247 also end in .2/.47? No — endswith matches
        # the LAST char only if we use .endswith('.2') strict. Let's count.
        ips = {h.ip for h in hits}
        self.assertIn("192.168.1.2", ips)
        self.assertIn("192.168.1.47", ips)
        # Sanity: at least two hits, all moonraker kind.
        self.assertGreaterEqual(len(hits), 2)
        for h in hits:
            self.assertEqual(h.kind, "moonraker")

    def test_no_hits_when_stub_returns_none(self):
        hits = sweep_subnet(
            "192.168.1.0",
            "255.255.255.0",
            "192.168.1.50",
            probe_fn=lambda ip: None,
            parallelism=4,
            overall_budget_sec=3.0,
        )
        self.assertEqual(hits, [])

    def test_swallows_probe_exceptions(self):
        # A probe that raises on every call must NOT bubble up. The sweep
        # should silently complete with zero hits.
        def boom(ip: str):
            raise RuntimeError(f"probe blew up for {ip}")

        hits = sweep_subnet(
            "192.168.1.0",
            "255.255.255.0",
            "192.168.1.50",
            probe_fn=boom,
            parallelism=4,
            overall_budget_sec=3.0,
        )
        # Exceptions from .result() bubble inside as_completed iteration;
        # the sweep already wraps as_completed in try/except for the
        # timeout — but per-future result() exceptions need to be caught
        # too. We accept either behavior: zero hits is the contract;
        # crashing is not. (If this fails, fix sweep_subnet.)
        self.assertEqual(hits, [])

    def test_empty_target_list_returns_empty(self):
        # A subnet of just the Pi itself + .1 + broadcast = no targets.
        hits = sweep_subnet(
            "192.168.1.0",
            "255.255.255.252",  # /30 = 4 IPs; .50 isn't even in network
            "192.168.1.50",
            probe_fn=lambda ip: None,
        )
        # Whatever IPs are enumerated, no hits because stub returns None.
        self.assertEqual(hits, [])


class TestProbeMoonraker(unittest.TestCase):
    """Direct probe tests — monkey-patch urlopen."""

    def test_moonraker_hit_with_followups(self):
        # First response = /server/info, then /printer/info, then
        # /machine/system_info. Each returns a stub Moonraker payload.
        responses = [
            json.dumps(
                {"result": {"klippy_state": "ready", "moonraker_version": "v0.9.3"}}
            ).encode("utf-8"),
            json.dumps(
                {"result": {"hostname": "voron24", "software_version": "v0.12.0"}}
            ).encode("utf-8"),
            json.dumps(
                {
                    "result": {
                        "system_info": {
                            "cpu_info": {
                                "hardware_description": "BCM2711 ARMv8 Processor rev 3"
                            }
                        }
                    }
                }
            ).encode("utf-8"),
        ]
        with mock.patch("makeros_hub.lan_scan.urllib.request.urlopen") as m:

            def _open(req, timeout=None):
                payload = responses.pop(0)
                resp = mock.MagicMock()
                resp.status = 200
                resp.read = io.BytesIO(payload).read
                resp.__enter__ = lambda s: s
                resp.__exit__ = lambda s, *a: None
                return resp

            m.side_effect = _open
            hit = probe_moonraker("192.168.1.47", timeout=1.0)

        self.assertIsNotNone(hit)
        assert hit is not None  # for mypy
        self.assertEqual(hit.kind, "moonraker")
        self.assertEqual(hit.ip, "192.168.1.47")
        self.assertEqual(hit.hostname, "voron24")
        self.assertEqual(hit.display_info["klippyState"], "ready")
        self.assertEqual(hit.display_info["klippyVersion"], "v0.12.0")
        self.assertEqual(hit.display_info["moonrakerVersion"], "v0.9.3")
        self.assertEqual(
            hit.display_info["hostHardware"], "BCM2711 ARMv8 Processor rev 3"
        )

    def test_non_moonraker_response_returns_none(self):
        # A 200 with the wrong shape — e.g. an OctoPrint API root or a
        # random web server — must yield no hit.
        with mock.patch("makeros_hub.lan_scan.urllib.request.urlopen") as m:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read = io.BytesIO(b'{"api": "0.1.0", "server": "octoprint"}').read
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            m.return_value = resp
            self.assertIsNone(probe_moonraker("192.168.1.99", timeout=1.0))

    def test_connection_refused_returns_none(self):
        # Most LAN IPs don't run Moonraker — refusal is the common case.
        with mock.patch("makeros_hub.lan_scan.urllib.request.urlopen") as m:
            m.side_effect = ConnectionRefusedError("refused")
            self.assertIsNone(probe_moonraker("192.168.1.99", timeout=1.0))

    def test_minimal_moonraker_with_no_followup_still_hits(self):
        # /server/info succeeds; both follow-ups time out — the hit still
        # lands with just the moonraker_version + klippyState.
        call_count = {"n": 0}

        def _open(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                resp = mock.MagicMock()
                resp.status = 200
                resp.read = io.BytesIO(
                    json.dumps(
                        {"result": {"klippy_state": "shutdown", "moonraker_version": "v0.9.4"}}
                    ).encode("utf-8")
                ).read
                resp.__enter__ = lambda s: s
                resp.__exit__ = lambda s, *a: None
                return resp
            raise TimeoutError("follow-up timed out")

        with mock.patch("makeros_hub.lan_scan.urllib.request.urlopen", side_effect=_open):
            hit = probe_moonraker("192.168.1.50", timeout=1.0)

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.display_info["klippyState"], "shutdown")
        self.assertEqual(hit.display_info["moonrakerVersion"], "v0.9.4")
        self.assertIsNone(hit.hostname)
        self.assertNotIn("klippyVersion", hit.display_info)
        self.assertNotIn("hostHardware", hit.display_info)


class TestDiscoveryHitSerialization(unittest.TestCase):
    def test_to_dict_drops_internal_observed_at(self):
        hit = DiscoveryHit(
            kind="moonraker",
            ip="1.2.3.4",
            hostname="host",
            display_info={"klippyState": "ready"},
            observed_at=12345.0,
        )
        out = hit.to_dict()
        self.assertEqual(out, {
            "kind": "moonraker",
            "ip": "1.2.3.4",
            "hostname": "host",
            "displayInfo": {"klippyState": "ready"},
        })
        # observed_at is internal; the cloud uses server-side lastSeenAt.
        self.assertNotIn("observedAt", out)

    def test_to_dict_handles_missing_hostname(self):
        hit = DiscoveryHit(kind="bambu", ip="10.0.0.5", display_info={"model": "X1C"})
        out = hit.to_dict()
        self.assertIsNone(out["hostname"])


if __name__ == "__main__":
    unittest.main()
