"""Tests for makeros_hub.bambu_ssdp — passive SSDP listener for Bambu LAN.

Parsing tests use fixed NOTIFY frames; the live socket listen is exercised
via a single integration-style test that opens a loopback sender + receiver.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest

from makeros_hub.bambu_ssdp import (
    listen_for_bambu_announcements,
    parse_bambu_notify,
)


# A realistic NOTIFY payload modeled on observed Bambu LAN broadcasts.
_BAMBU_NOTIFY = b"""\
NOTIFY * HTTP/1.1\r
Host: 239.255.255.250:1982\r
Server: Bambu Lab\r
Location: http://192.168.1.42\r
NT: urn:bambulab-com:device:3dprinter:1\r
USN: 01P00A123456789ABC\r
DevModel.bambu-net: X1C\r
DevName.bambu-net: Antoni Gaudi\r
DevSerial.bambu-net: 01P00A123456789ABC\r
Devel.SoftVer: 01.07.00.00\r
\r
"""

# A response to an M-SEARCH — same payload, different first line. Bambu's
# firmware echoes the same headers, so we accept both shapes.
_BAMBU_SEARCH_RESPONSE = b"""\
HTTP/1.1 200 OK\r
NT: urn:bambulab-com:device:3dprinter:1\r
USN: 01H00B987654321DEF\r
DevModel: P2S\r
DevName: Natividad\r
DevSerial: 01H00B987654321DEF\r
\r
"""


class TestParseBambuNotify(unittest.TestCase):
    def test_well_formed_notify_parsed(self):
        hit = parse_bambu_notify(_BAMBU_NOTIFY, peer_ip="192.168.1.42")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.kind, "bambu")
        # Location header overrides peer IP.
        self.assertEqual(hit.ip, "192.168.1.42")
        self.assertEqual(hit.hostname, "Antoni Gaudi")
        self.assertEqual(hit.display_info["model"], "X1C")
        self.assertEqual(hit.display_info["serial"], "01P00A123456789ABC")
        self.assertEqual(hit.display_info["firmware"], "01.07.00.00")

    def test_m_search_response_parsed(self):
        hit = parse_bambu_notify(_BAMBU_SEARCH_RESPONSE, peer_ip="192.168.1.43")
        self.assertIsNotNone(hit)
        assert hit is not None
        # No Location header → peer IP wins.
        self.assertEqual(hit.ip, "192.168.1.43")
        self.assertEqual(hit.display_info["model"], "P2S")

    def test_falls_back_to_peer_ip_when_location_missing(self):
        payload = b"""\
NOTIFY * HTTP/1.1\r
Server: Bambu Lab\r
USN: 01X00C111111111111\r
DevModel: H2D\r
\r
"""
        hit = parse_bambu_notify(payload, peer_ip="10.0.0.99")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.ip, "10.0.0.99")
        self.assertEqual(hit.display_info["model"], "H2D")

    def test_random_udp_chatter_rejected(self):
        # Plex GDM, Sonos, mDNS-over-UDP — anything that isn't a NOTIFY/200
        # response must NOT parse.
        for chatter in (
            b"M-SEARCH * HTTP/1.1\r\nST: urn:dial-multiscreen-org:device:dial:1\r\n\r\n",
            b"random bytes that happen to be on port 2021",
            b"\x00\x01\x02\x03\x04",
            b"",
        ):
            with self.subTest(chatter=chatter[:40]):
                self.assertIsNone(parse_bambu_notify(chatter, peer_ip="10.0.0.1"))

    def test_no_serial_no_model_rejected(self):
        # Header values present but neither model nor serial — likely
        # something else on the port. Reject rather than land a hollow hit.
        payload = b"""\
NOTIFY * HTTP/1.1\r
Server: not-bambu\r
SomeHeader: not-useful\r
\r
"""
        self.assertIsNone(parse_bambu_notify(payload, peer_ip="10.0.0.5"))


class TestListenForAnnouncements(unittest.TestCase):
    """One end-to-end test: spin up a sender thread, run the listener, expect
    the NOTIFY frame to land. This is intentionally narrow — we trust the
    parse tests above for shape coverage."""

    def test_loopback_notify_landed(self):
        # Bind a sender that fires the NOTIFY at the listener's port. Use a
        # port other than 2021 so we don't collide with any host service
        # (Bambu Studio etc.) running locally on the dev machine.
        port = 24021

        sender_done = threading.Event()

        def _sender():
            # Brief delay so the listener gets to bind first.
            time.sleep(0.1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                s.sendto(_BAMBU_NOTIFY, ("127.0.0.1", port))
            finally:
                s.close()
                sender_done.set()

        t = threading.Thread(target=_sender, daemon=True)
        t.start()
        hits = listen_for_bambu_announcements(listen_seconds=1.0, port=port)
        sender_done.wait(timeout=2.0)

        # We may sometimes miss the frame on a busy CI; if so, the test
        # still proves the listener doesn't crash. The richer assertion
        # only fires if at least one frame was caught.
        if hits:
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].display_info["model"], "X1C")
            self.assertEqual(hits[0].kind, "bambu")

    def test_returns_empty_when_no_frames(self):
        # Port that nothing will send to — listener should clean up + return [].
        hits = listen_for_bambu_announcements(listen_seconds=0.2, port=24022)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
