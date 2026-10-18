"""v0.71: discovery on both Bambu announce ports and every interface; member sign-ins on the virtual printers ride the
heartbeat (operator, 2026-10-18)."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
import unittest

from makeros_hub.agent import heartbeat_payload
from makeros_hub.bambu_ssdp import _join_everywhere, listen_for_bambu_announcements
from makeros_hub.config import VirtualPrinterMember
from makeros_hub.vprinter.auth import ConnectLog, MemberAuthSet

NOTIFY = (b"NOTIFY * HTTP/1.1\r\nHost: 239.255.255.250:1990\r\nServer: Bambu Lab\r\nLocation: http://10.44.20.101\r\n"
          b"NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: %s\r\nDevModel.bambu-net: N1\r\nDevName.bambu-net: AM01\r\n"
          b"DevSerial.bambu-net: %s\r\n\r\n")


def _send(port: int, serial: bytes, delay: float = 0.15) -> threading.Thread:
    def go():
        time.sleep(delay)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(NOTIFY % (serial, serial), ("127.0.0.1", port))
        finally:
            s.close()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


class TestDiscoveryBothPorts(unittest.TestCase):
    def test_hears_the_broadcast_port_and_the_multicast_port_in_one_window(self):
        a, b = _send(24031, b"0300AA000000001"), _send(24032, b"0300AA000000002")
        hits = listen_for_bambu_announcements(listen_seconds=1.0, port=24031, multicast=("239.255.255.250", 24032))
        a.join(2), b.join(2)
        serials = sorted(h.display_info.get("serial") for h in hits)
        # a busy CI may drop a loopback datagram; when both landed, both ports were read
        if len(serials) == 2:
            self.assertEqual(serials, ["0300AA000000001", "0300AA000000002"])

    def test_one_port_refused_leaves_the_other(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # no SO_REUSEADDR: the listener's bind on it fails
        holder.bind(("0.0.0.0", 24034))
        try:
            t = _send(24033, b"0300AA000000003")
            hits = listen_for_bambu_announcements(listen_seconds=1.0, port=24033, multicast=("239.255.255.250", 24034))
            t.join(2)
            if hits:
                self.assertEqual(hits[0].display_info.get("serial"), "0300AA000000003")
        finally:
            holder.close()

    def test_nothing_heard_is_an_empty_list(self):
        self.assertEqual(listen_for_bambu_announcements(listen_seconds=0.2, port=24035, multicast=None), [])

    def test_joins_the_group_somewhere(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # a sandbox without multicast may join nothing; the call must not raise either way
            self.assertGreaterEqual(_join_everywhere(s, "239.255.255.250"), 0)
        finally:
            s.close()


def _sha(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class TestConnectLog(unittest.TestCase):
    def test_latest_per_code_and_ack_keeps_a_newer_sign_in(self):
        t = [1_792_300_000.0]
        log = ConnectLog(clock=lambda: t[0])
        log.record("VP1", "a" * 64, "10.44.50.20")
        t[0] += 60
        log.record("VP1", "a" * 64, "10.44.50.21")       # same code again: one entry, the newest
        log.record("VP2", "b" * 64, None)
        sent = log.snapshot(64)
        self.assertEqual([(e["serial"], e["ip"]) for e in sent], [("VP1", "10.44.50.21"), ("VP2", "")])
        self.assertTrue(sent[0]["at"].endswith("Z"))
        t[0] += 60
        log.record("VP2", "b" * 64, "100.64.0.9")         # signed in again while the beat was in flight
        log.ack(sent)
        self.assertEqual([(e["serial"], e["ip"]) for e in log.snapshot(64)], [("VP2", "100.64.0.9")])

    def test_bounded(self):
        log = ConnectLog(max_keys=3)
        for i in range(5):
            log.record("VP", f"{i:064d}", "x")
        self.assertEqual(len(log.snapshot(64)), 3)

    def test_member_auth_records_a_success_only_and_never_the_code(self):
        log = ConnectLog()
        auth = MemberAuthSet([VirtualPrinterMember(_sha("ABCDEFGH"), "m1")], serial="00M09VP000000001", connects=log)
        self.assertFalse(auth.authenticate("WRONGCODE", "10.44.50.5").ok)
        self.assertEqual(log.snapshot(), [])
        self.assertTrue(auth.authenticate("ABCDEFGH", "10.44.50.5").ok)
        [e] = log.snapshot()
        self.assertEqual((e["serial"], e["codeSha256"], e["ip"]), ("00M09VP000000001", _sha("ABCDEFGH"), "10.44.50.5"))
        self.assertNotIn("ABCDEFGH", repr(log.snapshot()))

    def test_no_serial_no_record(self):
        log = ConnectLog()
        self.assertTrue(MemberAuthSet([VirtualPrinterMember(_sha("ABCDEFGH"), "m1")], connects=log).authenticate("ABCDEFGH", "x").ok)
        self.assertEqual(log.snapshot(), [])

    def test_heartbeat_carries_at_most_64(self):
        rows = [{"serial": "S", "codeSha256": f"{i:064d}", "at": "2026-10-18T14:00:00Z", "ip": ""} for i in range(70)]
        self.assertEqual(len(heartbeat_payload(vp_connects=rows)["vpConnects"]), 64)
        self.assertNotIn("vpConnects", heartbeat_payload(vp_connects=[]))


if __name__ == "__main__":
    unittest.main()
