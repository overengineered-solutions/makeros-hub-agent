"""Synthetic geometry only. Uses the ps-slicer's pinned packages; checker regressions also run the vendored bundle with Node."""
import hashlib
import base64
import io
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")
from PIL import Image
spec = importlib.util.spec_from_file_location("ps_slice_prepare", Path(__file__).resolve().parents[1] / "slicer/ps_slice.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def box(lo, hi):
    return trimesh.creation.box(bounds=[lo, hi])


def run(tmp_path, parts, *, scale=1, wall=0.84, large=False, thicken=True):
    src, dst = tmp_path / "original.stl", tmp_path / "prepared.stl"
    trimesh.util.concatenate(parts).export(src)
    before = src.read_bytes()
    report = ps.prepare_model(str(src), str(dst), {"version": 1, "thicken": thicken, "moveLargeGaps": large}, wall, scale)
    assert src.read_bytes() == before
    assert report["inputSha256"] == hashlib.sha256(before).hexdigest()
    if report["blocked"]:
        assert not dst.exists()
        return report, None
    assert report["sha256"] == hashlib.sha256(dst.read_bytes()).hexdigest()
    return report, trimesh.load_mesh(dst)


def test_drop_preserves_whole_assembly_and_scale_once(tmp_path):
    r, out = run(tmp_path, [box([0, 0, 5], [2000, 2000, 2005])], scale=0.01)
    np.testing.assert_allclose(out.extents, [20, 20, 20])
    assert out.bounds[0, 2] == 0
    assert r["changes"][0]["before"] == pytest.approx(0.05)
    assert r["settings"]["scale"] == 0.01


def test_floating_piece_small_gap_lands_on_real_flat_surface(tmp_path):
    r, out = run(tmp_path, [box([0, 0, 0], [20, 20, 3]), box([5, 5, 4.5], [15, 15, 9.5])])
    assert not r["blocked"]
    assert out.bounds[1, 2] == 8
    assert next(c for c in r["changes"] if c["fix"] == "lower")["before"] == 1.5


@pytest.mark.parametrize("gap", [2, 4])
def test_large_gap_requires_choice(tmp_path, gap):
    parts = [box([0, 0, 0], [20, 20, 3]), box([5, 5, 3 + gap], [15, 15, 8 + gap])]
    r, _ = run(tmp_path, parts)
    assert any("Choose to move" in s for s in r["blocked"])
    r, out = run(tmp_path, parts, large=True)
    assert not r["blocked"] and out.bounds[1, 2] == 8
    assert next(c for c in r["changes"] if c["fix"] == "lower")["default"] is False


def test_roof_above_empty_space_does_not_land_on_a_bounding_box(tmp_path):
    r, _ = run(tmp_path, [box([0, 0, 0], [3, 20, 8]), box([17, 0, 0], [20, 20, 8]), box([5, 5, 9], [15, 15, 12])])
    assert any("no flat surface" in s for s in r["blocked"])


def test_overlap_union_adds_no_new_void_and_never_moves_vertices(tmp_path):
    r, out = run(tmp_path, [box([0, 0, 0], [10, 10, 10]), box([5, 0, 0], [15, 10, 10])])
    assert out.is_volume and out.volume == pytest.approx(1500)
    assert next(c for c in r["changes"] if c["fix"] == "merge")["after"] == 1


@pytest.mark.parametrize("inverted", [True, False])
def test_inside_out_and_mixed_faces(tmp_path, inverted):
    p = box([0, 0, 0], [10, 10, 10])
    if inverted:
        p.invert()
    else:
        p.faces[0] = p.faces[0][::-1]
    r, out = run(tmp_path, [p])
    assert not r["blocked"] and out.is_volume and out.volume == pytest.approx(1000)
    assert any(c["fix"] == "faces" for c in r["changes"])


def test_hollow_is_not_filled_or_called_a_flipped_face(tmp_path):
    inner = box([2, 2, 2], [8, 8, 8])
    inner.invert()
    r, out = run(tmp_path, [box([0, 0, 0], [10, 10, 10]), inner])
    assert out.volume == pytest.approx(784)
    assert not any(c["fix"] == "faces" for c in r["changes"])


@pytest.mark.parametrize("island", [False, True])
def test_globally_reversed_hollow_keeps_its_cavity(tmp_path, island):
    outer = box([0, 0, 0], [10, 10, 10])
    outer.invert()
    parts = [outer, box([2, 2, 2], [8, 8, 8])]
    if island:
        inner = box([4, 4, 4], [6, 6, 6])
        inner.invert()
        parts.append(inner)
    r, out = run(tmp_path, parts, large=True)
    volume = 792 if island else 784
    assert out.volume == pytest.approx(volume)
    assert r["before"]["volume"] == r["after"]["volume"] == volume
    assert not out.contains([[3, 3, 3]])[0]
    assert next(c for c in r["changes"] if c["fix"] == "faces")["before"] == (36 if island else 24)


def test_ambiguous_nested_winding_is_refused(tmp_path):
    with pytest.raises(ps.SliceError, match="could not repair"):
        run(tmp_path, [box([0, 0, 0], [10, 10, 10]), box([2, 2, 2], [8, 8, 8])])
    assert not (tmp_path / "prepared.stl").exists()
    touching = box([0, 2, 2], [8, 8, 8])
    touching.invert()
    with pytest.raises(ps.SliceError, match="could not repair"):
        run(tmp_path, [box([0, 0, 0], [10, 10, 10]), touching])


def image_colors(encoded):
    pixels = np.array(Image.open(io.BytesIO(base64.b64decode(encoded))))
    orange = np.sum(np.all(pixels == [245, 158, 11], axis=2) | np.all(pixels == [180, 83, 9], axis=2))
    grey = np.sum(np.all(pixels == [148, 163, 184], axis=2))
    return orange, grey


def test_one_flipped_face_marks_only_that_face_in_both_views(tmp_path):
    p = box([0, 0, 0], [10, 10, 10])
    p.faces[0] = p.faces[0][::-1]   # hidden in the default view
    r, _ = run(tmp_path, [p])
    c = next(c for c in r["changes"] if c["fix"] == "faces")
    assert c["before"] == 1
    assert c["beforeImage"] != c["afterImage"]
    for key in ["beforeImage", "afterImage"]:
        orange, grey = image_colors(c[key])
        assert orange > 1000 and grey > orange   # the other surfaces stay unmarked


def test_only_small_holes_are_filled(tmp_path):
    p = box([0, 0, 0], [0.5, 0.5, 2])
    top = np.where(p.face_normals[:, 2] > 0.9)[0]
    p.update_faces(np.setdiff1d(np.arange(len(p.faces)), top[:1]))
    r, out = run(tmp_path, [p], wall=0.2)
    assert not r["blocked"] and out.is_volume and out.volume == pytest.approx(0.5)
    assert next(c for c in r["changes"] if c["fix"] == "holes")["before"] == 1
    c = next(c for c in r["changes"] if c["fix"] == "holes")
    before_orange, before_grey = image_colors(c["beforeImage"])
    after_orange, after_grey = image_colors(c["afterImage"])
    assert 0 < before_orange < after_orange   # boundary before, added face after
    assert before_grey > after_orange and after_grey > after_orange
    p = box([0, 0, 0], [20, 20, 20])
    p.update_faces(np.arange(len(p.faces)) != 0)
    (tmp_path / "prepared.stl").unlink()
    r, _ = run(tmp_path, [p])
    assert "We could not repair this file safely. Export it again as a closed solid." in r["blocked"]


@pytest.mark.parametrize("exterior", [True, False])
def test_thicken_only_thin_walls_adds_material_and_keeps_ends(tmp_path, exterior):
    low, high = (0, 0.5) if exterior else (10, 10.5)
    base = box([0, 0, 0], [20, 20, 2])
    wall = box([low, 4, 2], [high, 16, 12])
    r, out = run(tmp_path, [base, wall])
    assert not r["blocked"]
    c = next(c for c in r["changes"] if c["fix"] == "walls")
    assert c["before"] == 0.5 and c["after"] == 0.84
    verts = out.vertices[out.vertices[:, 2] > 2.01]
    assert np.ptp(verts[:, 0]) == pytest.approx(0.84, abs=1e-5)
    assert (verts[:, 0].min() if exterior else verts[:, 0].mean()) == pytest.approx(0 if exterior else 10.25)
    assert verts[:, 1].min() == 4 and verts[:, 1].max() == 16
    assert out.volume > base.volume + wall.volume
    np.testing.assert_allclose(out.bounds, [[0, 0, 0], [20, 20, 12]])
    assert c["beforeImage"] != c["afterImage"]


def test_thick_enough_wall_unchanged_and_chosen_minimum_used(tmp_path):
    parts = [box([0, 0, 0], [20, 20, 2]), box([0, 4, 2], [1, 16, 12])]
    r, _ = run(tmp_path, parts)
    assert not any(c["fix"] == "walls" for c in r["changes"])
    r, _ = run(tmp_path, parts, wall=2.0)
    assert next(c for c in r["changes"] if c["fix"] == "walls")["after"] == 2


@pytest.mark.parametrize("other", [0.7, 8])
def test_room_window_and_mating_clearances_are_not_guessed(tmp_path, other):
    r, _ = run(tmp_path, [box([0, 0, 0], [20, 20, 2]), box([0, 4, 2], [0.5, 16, 12]), box([other, 4, 2], [other + 2, 16, 12])])
    assert any("window" in b or "room size" in b for b in r["blocked"])


def test_tiny_detail_and_zero_thickness_have_plan_wording(tmp_path):
    r, _ = run(tmp_path, [box([0, 0, 0], [20, 20, 2]), box([0, 4, 2], [0.1, 16, 12])])
    assert "These are too small to print at this scale. Leave them off, or print a bigger scale." in r["blocked"]
    p = trimesh.Trimesh(vertices=[[0, 0, 0], [10, 0, 0], [0, 10, 0]], faces=[[0, 1, 2]])
    r, _ = run(tmp_path, [p])
    assert "Your model has no thickness. Give the walls a thickness in your program." in r["blocked"]


def test_whole_shell_budget_and_bad_choices_fail_closed(tmp_path):
    with pytest.raises(ps.SliceError, match="too many separate pieces"):
        run(tmp_path, [box([i * 3, 0, 0], [i * 3 + 1, 1, 1]) for i in range(129)])
    raw = {"mesh": "x.stl", "model": "A1 Mini", "filament": {"type": "PLA"}, "fixes": {"orient": False, "brim": False, "supports": False}}
    for choice in [True, {}, {"version": 1, "thicken": True, "moveLargeGaps": "false"}, {"version": True, "thicken": True, "moveLargeGaps": False}]:
        with pytest.raises(ps.SliceError):
            ps.read_spec({**raw, "prepare": choice})


def test_rotated_planar_wall_keeps_its_mating_planes(tmp_path):
    base = box([0, 0, 0], [20, 20, 2])
    wall = box([0, 4, 2], [0.5, 16, 12])
    turn = trimesh.transformations.rotation_matrix(np.pi / 6, [0, 0, 1])
    for p in [base, wall]:
        p.apply_transform(turn)
    r, out = run(tmp_path, [base, wall])
    assert not r['blocked']
    out.apply_transform(np.linalg.inv(turn))
    top = out.vertices[out.vertices[:, 2] > 2.01]
    assert np.ptp(top[:, 0]) == pytest.approx(0.84, abs=1e-5)
    assert top[:, 1].min() == pytest.approx(4, abs=1e-5)
    assert top[:, 1].max() == pytest.approx(16, abs=1e-5)


def test_short_thin_wall_is_not_mistaken_for_a_floor(tmp_path):
    r, out = run(tmp_path, [box([0, 0, 0], [20, 20, 2]), box([0, 4, 2], [0.5, 16, 3])])
    assert out is not None
    assert next(c for c in r['changes'] if c['fix'] == 'walls')['after'] == 0.84


def test_connecting_end_face_is_protected(tmp_path):
    r, _ = run(tmp_path, [box([0, 0, 0], [20, 20, 2]), box([10, 4, 2], [10.5, 16, 12]), box([9, 16, 2], [12, 18, 12])])
    assert any('connecting face' in b for b in r['blocked'])


def test_thin_floor_is_refused_even_when_its_sides_are_wide(tmp_path):
    r, out = run(tmp_path, [box([0, 0, 0], [20, 20, 0.2])])
    assert out is None
    assert any('floor is too thin' in b for b in r['blocked'])


def checker(work, data):
    work.mkdir(parents=True)
    (work / "model.stl").write_bytes(data)
    (work / "job.json").write_text(json.dumps({"name": "model.stl", "units": "millimeter", "scale": 1,
        "models": ["A1 Mini"], "minWall": 0.84, "picture": False, "stl": False}))
    subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "checker/check.mjs")],
                   env={**os.environ, "PS_JOB_DIR": str(work)}, check=True, capture_output=True)
    return json.loads((work / "out/report.json").read_text())


@pytest.mark.parametrize("beside_solid", [False, True])
def test_failed_circular_floor_cannot_be_cleared_by_preparation(tmp_path, beside_solid):
    floor = trimesh.creation.cylinder(radius=10, height=0.1, sections=64)
    floor.apply_translation([0, 0, 0.05])
    parts = [floor] + ([box([30, 0, 0], [40, 10, 10])] if beside_solid else [])
    before = checker(tmp_path / "check", trimesh.util.concatenate(parts).export(file_type="stl"))
    assert any(f["id"] == "too-thin" and f["level"] == "fail" for f in before["report"]["findings"])
    r, out = run(tmp_path, parts)
    assert out is None and any("floor is too thin" in b for b in r["blocked"])


def test_failed_wall_is_cleared_only_after_thickening_passes_the_same_checker(tmp_path):
    parts = [box([0, 0, 0], [20, 20, 2]), box([0, 4, 2], [0.3, 16, 12])]
    before = checker(tmp_path / "before-check", trimesh.util.concatenate(parts).export(file_type="stl"))
    assert any(f["id"] == "too-thin" and f["level"] == "fail" for f in before["report"]["findings"])
    report, out = run(tmp_path, parts)
    assert out is not None and any(c["fix"] == "walls" for c in report["changes"])
    after = checker(tmp_path / "after-check", (tmp_path / "prepared.stl").read_bytes())
    assert not any(f["level"] == "fail" for f in after["report"]["findings"])


def test_cone_bed_contact_is_advice_with_or_without_orientation(tmp_path, monkeypatch):
    from makeros_hub import checks
    cone = trimesh.creation.cone(radius=10, height=20, sections=64)
    cone.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    cone.apply_translation([0, 0, 20])
    before = checker(tmp_path / "before-check", cone.export(file_type="stl"))
    assert any(f["id"] == "no-first-layer" and f["level"] == "fail" for f in before["report"]["findings"])
    report, out = run(tmp_path, [cone])
    assert out is not None
    spool = tmp_path / "spool"
    def run_check(uid):
        work = spool / uid / ".check"
        subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "checker/check.mjs")],
                       env={**os.environ, "PS_JOB_DIR": str(work)}, check=True, capture_output=True)
        return 0
    monkeypatch.setattr(checks, "run_check_job", run_check)
    for orient in [False, True]:
        blocked = checks.check_prepared_model(spool, tmp_path / "prepared.stl", report,
            {"model": "A1 Mini", "fixes": {"orient": orient}})
        assert not blocked
        assert report["checked"] is True
    assert not list(spool.iterdir())
