import unittest

from makeros_hub.config import VirtualPrinterConfig, VirtualPrinterMember
from makeros_hub.vprinter.live_pool import (
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
        self.assertEqual(len(pool), 3)  # GFL99 PLA, GFA07 PLA, GFB00 ABS
        self.assertEqual(sorted(p["tray_type"] for p in pool), ["ABS", "PLA", "PLA"])
        white = next(p for p in pool if p["tray_info_idx"] == "GFL99")
        self.assertEqual(white["tray_color"], "FFFFFFFF")
        self.assertEqual(white["tray_sub_brands"], "Generic PLA")
        self.assertEqual(white["cols"], ["FFFFFFFF"])

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
        a = {"printerId": "p-a", **_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF", "productName": "AAA"}])}
        b = {"printerId": "p-b", **_status([{"slot": 0, "material": "PLA", "filamentId": "GFL99", "colorHex": "FFFFFFFF", "productName": "BBB"}])}
        self.assertEqual(vp_pool_from_statuses([b, a], 1, 4)[0]["tray_sub_brands"], "AAA")
        self.assertEqual(vp_pool_from_statuses([a, b], 1, 4)[0]["tray_sub_brands"], "AAA")


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
