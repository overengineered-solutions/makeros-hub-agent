"""B3 (hub v0.56): virtual → physical tray translation. The member's ams_mapping names VP pool positions; the
printer wants ams_id*4+slot (BambuStudio get_tray_id_by_ams_id_and_slot_id) or 254/255 for external spools."""

import unittest

from makeros_hub.printers import tray_translate as tt
from makeros_hub.vprinter.capture import parse_slice_info_config

UNITS = [
    {"unit": 0, "raw": {"id": "0"}, "trays": [
        {"slot": 0, "material": "PETG", "colorHex": "000000FF", "filamentId": "GFG00"},
        {"slot": 1, "material": "PLA", "colorHex": "FFFFFFFF", "filamentId": "GFL99"},
        {"slot": 2, "material": "pla", "colorHex": "ffffffff", "filamentId": "GFA00"},   # same white PLA, other brand
        {"slot": 3},                                                                     # empty
    ]},
    {"unit": 1, "raw": {"id": "128"}, "trays": [{"slot": 0, "material": "PLA-CF", "colorHex": "333333FF"}]},  # AMS-HT
    {"unit": 2, "trays": [{"slot": 0, "material": "ABS", "colorHex": "FF0000FF"}]},                           # no raw id
]
VT = {"material": "TPU", "colorHex": "00FF00FF"}


class TestLiveTrays(unittest.TestCase):
    def test_ids_are_raw_unit_times_4_plus_slot_and_unaddressable_units_are_skipped(self):
        trays = tt.live_trays(UNITS, VT)
        self.assertEqual([t["tray_id"] for t in trays], [0, 1, 2, 512, 254])
        self.assertEqual(trays[3], {"tray_id": 512, "material": "PLA-CF", "color": "333333", "filament_id": None})
        self.assertEqual(trays[4]["material"], "TPU")
        self.assertEqual(tt.unaddressable_units(UNITS), 1)

    def test_bad_slots_and_bools_are_ignored(self):
        units = [{"raw": {"id": 0}, "trays": [{"slot": 4, "material": "PLA"}, {"slot": True, "material": "PLA"}, {"slot": 1, "material": "PLA"}]}]
        self.assertEqual([t["tray_id"] for t in tt.live_trays(units)], [1])


class TestTranslateMapping(unittest.TestCase):
    def setUp(self):
        self.trays = tt.live_trays(UNITS, VT)

    def test_one_based_bambu_slots_map_by_position(self):
        # project filament 1 = white PLA GFL99, filament 2 = black PETG; the member's virtual ids were 5 and 9
        required = [{"slot": 1, "type": "PLA", "color": "FFFFFF", "idx": "GFL99"}, {"slot": 2, "type": "PETG", "color": "000000"}]
        self.assertEqual(tt.translate_mapping([5, 9], required, self.trays), [1, 0])

    def test_zero_based_slots_map_by_position(self):
        required = [{"slot": 0, "type": "PLA", "color": "FFFFFF"}, {"slot": 1, "type": "PETG", "color": "000000"}]
        self.assertEqual(tt.translate_mapping([5, 9], required, self.trays), [1, 0])

    def test_passthrough_and_unused(self):
        required = [{"slot": 2, "type": "PETG", "color": "000000"}]
        # k=0 unused (-1), k=1 → PETG, k=2 mapped by the member but not a required filament → unmapped, k=3 external kept
        self.assertEqual(tt.translate_mapping([-1, 7, 3, 254], required, self.trays), [-1, 0, -1, 254])

    def test_every_required_filament_ends_up_mapped(self):
        # codex r4: a TRUNCATED member mapping ([0] for a two-filament plate) or a -1 where the plate needs a spool is
        # extended/filled from the requirements — never sent short
        required = [{"slot": 1, "type": "PLA", "color": "FFFFFF"}, {"slot": 2, "type": "PETG", "color": "000000"}]
        self.assertEqual(tt.translate_mapping([0], required, self.trays), [1, 0])
        self.assertEqual(tt.translate_mapping([-1, -1], required, self.trays), [1, 0])
        self.assertEqual(tt.translate_mapping([], required, self.trays), [1, 0])
        # …and a required spool that is not loaded still refuses, even when the short list never mentioned it
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FFFFFF"}, {"slot": 2, "type": "ABS", "color": "FF0000"}], self.trays)
        # the H2D's second-nozzle map keeps -1 = "not on this nozzle", and the primary map is not filled for a filament the
        # second map covers (a two-colour dual-nozzle plate: PLA on the left, PETG on the right)
        out = tt.translate_print_trays({"use_ams": True, "ams_mapping": [5, -1], "ams_mapping2": [-1, 9]}, required, UNITS, VT)
        self.assertEqual((out["ams_mapping"], out["ams_mapping2"]), ([1, -1], [-1, 0]))

    def test_filament_id_is_exact_when_the_loaded_spools_carry_ids(self):
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FFFFFF", "idx": "GFA00"}], self.trays), [2])
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FFFFFF"}], self.trays), [1])
        # the member chose GFZ99 white PLA; only OTHER white PLAs are loaded → a refusal, never "close enough" (codex r1)
        with self.assertRaises(tt.TrayTranslationError) as ctx:
            tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FFFFFF", "idx": "GFZ99"}], self.trays)
        self.assertIn("a different PLA #FFFFFF is loaded", ctx.exception.detail)
        # …but when the loaded candidates carry NO id at all, material + colour is the only signal and it is honoured
        no_ids = [{"tray_id": 0, "material": "PLA", "color": "FFFFFF", "filament_id": None}]
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FFFFFF", "idx": "GFZ99"}], no_ids), [0])

    def test_external_spool_is_a_target_when_it_is_the_only_match(self):
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "TPU", "color": "00FF00"}], self.trays), [254])
        # an AMS tray always wins over the external holder for the same requirement
        both = self.trays + [{"tray_id": 254, "material": "PETG", "color": "000000", "filament_id": None}]
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "PETG", "color": "000000"}], both), [0])

    def test_no_requirements_with_ams_trays_mapped_is_a_refusal(self):
        for required in ([], None):
            with self.assertRaises(tt.TrayTranslationError) as ctx:
                tt.translate_mapping([5], required or [], self.trays)
            self.assertIn("no filament requirements", ctx.exception.detail)
        # external-only / unmapped prints have nothing to translate and pass through
        self.assertEqual(tt.translate_mapping([254, -1], [], self.trays), [254, -1])

    def test_ht_unit_and_colour_alpha_and_case(self):
        self.assertEqual(tt.translate_mapping([0], [{"slot": 1, "type": "pla-cf", "color": "#333333ff"}], self.trays), [512])

    def test_half_identity_is_never_a_wildcard(self):
        # codex r3: "PETG, any colour" / "black, any material" would print the wrong spool — refuse instead
        for req in ({"slot": 1, "color": "000000"}, {"slot": 1, "type": "PETG"}):
            with self.assertRaises(tt.TrayTranslationError) as ctx:
                tt.translate_mapping([0], [req], self.trays)
            self.assertIn("identity incomplete", ctx.exception.detail)

    def test_web_upload_mapping_is_built_from_requirements(self):
        required = [{"slot": 1, "type": "PLA", "color": "FFFFFF"}, {"slot": 3, "type": "PETG", "color": "000000"}]
        self.assertEqual(tt.mapping_from_requirements(required, self.trays), [1, -1, 0])
        out = tt.translate_print_trays({"use_ams": True, "ams_mapping": []}, required, UNITS, VT)      # malformed/empty replay
        self.assertEqual(out["ams_mapping"], [1, -1, 0])
        out = tt.translate_print_trays({"use_ams": True}, required, UNITS, VT)                           # no mapping at all
        self.assertEqual(out["ams_mapping"], [1, -1, 0])
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_print_trays({"use_ams": True}, [], UNITS, VT)

    def test_missing_spool_refuses_never_guesses(self):
        with self.assertRaises(tt.TrayTranslationError) as ctx:
            tt.translate_mapping([0], [{"slot": 1, "type": "PLA", "color": "FF00FF"}], self.trays)
        self.assertEqual(ctx.exception.reason, "spool_mismatch")
        self.assertIn("PLA #FF00FF", ctx.exception.detail)


class TestTranslatePrint(unittest.TestCase):
    def test_rewrites_both_nozzle_maps_and_leaves_external_prints_alone(self):
        cmd = {"use_ams": True, "ams_mapping": [5, 9], "ams_mapping2": [-1, 9], "bed_type": "cool_plate"}
        required = [{"slot": 1, "type": "PLA", "color": "FFFFFF"}, {"slot": 2, "type": "PETG", "color": "000000"}]
        out = tt.translate_print_trays(cmd, required, UNITS, VT)
        self.assertEqual((out["ams_mapping"], out["ams_mapping2"], out["bed_type"]), ([1, 0], [-1, 0], "cool_plate"))
        self.assertEqual(cmd["ams_mapping"], [5, 9])   # input untouched
        ext = {"use_ams": False, "ams_mapping": [254]}
        self.assertEqual(tt.translate_print_trays(ext, required, UNITS, VT), ext)

    def test_absent_requirements_refuse_ams_prints_but_not_external_ones(self):
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_print_trays({"use_ams": True, "ams_mapping": [5]}, None, UNITS, VT)
        self.assertEqual(tt.translate_print_trays({"use_ams": True, "ams_mapping": [254]}, None, UNITS, VT)["ams_mapping"], [254])

    def test_no_addressable_units_is_a_refusal(self):
        with self.assertRaises(tt.TrayTranslationError):
            tt.translate_print_trays({"use_ams": True, "ams_mapping": [0]}, [{"slot": 1, "type": "PLA"}], [{"unit": 0, "trays": [{"slot": 0, "material": "PLA"}]}])


SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <metadata key="filament_type" value="PLA;PETG;ABS"/>
  <metadata key="filament_colour" value="#FFFFFF;#000000;#FF0000"/>
  <plate>
    <metadata key="index" value="3"/>
    <metadata key="filament_type" value="TPU"/>
    <metadata key="filament_colour" value="#00FF00"/>
  </plate>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" tray_info_idx="GFL99" type="PLA" color="#FFFFFF" used_m="1.0" used_g="3.5"/>
    <filament id="2" tray_info_idx="GFG00" type="PETG" color="#000000" used_m="2.0" used_g="7.25"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <filament id="3" tray_info_idx="GFB00" type="ABS" color="#FF0000" used_m="0.5" used_g="1.5"/>
  </plate>
</config>"""


class TestPlateScopedRequirements(unittest.TestCase):
    def test_only_the_sent_plate(self):
        # project-wide metadata arrays must NOT seed a scoped plate (codex r1): plate 2 is ABS only
        self.assertEqual([f["slot"] for f in parse_slice_info_config(SLICE_INFO, 2)], [3])
        self.assertEqual([f["slot"] for f in parse_slice_info_config(SLICE_INFO, 1)], [1, 2])
        self.assertEqual(parse_slice_info_config(SLICE_INFO, 1)[1]["usedG"], 7.25)
        self.assertEqual(parse_slice_info_config(SLICE_INFO, 1)[0]["trayInfoIdx"], "GFL99")

    def test_array_only_plate_is_scoped_to_that_plate(self):
        # plate 3 has no <filament> elements, only its own arrays — read THOSE, not the project-wide ones (codex r3)
        self.assertEqual(parse_slice_info_config(SLICE_INFO, 3), [{"slot": 0, "material": "TPU", "color": "00FF00FF"}])

    def test_unknown_or_absent_plate_keeps_every_filament(self):
        self.assertEqual([f["slot"] for f in parse_slice_info_config(SLICE_INFO)], [1, 2, 3])
        self.assertEqual([f["slot"] for f in parse_slice_info_config(SLICE_INFO, 9)], [1, 2, 3])
