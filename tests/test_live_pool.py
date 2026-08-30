import unittest

from makeros_hub.config import VirtualPrinterConfig, VirtualPrinterMember
from makeros_hub.printers.bambu_parse import normalize_status
from makeros_hub.vprinter import live_pool
from makeros_hub.vprinter.live_pool import (
    loaded_keys,
    prune_scope_state,
    scoped_statuses,
    updated_config_if_pool_changed,
    vp_pool_from_statuses,
)


def _status(trays):
    return {"ams": [{"trays": trays}]}


def _cfg(pool):
    return VirtualPrinterConfig(
        enabled=True,
        serial="S",
        model="N1",
        name="VP",
        fw="01.08.00.00",
        bind_ip="100.64.0.10",
        units=1,
        trays=4,
        ams_type="ams",
        members=(VirtualPrinterMember("a" * 64, "m1"),),
        pool=tuple(pool),
    )


class TestLivePool(unittest.TestCase):
    def test_dedups_across_printers_and_maps_fields(self):
        statuses = [
            _status(
                [
                    {
                        "slot": 0,
                        "material": "PLA",
                        "filamentId": "GFL99",
                        "colorHex": "FFFFFFFF",
                        "productName": "Generic PLA",
                        "remainPct": 80,
                        "nozzleTempMin": 190,
                        "nozzleTempMax": 230,
                    },
                    {"slot": 1},  # empty -> ignored
                    {"slot": 3, "material": "PLA", "filamentId": "GFA07", "colorHex": "F7F3F0FF"},
                ]
            ),
            _status(
                [
                    # same GFL99 white PLA as printer 1 -> deduped to one
                    {"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF"},
                    {"slot": 1, "material": "ABS", "filamentId": "GFB00", "colorHex": "000000FF"},
                ]
            ),
        ]
        pool = vp_pool_from_statuses(statuses, units=1, trays=4)
        self.assertEqual(len(pool), 3)  # Generic PLA, GFA07 PLA, GFB00 ABS
        self.assertEqual(sorted(p["tray_type"] for p in pool), ["ABS", "PLA", "PLA"])
        # Generic PLA (GFL99) flows through UNCHANGED — the get_version AMS fix
        # (real hw_ver) makes OrcaSlicer resolve it natively as "Generic PLA".
        white = next(p for p in pool if p["tray_color"] == "FFFFFFFF")
        self.assertEqual(white["tray_info_idx"], "GFL99")
        self.assertEqual(white["tray_sub_brands"], "Generic PLA")
        self.assertEqual(white["cols"], ["FFFFFFFF"])

    def test_generic_pla_flows_through_unchanged(self):
        # The "2 ABS" fix is in get_version (real AMS hw_ver), NOT an id remap:
        # the genuine Generic-PLA id + product name must reach OrcaSlicer so the
        # Prepare tab shows "Generic PLA" (not a Bambu-branded preset).
        statuses = [
            _status(
                [{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "D5B6A4FF", "productName": "Generic PLA"}]
            )
        ]
        pool = vp_pool_from_statuses(statuses, 1, 4)
        self.assertEqual(pool[0]["tray_type"], "PLA")
        self.assertEqual(pool[0]["tray_info_idx"], "GFL99")
        self.assertEqual(pool[0]["tray_sub_brands"], "Generic PLA")

    def test_empty_inputs(self):
        self.assertEqual(vp_pool_from_statuses([_status([{"slot": 0}])], 1, 4), [])
        self.assertEqual(vp_pool_from_statuses([], 1, 4), [])
        self.assertEqual(vp_pool_from_statuses([{}], 1, 4), [])  # status without ams

    def test_caps_to_units_times_trays(self):
        trays = [
            {"slot": i, "material": "PLA", "filamentId": f"GF{i:02d}", "colorHex": f"{i:02d}0000FF"}
            for i in range(10)
        ]
        self.assertEqual(len(vp_pool_from_statuses([_status(trays)], units=1, trays=4)), 4)

    def test_deterministic_sorted_order(self):
        s = [
            _status(
                [
                    {"slot": 0, "material": "PLA", "filamentId": "GFB", "colorHex": "FFFFFFFF"},
                    {"slot": 1, "material": "PLA", "filamentId": "GFA", "colorHex": "FFFFFFFF"},
                ]
            )
        ]
        self.assertEqual(
            [t["tray_info_idx"] for t in vp_pool_from_statuses(s, 1, 4)], ["GFA", "GFB"]
        )

    def test_short_color_normalizes_to_8_hex(self):
        s = [_status([{"slot": 0, "material": "PLA", "filamentId": "X", "colorHex": "26A69A"}])]
        self.assertEqual(vp_pool_from_statuses(s, 1, 4)[0]["tray_color"], "26A69AFF")

    def test_lowercase_material_outputs_uppercase_tray_type(self):
        # cloud normalizeMaterialKey uppercases; the VP must too (parity).
        s = [_status([{"slot": 0, "material": "petg", "filamentId": "X", "colorHex": "00FF00FF"}])]
        self.assertEqual(vp_pool_from_statuses(s, 1, 4)[0]["tray_type"], "PETG")

    def test_missing_filament_id_falls_back_to_catalog(self):
        # material-only tray -> catalog infoIdx + temps, exactly like the cloud
        # (so identity matches config-down instead of diverging to "").
        s = [_status([{"slot": 0, "material": "PETG", "colorHex": "00FF00FF"}])]
        t = vp_pool_from_statuses(s, 1, 4)[0]
        self.assertEqual(t["tray_info_idx"], "GFG99")
        self.assertEqual(t["nozzle_temp_min"], "230")
        self.assertEqual(t["nozzle_temp_max"], "260")

    def test_cols_entries_are_normalized(self):
        s = [
            _status(
                [
                    {
                        "slot": 0,
                        "material": "PLA",
                        "filamentId": "X",
                        "colorHex": "26A69A",
                        "colors": ["26a69a", "ff0000"],
                    }
                ]
            )
        ]
        self.assertEqual(vp_pool_from_statuses(s, 1, 4)[0]["cols"], ["26A69AFF", "FF0000FF"])

    def test_dedup_tie_break_is_deterministic_by_printer_id(self):
        # same spool id+color in two printers, different productName -> the lower
        # printerId wins regardless of input order (deterministic across heartbeats).
        # GFA07 (recognized, not remapped) so the real productName flows through
        # and the tie-break is observable via tray_sub_brands.
        a = {"printerId": "p-a", **_status([{"slot": 0, "material": "PLA", "filamentId": "GFA07", "colorHex": "FFFFFFFF", "productName": "AAA"}])}
        b = {"printerId": "p-b", **_status([{"slot": 0, "material": "PLA", "filamentId": "GFA07", "colorHex": "FFFFFFFF", "productName": "BBB"}])}
        self.assertEqual(vp_pool_from_statuses([b, a], 1, 4)[0]["tray_sub_brands"], "AAA")
        self.assertEqual(vp_pool_from_statuses([a, b], 1, 4)[0]["tray_sub_brands"], "AAA")


def _raw_status(pid, model, trays, vt_tray=None):
    """A status built by the REAL DTO builder (bambu_parse.normalize_status) from a raw Bambu report —
    the RC1 regression guard: `model` reaches the pool only if the DTO carries it. `trays` are raw
    Bambu tray dicts at their array position (build_ams slots by position)."""
    print_obj = {"gcode_state": "IDLE", "ams": {"tray_now": "255", "ams": [{"id": "0", "tray": trays}]}}
    if vt_tray is not None:
        print_obj["vt_tray"] = vt_tray
    return normalize_status(pid, {"print": print_obj}, connection_state="connected", model=model)


_WHITE_PLA = {"id": "0", "state": 9, "tray_type": "PLA", "tray_info_idx": "GFL99", "tray_color": "FFFFFFFF"}
_BLACK_PETG = {"id": "0", "state": 9, "tray_type": "PETG", "tray_info_idx": "GFG99", "tray_color": "000000FF"}


class TestRc1Rc2(unittest.TestCase):
    def setUp(self):
        live_pool._scope_state.clear()

    def test_pool_scopes_by_the_model_the_dto_carries(self):
        a1 = _raw_status("p1", "A1 Mini", [_WHITE_PLA])
        p2s = _raw_status("p2", "P2S", [_BLACK_PETG])
        scoped = scoped_statuses([a1, p2s], "A1 Mini")
        self.assertEqual([s["printerId"] for s in scoped], ["p1"])
        self.assertEqual([t["tray_type"] for t in vp_pool_from_statuses(scoped, 4, 4)], ["PLA"])

    def test_model_offline_means_an_empty_pool_never_another_models_spools(self):
        a1 = _raw_status("p1", "A1 Mini", [_WHITE_PLA])
        with self.assertLogs("makeros-hub.vprinter", level="WARNING") as engaged:
            self.assertEqual(scoped_statuses([a1], "H2D"), [])  # no H2D online: NOTHING, not the A1's spools
        self.assertIn("NO spools", engaged.output[0])
        self.assertIn("a1 mini", engaged.output[0])
        with self.assertNoLogs("makeros-hub.vprinter", level="INFO"):
            scoped_statuses([a1], "H2D")  # next beat: same condition, no drumbeat
        h2d = _raw_status("p3", "H2D", [_BLACK_PETG])
        with self.assertLogs("makeros-hub.vprinter", level="INFO") as recovered:
            self.assertEqual([s["printerId"] for s in scoped_statuses([a1, h2d], "H2D")], ["p3"])
        self.assertIn("scoped to the model group", recovered.output[0])
        self.assertEqual(scoped_statuses([], "H2D"), [])  # no printers at all: empty, no crash

    def test_compat_fallback_only_when_no_status_carries_a_model(self):
        legacy = {"printerId": "p1", "ams": [{"trays": [{"slot": 0, "material": "PLA", "colorHex": "FFFFFFFF"}]}]}
        with self.assertLogs("makeros-hub.vprinter", level="WARNING") as compat:
            self.assertEqual(len(scoped_statuses([legacy], "A1 Mini")), 1)  # pre-0.50 DTO: whole hub
        self.assertIn("COMPATIBILITY", compat.output[0])
        # one modelled status is enough to end the compatibility era: the legacy one no longer matches
        with self.assertLogs("makeros-hub.vprinter", level="WARNING"):
            self.assertEqual(scoped_statuses([legacy, _raw_status("p2", "P2S", [_BLACK_PETG])], "A1 Mini"), [])

    def test_scope_state_prunes_to_the_configured_vps_so_a_readded_vp_warns_again(self):
        a1 = _raw_status("p1", "A1 Mini", [_WHITE_PLA])
        with self.assertLogs("makeros-hub.vprinter", level="WARNING"):
            scoped_statuses([a1], "H2D")
        prune_scope_state(["A1 Mini"])  # the H2D VP was removed from config...
        with self.assertLogs("makeros-hub.vprinter", level="WARNING"):
            scoped_statuses([a1], "H2D")  # ...and re-added while still offline: warns afresh

    def test_external_spool_joins_the_pool(self):
        vt = {"id": "254", "state": 9, "tray_type": "PLA", "tray_info_idx": "GFL99", "tray_color": "FFFFFFFF"}
        s = _raw_status("p1", "A1 Mini", [{"id": "0"}], vt_tray=vt)
        self.assertEqual([(t["tray_type"], t["tray_color"]) for t in vp_pool_from_statuses([s], 1, 4)], [("PLA", "FFFFFFFF")])
        self.assertEqual(loaded_keys([s]), ["PLA|GFL99|FFFFFFFF"])
        # an EMPTY external holder (full field set, no type, no state) contributes nothing
        empty = _raw_status("p1", "A1 Mini", [{"id": "0"}], vt_tray={"id": "254", "tray_type": "", "tray_color": "00000000", "remain": 0})
        self.assertEqual(vp_pool_from_statuses([empty], 1, 4), [])

    def test_unidentified_spools_ride_the_dto_but_never_enter_the_pool(self):
        # A spool without a type can never be matched to a job, so it must never be SELECTABLE: it stays
        # out of the pool, the ranking signals and the display identity. The Floor reads unidentifiedSpools.
        s1 = _raw_status("p1", "A1 Mini", [_WHITE_PLA, {"id": "1", "state": 9, "tray_type": ""}])
        s2 = _raw_status("p2", "A1 Mini", [{"id": "0", "state": 9, "tray_type": ""}])
        self.assertEqual(s1["unidentifiedSpools"], [{"unit": 0, "slot": 1}])
        self.assertEqual(s2["unidentifiedSpools"], [{"unit": 0, "slot": 0}])
        self.assertEqual([t["tray_type"] for t in vp_pool_from_statuses([s1, s2], 1, 4)], ["PLA"])
        self.assertEqual(loaded_keys([s1, s2]), ["PLA|GFL99|FFFFFFFF"])
        cfg = _cfg(vp_pool_from_statuses([s1], 1, 4))
        self.assertIsNone(updated_config_if_pool_changed(cfg, [s1, s2]))  # an unknown spool is not a display change


class TestUpdatedConfig(unittest.TestCase):
    def test_no_display_change_returns_none(self):
        statuses = [_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF"}])]
        cfg = _cfg(vp_pool_from_statuses(statuses, 1, 4))
        self.assertIsNone(updated_config_if_pool_changed(cfg, statuses))

    def test_remain_only_change_does_not_churn(self):
        s1 = [_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF", "remainPct": 90}])]
        cfg = _cfg(vp_pool_from_statuses(s1, 1, 4))
        s2 = [_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF", "remainPct": 4}])]
        self.assertIsNone(updated_config_if_pool_changed(cfg, s2))  # remain% is volatile

    def test_material_change_returns_new_config(self):
        s1 = [_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF"}])]
        cfg = _cfg(vp_pool_from_statuses(s1, 1, 4))
        s2 = [_status([{"slot": 0, "material": "ABS", "filamentId": "GFB00", "colorHex": "000000FF"}])]
        new = updated_config_if_pool_changed(cfg, s2)
        self.assertIsNotNone(new)
        self.assertEqual(new.pool[0]["tray_type"], "ABS")
        self.assertEqual(cfg.pool[0]["tray_type"], "PLA")  # original frozen config untouched

    def test_lowercase_report_does_not_churn_against_uppercase_config(self):
        # Regression for the parity bug: the cloud config-down stores an UPPERCASE
        # tray_type ("PLA"); if the printer reports lowercase material + short
        # color, the live-mirror must normalize identically and see no display
        # change -> None (no needless ams.version bump every config-down).
        cfg = _cfg(
            [
                {
                    "tray_type": "PLA",
                    "tray_info_idx": "GFL99",
                    "tray_sub_brands": "Generic PLA",
                    "tray_color": "FFFFFFFF",
                    "cols": ["FFFFFFFF"],
                }
            ]
        )
        statuses = [
            _status(
                [
                    {
                        "slot": 0,
                        "material": "pla",
                        "filamentId": "GFL99",
                        "colorHex": "ffffff",
                        "productName": "Generic PLA",
                    }
                ]
            )
        ]
        self.assertIsNone(updated_config_if_pool_changed(cfg, statuses))

    def test_none_config(self):
        self.assertIsNone(updated_config_if_pool_changed(None, []))


if __name__ == "__main__":
    unittest.main()
