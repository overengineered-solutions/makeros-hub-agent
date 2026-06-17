"""Tests for makeros_hub.discovery — the facade combining LAN scan + SSDP.

Network-free: monkey-patch the sweep + listen helpers.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from makeros_hub import discovery
from makeros_hub.lan_scan import DiscoveryHit


def _stub_moonraker_hits():
    return [
        DiscoveryHit(
            kind="moonraker",
            ip="192.168.1.47",
            hostname="voron24",
            display_info={"klippyState": "ready"},
            observed_at=1.0,
        )
    ]


def _stub_bambu_hits():
    return [
        DiscoveryHit(
            kind="bambu",
            ip="192.168.1.42",
            hostname="Antoni Gaudi",
            display_info={"model": "X1C", "serial": "01P00A..."},
            observed_at=1.5,
        )
    ]


class TestDiscoveryFacade(unittest.TestCase):
    def setUp(self):
        discovery.reset_for_tests()

    def test_periodic_runs_when_no_prior_sweep(self):
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=_stub_moonraker_hits()), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=_stub_bambu_hits()):
            fired = discovery.maybe_run_periodic_scan(now=1000.0)
        self.assertTrue(fired)
        cached = discovery.get_cached_hits(now=1000.0)
        self.assertEqual(len(cached), 2)
        # Bambu comes first per the merge ordering (more identifying info).
        self.assertEqual(cached[0].kind, "bambu")

    def test_periodic_skipped_inside_interval(self):
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=[]), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=[]):
            discovery.maybe_run_periodic_scan(now=1000.0)
            # Second call <PERIODIC_INTERVAL_SEC later — must skip.
            fired = discovery.maybe_run_periodic_scan(now=1010.0)
        self.assertFalse(fired)

    def test_periodic_re_fires_after_interval(self):
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=[]) as m_lan, \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=[]):
            discovery.maybe_run_periodic_scan(now=1000.0)
            self.assertEqual(m_lan.call_count, 1)
            fired = discovery.maybe_run_periodic_scan(
                now=1000.0 + discovery.PERIODIC_INTERVAL_SEC + 1.0
            )
        self.assertTrue(fired)

    def test_immediate_scan_bypasses_rate_limit(self):
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=_stub_moonraker_hits()), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=_stub_bambu_hits()):
            # First periodic
            discovery.maybe_run_periodic_scan(now=1000.0)
            # Immediate immediately after — must fire again.
            hits = discovery.run_immediate_scan()
        self.assertEqual(len(hits), 2)

    def test_cache_expires_after_ttl(self):
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=_stub_moonraker_hits()), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=[]):
            discovery.maybe_run_periodic_scan(now=1000.0)
        # Past TTL → cache returns empty even though the underlying scan succeeded.
        cached = discovery.get_cached_hits(now=1000.0 + discovery.HIT_CACHE_TTL_SEC + 1.0)
        self.assertEqual(cached, [])

    def test_merge_dedups_by_kind_and_ip(self):
        # Bambu and Moonraker BOTH point at the same IP — both stay, since the
        # kind differs (rare in practice, but the contract).
        dup_bambu = [DiscoveryHit(kind="bambu", ip="192.168.1.99")]
        dup_moonraker = [DiscoveryHit(kind="moonraker", ip="192.168.1.99")]
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=dup_moonraker), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=dup_bambu):
            discovery.maybe_run_periodic_scan(now=1000.0)
            cached = discovery.get_cached_hits(now=1000.0)
        self.assertEqual(len(cached), 2)

    def test_merge_dedups_within_kind(self):
        # Two NOTIFY frames from the same Bambu printer in the same listen
        # window — only one hit must land.
        same_bambu = [
            DiscoveryHit(kind="bambu", ip="192.168.1.42", display_info={"model": "X1C"}),
            DiscoveryHit(kind="bambu", ip="192.168.1.42", display_info={"model": "X1C", "stale": True}),
        ]
        with mock.patch.object(discovery.lan_scan, "run_scan", return_value=[]), \
             mock.patch.object(discovery.bambu_ssdp, "listen_for_bambu_announcements", return_value=same_bambu):
            discovery.maybe_run_periodic_scan(now=1000.0)
            cached = discovery.get_cached_hits(now=1000.0)
        self.assertEqual(len(cached), 1)
        # First wins.
        self.assertNotIn("stale", cached[0].display_info)

    def test_hits_to_payload_serializes_each(self):
        hits = _stub_moonraker_hits() + _stub_bambu_hits()
        payload = discovery.hits_to_payload(hits)
        self.assertEqual(len(payload), 2)
        for entry in payload:
            self.assertIn("kind", entry)
            self.assertIn("ip", entry)
            self.assertIn("hostname", entry)
            self.assertIn("displayInfo", entry)

    def test_probe_output_is_compact_json(self):
        hits = _stub_moonraker_hits()
        out = discovery.hits_to_json_for_probe(hits)
        # No spaces — important for the probe size budget.
        self.assertNotIn(", ", out)
        self.assertNotIn(": ", out)
        # Round-trips through json.loads.
        loaded = json.loads(out)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["ip"], "192.168.1.47")


if __name__ == "__main__":
    unittest.main()
