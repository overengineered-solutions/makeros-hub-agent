"""A synthetic token and letters; never a member's model."""
import importlib.util
from pathlib import Path
import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")
spec = importlib.util.spec_from_file_location("join_slice", Path(__file__).parents[1] / "slicer/ps_slice.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def box(lo, hi):
    return trimesh.creation.box(bounds=[lo, hi])


def prepare(tmp_path, parts):
    src, dst = tmp_path / "model.stl", tmp_path / "joined.stl"
    trimesh.util.concatenate(parts).export(src)
    original = src.read_bytes()
    report = ps.prepare_model(str(src), str(dst), {"version": 1, "thicken": False, "moveLargeGaps": False, "selected": ["join"]}, 0.84, 1)
    assert src.read_bytes() == original
    assert not report["blocked"]
    assert all(c["fix"] == "join" and c["default"] is False for c in report["changes"])
    for c in report["changes"]:
        assert c["beforeImage"] and c["afterImage"]
    return trimesh.load_mesh(dst), report


@pytest.mark.parametrize("sink", [0.2, 0])
def test_token_with_raised_letter_sunk_or_surface_touching(tmp_path, sink):
    coin = trimesh.creation.cylinder(radius=12, height=2.5, sections=64)
    coin.apply_translation([0, 0, 1.25])
    # A raised T, including its overlap, resting on the 2.5 mm solid token.
    stem = box([-1, -4, 2.5 - sink], [1, 4, 4])
    top = box([-4, 2, 2.5 - sink], [4, 4, 4])
    out, _ = prepare(tmp_path, [coin, stem, top])
    assert out.is_volume and len(out.split()) == 1
    np.testing.assert_allclose(out.bounds, [[-12, -12, 0], [12, 12, 4]], atol=1e-5)
    assert out.volume > coin.volume


def test_token_letter_touching_only_along_a_line(tmp_path):
    coin = trimesh.creation.cylinder(radius=12, height=2.5, sections=64)
    coin.apply_translation([0, 0, 1.25])
    # A triangular raised stroke meets one edge of the token's top, exactly.
    edge = coin.vertices[np.isclose(coin.vertices[:, 2], 2.5)]
    edge = sorted(edge, key=lambda v: np.arctan2(v[1], v[0]))
    a, b = edge[1], edge[2]
    normal = (a + b) / 2; normal[2] = 0; normal /= np.linalg.norm(normal)
    letter = trimesh.convex.convex_hull(np.array([a, b, a + normal * 2 + [0, 0, 2], b + normal * 2 + [0, 0, 2], a + [0, 0, 2], b + [0, 0, 2]]))
    out, report = prepare(tmp_path, [coin, letter])
    assert out.is_volume and len(out.split()) == 1
    assert "less than 0.05 mm" in report["changes"][0]["words"]
    assert out.volume >= coin.volume + letter.volume - 1e-4


def test_blocks_edge_to_edge_weld_without_losing_material(tmp_path):
    out, report = prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), box([10, 10, 0], [20, 20, 10])])
    assert out.is_volume and len(out.split()) == 1
    assert 2000 <= out.volume < 2001
    assert np.all(out.bounds >= np.array([[0, 0, -0.05], [20, 20, 10]]))
    assert report["before"]["size"] == [20, 20, 10]


def test_hidden_inside_face_removed_but_open_external_face_refused(tmp_path):
    face = trimesh.Trimesh(vertices=[[2, 2, 2], [3, 2, 2], [2, 3, 2]], faces=[[0, 1, 2]])
    out, _ = prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), face])
    assert out.volume == pytest.approx(1000)
    face.apply_translation([20, 0, 0])
    with pytest.raises(ps.SliceError, match="original will be sliced"):
        prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), face])


def test_nearby_separate_parts_not_welded(tmp_path):
    out, _ = prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), box([10.01, 10, 0], [20, 20, 10])])
    assert len(out.split()) == 2


def test_cavity_never_filled(tmp_path):
    inner = box([2, 2, 2], [8, 8, 8]); inner.invert()
    with pytest.raises(ps.SliceError, match="inside space"):
        prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), inner])


@pytest.mark.parametrize("start,volume", [(5, 1500), (10, 2000)])
def test_blocks_overlap_or_share_an_entire_face(tmp_path, start, volume):
    out, report = prepare(tmp_path, [box([0, 0, 0], [10, 10, 10]), box([start, 0, 0], [start + 10, 10, 10])])
    assert out.is_volume and len(out.split()) == 1
    assert out.volume == pytest.approx(volume)
    assert len(report["changes"]) == 1
    np.testing.assert_allclose(out.bounds, [[0, 0, 0], [start + 10, 10, 10]])
