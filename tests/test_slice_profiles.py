import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location("ps_slice", pathlib.Path(__file__).resolve().parents[1] / "slicer" / "ps_slice.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

X1C = "Bambu Lab X1 Carbon 0.4 nozzle"
A1M = "Bambu Lab A1 mini 0.4 nozzle"


def _index():
    # the shapes OrcaSlicer 2.4.2 ships: the X1C's presets carry no "@BBL X1C" suffix, the A1 mini's do; ids sit on @base
    return {
        "Generic PLA @base": {"filament_id": "GFL99"},
        "Generic PLA": {"inherits": "Generic PLA @base", "instantiation": "true", "compatible_printers": [X1C, "Bambu Lab P1S 0.4 nozzle"]},
        "Generic PLA @BBL A1M": {"inherits": "Generic PLA @base", "instantiation": "true", "compatible_printers": [A1M]},
        "Bambu PLA Basic @base": {"filament_id": "GFA00"},
        "Bambu PLA Basic @BBL X1C": {"inherits": "Bambu PLA Basic @base", "instantiation": "true", "compatible_printers": [X1C]},
        "Bambu PLA Basic @BBL A1M": {"inherits": "Bambu PLA Basic @base", "instantiation": "true", "compatible_printers": [A1M]},
        "Bambu PLA Matte @base": {"filament_id": "GFA01"},
        "Bambu PLA Matte @BBL A1M": {"inherits": "Bambu PLA Matte @base", "instantiation": "true", "compatible_printers": [A1M]},
    }


def test_a_generic_spool_on_the_x1c_gets_the_generic_profile_not_bambu_basic():
    # 2026-10-19 coverage check: the name-only search sliced the X1C's generic PLA with Bambu PLA Basic's settings
    assert ps.pick_filament(_index(), X1C, "X1C", "PLA", "GFL99") == "Generic PLA"
    assert ps.pick_filament(_index(), X1C, "X1C", "PLA", "GFA00") == "Bambu PLA Basic @BBL X1C"
    assert ps.pick_filament(_index(), A1M, "A1M", "PLA", "GFL99") == "Generic PLA @BBL A1M"


def test_a_known_spool_gets_its_profile_an_unknown_plain_one_goes_generic_a_variant_is_refused():
    # operator, 2026-10-19: exact profiles when we know what it is; "if we dont know what it is but it is stated as pla then
    # its okay to go generic"
    assert ps.pick_filament(_index(), A1M, "A1M", "PLA", "GFA01", "PLA") == "Bambu PLA Matte @BBL A1M"
    assert ps.pick_filament(_index(), X1C, "X1C", "PLA", "GFA01", "PLA") == "Generic PLA"   # no Matte profile for this printer here
    with pytest.raises(ps.SliceError, match="no slicer profile for this PLA-CF spool"):
        ps.pick_filament(_index(), X1C, "X1C", "PLA", "GFA50", "PLA-CF")
    # the middle ground: a variant's own generic profile when the exact one is missing, never the plain material's
    idx = {**_index(), "Generic PLA-CF @BBL X1C": {"inherits": "Generic PLA @base", "instantiation": "true", "compatible_printers": [X1C], "filament_id": "GFL98"}}
    assert ps.pick_filament(idx, X1C, "X1C", "PLA", "GFA50", "PLA-CF") == "Generic PLA-CF @BBL X1C"


def test_a_spool_with_no_id_gets_the_generic_profile_of_its_type():
    assert ps.pick_filament(_index(), A1M, "A1M", "PLA", None) == "Generic PLA @BBL A1M"


def test_pla_and_petg_run_the_aux_fan_at_most_10_percent_and_other_materials_keep_their_own():
    # v0.88.0, operator 2026-10-28: "if we are printing with pla or petg the aux fan needs to be popped down to no more than 10 percent"
    assert ps.cap_aux_fan({"additional_cooling_fan_speed": ["70"]}, "PLA")["additional_cooling_fan_speed"] == ["10"]
    assert ps.cap_aux_fan({"additional_cooling_fan_speed": ["5"]}, "petg")["additional_cooling_fan_speed"] == ["5"]   # a cap, never a raise
    assert ps.cap_aux_fan({}, "PLA")["additional_cooling_fan_speed"] == ["10"]
    assert ps.cap_aux_fan({"additional_cooling_fan_speed": ["80"]}, "ABS")["additional_cooling_fan_speed"] == ["80"]
    assert ps.cap_aux_fan({"first_x_layer_fan_speed": ["40"]}, "PLA")["first_x_layer_fan_speed"] == ["10"]
    assert ps.cap_aux_fan({"first_x_layer_fan_speed": ["0"]}, "PETG")["first_x_layer_fan_speed"] == ["0"]
    assert "first_x_layer_fan_speed" not in ps.cap_aux_fan({}, "PLA")
    assert ps.cap_aux_fan({"first_x_layer_fan_speed": ["40"]}, "ABS")["first_x_layer_fan_speed"] == ["40"]
