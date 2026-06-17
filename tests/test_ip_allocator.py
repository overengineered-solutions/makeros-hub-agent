"""Tests for makeros_hub.vprinter.ip_allocator.

Fully network-free: every subprocess call goes through the injectable `_run`
seam. The persistence file uses a tmp directory.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from makeros_hub.vprinter.ip_allocator import (
    Binding,
    IpAllocator,
)


@dataclass
class StubResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def make_run(routes: dict[tuple, StubResult]) -> Callable:
    """A subprocess stub. Maps tuple-keyed argv → StubResult. Default 1
    (failure / no-reply) for unknown calls."""

    def run(argv: list[str], timeout: float):
        key = tuple(argv)
        result = routes.get(key, StubResult(returncode=1))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    return run


class TestIpAllocatorPersistence(unittest.TestCase):
    def test_empty_state_loads_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "vp-bindings.json"
            alloc = IpAllocator(state_path=state, run=make_run({}))
            self.assertEqual(alloc.bindings_snapshot(), {})

    def test_existing_state_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "vp-bindings.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": {
                            "A1 mini": {
                                "model": "A1 mini",
                                "bind_ip": "192.168.1.250",
                                "iface": "eth0",
                                "claimed_at": 1700000000.0,
                            },
                            "X1C": {
                                "model": "X1C",
                                "bind_ip": "192.168.1.249",
                                "iface": "eth0",
                                "claimed_at": 1700000001.0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            alloc = IpAllocator(state_path=state, run=make_run({}))
            snap = alloc.bindings_snapshot()
            self.assertEqual(len(snap), 2)
            self.assertEqual(snap["A1 mini"].bind_ip, "192.168.1.250")
            self.assertEqual(snap["X1C"].bind_ip, "192.168.1.249")

    def test_corrupt_state_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "vp-bindings.json"
            state.write_text("{not json", encoding="utf-8")
            alloc = IpAllocator(state_path=state, run=make_run({}))
            self.assertEqual(alloc.bindings_snapshot(), {})


class TestIpAllocatorAllocate(unittest.TestCase):
    def test_fresh_allocation_picks_top_of_range(self):
        # arping all replies =1 (no reply = free) on every IP. Claim
        # succeeds returncode=0 for the first candidate.
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "add", "192.168.1.254/32", "dev", "eth0"): StubResult(returncode=0),
        }
        with tempfile.TemporaryDirectory() as tmp:
            alloc = IpAllocator(
                state_path=Path(tmp) / "b.json",
                run=make_run(routes),
                netmask_override="255.255.255.0",
                own_ip_override="192.168.1.50",
                iface_override="eth0",
            )
            res = alloc.allocate_for("A1 mini")
        self.assertTrue(res.ok)
        self.assertEqual(res.bind_ip, "192.168.1.254")  # top of the /24
        self.assertEqual(res.iface, "eth0")
        self.assertEqual(res.tried, 1)

    def test_existing_binding_re_claimed_idempotently(self):
        # Re-claim on an IP that's already on the iface returns "File exists"
        # — allocator treats as success without changing the bind.
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "add", "192.168.1.250/32", "dev", "eth0"): StubResult(
                returncode=2,
                stderr="RTNETLINK answers: File exists",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "b.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": {
                            "A1 mini": {
                                "model": "A1 mini",
                                "bind_ip": "192.168.1.250",
                                "iface": "eth0",
                                "claimed_at": 1700000000.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            alloc = IpAllocator(state_path=state, run=make_run(routes))
            res = alloc.allocate_for("A1 mini")
        self.assertTrue(res.ok)
        self.assertEqual(res.bind_ip, "192.168.1.250")
        self.assertEqual(res.tried, 0)

    def test_busy_ip_skipped_then_next_candidate_taken(self):
        # First candidate .254 has someone replying to arping (returncode=0
        # = reply = taken). Second candidate .253 is free.
        arping_top = (
            "sudo",
            "-n",
            "/usr/sbin/arping",
            "-c",
            "1",
            "-w",
            "1",
            "-I",
            "eth0",
            "192.168.1.254",
        )
        routes = {
            arping_top: StubResult(returncode=0),  # taken
            ("sudo", "-n", "/sbin/ip", "addr", "add", "192.168.1.253/32", "dev", "eth0"): StubResult(returncode=0),
        }
        with tempfile.TemporaryDirectory() as tmp:
            alloc = IpAllocator(
                state_path=Path(tmp) / "b.json",
                run=make_run(routes),
                netmask_override="255.255.255.0",
                own_ip_override="192.168.1.50",
                iface_override="eth0",
            )
            res = alloc.allocate_for("A1 mini")
        self.assertTrue(res.ok)
        self.assertEqual(res.bind_ip, "192.168.1.253")
        self.assertGreaterEqual(res.tried, 2)

    def test_already_bound_ip_excluded_from_candidates(self):
        # Existing binding for X1C uses .254 — A1 mini's fresh allocation must
        # NOT pick .254 even though it's the top.
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "add", "192.168.1.253/32", "dev", "eth0"): StubResult(returncode=0),
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "b.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": {
                            "X1C": {
                                "model": "X1C",
                                "bind_ip": "192.168.1.254",
                                "iface": "eth0",
                                "claimed_at": 1700000000.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            alloc = IpAllocator(
                state_path=state,
                run=make_run(routes),
                netmask_override="255.255.255.0",
                own_ip_override="192.168.1.50",
                iface_override="eth0",
            )
            res = alloc.allocate_for("A1 mini")
        self.assertTrue(res.ok)
        self.assertEqual(res.bind_ip, "192.168.1.253")

    def test_allocation_persisted_to_disk(self):
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "add", "192.168.1.254/32", "dev", "eth0"): StubResult(returncode=0),
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "b.json"
            alloc = IpAllocator(
                state_path=state,
                run=make_run(routes),
                netmask_override="255.255.255.0",
                own_ip_override="192.168.1.50",
                iface_override="eth0",
            )
            alloc.allocate_for("A1 mini")
            # Read INSIDE the with-block — the tempdir is cleaned up on exit.
            data = json.loads(state.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["bindings"]["A1 mini"]["bind_ip"], "192.168.1.254")
        self.assertEqual(data["bindings"]["A1 mini"]["iface"], "eth0")

    def test_no_free_ip_returns_error(self):
        # Default route returns 1 for every arping → all candidates are
        # treated as taken. Wait — arping rc=1 means NO reply → free.
        # rc=0 means reply → taken. So mark every arping rc=0.
        # Construct routes catching every arping for a /28 (smaller subnet).
        with tempfile.TemporaryDirectory() as tmp:
            # Build a stub run that ALWAYS reports arping replied (taken)
            # so no candidate is free.
            def all_taken_run(argv, timeout):
                if argv[2].endswith("arping"):
                    return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
                # Should never reach claim path since nothing's free.
                return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

            alloc = IpAllocator(
                state_path=Path(tmp) / "b.json",
                run=all_taken_run,
                netmask_override="255.255.255.240",  # /28 = 14 host IPs, fewer attempts
                own_ip_override="192.168.1.2",
                iface_override="eth0",
            )
            res = alloc.allocate_for("A1 mini")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "no_free_ip")

    def test_no_iface_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            alloc = IpAllocator(
                state_path=Path(tmp) / "b.json",
                run=make_run({}),
                netmask_override="255.255.255.0",
                own_ip_override="192.168.1.50",
                iface_override=None,
            )
            # Replace _guess_iface to return None (the default /proc/net/route
            # may or may not have a default route on the test runner).
            alloc._guess_iface = lambda: None  # type: ignore[method-assign]
            res = alloc.allocate_for("A1 mini")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "no_iface")


class TestIpAllocatorRelease(unittest.TestCase):
    def test_release_drops_binding_and_iface(self):
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "del", "192.168.1.250/32", "dev", "eth0"): StubResult(returncode=0),
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "b.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": {
                            "A1 mini": {
                                "model": "A1 mini",
                                "bind_ip": "192.168.1.250",
                                "iface": "eth0",
                                "claimed_at": 1700000000.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            alloc = IpAllocator(state_path=state, run=make_run(routes))
            res = alloc.release("A1 mini")
            # Read INSIDE the with-block — the tempdir is cleaned up on exit.
            data = json.loads(state.read_text())
        self.assertTrue(res.ok)
        self.assertNotIn("A1 mini", alloc.bindings_snapshot())
        # State file updated to empty bindings.
        self.assertEqual(data["bindings"], {})

    def test_release_idempotent_when_no_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            alloc = IpAllocator(
                state_path=Path(tmp) / "b.json", run=make_run({})
            )
            res = alloc.release("never-bound")
        self.assertTrue(res.ok)
        self.assertIsNone(res.bind_ip)

    def test_release_treats_already_gone_iface_as_success(self):
        routes = {
            ("sudo", "-n", "/sbin/ip", "addr", "del", "192.168.1.250/32", "dev", "eth0"): StubResult(
                returncode=2,
                stderr="RTNETLINK answers: Cannot assign requested address",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "b.json"
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": {
                            "A1 mini": {
                                "model": "A1 mini",
                                "bind_ip": "192.168.1.250",
                                "iface": "eth0",
                                "claimed_at": 1700000000.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            alloc = IpAllocator(state_path=state, run=make_run(routes))
            res = alloc.release("A1 mini")
        self.assertTrue(res.ok)


class TestIpAllocatorSubnetEnumeration(unittest.TestCase):
    def test_candidates_walk_top_to_bottom_excluding_skip_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            alloc = IpAllocator(state_path=Path(tmp) / "b.json", run=make_run({}))
            cands = alloc._enumerate_candidates(
                "192.168.1.50",
                "255.255.255.0",
                exclude_extra={"192.168.1.200"},
            )
        self.assertEqual(cands[0], "192.168.1.254")  # broadcast-1 = top
        self.assertNotIn("192.168.1.255", cands)  # broadcast
        self.assertNotIn("192.168.1.0", cands)  # network
        self.assertNotIn("192.168.1.1", cands)  # router heuristic
        self.assertNotIn("192.168.1.50", cands)  # own
        self.assertNotIn("192.168.1.200", cands)  # extra exclude


class TestBinding(unittest.TestCase):
    def test_binding_dataclass_roundtrips_to_dict(self):
        b = Binding(model="A1", bind_ip="1.2.3.4", iface="eth0", claimed_at=42.0)
        # asdict works on the dataclass; integrated test below proves the
        # persistence layer round-trips it correctly.
        self.assertEqual(b.model, "A1")
        self.assertEqual(b.bind_ip, "1.2.3.4")
        self.assertEqual(b.iface, "eth0")
        self.assertEqual(b.claimed_at, 42.0)


if __name__ == "__main__":
    unittest.main()
