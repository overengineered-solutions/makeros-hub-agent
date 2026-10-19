"""slicer/ps_slice.py, the wrapper inside the ps-slicer container: its pure parts, without the slicer binary."""

import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ps_slice", Path(__file__).resolve().parents[1] / "slicer" / "ps_slice.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def test_selftest():
    ps.selftest()


def test_picks_the_loaded_spools_own_preset_then_the_generic_one():
    m = "Bambu Lab A1 mini 0.4 nozzle"
    idx = {
        "fdm_filament_pla": {"name": "fdm_filament_pla", "filament_type": ["PLA"]},
        "Bambu PLA Basic @base": {"name": "Bambu PLA Basic @base", "inherits": "fdm_filament_pla", "filament_id": "GFA00"},
        "Bambu PLA Basic @BBL A1M": {"name": "Bambu PLA Basic @BBL A1M", "inherits": "Bambu PLA Basic @base", "instantiation": "true", "compatible_printers": [m]},
        "Generic PLA @BBL A1M": {"name": "Generic PLA @BBL A1M", "instantiation": "true", "filament_id": "GFL99", "compatible_printers": [m]},
        "Generic PLA @BBL A1M 0.2 nozzle": {"name": "Generic PLA @BBL A1M 0.2 nozzle", "instantiation": "true", "filament_id": "GFL99", "compatible_printers": [m]},
        "Generic PLA @BBL P2S": {"name": "Generic PLA @BBL P2S", "instantiation": "true", "filament_id": "GFL99", "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"]},
    }
    assert ps.pick_filament(idx, m, "A1M", "PLA", "GFA00") == "Bambu PLA Basic @BBL A1M"
    # 2026-10-19 (operator): an id the slicer does not know, stated as plain PLA, prints as generic PLA
    assert ps.pick_filament(idx, m, "A1M", "PLA", "GFZZ9") == "Generic PLA @BBL A1M"
    assert ps.pick_filament(idx, m, "A1M", "PLA", "GFZZ9", "PLA") == "Generic PLA @BBL A1M"
    assert ps.pick_filament(idx, m, "A1M", "PLA", None) == "Generic PLA @BBL A1M"
    with pytest.raises(ps.SliceError):
        ps.pick_filament(idx, m, "A1M", "TPU", None)


def test_writes_the_model_code_and_summarizes_the_plate(tmp_path):
    info = '<config><plate><metadata key="printer_model_id" value=""/><metadata key="prediction" value="1174"/>' \
           '<metadata key="support_used" value="false"/><filament id="1" used_g="3.68"/></plate></config>'
    f = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/slice_info.config", info)
        z.writestr("Metadata/plate_1.gcode", "; g")
    ps.set_model_id(str(f), "N1")
    assert ps.summarize(str(f)) == {"grams": 3.68, "seconds": 1174, "supportUsed": False, "modelId": "N1", "plates": 1}
    g = tmp_path / "nomodel.3mf"
    with zipfile.ZipFile(g, "w") as z:
        z.writestr("Metadata/slice_info.config", "<config/>")
    with pytest.raises(ps.SliceError):
        ps.set_model_id(str(g), "N1")
