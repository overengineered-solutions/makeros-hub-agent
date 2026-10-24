"""slicer/ps_slice.py, the wrapper inside the ps-slicer container: its pure parts, without the slicer binary."""

import importlib.util
import hashlib
import io
import json
import struct
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


@pytest.mark.parametrize("scale", [1.0, 0.5, 1 / 150, 5.0])
def test_orcaslicer_is_never_given_a_scale(scale):
    # 2026-10-22, ps-slicer:2.4.2-1: OrcaSlicer's --scale crashed (exit 139) on every factor but 1; the wrapper sizes the mesh
    args = ps.orca_args(ps.read_spec({**SPEC, "scale": scale}), PATHS, "/job/out", "/job/work/part.stl")
    assert "--scale" not in args
    assert args[-1] == "/job/work/part.stl"


def _stl(*tris) -> bytes:
    out = bytearray(80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, -1) + struct.pack("<9f", *t) + b"\0\0"
    return bytes(out)


def test_the_wrapper_sizes_the_binary_stl_once_and_leaves_the_hubs_copy(tmp_path):
    src, dst = tmp_path / "part.stl", tmp_path / "work.stl"
    src.write_bytes(_stl((0, 0, 0, 20000, 0, 0, 0, 10000, 0)))
    ps.scale_stl(str(src), str(dst), 1 / 200)
    got = dst.read_bytes()
    assert struct.unpack_from("<3f", got, 84) == (0, 0, -1)   # the normal, unchanged
    assert struct.unpack_from("<9f", got, 96) == pytest.approx((0, 0, 0, 100, 0, 0, 0, 50, 0))
    assert src.read_bytes() == _stl((0, 0, 0, 20000, 0, 0, 0, 10000, 0))
    ps.scale_stl(str(src), str(dst), 1.0)
    assert dst.read_bytes() == src.read_bytes()
    text = tmp_path / "text.stl"
    text.write_bytes(b"solid x\nendsolid x\n")
    with pytest.raises(ps.SliceError, match="binary STL"):
        ps.scale_stl(str(text), str(dst), 2.0)


# Send to PS step 2 (operator, 2026-10-22: students "shrink them down to scale without losing walls to thinness"): the site
# sends one factor, the file's units and the member's scale together, so a building at 1:200 is 0.005 and one drawn in metres
# at 1:20 is 50. The old 0.05 to 20 refused both.
@pytest.mark.parametrize("scale", [0.001, 0.005, 1 / 150, 0.05, 1, 20, 50, 1000])
def test_the_scale_takes_what_the_site_sends(scale):
    assert ps.read_spec({**SPEC, "scale": scale})["scale"] == scale


@pytest.mark.parametrize("scale", [0, -1, 0.0009, 1000.5, float("inf"), float("nan"), "0.005", None, True])
def test_a_scale_outside_the_range_or_not_a_number_is_refused(scale):
    with pytest.raises(ps.SliceError, match="size change"):
        ps.read_spec({**SPEC, "scale": scale})


@pytest.mark.parametrize("model", sorted(ps.MODELS))
def test_every_model_carries_the_shops_infill_pattern_explicitly(model):
    # operator, 2026-10-21: "the auto slicer defaults to gyroid which is overkill and takes longer"; the pattern is the
    # shop's choice per model, written into every slice, never left to the preset. 2026-10-22: "I like rectilinear" (board
    # 6e8c967f: cubic crosses itself in every layer)
    got = ps.shop_process(ps.read_spec({**SPEC, "model": model, "nozzle": "left"}))
    assert got["sparse_infill_pattern"] == ps.SHOP_PROCESS[model]["sparse_infill_pattern"] == "rectilinear"
    assert got["sparse_infill_density"].endswith("%")


# operator, 2026-10-22: "A1 minis PLA only"; the site offers them nothing else, and the wrapper refuses it too
@pytest.mark.parametrize("model, ftype, ok", [("A1 Mini", "PLA", True), ("A1 Mini", "PETG", False), ("A1 Mini", "TPU", False), ("P2S", "PETG", True)])
def test_the_a1_minis_take_pla_only(model, ftype, ok):
    raw = {**SPEC, "model": model, "filament": {**SPEC["filament"], "type": ftype}}
    if ok:
        assert ps.read_spec(raw)["type"] == ftype
    else:
        with pytest.raises(ps.SliceError, match="prints PLA only"):
            ps.read_spec(raw)


# board 459044ef: the H2D's two nozzles. The spec names the one whose AMS holds the member's spool; the slice maps the member's one
# filament to it (OrcaSlicer's manual filament map, a logical extruder from 1), turned into a logical extruder by the preset's own
# physical_extruder_map, so the plate's group_id is what hub 0.77's dispatch reads (tests/test_real_slice.py slices it for real)
def test_a_two_nozzle_printer_is_told_the_nozzle_and_the_map_follows_the_preset():
    with pytest.raises(ps.SliceError, match="nozzles holds the spool"):
        ps.read_spec({**SPEC, "model": "H2D"})
    with pytest.raises(ps.SliceError, match="nozzles holds the spool"):
        ps.read_spec({**SPEC, "model": "H2D", "nozzle": 1})
    assert ps.read_spec({**SPEC, "model": "H2D", "nozzle": "right"})["nozzle"] == "right"
    assert ps.read_spec({**SPEC, "nozzle": "right"})["nozzle"] is None   # a one-nozzle printer ignores it
    h2d = {"physical_extruder_map": ["1", "0"]}   # Bambu Lab H2D 0.4 nozzle, OrcaSlicer 2.4.2
    assert ps.nozzle_map(h2d, "left") == {"filament_map_mode": "Manual", "filament_map": ["1"]}
    assert ps.nozzle_map(h2d, "right") == {"filament_map_mode": "Manual", "filament_map": ["2"]}
    assert ps.nozzle_map({"physical_extruder_map": ["0", "1"]}, "left")["filament_map"] == ["2"]
    for bad in ({}, {"physical_extruder_map": ["0"]}, {"physical_extruder_map": ["x", "y"]}):
        with pytest.raises(ps.SliceError, match="which nozzle is which"):
            ps.nozzle_map(bad, "left")


# board 72f424f2, "show the member what the slice actually used": slice_info's support_used is OrcaSlicer's enable_support, so a
# ticked box over a part that needed none said "with temporary supports"; the G-code says what was laid
@pytest.mark.parametrize("gcode, made", [("; FEATURE: Outer wall\nG1 X1 E1\n", False), ("; FEATURE: Support\nG1 X1 E1\n", True),
                                         ("; FEATURE: Support interface\nG1 X1 E1\n", True)])
def test_supports_are_reported_as_the_gcode_lays_them(tmp_path, gcode, made):
    f = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="support_used" value="true"/><filament id="1" used_g="2"/></plate></config>')
        z.writestr("Metadata/plate_1.gcode", gcode)
    assert ps.summarize(str(f))["supportUsed"] is made


@pytest.mark.parametrize("fixes", [{"orient": True, "brim": False}, {"orient": True, "brim": "no", "supports": False}, {}])
def test_a_request_that_leaves_a_choice_out_is_refused(fixes):
    with pytest.raises(ps.SliceError, match="does not say whether to"):
        ps.read_spec({**SPEC, "fixes": fixes})


def _profiles(tmp_path, widths):
    """A preset tree with each model's machine and process; widths: model -> outer_wall_line_width (absent leaves it out)."""
    for kind in ("machine", "process", "filament"):
        (tmp_path / kind).mkdir(parents=True)
    for model, (machine, sfx) in ps.MODELS.items():
        (tmp_path / "machine" / f"{sfx}.json").write_text(json.dumps({"name": machine, "nozzle_diameter": ["0.4"]}))
        process = {"name": ps.PROCESS.format(sfx=sfx), "wall_loops": "2"}
        if widths.get(model) is not None:
            process["outer_wall_line_width"] = widths[model]
        (tmp_path / "process" / f"{sfx}.json").write_text(json.dumps(process))
    return str(tmp_path)


# Send to PS step 2 (review): the hub asks the image itself what it slices; the answer carries the scale range and, per model,
# the outer wall line width of the process it slices with (the site's minimum wall is two of those lines)
def test_a_probe_is_answered_with_what_this_image_slices_and_slices_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES", _profiles(tmp_path / "p", {"A1 Mini": "0.42", "P2S": "105%"}))
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.json").write_text(json.dumps({"probe": True}))
    assert ps.run(str(job)) == {"ok": True, "probe": True, "scaleMin": 0.001, "scaleMax": 1000.0, "lineWidth": {"A1 Mini": 0.42, "P2S": 0.42}, "prepareVersion": 1, "splitVersion": 1, "colours": True}
    assert not (job / "work").exists()


def test_a_refusal_still_says_what_this_image_takes(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "PROFILES", _profiles(tmp_path / "p", {}))
    monkeypatch.setattr(ps, "JOB", str(tmp_path))
    (tmp_path / "job.json").write_text(json.dumps({**SPEC, "scale": 0.0001}))
    assert ps.main(["ps_slice.py"]) == 3
    result = json.loads((tmp_path / "out" / "result.json").read_text())
    assert result["ok"] is False and "size change" in result["error"] and (result["scaleMin"], result["scaleMax"]) == (0.001, 1000.0)


def test_the_settings_reported_are_the_ones_this_slice_used():
    process = {"layer_height": "0.2", "wall_loops": "2", "sparse_infill_pattern": "crosshatch", "sparse_infill_density": "15%", "brim_type": "auto_brim"}
    process.update(ps.shop_process(ps.read_spec(SPEC)))
    assert ps.effective_settings(process) == {"layer_height": "0.2", "wall_loops": "2", "sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%"}


def test_slow_group_resumes_within_each_runner_deadline_and_verifies_saved_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    machine, suffix = ps.MODELS["A1 Mini"]
    profiles = {"machine": {machine: {"name": machine, "printer_model": "mini"}, "mini": {"model_id": "N1"}},
                "process": {ps.PROCESS.format(sfx=suffix): {"name": ps.PROCESS.format(sfx=suffix), "outer_wall_line_width": "0.42"}},
                "filament": {"Synthetic PLA": {"name": "Synthetic PLA"}}}
    monkeypatch.setattr(ps, "load_index", lambda kind: profiles[kind])
    monkeypatch.setattr(ps, "pick_filament", lambda *args: "Synthetic PLA")
    monkeypatch.setattr(ps, "caps", lambda *args: {"splitVersion": 1})
    monkeypatch.setattr(ps, "JOB", str(tmp_path))
    prepared, elapsed = [], []
    def prepare(src, dst, *args):
        prepared.append(src)
        pieces = []
        for i in range(2):
            data = f"synthetic piece {i}".encode()
            (Path(dst).parent / f"piece-{i}.stl").write_bytes(data)
            pieces.append(dict(index=i, sha256=hashlib.sha256(data).hexdigest()))
        return dict(blocked=[], pieces=pieces)
    monkeypatch.setattr(ps, "prepare_model", prepare)
    def slow_orca(args, **kwargs):
        # Simulated clock: two individually valid 310 s slices exceed the real runner's 600 s aggregate limit.
        assert kwargs["timeout"] == 540
        elapsed.append(310)
        out = Path(args[args.index("--outputdir") + 1])
        with zipfile.ZipFile(out / "sliced.3mf", "w") as z:
            z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="printer_model_id" value="N1"/><filament id="1" used_g="4"/></plate></config>')
            z.writestr("Metadata/plate_1.gcode", "; synthetic")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(ps.subprocess, "run", slow_orca)
    raw = SPEC | {"prepare": dict(version=1, thicken=False, moveLargeGaps=False,
                                  split=dict(mode="fit", bed=[177.5, 177.5, 180], clearance=.2, layers=[]))}
    (tmp_path / "part.stl").write_bytes(b"synthetic original")
    (tmp_path / "job.json").write_text(json.dumps(raw))
    for n in range(2):
        before = sum(elapsed)
        assert ps.main(["ps_slice.py"]) == 0
        assert sum(elapsed) - before == 310 < 600
        result = json.loads((tmp_path / "out/result.json").read_text())
        assert result.get("pending", False) == (n == 0)
    assert sum(elapsed) == 620 and len(prepared) == 1
    assert len(result["pieces"]) == 2 and result["ok"]
    # An unchanged retry does no work; a corrupt cached plate is sliced again, never trusted.
    assert ps.run(str(tmp_path))["ok"] and len(elapsed) == 2
    (tmp_path / "out/piece-1/sliced.3mf").write_bytes(b"corrupted")
    assert ps.run(str(tmp_path))["ok"] and len(elapsed) == 3
    (tmp_path / "work/piece-0.stl").write_bytes(b"corrupted")
    assert ps.run(str(tmp_path))["pending"] and len(prepared) == 2
    checkpoint = tmp_path / "out/pieces.json"
    saved = json.loads(checkpoint.read_text())
    checkpoint.write_text(json.dumps(saved | {"pieces": "unreadable"}))
    assert ps.run(str(tmp_path))["pending"] and len(prepared) == 3
    (tmp_path / "job.json").write_text(json.dumps(raw | {"scale": 1.5}))
    assert ps.run(str(tmp_path))["pending"] and len(prepared) == 4
