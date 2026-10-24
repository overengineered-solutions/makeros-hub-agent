"""Synthetic closed solids; no member files. Run with the ps-slicer's pinned geometry packages."""
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "slicer"))
spec = importlib.util.spec_from_file_location("ps_slice_split", Path(__file__).resolve().parents[1] / "slicer/ps_slice.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
cut_model, UnsafeCut = ps.cut_model, ps.SliceError


def request(mode="fit", clearance=0.2, layers=None):
    return dict(mode=mode, clearance=clearance, bed=[177.5, 177.5, 180], layers=layers or [])


def box(lo, hi):
    return trimesh.creation.box(bounds=[lo, hi])


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_planes_minimum_piece_count_bed_and_closed_cut_faces(axis):
    size = np.array([20., 20., 20.])
    size[axis] = 300
    parts, _, _, joints = cut_model(box([0, 0, 0], size), request(), .84)
    assert len(parts) == 3  # two body pieces plus their locating peg
    assert len(joints) == 1 and joints[0]["axis"] == axis
    assert joints[0]["plane"] == pytest.approx(150)
    for p in parts:
        assert p.is_volume and np.all(p.extents <= [177.5, 177.5, 180])
        assert p.bounds[0, 2] == pytest.approx(0)
        assert p.area_faces[(p.triangles[:, :, 2] < 1e-5).all(axis=1)].sum() > 1


@pytest.mark.parametrize("gap", [.05, .2, .5])
def test_keyed_joint_clearance_at_every_side_and_end(gap):
    from manifold3d import Manifold, Mesh
    pieces, _, _, joints = cut_model(box([0, 0, 0], [300, 20, 20]), request(clearance=gap), .84)
    np.testing.assert_allclose(np.array(joints[0]["socketSize"]) - joints[0]["pegSize"], gap * 2)
    for part in pieces[:2]:
        actual = Manifold(Mesh(np.asarray(part.vertices, dtype=np.float32), np.asarray(part.faces, dtype=np.uint32)))
        socket = Manifold.cube(part.extents) - actual
        bounds = np.array(socket.bounding_box()).reshape(2, 3)
        np.testing.assert_allclose(sorted(bounds[1] - bounds[0]), sorted([3 + 2 * gap, 5 + 2 * gap, 2 + gap]), atol=1e-4)
    np.testing.assert_allclose(sorted(pieces[2].extents), [3, 4, 5], atol=1e-4)


def test_grid_when_no_single_axis_can_fit():
    pieces, _, _, joints = cut_model(box([0, 0, 0], [300, 300, 20]), request(), .84)
    assert len(pieces) == 8 and len(joints) == 4
    assert {j["axis"] for j in joints} == {0, 1}
    assert all(np.all(p.extents <= [177.5, 177.5, 180]) for p in pieces)


def test_floor_roof_layers_preserve_heights_and_scale_once(tmp_path):
    src = tmp_path / "building.stl"
    original = trimesh.util.concatenate([box([0, 0, 0], [3000, 3000, 1000]), box([0, 0, 1000], [3000, 3000, 1800]), box([0, 0, 1800], [3000, 3000, 2400])])
    original.export(src)
    before = src.read_bytes()
    layers = [dict(name=n, first=i * 12, count=12) for i, n in enumerate(["Floor 1", "Level 2", "Roof"])]
    r = ps.prepare_model(str(src), str(tmp_path / "prepared.stl"), dict(version=1, thicken=False, moveLargeGaps=False, split=request("floors", layers=layers)), .84, .01)
    assert [j["plane"] for j in r["joints"]] == [10, 18]
    assert [p["name"] for p in r["pieces"][:3]] == ["Floor 1", "Level 2", "Roof"]
    assert len(r["pieces"]) == 5 and r["changes"][0]["fix"] == "split"
    assert src.read_bytes() == before and r["inputSha256"] == hashlib.sha256(before).hexdigest()
    for p in r["pieces"]:
        assert p["sha256"] == hashlib.sha256((tmp_path / f"piece-{p['index']}.stl").read_bytes()).hexdigest()
    r2 = ps.prepare_model(str(src), str(tmp_path / "prepared.stl"), r["settings"] | {}, .84, .01)
    assert r2["sha256"] == r["sha256"]


def test_refuses_thin_cut_joint_disconnected_open_and_excessive_models():
    open_mesh = box([0, 0, 0], [300, 20, 20])
    open_mesh.update_faces(np.arange(len(open_mesh.faces) - 1))
    models = [open_mesh, box([0, 0, 0], [300, 1, 20]), box([0, 0, 0], [5000, 5000, 5000]),
              trimesh.util.concatenate([box([0, 0, 0], [20, 20, 20]), box([300, 0, 0], [320, 20, 20])])]
    for m in models:
        with pytest.raises(UnsafeCut):
            cut_model(m, request(), .84)


@pytest.mark.parametrize("name,first,z", [("Walls", 12, 10), ("Roof", 11, 10), ("Roof", 12, 9), ("Roof", 12, 12)])
def test_refuses_ambiguous_missing_or_overlapping_layers(name, first, z):
    m = trimesh.util.concatenate([box([0, 0, 0], [30, 30, 10]), box([0, 0, z], [30, 30, z + 10])])
    with pytest.raises(UnsafeCut):
        cut_model(m, request("floors", layers=[dict(name="Floor 1", first=0, count=12), dict(name=name, first=first, count=12)]), .84)


def test_wire_rejects_unbounded_and_invalid_choices():
    raw = dict(mesh="synthetic.stl", model="A1 Mini", filament=dict(type="PLA"), fixes=dict(orient=False, brim=False, supports=False))
    for split in [request(clearance=0), request(clearance=float("nan")), request() | {"bed": [1, 2, 3]}, request() | {"mode": "guess"}]:
        with pytest.raises(ps.SliceError):
            ps.read_spec(raw | {"prepare": dict(version=1, thicken=False, moveLargeGaps=False, split=split)})
    good = raw | {"prepare": dict(version=1, thicken=False, moveLargeGaps=False, split=request())}
    assert ps.read_spec(good)["prepare"]["split"]["clearance"] == .2
    with pytest.raises(ps.SliceError):
        ps.read_spec(good | {"piece": dict(index=-1, sha256="a" * 64)})


def test_cutting_far_from_cad_origin_keeps_the_same_joint_fit():
    origin = box([0, 0, 0], [300, 20, 20])
    shifted = origin.copy()
    shifted.apply_translation([100000, -50000, 200000])
    a, _, _, joints_a = cut_model(origin, request(), .84)
    b, _, _, joints_b = cut_model(shifted, request(), .84)
    assert joints_a == joints_b
    for left, right in zip(a, b):
        assert left.export(file_type="stl") == right.export(file_type="stl")


def test_floor_preview_keeps_the_tab_visible_in_the_gap():
    original = trimesh.util.concatenate([box([0, 0, 0], [30, 30, 10]), box([0, 0, 10], [30, 30, 20])])
    _, exploded, _, _ = cut_model(original, request("floors", layers=[dict(name="Floor 1", first=0, count=12), dict(name="Roof", first=12, count=12)]), .84)
    lower, upper, tab = exploded
    assert tab.bounds[0, 2] > lower.bounds[1, 2]
    assert tab.bounds[1, 2] < upper.bounds[0, 2]
