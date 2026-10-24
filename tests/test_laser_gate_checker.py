"""Laser gate checker, contract G1-G7. Pure: no sockets."""
import random
import subprocess
import sys
import unittest
from pathlib import Path

import laser_gate_fixtures as fx
from laser_gate.checker import check, swizzle
from laser_gate.gate import Config

CFG = Config(min_power_pct=10.0)
SAMPLES = Path(__file__).resolve().parents[1] / "laser_gate" / "samples"


def refused(plain, cfg=CFG, cap=100):
    v = check(swizzle(plain), cap, cfg)
    assert v["verdict"] == "refused", v
    return v


class Checked(unittest.TestCase):
    def test_g1_square_is_checked_with_its_size_and_layer(self):
        body = fx.square()
        v = check(body, 100, CFG)
        self.assertEqual(v["verdict"], "checked", v)
        self.assertEqual((v["widthMm"], v["heightMm"]), (20.0, 20.0))
        self.assertEqual(v["layers"], [{"index": 0, "maxPowerPct": 30.0, "minPowerPct": 20.0, "speedMmS": 20.0}])
        self.assertEqual((v["sizeBytes"], v["maxPowerPct"]), (len(body), 100))
        self.assertEqual(len(v["sha256"]), 64)

    def test_g2_operator_samples_are_checked(self):
        files = sorted(SAMPLES.glob("*.rd"))
        if not files:
            self.skipTest("no operator samples yet")
        for f in files:
            with self.subTest(sample=f.name):
                v = check(f.read_bytes(), 100, Config())
                self.assertEqual(v["verdict"], "checked", v)


class CommandAllowlist(unittest.TestCase):
    def test_g3_injected_commands_are_refused_with_hex_and_offset(self):
        injected = {
            "D8 01": b"\xD8\x01", "D8 02": b"\xD8\x02", "D8 03": b"\xD8\x03", "D8 2C": b"\xD8\x2C",
            "D9 00": b"\xD9\x00\x02" + fx.e35(0), "DA 00": b"\xDA\x00\x02\x00",
            "DA 01": b"\xDA\x01\x06\x20" + fx.e35(0) * 2, "E8 00": b"\xE8\x00" + b"\x00" * 4,
            "E8 02": b"\xE8\x02", "E8 03": b"\xE8\x03\x00\x01", "E7 01": b"\xE7\x01AB1234\x00",
            "A5 50": b"\xA5\x50\x02", "CC": b"\xCC", "BB": b"\xBB\x01",
        }
        for name, cmd in injected.items():
            with self.subTest(command=name):
                plain, at = fx.insert_before(fx.square_plain(), 0x88, cmd)
                v = refused(plain)
                self.assertEqual((v["code"], v["offset"]), ("command_not_allowed", at))
                self.assertIn(f"command {name} at byte {at} isn't allowed", v["reason"])

    def test_never_entries_read_by_name(self):
        v = refused(fx.insert_before(fx.square_plain(), 0x88, b"\xD8\x01")[0])
        self.assertTrue(v["reason"].endswith("isn't allowed (stop)"), v["reason"])
        v = refused(fx.insert_before(fx.square_plain(), 0x88, b"\xD9\x10\x02" + fx.e35(0) * 2)[0])
        self.assertIn("(jog/rapid move)", v["reason"])

    def test_only_the_recorded_scratch_write_is_stripped(self):
        plain = fx.square_plain()
        self.assertEqual(check(swizzle(plain[:-1] + b"\xDA\x01\x06\x20" + fx.e35(0) * 2 + b"\xD7"), 80, CFG)["verdict"], "checked")
        v = refused(plain[:-1] + b"\xDA\x01\x06\x21" + fx.e35(0) * 2 + b"\xD7")
        self.assertEqual(v["code"], "command_not_allowed")
        self.assertIn("DA 01", v["reason"])


class Structure(unittest.TestCase):
    def test_g4_header_marker(self):
        plain = fx.square_plain()
        no_start = plain.replace(b"\xD8\x00", b"", 1)
        self.assertEqual(refused(no_start)["code"], "no_header_marker")
        self.assertEqual(refused(no_start)["reason"], "the header has no D8 00")
        late = no_start.index(b"\x88") + 11  # just after the first move
        self.assertEqual(refused(no_start[:late] + b"\xD8\x00" + no_start[late:])["code"], "no_header_marker")
        twice = fx.insert_before(plain, 0x88, b"\xD8\x00")[0]
        self.assertEqual(refused(twice)["code"], "command_not_allowed")
        bloated = b"\xE7\x03" + fx.xy(10, 10)
        long_header = plain.replace(b"\xD8\x00", bloated * 90 + b"\xD8\x00", 1)
        v = refused(long_header)
        self.assertEqual(v["code"], "header_too_long")
        self.assertEqual(v["reason"], f"the header is {11 + 10 + 90 * 12} bytes; it has to fit in one 1000-byte packet")

    def test_g5_framing(self):
        plain = fx.square_plain()
        v = refused(plain[:-1])
        self.assertEqual((v["code"], v["reason"]), ("no_end", "the file doesn't end with D7"))
        mid = plain.index(b"\xE7\x00")
        v = refused(plain[:mid] + b"\xD7" + plain[mid:])
        self.assertEqual((v["code"], v["offset"]), ("data_after_end", mid))
        self.assertEqual(v["reason"], f"there is data after the end mark (D7) at byte {mid}")
        last_cut = plain.rindex(b"\xA8")
        v = refused(plain[:last_cut + 4])
        self.assertEqual((v["code"], v["offset"]), ("truncated", last_cut))
        self.assertEqual(v["reason"], f"the file ends in the middle of command A8 (byte {last_cut})")
        self.assertEqual(refused(plain + b"\xC6")["code"], "truncated")  # ends on an opcode that needs a sub-code
        v = refused(plain[:last_cut + 10] + plain[last_cut + 11:])  # one A8 with 9 parameter bytes
        self.assertEqual((v["code"], v["offset"]), ("bad_length", last_cut))
        self.assertEqual(v["reason"], f"command A8 at byte {last_cut} has 9 parameter bytes; it takes 10")
        rnd = random.Random(20261014)
        for junk in (bytes(rnd.randrange(256) for _ in range(4096)), b"G21 G90 ; this is g-code, not a laser file\n" * 30,
                     b"", b"\x00\x01\x02"):
            with self.subTest(junk=junk[:12]):
                v = check(junk, 100, CFG)
                self.assertEqual((v["code"], v["reason"]), ("not_rd", "this isn't a Ruida .rd file"))
        body = fx.square()
        v = check(body, 100, Config(min_power_pct=10.0, max_file_bytes=100))
        self.assertEqual((v["code"], v["reason"]), ("too_large", f"the file is {len(body)} bytes; the limit is 100"))


class Limits(unittest.TestCase):
    def test_g6_power_cap_names_the_layer_and_is_clamped_by_the_gate_max(self):
        plain = fx.square_plain(layers=((30, 20), (80, 20)))
        v = refused(plain, cap=50)
        self.assertEqual((v["code"], v["reason"]), ("power_over_cap", "layer 2 power 70.0% is over the 50.0% cap"))
        v = refused(plain, Config(min_power_pct=10.0, max_power_pct=70.0), cap=100)
        self.assertEqual(v["reason"], "layer 2 power 80.0% is over the 70.0% cap")
        ok = check(fx.square(), 100, Config(min_power_pct=10.0, max_power_pct=70.0))
        self.assertEqual((ok["verdict"], ok["maxPowerPct"]), ("checked", 70.0))

    def test_g6_power_under_min(self):
        v = refused(fx.square_plain(), Config(min_power_pct=40.0))
        self.assertEqual((v["code"], v["reason"]), (
            "power_under_min", "layer 1 power 30.0% is under the 40.0% minimum (below it the exhaust fans don't start)"))
        powerless = fx.square_plain().replace(b"\xC6\x32\x00" + fx.pw(30), b"").replace(b"\xC6\x02" + fx.pw(30), b"")
        self.assertEqual(refused(powerless)["code"], "power_under_min")
        self.assertEqual(check(fx.square(layers=((5, 20),)), 100, Config())["verdict"], "checked")  # no minimum before T5
        self.assertEqual(refused(powerless, Config())["code"], "settings_missing")   # but a cut still needs its power

    def test_every_cut_needs_its_own_layers_power_and_speed(self):
        # codex review of release-0.68.0: layer 2's max power stood in for layer 1's, and a file with no speed at all
        # was checked; either cut would run at whatever the controller last held
        two = fx.square_plain(layers=((30, 20), (40, 25)))
        no_max = two.replace(b"\xC6\x32\x00" + fx.pw(30), b"").replace(b"\xC6\x02" + fx.pw(30), b"")
        v = refused(no_max)
        self.assertEqual(v["code"], "settings_missing")
        self.assertRegex(v["reason"], r"^layer 1 cuts at byte \d+ with no max power setting before it$")
        no_speed = two.replace(b"\xC9\x04\x00" + fx.sp(20), b"").replace(b"\xC9\x02" + fx.sp(20), b"")
        self.assertIn("with no speed setting", refused(no_speed)["reason"])
        no_min = two.replace(b"\xC6\x31\x01" + fx.pw(30), b"").replace(b"\xC6\x01" + fx.pw(30), b"")
        self.assertRegex(refused(no_min)["reason"], r"^layer 2 cuts .* no min power setting")
        # a setting after the cut does not cover it
        late = no_speed.replace(b"\xE7\x00\xEB\xD7", b"\xCA\x02\x00\xC9\x02" + fx.sp(20) + b"\xE7\x00\xEB\xD7")
        self.assertEqual(refused(late)["code"], "settings_missing")
        self.assertEqual(check(swizzle(two), 100, CFG)["verdict"], "checked")

    def test_g7_speed_range(self):
        v = refused(fx.square_plain(layers=((30, 1500),)))
        self.assertEqual((v["code"], v["reason"]), ("speed_out_of_range", "layer 1 speed 1500.0 mm/s is outside 1–1000 mm/s"))
        self.assertEqual(refused(fx.square_plain(layers=((30, 0.5),)))["code"], "speed_out_of_range")

    def test_g7_out_of_bed(self):
        # a cut to x = 1001 mm (header bounds pulled back in, so the path itself is what fails)
        plain = fx.square_plain(at=(990, 10), size=11)
        for op in (b"\xE7\x07", b"\xE7\x51"):
            plain = plain.replace(op + fx.xy(1001, 21), op + fx.xy(1000, 21))
        for op in (b"\xE7\x53\x00", b"\xE7\x62\x00"):
            plain = plain.replace(op + fx.xy(1001, 21), op + fx.xy(1000, 21))
        v = refused(plain)
        self.assertEqual(v["code"], "out_of_bed")
        self.assertEqual(v["reason"], "a path leaves the bed (1001.0, 10.0 mm is outside 1000.0 × 600.0 mm)")
        self.assertEqual(refused(fx.square_plain(at=(990, 10), size=11))["code"], "out_of_bed")  # bounds alone
        # an oversized extent built from relative steps: 30 x 1 mm cuts from x=980 walk past 1000
        plain = fx.square_plain(at=(975, 10), size=4)
        steps = b"".join(b"\xAA" + fx.e14(fx.um(1)) for _ in range(30))
        v = refused(plain.replace(b"\xE7\x00\xEB\xD7", steps + b"\xE7\x00\xEB\xD7"))
        self.assertEqual(v["code"], "out_of_bed")
        self.assertIn("(1001.0, 10.0 mm", v["reason"])
        # negative relative step off the left edge
        v = refused(fx.square_plain(at=(0.5, 10), size=4).replace(b"\xE7\x00\xEB\xD7", b"\xAA" + fx.e14(-fx.um(1))
                                                                       + b"\xE7\x00\xEB\xD7"))
        self.assertIn("(-0.5, 10.0 mm", v["reason"])
        # a header bound outside the bed
        plain = fx.square_plain()
        bad = plain.replace(b"\xE7\x07" + fx.xy(30, 30), b"\xE7\x07" + fx.xy(30, 700), 1)
        self.assertEqual(refused(bad)["code"], "out_of_bed")

    def test_limits_compare_unrounded_values_within_one_step(self):
        plain = fx.square_plain(layers=((70, 20),))
        self.assertEqual(check(swizzle(plain), 70, CFG)["verdict"], "checked")  # 70% as LightBurn-style rounding writes it
        one_step = plain.replace(b"\xC6\x02" + fx.pw(70), b"\xC6\x02" + fx.e14(fx.u14(fx.pw(70)) + 1))
        self.assertEqual(check(swizzle(one_step), 70, CFG)["verdict"], "checked")  # a writer that rounds up
        over = plain.replace(b"\xC6\x02" + fx.pw(70), b"\xC6\x02\x59\x54")  # 70.048%: rounds to 70.0, still over
        self.assertEqual((refused(over, cap=70)["code"], refused(over, cap=70)["reason"]),
                         ("power_over_cap", "layer 1 power 70.0% is over the 70.0% cap"))
        under = fx.square_plain().replace(b"\xC6\x02" + fx.pw(30), b"\xC6\x02" + fx.e14(4913))  # 29.99%
        self.assertEqual(refused(under, Config(min_power_pct=30.0))["code"], "power_under_min")
        self.assertEqual(check(fx.square(), 100, Config(min_power_pct=30.0))["verdict"], "checked")
        self.assertEqual(refused(fx.square_plain(layers=((30, 1000.049),)))["code"], "speed_out_of_range")
        self.assertEqual(check(fx.square(layers=((30, 1000),)), 100, CFG)["verdict"], "checked")

    def test_size_counts_both_ends_of_every_cut(self):
        # after the square, travel to (100, 100) and cut back to (10, 10): the cutting spans 10..100 on both axes
        plain = fx.square_plain().replace(b"\xE7\x00\xEB\xD7", b"\x88" + fx.xy(100, 100) + b"\xA8" + fx.xy(10, 10)
                                          + b"\xE7\x00\xEB\xD7")
        v = check(swizzle(plain), 100, CFG)
        self.assertEqual((v["verdict"], v["widthMm"], v["heightMm"]), ("checked", 90.0, 90.0))


class Memory(unittest.TestCase):
    def test_maximum_size_repeated_modes_fit_the_units_memory(self):
        code = ("import re; from laser_gate.checker import check, swizzle; from laser_gate.gate import Config;"
                "p = b'\\xd8\\x00' + b'\\xd8\\x12' * ((4194304 - 4) // 2) + b'\\xeb\\xd7';"
                "assert len(p) == 4194304; v = check(swizzle(p), 80, Config(min_power_pct=10.0));"
                "kb = int(re.search(r'VmHWM:\\s+(\\d+) kB', open('/proc/self/status').read()).group(1));"
                "print(v['verdict'], kb // 1024)")
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        verdict, mb = r.stdout.split()
        self.assertEqual(verdict, "refused")
        self.assertLess(int(mb), 64)

    def test_a_flood_of_power_commands_fits_the_units_memory(self):
        # codex review of release-0.68.0: 2 MiB of C6 02 took ~212 MiB when every power command was kept
        code = ("import re, sys; sys.path.insert(0, 'tests'); import laser_gate_fixtures as fx;"
                "from laser_gate.checker import check, swizzle; from laser_gate.gate import Config;"
                "p = fx.square_plain(); h = p.index(b'\\xD8\\x00') + 2;"
                "v = check(swizzle(p[:h] + (b'\\xC6\\x02' + fx.pw(30)) * (1 << 19) + p[h:]), 100, Config(min_power_pct=10.0));"
                "kb = int(re.search(r'VmHWM:\\s+(\\d+) kB', open('/proc/self/status').read()).group(1));"
                "print(v['verdict'], kb // 1024)")
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           text=True, timeout=60)
        verdict, mb = r.stdout.split()
        self.assertEqual(verdict, "checked", r.stderr)
        self.assertLess(int(mb), 64)

    def test_one_byte_commands_fit_the_units_memory(self):
        # MemoryMax=128M. As a list of Tok, 4 MiB of 1-byte commands took ~750 MB; 1 MiB took ~190 MB. Now ~25 MB.
        # The peak is this process's own VmHWM. ru_maxrss would not do: Linux carries the parent's peak across exec, so
        # under a long pytest run it reports pytest's size, not the check's.
        code = ("import re, sys; sys.path.insert(0, 'tests'); import laser_gate_fixtures as fx;"
                "from laser_gate.checker import check, swizzle; from laser_gate.gate import Config;"
                "p = fx.square_plain(); h = p.index(b'\\xD8\\x00') + 2;"
                "v = check(swizzle(p[:h] + b'\\xF0' * (1 << 20) + p[h:]), 100, Config());"
                "kb = int(re.search(r'VmHWM:\\s+(\\d+) kB', open('/proc/self/status').read()).group(1));"
                "print(v['verdict'], kb // 1024)")
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           text=True, timeout=60)
        verdict, mb = r.stdout.split()
        self.assertEqual(verdict, "checked", r.stderr)
        self.assertLess(int(mb), 64)


class RepeatAndArray(unittest.TestCase):
    """The check walks one copy of the path, once: repeat and array commands may carry only their do-nothing values."""

    def test_process_repeat_above_one_is_refused(self):
        plain = fx.square_plain()
        at = plain.index(b"\xE7\x04")
        v = refused(plain.replace(b"\xE7\x04" + fx.e14(1) * 2, b"\xE7\x04" + fx.e14(50) + fx.e14(1)))
        self.assertEqual((v["code"], v["offset"]), ("repeat_or_array", at))
        self.assertEqual(v["reason"], f"command E7 04 at byte {at} sets process repeat to 50, 1; only 1, 1 (one copy, "
                                      "run once) is allowed")

    def test_feed_repeat_other_than_zero_is_refused(self):
        plain = fx.square_plain()
        at = plain.index(b"\xE7\x06")
        v = refused(plain.replace(b"\xE7\x06" + fx.e35(0) * 2, b"\xE7\x06" + fx.e35(0) + fx.e35(3)))
        self.assertEqual((v["code"], v["offset"]), ("repeat_or_array", at))
        self.assertIn("sets feed repeat to 0, 3", v["reason"])

    def test_element_array_needs_one_by_one(self):
        # MeerK40t's LightBurn-style values: counts 1 x 1, then spacing fields the check leaves alone
        tail = fx.e14(0) + fx.e14(257) + fx.e14(-3072) + fx.e14(2) + fx.e14(5232)
        ok, _ = fx.insert_before(fx.square_plain(), 0xEA, b"\xF2\x05" + fx.e14(1) * 2 + tail)
        self.assertEqual(check(swizzle(ok), 100, CFG)["verdict"], "checked")
        plain, at = fx.insert_before(fx.square_plain(), 0xEA, b"\xF2\x05" + fx.e14(10) * 2 + tail)
        v = refused(plain)
        self.assertEqual((v["code"], v["offset"]), ("repeat_or_array", at))
        self.assertIn("sets element array to 10, 10", v["reason"])

    def test_array_repeat_is_not_allowed_at_all_yet(self):
        plain, at = fx.insert_before(fx.square_plain(), 0xEA, b"\xE7\x08" + fx.e14(1) * 2 + fx.e14(0) * 5)
        self.assertEqual((refused(plain)["code"], refused(plain)["offset"]), ("command_not_allowed", at))


if __name__ == "__main__":
    unittest.main()
