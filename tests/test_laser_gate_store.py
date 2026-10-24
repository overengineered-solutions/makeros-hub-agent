"""Laser gate store and never-send guard, contract G8-G11. The controller is a fake UDP socket on 127.0.0.1 with
ephemeral ports; nothing binds 50200 or 40200 and nothing leaves loopback."""
import hashlib
import json
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

import laser_gate_fixtures as fx
from laser_gate.checker import envelope, plan_packets, swizzle, unswizzle
from laser_gate.gate import ACK, Controller, GuardError, StoreExpired, guard

LONG = dict(extra_cuts=700)  # about 2.4 kB: 4 packets at 1000 bytes


def sha(b):
    return hashlib.sha256(b).hexdigest()


class StoreBase(unittest.TestCase):
    def fake(self, **kw):
        f = fx.FakeController(**kw)
        self.addCleanup(f.close)
        return f

    def gate(self, fake, **over):
        g = fx.gate_for(fake, **over)
        if fake:
            self.addCleanup(fake.assert_clean, g.reads)  # G11, for every test that talks to a fake
        return g

    def store(self, gate, body, name="MD0412", cap=100):
        return gate.store(body, name, cap, sha(body), expires=time.time() + 120)


class HappyPath(StoreBase):
    def test_g8_packets_envelope_boundaries_ack_order_and_count(self):
        fake = self.fake()
        fake.ack_delay = 0.03
        gate = self.gate(fake)
        plain = fx.square_plain(**LONG)
        code, out = self.store(gate, swizzle(plain))
        self.assertEqual(code, 200, out)
        jobs = fake.jobs()
        self.assertEqual(out, {"ok": True, "name": "MD0412", "fileCountBefore": 5, "fileCountAfter": 6,
                               "packets": len(jobs), "bytes": sum(map(len, jobs)), "preparedSha256": sha(swizzle(plain))})
        self.assertGreaterEqual(len(jobs), 3)
        self.assertEqual(jobs[0], envelope("MD0412") + plain[:fx.header_end(plain)])
        self.assertEqual(b"".join(jobs)[len(envelope("MD0412")):], plain)  # nothing added, dropped or reordered
        for p in jobs[1:]:
            self.assertLessEqual(len(p), 1000)
            self.assertGreaterEqual(p[0], 0x80)  # starts on a command: cut only between commands
        # each job packet arrived only after the ACK of the one before it had been sent
        got_times = [t for kind, t, d in fake.log if kind == "got" and not d.startswith(b"\xDA\x00")]
        ack_times = [t for kind, t, d in fake.log if kind == "sent" and d == ACK]
        for k in range(1, len(got_times)):
            self.assertTrue(any(got_times[k - 1] <= a <= got_times[k] for a in ack_times), f"packet {k + 1} early")


class Aborts(StoreBase):
    def test_reply_wait_is_bounded_by_the_whole_request(self):
        ctl = self.gate(None).ctl
        sock = mock.Mock()
        with mock.patch("laser_gate.gate.time.monotonic", return_value=1000.0):
            sock.recvfrom.return_value = (ACK, (ctl.ip, 1234))
            self.assertEqual(ctl.recv(sock, 1000.05), ACK)
            self.assertAlmostEqual(sock.settimeout.call_args.args[0], 0.05)
        with mock.patch("laser_gate.gate.time.monotonic", return_value=1000.05):
            with self.assertRaises(StoreExpired):
                ctl.send(sock, b"\xda\x00\x04\x00", 1000.05)
        sock.sendto.assert_not_called()

    def test_waiting_for_status_io_cannot_outlive_expiry(self):
        fake = self.fake()
        gate = self.gate(fake)
        gate._io_lock.acquire()
        try:
            started = time.monotonic()
            self.assertEqual(gate.store(fx.square(), "MD0412", 80, sha(fx.square()), expires=time.time() + 0.1),
                             (409, {"error": "expired"}))
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(gate._store_lock.locked())
            self.assertEqual(fake.got, [])
        finally:
            gate._io_lock.release()

    def test_slow_names_expire_before_any_job_bytes(self):
        fake = self.fake()
        gate = self.gate(fake)
        now = [1000.0]
        original = gate.ctl.name

        def slow_name(sock, index, deadline=None):
            name = original(sock, index, deadline)
            now[0] += 25  # successful replies, but the whole scan exceeds the 120-second request
            return name

        with mock.patch("laser_gate.gate.time.monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(gate.ctl, "name", side_effect=slow_name):
            self.assertEqual(self.store(gate, fx.square()), (409, {"error": "expired"}))
        self.assertEqual(fake.jobs(), [])

    def test_expiry_after_final_idle_read_is_checked_at_first_write(self):
        fake = self.fake()
        gate = self.gate(fake)
        now = [1000.0]
        original = gate.ctl.read
        idle_reads = 0

        def late_read(sock, addr, deadline=None):
            nonlocal idle_reads
            value = original(sock, addr, deadline)
            if addr == 0x0200:
                idle_reads += 1
                if idle_reads == 2:
                    now[0] += 121
            return value

        with mock.patch("laser_gate.gate.time.monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(gate.ctl, "read", side_effect=late_read):
            self.assertEqual(self.store(gate, fx.square()), (409, {"error": "expired"}))
        self.assertEqual(fake.jobs(), [])

    def test_expiry_during_transfer_stops_without_retry(self):
        fake = self.fake()
        gate = self.gate(fake)
        now = [1000.0]
        original = gate.ctl.send

        def slow_send(sock, payload, deadline=None):
            original(sock, payload, deadline)
            if payload.startswith(b"\xe8\x02"):
                now[0] += 121

        with mock.patch("laser_gate.gate.time.monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(gate.ctl, "send", side_effect=slow_send):
            code, result = self.store(gate, swizzle(fx.square_plain(**LONG)))
        self.assertEqual((code, result["error"]), (502, "transfer_aborted"))
        time.sleep(0.05)
        self.assertEqual(len(fake.jobs()), 1)

    def test_g9_a_missing_ack_aborts_and_nothing_more_is_sent(self):
        body = swizzle(fx.square_plain(**LONG))
        for which in ("first", "middle", "last"):
            with self.subTest(dropped=which):
                fake = self.fake()
                gate = self.gate(fake)
                n = len(plan_packets(body, "MD0412", 1000))
                k = {"first": 1, "middle": n // 2 + 1, "last": n}[which]
                fake.drop = {k}
                code, out = self.store(gate, body)
                self.assertEqual((code, out), (502, {"error": "transfer_aborted", "packet": k, "packets": n}))
                time.sleep(0.1)
                self.assertEqual(len(fake.jobs()), k)  # packet k+1 never sent
                self.assertEqual(fake.mem[fx.FILE_COUNT], 5)
                fake.reset()
                code, out = self.store(gate, body)
                self.assertEqual((code, out["fileCountAfter"]), (200, 6))

    def test_g10_non_ack_byte_aborts(self):
        fake = self.fake()
        fake.nak = {2}
        code, out = self.store(self.gate(fake), swizzle(fx.square_plain(**LONG)))
        self.assertEqual((code, out["error"], out["packet"]), (502, "transfer_aborted", 2))
        self.assertEqual(len(fake.jobs()), 2)

    def test_g10_count_must_step_by_exactly_one(self):
        for step, after in ((0, 5), (2, 7)):
            with self.subTest(step=step):
                fake = self.fake(step=step)
                code, out = self.store(self.gate(fake), fx.square())
                self.assertEqual((code, out), (502, {"error": "count_mismatch", "before": 5, "after": after}))

    def test_g10_moving_head_refuses_after_reads_only(self):
        fake = self.fake(status=0x08000000 | 1 << 15)
        code, out = self.store(self.gate(fake), fx.square())
        self.assertEqual((code, out), (409, {"error": "head_moving"}))
        self.assertTrue(fake.got)
        self.assertTrue(all(p.startswith(b"\xDA\x00") and len(p) == 4 for p in fake.got))

    def test_g10_controller_silent(self):
        code, out = self.store(self.gate(None), fx.square())
        self.assertEqual((code, out), (502, {"error": "controller_unreachable"}))

    def test_g10_sha_mismatch_and_refused_file_never_reach_the_controller(self):
        fake = self.fake()
        gate = self.gate(fake)
        body = fx.square()
        self.assertEqual(gate.store(body, "MD0412", 100, "0" * 64, expires=time.time() + 120), (400, {"error": "sha_mismatch"}))
        bad = swizzle(fx.insert_before(fx.square_plain(), 0x88, b"\xD8\x01")[0])
        code, out = self.store(gate, bad)
        self.assertEqual((code, out["error"], out["code"]), (422, "refused", "command_not_allowed"))
        self.assertEqual(fake.got, [])

    def test_g10_concurrent_store_is_busy(self):
        fake = self.fake()
        fake.ack_delay = 0.1
        gate = self.gate(fake)
        body = swizzle(fx.square_plain(**LONG))
        first = {}
        t = threading.Thread(target=lambda: first.update(r=self.store(gate, body)))
        t.start()
        deadline = time.monotonic() + 2
        while not fake.jobs() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store(gate, body), (409, {"error": "busy"}))
        self.assertTrue(gate.status()["busy"])
        t.join(10)
        self.assertEqual(first["r"][0], 200)
        self.assertFalse(gate.status()["busy"])


class HttpStoreRefusals(StoreBase):
    """Steps 1-3 of /store run in the HTTP layer before the body is read: zero datagrams."""

    def post(self, gate, query, body):
        srv = fx.Running(gate)
        self.addCleanup(srv.close)
        return srv.call("POST", "/store?" + query, body)

    def q(self, body, name="MD0412", cap="80"):
        return f"name={name}&maxPowerPct={cap}&sha256={sha(body)}&" + urlencode({
            "profile": json.dumps([{"speed": 20, "min": 20, "max": 30}]), "preparedSha256": sha(body), "expiresAt": f"{time.time() + 120:.6f}"})

    def test_g10_store_disabled_sends_nothing(self):
        fake, body = self.fake(), fx.square()
        self.assertEqual(self.post(self.gate(fake, store_enabled=False), self.q(body), body),
                         (503, {"error": "store_disabled"}))
        self.assertEqual(fake.got, [])

    def test_g10_not_calibrated(self):
        fake, body = self.fake(), fx.square()
        self.assertEqual(self.post(self.gate(fake, file_count_addr=""), self.q(body), body),
                         (503, {"error": "not_calibrated", "detail": "file_count_addr is not set (T0)"}))
        self.assertEqual(self.post(self.gate(fake, min_power_pct=0.0), self.q(body), body),
                         (503, {"error": "not_calibrated", "detail": "min_power_pct is not set (T5)"}))
        self.assertEqual(fake.got, [])

    def test_g10_bad_params_and_sha(self):
        fake, body = self.fake(), fx.square()
        gate = self.gate(fake)
        for query, detail in ((self.q(body, name="md0412"), "name"), (self.q(body, name="MD04123"), "name"),
                              (self.q(body, cap="0"), "maxPowerPct"), (self.q(body, cap="101"), "maxPowerPct"),
                              (self.q(body).replace("sha256=", "sha256=x"), "sha256"),
                              (self.q(body) + "&start=1", "query")):
            with self.subTest(query=query):
                self.assertEqual(self.post(gate, query, body), (400, {"error": "bad_request", "detail": detail}))
        self.assertEqual(self.post(gate, self.q(body).replace(f"sha256={sha(body)}", f"sha256={'0' * 64}"), body),
                         (400, {"error": "sha_mismatch"}))
        self.assertEqual(self.post(gate, f"name=MD0412&maxPowerPct=80&sha256={sha(body)}", body),
                         (400, {"error": "bad_request", "detail": "profile/preparedSha256"}))
        self.assertEqual(fake.got, [])

    def test_store_over_http_happy_path(self):
        fake, body = self.fake(), fx.square()
        code, out = self.post(self.gate(fake), self.q(body), body)
        self.assertEqual((code, out["ok"], out["fileCountAfter"]), (200, True, 6))

    def test_http_expiry_is_required_and_expired_requests_send_nothing(self):
        fake, body = self.fake(), fx.square()
        gate = self.gate(fake)
        query = self.q(body).split("&expiresAt=")[0]
        for value in ("", "&expiresAt=NaN", "&expiresAt=inf", "&expiresAt=-1"):
            self.assertEqual(self.post(gate, query + value, body),
                             (400, {"error": "bad_request", "detail": "expiresAt"}))
        self.assertEqual(self.post(gate, query + "&expiresAt=1", body), (409, {"error": "expired"}))
        self.assertEqual(gate.store(body, "MD0412", 80, sha(body)),
                         (400, {"error": "bad_request", "detail": "expiresAt"}))
        self.assertEqual(fake.got, [])


class Guard(unittest.TestCase):
    READS = frozenset({0x0200, fx.FILE_COUNT})

    def test_g11_sender_refuses_before_any_write(self):
        ctl = Controller("127.0.0.1", "127.0.0.1", 0.2, self.READS, send_port=9, recv_port=0)
        sock = mock.Mock()
        for payload in (b"\xD8\x01", b"\xD8\x02", b"\xDA\x01\x06\x20" + fx.e35(0) * 2, b"\xA5\x50\x02",
                        b"\xD9\x00\x02" + fx.e35(0), b"\xE8\x03\x00\x01", b"\xE8\x00\x00\x00\x00\x00",
                        b"\xDA\x00\x00\x26",  # a read, but not of an allowlisted address
                        b"\xD8\x00",  # the header marker without the envelope in front
                        b"\xA8" + fx.xy(1, 1) + b"\xE8\x02\xE7\x01AB1234\x00",  # envelope not at the start
                        b"\xA8" + fx.xy(1, 1)[:9],  # wrong length
                        b""):
            with self.subTest(payload=payload.hex()):
                with self.assertRaises(GuardError):
                    ctl.send(sock, payload)
        sock.sendto.assert_not_called()

    def test_g11_guard_accepts_exactly_what_store_and_reads_send(self):
        guard(b"\xDA\x00\x04\x00", self.READS)
        guard(b"\xDA\x00" + bytes([(fx.FILE_COUNT >> 7) & 0x7F, fx.FILE_COUNT & 0x7F]), self.READS)
        plain = fx.square_plain(**LONG)
        for p in plan_packets(swizzle(plain), "MD0412", 1000):
            guard(p, self.READS)

    def test_g11_reply_from_another_ip_is_ignored(self):
        fake = fx.FakeController()
        self.addCleanup(fake.close)
        fake.drop, fake.spoof_from = {1}, "127.0.0.2"
        gate = fx.gate_for(fake)
        code, out = gate.store(fx.square(), "MD0412", 100, sha(fx.square()), expires=time.time() + 120)
        self.assertEqual((code, out["error"], out["packet"]), (502, "transfer_aborted", 1))
        self.assertTrue(any(kind == "sent" for kind, _, d in fake.log if d == ACK))  # the spoofed ACK did go out

    def test_codec_round_trip_and_wire_ack(self):
        self.assertEqual(unswizzle(swizzle(bytes(range(256)))), bytes(range(256)))
        self.assertEqual(ACK, b"\xC6")


if __name__ == "__main__":
    unittest.main()
