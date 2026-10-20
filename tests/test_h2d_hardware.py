"""H2D regression from observed hardware and BambuStudio's actual serializers.

Protocol references: SelectMachine.cpp get_ams_mapping_result; SelectMachine.hpp
CloudTaskNozzleId; DeviceCore/DevMapping.cpp and DevFilaSystem.cpp. The fixture
is a read-only hardware report, not a captured/validated print command.
"""
import copy
import dataclasses
import asyncio
import json
from pathlib import Path
import unittest
import tempfile

from makeros_hub.config import VirtualPrinterConfig
from makeros_hub.printers import bambu_parse, bambu_send, tray_translate
from makeros_hub.vprinter.live_pool import updated_configs_if_pools_changed, vp_pool_from_statuses_ranked
from makeros_hub.vprinter.report import build_push_status


def hardware():
    raw = json.loads((Path(__file__).parent / "fixtures/h2d-device-observed-2026-09-09.json").read_text())["raw"]
    # Deliberate test spools: same identity on both nozzle paths, fuller on left.
    for unit in raw["ams"]["ams"]:
        unit["tray"] = [{"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF",
                         "remain": 95 if unit["id"] == "128" else 10}]
    return raw


def status(raw=None, connected=True):
    return bambu_parse.normalize_status("h2d", {"print": raw or hardware()}, model="H2D",
                                        connection_state="connected" if connected else "offline")


REQUIRED = [{"slot": 1, "type": "PLA", "color": "FFFFFF"}, {"slot": 2, "type": "PLA", "color": "FFFFFF"}]


def command():
    return {"use_ams": True, "ams_mapping": [5, 9],
            "ams_mapping2": [{"ams_id": 1, "slot_id": 1}, {"ams_id": 2, "slot_id": 1}],
            "ams_mapping_info": [{"ams": 5, "nozzleId": 0}, {"ams": 9, "nozzleId": 1}],
            "nozzles_info": [{"id": 0, "diameter": 0.4}, {"id": 1, "diameter": 0.4}]}


def translate(cmd=None, st=None):
    st = st or status()
    return tray_translate.translate_print_trays(cmd or command(), REQUIRED, st.get("ams"),
                                               extruder_count=st.get("extruderCount"), nozzles=st.get("nozzles"))


class TestObservedHardware(unittest.TestCase):
    def test_observed_h2d_normalizes_physical_ids_without_relabeling_units(self):
        s = status()
        self.assertEqual(s["extruderCount"], 2)
        self.assertEqual(s["nozzles"], [{"id": 0, "diameterMm": 0.4, "type": "HS01", "extruderId": 0},
                                         {"id": 1, "diameterMm": 0.4, "type": "HS01", "extruderId": 1}])
        self.assertEqual([(u["unit"], u["raw"]["id"], u["amsType"], u["extruderId"]) for u in s["ams"]],
                         [(0, "0", "ams_2_pro", 0), (1, "128", "ams_ht", 1)])

    def test_missing_malformed_and_switcher_bindings_are_unknown(self):
        for value in (None, "wrong", "", "0e04", True):
            with self.subTest(info=value):
                raw = hardware()
                raw["ams"]["ams"][1]["info"] = value
                self.assertNotIn("extruderId", status(raw)["ams"][1])
                with self.assertRaises(tray_translate.TrayTranslationError):
                    translate(st=status(raw))
        off = status(connected=False)
        self.assertNotIn("nozzles", off)
        self.assertNotIn("extruderCount", off)

    def test_invalid_or_removed_nozzles_do_not_become_defaults(self):
        for value in (None, True, "nan", -1, 0):
            raw = hardware()
            raw["device"]["nozzle"]["info"][0]["diameter"] = value
            self.assertEqual([n["id"] for n in status(raw)["nozzles"]], [1])
        raw = hardware()
        raw["device"]["nozzle"]["exist"] = 1
        self.assertEqual([n["id"] for n in status(raw)["nozzles"]], [0])


class TestSourceBackedH2dTranslation(unittest.TestCase):
    def test_structured_map_survives_replay_and_both_maps_use_correct_nozzle(self):
        raw = command()
        built = bambu_send.build_print_start_payload("part.3mf", sequence_id="test", raw_print=raw)["print"]
        self.assertEqual(built["ams_mapping2"], raw["ams_mapping2"])
        sent = translate(built)
        # Source generator uses 128 for AMS HT, never universal 128*4=512.
        self.assertEqual(sent["ams_mapping"], [0, 128])
        self.assertEqual(sent["ams_mapping2"], [{"ams_id": 0, "slot_id": 0}, {"ams_id": 128, "slot_id": 0}])
        self.assertEqual([m["ams"] for m in sent["ams_mapping_info"]], [0, 128])
        self.assertEqual(raw, command())

    def test_json_task_metadata_retains_source_nozzle_convention(self):
        raw = command()
        raw["ams_mapping_info"] = json.dumps(raw["ams_mapping_info"])
        raw["nozzles_info"] = json.dumps(raw["nozzles_info"])
        sent = translate(raw)
        self.assertEqual(sent["ams_mapping"], [0, 128])
        self.assertEqual([m["nozzleId"] for m in json.loads(sent["ams_mapping_info"])], [0, 1])

    def test_unsupported_or_missing_nozzle_semantics_refuse(self):
        for changes in ({"ams_mapping_info": None}, {"ams_mapping_info": [{"nozzleId": True}, {"nozzleId": 1}]},
                        {"nozzles_info": [{"id": 0, "diameter": 0.6}, {"id": 1, "diameter": 0.4}]},
                        {"nozzle_mapping": [0, 1]}, {"use_ams": False}, {"ams_mapping2": [128]},
                        {"ams_mapping2": [{"ams_id": True, "slot_id": 0}]}):
            with self.subTest(changes=changes), self.assertRaises(tray_translate.TrayTranslationError):
                translate(dict(command(), **changes))

    def test_same_filament_on_wrong_nozzle_does_not_satisfy_job(self):
        s = status()
        s["ams"][0]["trays"] = []  # left still has a fuller, identical spool
        with self.assertRaises(tray_translate.TrayTranslationError) as error:
            translate(st=s)
        self.assertEqual(error.exception.reason, "spool_mismatch")

    def test_incomplete_or_duplicate_filament_indices_refuse_instead_of_dropping_requirements(self):
        s = status()
        for slot in (None, True, 1, -1, 1.5, "2"):
            with self.subTest(slot=slot), self.assertRaises(tray_translate.TrayTranslationError):
                tray_translate.translate_print_trays(command(), [REQUIRED[0], dict(REQUIRED[1], slot=slot)], s["ams"],
                                                     extruder_count=2, nozzles=s["nozzles"])

    def test_physical_ams_ids_are_not_truncated_or_coerced_from_booleans(self):
        for value in (True, 0.5, 0.0, "0.5", "1e0", "", -1, 254, "9" * 5000):
            with self.subTest(value=str(value)[:20]):
                units = [{"raw": {"id": value}, "trays": [{"slot": 0, "material": "PLA"}]}]
                self.assertEqual(tray_translate.live_trays(units), [])
                self.assertEqual(tray_translate.unaddressable_units(units), 1)


class TestObservedVirtualH2d(unittest.TestCase):
    def config(self):
        return VirtualPrinterConfig(True, "VP", "H2D", "Virtual H2D", "test", "127.0.0.1", 2, 4, "n3f", (), ())

    def test_live_mirror_keeps_identical_spools_on_separate_observed_nozzle_paths(self):
        s = status()
        pool = vp_pool_from_statuses_ranked([s], 2, 4, None, 0)
        self.assertEqual({t["extruderId"] for t in pool}, {0, 1})
        self.assertEqual(len(pool), 2)
        [cfg] = updated_configs_if_pools_changed([self.config()], [s], None, 0)
        report = build_push_status(units=2, trays=4, model=cfg.model, filaments=cfg.pool, nozzles=cfg.nozzles)["print"]
        self.assertEqual(bambu_parse.build_nozzle_hardware(report), {"extruderCount": 2, "nozzles": s["nozzles"]})
        units = bambu_parse.build_ams(report)
        self.assertEqual([u["extruderId"] for u in units], [0, 1])
        self.assertEqual([[t["material"] for t in u["trays"] if t.get("material")] for u in units], [["PLA"], ["PLA"]])
        self.assertIsNone(updated_configs_if_pools_changed([cfg], [s], None, 0))
        changed = copy.deepcopy(s)
        changed["nozzles"][0]["diameterMm"] = 0.6
        self.assertIsNotNone(updated_configs_if_pools_changed([cfg], [changed], None, 0))

    def test_unknown_or_disagreeing_hardware_is_not_advertised(self):
        s = status()
        different = copy.deepcopy(s)
        different["nozzles"][0]["diameterMm"] = 0.6
        for sources in ([dict(s, nozzles=[])], [s, different]):
            [cfg] = updated_configs_if_pools_changed([self.config()], sources, None, 0)
            self.assertEqual(cfg.nozzles, ())
            report = build_push_status(units=2, model=cfg.model, filaments=cfg.pool, nozzles=cfg.nozzles)["print"]
            self.assertNotIn("device", report)
            self.assertEqual(report["ams"]["tray_exist_bits"], "0")

    def test_nozzle_only_change_bumps_virtual_device_version(self):
        from makeros_hub.vprinter.manager import _VirtualPrinterRuntime
        [cfg] = updated_configs_if_pools_changed([self.config()], [status()], None, 0)
        with tempfile.TemporaryDirectory() as directory:
            runtime = _VirtualPrinterRuntime(cfg, base_dir=Path(directory), on_capture=lambda *_: None)
            before = runtime._ams_version
            nozzles = copy.deepcopy(cfg.nozzles)
            nozzles[0]["diameterMm"] = 0.6
            changed = dataclasses.replace(cfg, nozzles=nozzles)
            asyncio.run(runtime.apply_hot(changed))
            self.assertGreater(runtime._ams_version, before)
            after = runtime._ams_version
            asyncio.run(runtime.apply_hot(changed))
            self.assertEqual(runtime._ams_version, after)


def orca_nozzles(report):
    """{extruder id: (diameter, flow)} as OrcaSlicer 2.4.2 reads a push_status (DeviceManager.cpp parse_json then
    parse_new_info; DevExtruderSystem.cpp, DevNozzleSystem.cpp). Flat fields always give extruder 0; `device` counts
    only behind check_enable_np, and a non-string cfg/fun/aux/stat throws inside a try that drops the whole block."""
    flat = {0: (float(report["nozzle_diameter"]), "S")}
    if not all(k in report for k in ("cfg", "fun", "aux", "stat")) or "device" not in report:
        return flat
    if not all(isinstance(report[k], str) for k in ("cfg", "fun", "aux", "stat")):
        return flat
    device = report["device"]
    nozzles = {n["id"]: (float(n["diameter"]), n["type"][1:2] if len(n["type"]) >= 4 else "") for n in device["nozzle"]["info"]}
    for e in device["extruder"]["info"]:
        assert all(isinstance(e[k], int) for k in ("id", "info", "temp", "spre", "snow", "star", "stat", "hnow"))
        assert all(isinstance(v, int) for v in e["filam_bak"])
    return {e["id"]: nozzles.get(e["hnow"], (0.0, "")) for e in device["extruder"]["info"]}


def orca_send_nozzle_ok(report, nozzle):
    """SelectMachine.cpp _is_nozzle_data_valid for a filament on this physical nozzle (0 right, 1 left)."""
    diameter, flow = orca_nozzles(report).get(nozzle, (0.0, ""))
    return diameter > 0 and flow in ("S", "H", "A", "X")


class TestOrcaReadsVirtualH2D(unittest.TestCase):
    def report(self):
        [cfg] = updated_configs_if_pools_changed([TestObservedVirtualH2d.config(self)], [status()], None, 0)
        return build_push_status(units=2, trays=4, model=cfg.model, filaments=cfg.pool, nozzles=cfg.nozzles)["print"]

    def test_orca_sees_both_nozzles_so_send_is_enabled_for_either(self):
        report = self.report()
        self.assertEqual(orca_nozzles(report), {0: (0.4, "S"), 1: (0.4, "S")})
        self.assertTrue(orca_send_nozzle_ok(report, 0))
        self.assertTrue(orca_send_nozzle_ok(report, 1))
        self.assertIs(report["sdcard"], True)  # empty aux: Orca keeps the flat sdcard flag

    def test_without_the_new_protocol_fields_orca_has_no_left_nozzle(self):
        # The v0.76.0 report: the device block was there, Orca ignored it, Send was grey for the left nozzle.
        report = {k: v for k, v in self.report().items() if k not in ("cfg", "fun", "aux", "stat")}
        self.assertFalse(orca_send_nozzle_ok(report, 1))

    def test_unobserved_hardware_still_advertises_no_device(self):
        report = build_push_status(units=2, model="H2D", filaments=[], nozzles=())["print"]
        self.assertNotIn("device", report)
        self.assertNotIn("cfg", report)
