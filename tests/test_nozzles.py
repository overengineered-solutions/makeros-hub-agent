import zipfile

import pytest

from makeros_hub.printers.klipper import _build_status, nozzle_hardware
from makeros_hub.printers.bambu_parse import build_nozzle_hardware
from makeros_hub.printers.gcode_meta import read_gcode_meta
from makeros_hub.printers.nozzles import nozzle_refusal
from makeros_hub.vprinter.capture import parse_required_filaments
from makeros_hub.agent import heartbeat_payload
from makeros_hub.printers.manager import slice_spec_of


def test_klipper_reports_the_installed_nozzle_from_config_in_the_heartbeat():
    settings = {"configfile": {"settings": {"extruder": {"nozzle_diameter": 1.0}}}}
    status = _build_status("synthetic-giga", {}, "connected", nozzles=nozzle_hardware(settings))
    assert status["nozzles"] == [{"id": 0, "extruderId": 0, "diameterMm": 1.0}]
    assert heartbeat_payload(printers=[status])["printers"][0]["nozzles"] == status["nozzles"]
    assert "nozzles" not in _build_status("synthetic-giga", {}, "offline", nozzles=nozzle_hardware(settings))
    for value in [None, 0, -1, 3, True, "nan", "inf", "bad"]:
        assert nozzle_hardware({"configfile": {"settings": {"extruder": {"nozzle_diameter": value}}}}) == {}
    settings["configfile"]["settings"]["extruder1"] = {"nozzle_diameter": "0.6"}
    assert nozzle_hardware(settings)["nozzles"][1] == {"id": 1, "extruderId": 1, "diameterMm": .6}


def test_bambu_reports_flat_older_firmware_and_both_h2d_nozzles_without_a_guessed_default():
    assert build_nozzle_hardware({"nozzle_diameter": "0.6"})["nozzles"][0]["diameterMm"] == .6
    assert build_nozzle_hardware({}) == {}
    device = {"extruder": {"state": 2, "info": [{"id": 0, "hnow": 2}, {"id": 1, "hnow": 3}]},
              "nozzle": {"exist": 12, "info": [{"id": 2, "diameter": .4}, {"id": 3, "diameter": .6, "type": "HS01"}]}}
    got = build_nozzle_hardware({"device": device})
    assert [(n["extruderId"], n["diameterMm"]) for n in got["nozzles"]] == [(0, .4), (1, .6)]
    assert build_nozzle_hardware({"device": {"extruder": {"state": 2}}, "nozzle_diameter": .4}) == {"extruderCount": 2}


def test_gcode_carries_the_nozzle_in_both_header_layouts_and_never_invents_one(tmp_path):
    path = tmp_path / "part.gcode"
    for size, expected in [("1", 1), ("0.6", .6), ("nan", None), ("0", None), ("0.4;0.6", None)]:
        path.write_text(f'; nozzle_diameter = {size}\n; CONFIG_BLOCK_START\n; printer_model = Elegoo OrangeStorm Giga\n; filament_type = PLA\n; CONFIG_BLOCK_END\n')
        assert read_gcode_meta(path)["requiredFilaments"][0].get("nozzleDiameterMm") == expected
        path.write_text(f'; CONFIG_BLOCK_START\n; nozzle_diameter = {size}\n; printer_model = Elegoo OrangeStorm Giga\n; filament_type = PLA\n; CONFIG_BLOCK_END\n')
        assert read_gcode_meta(path)["requiredFilaments"][0].get("nozzleDiameterMm") == expected


def test_bambu_plate_metadata_survives_capture_for_single_and_dual_nozzles(tmp_path):
    path = tmp_path / "part.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="index" value="1"/><metadata key="nozzle_diameters" value="0.8"/><filament id="1" type="PLA"/></plate></config>')
    assert parse_required_filaments(path, 1)[0]["nozzleDiameterMm"] == .8
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", '{"physical_extruder_map":["1","0"]}')
        z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="index" value="1"/><metadata key="filament_maps" value="1 2"/><filament id="1" group_id="0" nozzle_diameter="0.6" type="PLA"/><filament id="2" group_id="1" nozzle_diameter="0.4" type="PLA"/></plate></config>')
    assert [(f["extruderId"], f["nozzleDiameterMm"]) for f in parse_required_filaments(path, 1)] == [(1, .6), (0, .4)]


def test_the_last_hardware_check_refuses_unknown_and_mismatched_nozzles():
    hardware = {"extruderCount": 1, "nozzles": [{"id": 0, "diameterMm": 1}]}
    assert nozzle_refusal([{"nozzleDiameterMm": 1}], hardware) is None
    assert nozzle_refusal([{"nozzleDiameterMm": .4}], hardware).startswith("nozzle_mismatch")
    assert nozzle_refusal([{}], hardware).startswith("nozzle_unknown")
    assert nozzle_refusal([{"nozzleDiameterMm": True}], hardware).startswith("nozzle_unknown")
    assert nozzle_refusal([{"nozzleDiameterMm": .4}], {}, require_report=True).startswith("nozzle_unknown")
    assert nozzle_refusal([{"nozzleDiameterMm": .4}], {}) is None  # Older Bambu: the site's staff record checks it.
    dual = {"extruderCount": 2, "nozzles": [{"extruderId": 0, "diameterMm": .4}, {"extruderId": 1, "diameterMm": .6}]}
    assert nozzle_refusal([{"extruderId": 1, "nozzleDiameterMm": .6}], dual) is None
    assert nozzle_refusal([{"extruderId": 0, "nozzleDiameterMm": .6}], dual).startswith("nozzle_mismatch")


def test_shop_slicing_requires_the_recorded_diameter_and_checks_the_stock_profile():
    from test_slicer_wrapper import ps, SPEC
    assert slice_spec_of({"nozzleDiameterMm": .4})["nozzleDiameterMm"] == .4
    assert ps.read_spec(SPEC)["nozzleDiameterMm"] == .4
    for bad in [None, 1, True, "0.4"]:
        with pytest.raises(ps.SliceError, match="recorded nozzle"):
            ps.read_spec({**SPEC, "nozzleDiameterMm": bad})
    ps.verify_nozzle_profile({"nozzle_diameter": ["0.4", "0.4"]}, .4)
    with pytest.raises(ps.SliceError, match="differs"):
        ps.verify_nozzle_profile({"nozzle_diameter": ["0.4", "0.6"]}, .4)


def test_the_poll_never_carries_the_large_config_and_the_nozzle_read_is_its_own():
    # The SV08's configfile=settings was 215 KB on 2026-10-25: over the poll's 64 KB read, so every poll would have failed.
    import threading
    from makeros_hub.printers import klipper
    assert "configfile" not in klipper._QUERY_OBJECTS
    a = object.__new__(klipper.KlipperAdapter)
    a.printer_id, a._lock, a._nozzles, a._nozzles_next = "synthetic-sv08", threading.Lock(), {}, 0.0
    calls = []

    def fetch(query, limit=0, **_):
        calls.append((query, limit))
        return {"configfile": {"settings": {"extruder": {"nozzle_diameter": 0.4}, "pad": "x" * 300_000}}}
    a._fetch_once = fetch
    a._refresh_nozzles()
    a._refresh_nozzles()   # within NOZZLE_REFRESH_SEC: no second read
    assert calls == [("configfile=settings", klipper.CONFIG_MAX_BYTES)]
    assert a._nozzles["nozzles"] == [{"id": 0, "extruderId": 0, "diameterMm": 0.4}]
