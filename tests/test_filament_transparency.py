import unittest

from makeros_hub.printers.tray_translate import live_trays, translate_print_trays, TrayTranslationError
from makeros_hub.vprinter.live_pool import tray_key, vp_pool_from_statuses
from makeros_hub.vprinter.capture import parse_slice_info_config, _vp_submit_filament


class TestFilamentTransparency(unittest.TestCase):
    def test_capture_to_physical_tray_preserves_alpha(self):
        required = [_vp_submit_filament(f) for f in parse_slice_info_config(
            '<config><plate><metadata key="index" value="1"/>'
            '<filament id="1" type="PETG" color="#00000000" tray_info_idx="GFG01" used_g="12"/>'
            '</plate></config>', 1)]
        self.assertEqual(required[0]["color"], "00000000")
        trays = [
            {"slot": 0, "material": "PETG", "colorHex": "000000FF", "filamentId": "GFG01", "remainPct": 99},
            {"slot": 1, "material": "PETG", "colorHex": "00000000", "filamentId": "GFG01", "tagUid": "0123456789ABCDEF", "remainPct": 10},
            {"slot": 2, "material": "PETG", "colorHex": "00000000", "filamentId": "GFG01", "tagUid": "0000000000000000", "remainPct": 100},
        ]
        units = [{"raw": {"id": 0}, "trays": trays}]
        self.assertEqual([t["tray_id"] for t in live_trays(units)], [0, 1])
        self.assertEqual(translate_print_trays({"use_ams": True, "ams_mapping": [0]}, required, units)["ams_mapping"], [1])
        opaque = [dict(required[0], color="000000")]
        self.assertEqual(translate_print_trays({"use_ams": True, "ams_mapping": [1]}, opaque, units)["ams_mapping"], [0])
        for subset in [[trays[0]], [trays[2]]]:
            with self.assertRaises(TrayTranslationError):
                translate_print_trays({"use_ams": True}, required, [{"raw": {"id": 0}, "trays": subset}])

    def test_virtual_pool_and_external_holder_reject_cleared_slots(self):
        clear = {"material": "PETG", "colorHex": "00000000", "filamentId": "GFG01", "tagUid": "0123456789ABCDEF"}
        empty = dict(clear, tagUid="0000000000000000")
        self.assertIsNone(tray_key(empty))
        self.assertIsNotNone(tray_key(clear))
        pool = vp_pool_from_statuses([{"ams": [{"trays": [empty, clear]}]}], 1, 4)
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["tray_color"], "00000000")
        self.assertEqual(live_trays([], empty), [])
        self.assertEqual(live_trays([], clear)[0]["color"], "00000000")
