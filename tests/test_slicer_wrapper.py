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


SPEC = {"mesh": "part.stl", "model": "A1 Mini", "filament": {"type": "PLA", "colorHex": "#FFFFFF", "trayInfoIdx": "GFA00"},
        "fixes": {"orient": True, "brim": False, "supports": False}, "scale": 1.0}
PATHS = {"machine": "/job/work/machine.json", "process": "/job/work/process.json", "filament": "/job/work/filament.json"}


def _spec(**fixes):
    return ps.read_spec({**SPEC, "fixes": {**SPEC["fixes"], **fixes}})


@pytest.mark.parametrize("brim, expected", [(True, {"brim_type": "outer_only", "brim_width": "5"}), (False, {"brim_type": "no_brim"})])
def test_the_brim_choice_is_written_both_ways(brim, expected):
    # operator, 2026-10-21 (board 72f424f2): an unticked brim still sliced a brim, because the Bambu presets leave brim_type
    # unset and OrcaSlicer's own default is auto_brim
    got = ps.shop_process(_spec(brim=brim))
    assert {k: got[k] for k in expected} == expected
    if not brim:
        assert "brim_width" not in got


@pytest.mark.parametrize("supports, expected", [(True, {"enable_support": "1", "support_type": "tree(auto)"}), (False, {"enable_support": "0"})])
def test_the_supports_choice_is_written_both_ways(supports, expected):
    got = ps.shop_process(_spec(supports=supports))
    assert {k: got[k] for k in expected} == expected


@pytest.mark.parametrize("orient, flag", [(True, "1"), (False, "0")])
def test_the_orientation_choice_is_on_the_command_line_both_ways(orient, flag):
    args = ps.orca_args(_spec(orient=orient), PATHS, "/job/out", "/job/part.stl")
    assert args[args.index("--orient") + 1] == flag


@pytest.mark.parametrize("scale, text", [(1.0, "1.0000"), (0.5, "0.5000"), (2.25, "2.2500")])
def test_the_scale_is_on_the_command_line_every_time_even_at_full_size(scale, text):
    args = ps.orca_args(ps.read_spec({**SPEC, "scale": scale}), PATHS, "/job/out", "/job/part.stl")
    assert args[args.index("--scale") + 1] == text
    assert args[-1] == "/job/part.stl"


@pytest.mark.parametrize("model", sorted(ps.MODELS))
def test_every_model_carries_the_shops_infill_pattern_explicitly(model):
    # operator, 2026-10-21: "the auto slicer defaults to gyroid which is overkill and takes longer"; the pattern is the
    # shop's choice per model, written into every slice, never left to the preset
    got = ps.shop_process(ps.read_spec({**SPEC, "model": model}))
    assert got["sparse_infill_pattern"] == ps.SHOP_PROCESS[model]["sparse_infill_pattern"] != "gyroid"
    assert got["sparse_infill_density"].endswith("%")


@pytest.mark.parametrize("fixes", [{"orient": True, "brim": False}, {"orient": True, "brim": "no", "supports": False}, {}])
def test_a_request_that_leaves_a_choice_out_is_refused(fixes):
    with pytest.raises(ps.SliceError, match="does not say whether to"):
        ps.read_spec({**SPEC, "fixes": fixes})
