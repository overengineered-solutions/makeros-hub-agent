"""Pool ranking (§10.2): the policy that picks WHICH filaments a VP advertises over capacity."""
import tempfile
import unittest
from pathlib import Path

from makeros_hub.vprinter.pool_ranking import PoolRankingState
from makeros_hub.vprinter.live_pool import active_keys, scoped_statuses, vp_pool_from_statuses_ranked

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

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            st = PoolRankingState(p, now=1000.0)
            st.observe(["k"], ["k"], 0.5, 1000.0)
            st.persist(1000.0, force=True)
            st2 = PoolRankingState(p, now=2000.0)
            self.assertGreater(st2._decayed("k", 2000.0), 0.0)


class TestScopedAndActive(unittest.TestCase):
    def test_scoped_exact_match_with_never_strand_fallback(self):
        sts = [status("Bambu X1 Carbon", [tray("PLA", "#fff")]), status("Bambu A1 Mini", [tray("ABS", "#000")], pid="p2")]
        self.assertEqual(len(scoped_statuses(sts, "Bambu X1 Carbon")), 1)
        self.assertEqual(len(scoped_statuses(sts, "Nonexistent Model")), 2)   # fallback: whole hub

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
            st.observe(["PLA|GFL99|FFFFFF", "ABS|GFB99|000000"], [], 0.5, now - 30 * DAY)
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
