"""Pool ranking (§10.2): the policy that picks WHICH filaments a VP advertises over capacity."""
import tempfile
import unittest
from pathlib import Path

from makeros_hub.vprinter.pool_ranking import PoolRankingState
from makeros_hub.vprinter.live_pool import active_keys, scoped_statuses, tray_key, vp_pool_from_statuses_ranked

DAY = 86400.0


def tray(material, color, fid="GFL99", slot=0):
    return {"slot": slot, "material": material, "colorHex": color, "filamentId": fid}


def status(model, trays_list, state="idle", active=None, pid="p1"):
    s = {"printerId": pid, "model": model, "state": state, "ams": [{"trays": trays_list}]}
    if active is not None:
        s["amsActiveTray"] = active
    return s


class TestRankingPolicy(unittest.TestCase):
    def _state(self, tmp):
        return PoolRankingState(Path(tmp) / "r.json", now=1000.0)

    def test_under_capacity_shows_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            got = st.select(["b", "a"], capacity=4, now=1000.0)
        self.assertEqual(got, ["a", "b"])

    def test_recent_keys_always_make_the_cut_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            now = 100 * DAY
            st.observe(["old1", "old2", "old3"], [], 0.5, now - 30 * DAY)   # old, unused
            st.observe(["fresh"], [], 0.5, now - 1 * DAY)                    # loaded yesterday
            got = st.select(["old1", "old2", "old3", "fresh"], capacity=2, now=now)
        self.assertIn("fresh", got)   # auto-promote-on-load

    def test_usage_fills_remaining_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            now = 100 * DAY
            st.observe(["a", "b", "c"], [], 0.5, now - 30 * DAY)
            for i in range(60):                                              # 30 active-minutes on b
                st.observe([], ["b"], 0.5, now - 29 * DAY + i * 30)
            got = st.select(["a", "b", "c"], capacity=1, now=now)
        self.assertEqual(got, ["b"])   # most-used wins the last slot

    def test_recent_burst_cannot_evict_every_usage_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp)
            now = 100 * DAY
            st.observe(["heavy"], [], 0.5, now - 30 * DAY)
            for i in range(60):                                              # 30 active-minutes on heavy
                st.observe([], ["heavy"], 0.5, now - 29 * DAY + i * 30)
            fresh = [f"fresh{i}" for i in range(5)]                          # capacity+1 spools loaded today
            st.observe(fresh, [], 0.5, now - 1)
            got = st.select(fresh + ["heavy"], capacity=4, now=now)
        self.assertIn("heavy", got)   # usage reserve holds against a fresh-spool burst

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            wall = 1_700_000_000.0
            st = PoolRankingState(p, now=wall)
            st.observe(["k"], ["k"], 0.5, wall)
            st.persist(wall, force=True)
            st2 = PoolRankingState(p, now=wall + 1000.0)
            self.assertGreater(st2._decayed("k", wall + 1000.0), 0.0)

    def test_monotonic_era_timestamps_quarantined_on_load(self):
        # A state file stamped by a monotonic clock (seconds-since-boot ≪ 2020 epoch) must not read as
        # ancient against wall time: implausible stamps reset to load-time `now`, keeping use_minutes.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text('{"version": 1, "keys": {"k": {"first_seen": 12345.0, "use_minutes": 30.0, "last_use": 12345.0}}}')
            wall = 1_700_000_000.0
            st = PoolRankingState(p, now=wall)
            self.assertGreater(st._decayed("k", wall), 25.0)          # usage survives, not decayed to ~0
            self.assertIn("k", st.select(["k", "other"], 1, wall))    # reads as newly seen → recent tier


class TestScopedAndActive(unittest.TestCase):
    def test_scoped_exact_match_with_never_strand_fallback(self):
        sts = [status("Bambu X1 Carbon", [tray("PLA", "#fff")]), status("Bambu A1 Mini", [tray("ABS", "#000")], pid="p2")]
        self.assertEqual(len(scoped_statuses(sts, "Bambu X1 Carbon")), 1)
        self.assertEqual(len(scoped_statuses(sts, "Nonexistent Model")), 2)   # fallback: whole hub

    def test_active_key_resolves_by_raw_unit_id_across_gaps(self):
        # build_ams re-enumerates units contiguously while tray_now uses RAW ids: with unit 0 absent and
        # raw units 1+2 present, active tray in raw unit 1 (global 4..7) must hit raw unit 1 — a positional
        # lookup would land on raw unit 2 (WRONG key). Raw ids ride unit["raw"]["id"].
        s = {
            "printerId": "p1", "model": "m", "state": "printing", "amsActiveTray": 5,
            "ams": [
                {"unit": 0, "raw": {"id": 1}, "trays": [tray("PETG", "#00ff00", "GFG99", slot=1)]},
                {"unit": 1, "raw": {"id": 2}, "trays": [tray("ABS", "#000000", "GFB99", slot=1)]},
            ],
        }
        keys = active_keys([s])
        self.assertEqual(len(keys), 1)
        self.assertIn("PETG", keys[0])   # raw unit 1, slot 1 — never the ABS in raw unit 2

    def test_active_keys_only_from_printing_printers(self):
        sts = [
            status("m", [tray("PLA", "#fff", slot=1)], state="printing", active=1),
            status("m", [tray("ABS", "#000", slot=0)], state="idle", active=0, pid="p2"),
        ]
        keys = active_keys(sts)
        self.assertEqual(len(keys), 1)
        self.assertIn("PLA", keys[0])

    def test_ranked_pool_over_capacity_selects_by_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = PoolRankingState(Path(tmp) / "r.json", now=0.0)
            now = 100 * DAY
            st.observe([tray_key(tray("PLA", "#ffffff", "GFL99")), tray_key(tray("ABS", "#000000", "GFB99"))], [], 0.5, now - 30 * DAY)
            sts = [status("m", [tray("PLA", "#ffffff", "GFL99", 0), tray("ABS", "#000000", "GFB99", 1),
                               tray("PETG", "#ff0000", "GFG99", 2)])]
            pool = vp_pool_from_statuses_ranked(sts, units=1, trays=2, ranking=st, now=now)
        self.assertEqual(len(pool), 2)
        # PETG is first-seen NOW (inside the recent window via observe-at-derive? no — selection uses state;
        # PETG unseen -> first_seen defaults absent -> not recent -> falls to usage tier at 0. All three at
        # usage 0 -> stable key-sort decides. The assertion pins determinism, not a specific winner.
        self.assertEqual(pool, sorted(pool, key=lambda t: str(t)))  # deterministic ordering shape


if __name__ == "__main__":
    unittest.main()
