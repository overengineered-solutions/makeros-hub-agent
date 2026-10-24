"""v0.84: synthetic VisiCut vector exports, exact material profiles and store-only delivery."""
import hashlib
import json
import unittest
from dataclasses import replace
from urllib.parse import urlencode

import laser_gate_fixtures as fx
from laser_gate.checker import check, job_bytes, plan_packets, swizzle, tokenize, u35
from laser_gate.gate import Config, ConfigError, validate
from makeros_hub.laser import _valid_profile, _checked

PROFILE = [{"speed": 20, "min": 20, "max": 30}]
CFG = Config(min_power_pct=10.0)


def visicut(at=(900, 10)):
    plain = fx.square_plain(at=at).replace(b"\xd8\x10\xe6\x01", b"\xd8\x12")
    plain = plain.replace(b"\xf0", b"\xf0\xe8\x02\xe7\x01DRAWING\x00")
    plain = plain.replace(b"\xca\x06", b"\xc6\x60\x00\x00" + fx.e35(20000) + b"\xca\x41\x00\x00\xca\x06")
    return plain.replace(b"\xe7\x00\xeb\xd7", b"\xda\x01\x06\x20" + fx.e35(123) * 2 + b"\xe7\x00\xd7")


class LaserSend(unittest.TestCase):
    def verdict(self, plain, profile=PROFILE):
        return check(swizzle(plain), 80, CFG, profile)

    def test_translation_and_frames_preserve_handedness_and_do_not_send_client_name_or_scratch(self):
        plain = visicut()
        v = self.verdict(plain)
        self.assertEqual(v["verdict"], "checked", v)
        prepared, _ = job_bytes(swizzle(plain))
        points = [prepared[t.ps:t.end] for t in tokenize(prepared) if t.key in ((0x88,), (0xA8,))]
        self.assertEqual([(u35(p[:5]), u35(p[5:])) for p in points], [(0, 0), (20000, 0), (20000, 20000), (0, 20000), (0, 0)])
        self.assertNotIn(b"\xda", prepared)
        self.assertNotIn(b"DRAWING", prepared)
        other, _ = job_bytes(swizzle(visicut((500, 100))))
        self.assertEqual(other, prepared)
        self.assertEqual(v["preparedSha256"], hashlib.sha256(swizzle(prepared)).hexdigest())
        packets = plan_packets(swizzle(plain), "PS0123", 1000)
        self.assertTrue(packets[0].startswith(b"\xe8\x02\xe7\x01PS0123\x00"))
        for t in tokenize(prepared):
            if t.key in ((0xe7, 0x03), (0xe7, 0x50)):
                self.assertEqual(prepared[t.ps:t.end], fx.xy(0, 0))

    def test_matches_every_cut_not_just_last_or_highest_setting(self):
        p = visicut()
        self.assertEqual(self.verdict(p)["verdict"], "checked")
        p = p.replace(b"\xa8", b"\xc9\x02" + fx.sp(21) + b"\xa8", 1)
        self.assertEqual(self.verdict(p)["code"], "profile_mismatch")
        self.assertEqual(self.verdict(visicut(), [{"speed": 20, "min": 19, "max": 30}])["code"], "profile_mismatch")

    def test_refuses_unproven_modes_unknown_writes_and_malformed_parameters(self):
        for change in (b"\xd8\x11", b"\xd9\x00\x00", b"\xe8\x03\x00\x01", b"\xda\x01\x06\x21" + fx.e35(0) * 2):
            self.assertEqual(self.verdict(visicut().replace(b"\xd7", change + b"\xd7"))["verdict"], "refused")
        self.assertEqual(self.verdict(visicut().replace(b"\xa8", b"\xa8\xff", 1))["verdict"], "refused")
        self.assertEqual(self.verdict(visicut().replace(b"\xca\x41\x00\x00", b"\xca\x41\x00\x02"))["code"], "engraving_not_ready")
        self.assertEqual(self.verdict(visicut().replace(b"\xc6\x60\x00\x00" + fx.e35(20000), b"\xc6\x60\x00\x00" + fx.e35(30000), 1))["code"], "frequency")

    def test_empty_oversized_negative_and_repeated_modes_refuse(self):
        for p in (visicut().replace(b"\xa8", b"\x88"), visicut().replace(b"\xd8\x12", b"\xd8\x12\xd8\x12"),
                  visicut().replace(b"\x88" + fx.xy(900, 10), b"\x88" + fx.xy(-1, 10))):
            self.assertEqual(self.verdict(p)["verdict"], "refused")
        self.assertEqual(self.verdict(fx.square_plain(size=1001).replace(b"\xd8\x10\xe6\x01", b"\xd8\x12"))["code"], "out_of_bed")

    def test_only_recorded_visicut_padding_is_dropped_and_never_transmitted(self):
        p = visicut()
        self.assertEqual(job_bytes(swizzle(p + b"\xd8\x2a")), job_bytes(swizzle(p)))
        self.assertEqual(self.verdict(fx.square_plain() + b"\xd8\x2a")["verdict"], "refused")

    def test_profile_boundary_and_receipt(self):
        for bad in (None, [], [{"speed": True, "min": 1, "max": 2}], [{"speed": float('nan'), "min": 1, "max": 2}]):
            self.assertFalse(_valid_profile(bad))
        self.assertTrue(_checked(self.verdict(visicut())))
        self.assertFalse(_checked({"checkVersion": 1}))
        for changes in ({"checkVersion": True}, {"layers": [{"index": 0, "minPowerPct": 20, "maxPowerPct": 10, "speedMmS": 20}]}):
            self.assertFalse(_checked({**self.verdict(visicut()), **changes}))

    def test_reported_size_includes_travel_after_the_last_cut(self):
        p = visicut().replace(b"\xda\x01", b"\x88" + fx.xy(950, 10) + b"\xda\x01")
        v = self.verdict(p)
        self.assertEqual((v["verdict"], v["widthMm"], v["heightMm"]), ("checked", 50, 20))

    def test_unknown_and_paused_status_do_not_mean_idle(self):
        g = fx.gate_for(None)
        self.assertIsNone(g.idle(None))
        self.assertTrue(g.idle(1 << 27))
        self.assertFalse(g.idle((1 << 27) | (1 << 16)))
        self.assertFalse(g.idle((1 << 27) | (1 << 15)))
        self.assertIsNone(fx.gate_for(None, idle_status_mask=0).idle(0))
        with self.assertRaises(ConfigError):
            validate({"idle_status_mask": 1 << 15})

    def test_store_checks_profile_prepared_hash_names_and_idle(self):
        fake = fx.FakeController()
        self.addCleanup(fake.close)
        g = fx.gate_for(fake)
        body = swizzle(visicut())
        sha = hashlib.sha256(body).hexdigest()
        prepared = self.verdict(visicut())["preparedSha256"]
        self.assertEqual(g.store(body, "PS0123", 80, sha, PROFILE, "0" * 64)[1]["error"], "prepared_mismatch")
        self.assertFalse(fake.jobs())
        code, result = g.store(body, "PS0123", 80, sha, PROFILE, prepared)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["preparedSha256"], prepared)
        before = len(fake.jobs())
        self.assertEqual(g.store(body, "PS0123", 80, sha, PROFILE, prepared)[1]["error"], "name_taken")
        self.assertEqual(len(fake.jobs()), before)
        fake.mem[0x0200] |= 1 << 16
        self.assertEqual(g.store(body, "PS0124", 80, sha, PROFILE, prepared)[1]["error"], "not_idle")
        self.assertEqual(len(fake.jobs()), before)
        fake.assert_clean(g.reads)

    def test_full_memory_and_missing_name_read_send_nothing(self):
        fake = fx.FakeController(count=2)
        self.addCleanup(fake.close)
        g = fx.gate_for(fake, max_stored_files=2)
        body = fx.square()
        args = (body, "PS0123", 80, hashlib.sha256(body).hexdigest())
        self.assertEqual(g.store(*args)[1]["error"], "memory_full")
        g.cfg = replace(g.cfg, max_stored_files=3)
        fake.names.clear()
        self.assertEqual(g.store(*args)[1]["error"], "name_unverified")
        self.assertFalse(fake.jobs())

    def test_http_profile_protocol_fails_closed(self):
        g = fx.gate_for(None)
        srv = fx.Running(g)
        self.addCleanup(srv.close)
        body = swizzle(visicut())
        code, result = srv.call("POST", "/check?" + urlencode({"maxPowerPct": 80, "profile": json.dumps(PROFILE)}), body)
        self.assertEqual(code, 200)
        self.assertEqual(result["verdict"], "checked")
        code, _ = srv.call("POST", "/check?maxPowerPct=80&profile=garbage", body)
        self.assertEqual(code, 400)
