"""H2D jobs sent over LAN: the nozzle per filament comes from the sliced plate, not the print command.

Fixture: the slice_info.config of a real OrcaSlicer 2.4.2 export for the H2D (2026-10-20, filament grouping
"Auto For Match"): filament 1 PETG-CF black on the left nozzle, filament 4 PLA white on the right
(filament_maps "1 2 2 2", group_id 0 / 1). Its project_settings.config carried physical_extruder_map ["1","0"] and a
stale project-wide filament_map ["1","1","1","1"]. A LAN send carries no nozzle routing: CUBE_SEND is the command
OrcaSlicer 2.4.2 sent for that same plate through the H2D VP on hub 0.76.1 (prod print_jobs.raw_print, selected fields);
a student's send the same day carried only {"use_ams": true, "ams_mapping": [-1, 0, -1, -1]}. Bambu Studio's working LAN dual-nozzle print to a real H2D
carried ams_mapping [128, 2] + ams_mapping2 [{128,0},{0,2}] (bambuddy issue #318).
"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.vprinter.capture import parse_required_filaments, _vp_submit_filament
from makeros_hub.printers import tray_translate
from test_h2d_hardware import hardware, status

SLICE_INFO = (Path(__file__).parent / "fixtures/h2d-auto-for-match-2026-10-20.slice_info.config").read_text()
PROJECT = {"physical_extruder_map": ["1", "0"], "filament_map": ["1", "1", "1", "1"]}
CUBE_SEND = {"command": "project_file", "param": "Metadata/plate_1.gcode", "use_ams": True, "cfg": "0",
             "ams_mapping": [8, -1, -1, 2],
             "ams_mapping2": [{"ams_id": 2, "slot_id": 0}, {"ams_id": 255, "slot_id": 255},
                              {"ams_id": 255, "slot_id": 255}, {"ams_id": 0, "slot_id": 2}],
             "auto_bed_leveling": 2, "extrude_cali_flag": 2, "nozzle_offset_cali": 2}


def threemf(directory, slice_info=SLICE_INFO, project=PROJECT):
    path = Path(directory) / "plate.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        if project is not None:
            z.writestr("Metadata/project_settings.config", json.dumps(project))
    return path


def loaded(left=("PETG-CF", "000000FF"), right=("PLA", "FFFFFFFF")):
    raw = hardware()
    for unit in raw["ams"]["ams"]:
        material, color = left if unit["id"] == "128" else right
        unit["tray"] = [{"id": "0", "tray_type": material, "tray_color": color}]
    return status(raw)


class TestPlateNozzles(unittest.TestCase):
    def required(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            return parse_required_filaments(threemf(d, **kw), plate=1)

    def test_real_export_names_each_filaments_physical_nozzle(self):
        self.assertEqual([(f["slot"], f["material"], f.get("extruderId"), f.get("nozzleDiameterMm")) for f in self.required()],
                         [(1, "PETG-CF", 1, 0.4), (4, "PLA", 0, 0.4)])
        self.assertEqual(_vp_submit_filament(self.required()[0])["extruderId"], 1)

    def test_disagreeing_or_missing_sources_leave_the_nozzle_unknown(self):
        for broken in (SLICE_INFO.replace('value="1 2 2 2"', 'value="2 2 2 2"'),   # filament_maps vs group_id
                       SLICE_INFO.replace(' group_id="0"', ''),
                       SLICE_INFO.replace('<metadata key="filament_maps" value="1 2 2 2"/>', '')):
            self.assertNotIn("extruderId", self.required(slice_info=broken)[0])
        for project in (None, {"physical_extruder_map": ["0"]}, {"physical_extruder_map": [float("inf"), 0]}):   # one-extruder printers, a crafted file: nothing added
            self.assertEqual({k for f in self.required(project=project) for k in f} & {"extruderId"}, set())


class TestLanDispatch(unittest.TestCase):
    def translate(self, st, command=CUBE_SEND):
        with tempfile.TemporaryDirectory() as d:
            required = [_vp_submit_filament(f) for f in parse_required_filaments(threemf(d), plate=1)]
        return tray_translate.translate_print_trays(dict(command), required, st.get("ams"),
                                                   extruder_count=st.get("extruderCount"), nozzles=st.get("nozzles"))

    def test_lan_send_dispatches_each_filament_on_its_sliced_nozzle(self):
        for command in (CUBE_SEND, {"use_ams": True, "ams_mapping": [0, -1, -1, 4]}, {"use_ams": True}):
            out = self.translate(loaded(), command)   # captured, legacy-only, and a web upload's rebuilt command
            self.assertEqual(out["ams_mapping"], [128, -1, -1, 0])
            self.assertEqual(out["ams_mapping2"], [{"ams_id": 128, "slot_id": 0}, {"ams_id": 255, "slot_id": 255},
                                                   {"ams_id": 255, "slot_id": 255}, {"ams_id": 0, "slot_id": 0}])
        self.assertEqual(self.translate(loaded())["nozzle_offset_cali"], 2)   # the member's other choices replay as sent

    def test_spool_on_the_other_nozzle_does_not_satisfy_it(self):
        with self.assertRaises(tray_translate.TrayTranslationError) as caught:
            self.translate(loaded(left=("PLA", "FFFFFFFF"), right=("PETG-CF", "000000FF")))
        self.assertEqual(caught.exception.reason, "spool_mismatch")

    def test_a_different_nozzle_size_refuses(self):
        st = loaded()
        st["nozzles"] = [dict(n, diameterMm=0.6) if n["extruderId"] == 1 else n for n in st["nozzles"]]
        with self.assertRaises(tray_translate.TrayTranslationError) as caught:
            self.translate(st)
        self.assertEqual(caught.exception.reason, "unsupported_nozzle_mapping")


if __name__ == "__main__":
    unittest.main()
