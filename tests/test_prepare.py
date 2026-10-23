"""Synthetic geometry only. Run with the same pinned packages as the ps-slicer image."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")
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


def test_only_small_holes_are_filled(tmp_path):
    p = box([0, 0, 0], [0.5, 0.5, 2])
    top = np.where(p.face_normals[:, 2] > 0.9)[0]
    p.update_faces(np.setdiff1d(np.arange(len(p.faces)), top[:1]))
    r, out = run(tmp_path, [p], wall=0.2)
    assert not r["blocked"] and out.is_volume and out.volume == pytest.approx(0.5)
    assert next(c for c in r["changes"] if c["fix"] == "holes")["before"] == 1
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
