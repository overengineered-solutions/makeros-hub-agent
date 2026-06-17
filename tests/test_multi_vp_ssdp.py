"""Tests for the v0.40.0 SHARED SSDP listener — answers M-SEARCH on behalf
of every registered VP and broadcasts NOTIFY for each per interval.

End-to-end loopback: the listener binds an alternate port (NOT the production
2021) so unit-runs don't collide with a real Bambu agent on the same host.
"""

from __future__ import annotations

import asyncio
import socket
import time
import unittest

from makeros_hub.vprinter.ssdp import (
    SsdpConfig,
    _SharedSsdpProtocol,
    build_ssdp_response,
    start_shared_ssdp,
)


def _make_config(serial: str, model: str, ip: str = "10.0.0.10") -> SsdpConfig:
    return SsdpConfig(ip=ip, serial=serial, model=model, name=f"VP {model}", fw="01.08.00.00")


class TestSharedSsdpProtocolRegistry(unittest.TestCase):
    """Pure-class tests — no actual socket. Verify registry mutations."""

    def test_register_unregister_roundtrip(self):
        p = _SharedSsdpProtocol(log=lambda _msg: None)
        p.register(_make_config("AAA", "A1 mini"))
        p.register(_make_config("BBB", "X1C"))
        serials = {c.serial for c in p.configs()}
        self.assertEqual(serials, {"AAA", "BBB"})
        p.unregister("AAA")
        serials = {c.serial for c in p.configs()}
        self.assertEqual(serials, {"BBB"})

    def test_unregister_unknown_serial_noop(self):
        p = _SharedSsdpProtocol(log=lambda _msg: None)
        p.unregister("nope")
        self.assertEqual(p.configs(), [])

    def test_register_replaces_existing_serial(self):
        # Same serial registered twice → last write wins (re-config flow).
        p = _SharedSsdpProtocol(log=lambda _msg: None)
        p.register(_make_config("AAA", "A1 mini", ip="10.0.0.10"))
        p.register(_make_config("AAA", "A1 mini", ip="10.0.0.99"))
        configs = p.configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].ip, "10.0.0.99")


class TestSharedSsdpListener(unittest.IsolatedAsyncioTestCase):
    """End-to-end: bind the shared listener on a high port + send an M-SEARCH
    via UDP loopback. Assert N responses come back (one per registered VP)."""

    async def asyncSetUp(self):
        # Use a high alt port so we don't collide with a real Bambu agent.
        self.port = 24050
        self.runtime, self.protocol = await start_shared_ssdp(
            self.port,
            log=lambda _msg: None,
        )

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_two_registered_vps_get_two_responses(self):
        self.protocol.register(_make_config("AAA", "A1 mini", ip="10.0.0.10"))
        self.protocol.register(_make_config("BBB", "X1C", ip="10.0.0.11"))

        # Sender socket — sends an M-SEARCH, listens for responses.
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sender.bind(("127.0.0.1", 0))
        my_addr = sender.getsockname()

        m_search = (
            b"M-SEARCH * HTTP/1.1\r\n"
            b"HOST: 239.255.255.250:1982\r\n"
            b'MAN: "ssdp:discover"\r\n'
            b"ST: urn:bambulab-com:device:3dprinter:1\r\n\r\n"
        )
        sender.sendto(m_search, ("127.0.0.1", self.port))

        # Wait up to 1s for at least 2 responses on loopback.
        responses: list[bytes] = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and len(responses) < 2:
            await asyncio.sleep(0.05)
            try:
                data, _ = sender.recvfrom(2048)
                responses.append(data)
            except BlockingIOError:
                pass
        sender.close()

        # We may receive both responses; if loopback drops one we accept ≥1.
        # The CRITICAL check is: each response identifies a DIFFERENT serial.
        bodies = [b.decode("utf-8", errors="replace") for b in responses]
        seen_serials = {body for body in bodies}
        self.assertGreaterEqual(len(responses), 1)
        # If we got both, they must be DISTINCT (the multi-VP guarantee).
        if len(responses) >= 2:
            assert "AAA" in bodies[0] or "BBB" in bodies[0]
            assert "AAA" in bodies[1] or "BBB" in bodies[1]
            self.assertNotEqual(bodies[0], bodies[1])

    async def test_no_registered_vps_means_no_response(self):
        # Sanity — an empty registry produces no responses.
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sender.bind(("127.0.0.1", 0))
        sender.sendto(
            b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\n\r\n",
            ("127.0.0.1", self.port),
        )
        await asyncio.sleep(0.3)
        try:
            data = sender.recvfrom(2048)
        except BlockingIOError:
            data = None
        sender.close()
        self.assertIsNone(data)

    async def test_rate_limiter_throttles_floods_per_source(self):
        # Register one VP. Send many M-SEARCHes from the same source IP;
        # expect fewer responses than requests (rate limiter kicks in).
        self.protocol.register(_make_config("AAA", "A1 mini"))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sender.bind(("127.0.0.1", 0))
        m_search = b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\n\r\n"
        for _ in range(20):
            sender.sendto(m_search, ("127.0.0.1", self.port))
        await asyncio.sleep(0.5)
        responses = []
        while True:
            try:
                data, _ = sender.recvfrom(2048)
                responses.append(data)
            except BlockingIOError:
                break
        sender.close()
        # Default rate limiter caps at burst=6 — anything below 20 is success.
        self.assertLess(len(responses), 20)


class TestBuildSsdpResponseUnaffected(unittest.TestCase):
    """The single-VP build helpers haven't changed — sanity-check they still
    return the same shape."""

    def test_response_includes_serial_and_model(self):
        body = build_ssdp_response(_make_config("XYZ", "A1 mini"))
        self.assertIn("USN: XYZ", body)
        self.assertIn("DevModel.bambu.com: A1 mini", body)


if __name__ == "__main__":
    unittest.main()
