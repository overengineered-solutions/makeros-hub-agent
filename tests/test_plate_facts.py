"""The sliced plate's own picture and its objects' first-layer boxes, for the site (operator, 2026-10-26: "sending a sliced image back tot
he user showing our recommended settings"; the site's skip-objects tab outlines each object by its box): read from the sliced 3MF,
bounded, never raising, carried on the fetched report of a Print it slice and on the 'uploading' report of a dispatched plate."""

import base64
import hashlib
import json
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path

import pytest

from makeros_hub.printers import manager as manager_mod
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.threemf_objects import PLATE_PICTURE_MAX, plate_boxes, plate_picture
from tests.test_dispatch import FakeAdapter, _write_3mf_with_objects, assignment
from tests.test_v072 import MESH, SLICED_INFO, fetch


def png(w: int = 4, h: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x10\x20\x30\xff" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


BOXES = {"bbox_objects": [{"id": 1, "name": "Cube", "bbox": [10.0, 12.5, 40.0, 42.5], "area": 900, "layer_height": 0.2},
                          {"id": 2, "name": " Sphere ", "bbox": [50, 50, 70, 70]},
                          {"id": 3, "name": "Bad", "bbox": [5, 5, 5, 9]}, {"id": 4, "name": "NaN", "bbox": [0, 0, "x", 1]}, {"name": 7, "bbox": [0, 0, 1, 1]}]}


def sliced(path: Path, plate: int = 1, picture: bytes | None = None, boxes: dict | None = None, small: bytes | None = None) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICED_INFO)
        z.writestr(f"Metadata/plate_{plate}.gcode", b"; sliced")
        if picture is not None:
            z.writestr(f"Metadata/plate_{plate}.png", picture)
        if small is not None:
            z.writestr(f"Metadata/plate_{plate}_small.png", small)
        if boxes is not None:
            z.writestr(f"Metadata/plate_{plate}.json", json.dumps(boxes))


def test_the_plate_picture_is_a_bounded_png_or_nothing(tmp_path):
    f = tmp_path / "a.3mf"
    sliced(f, picture=png())
    assert plate_picture(f) == png()
    sliced(f, picture=b"GIF89a not a png")
    assert plate_picture(f) is None
    sliced(f, picture=png() + b"\0" * PLATE_PICTURE_MAX, small=png(2, 2))   # too big: the small one instead
    assert plate_picture(f) == png(2, 2)
    sliced(f, plate=2, picture=png())
    assert plate_picture(f, 1) is None and plate_picture(f, 2) == png()
    (tmp_path / "junk.3mf").write_bytes(b"not a zip")
    assert plate_picture(tmp_path / "junk.3mf") is None and plate_picture(tmp_path / "missing.3mf") is None


def test_plate_boxes_are_the_files_own_finite_boxes_named_and_bounded(tmp_path):
    f = tmp_path / "a.3mf"
    sliced(f, boxes=BOXES)
    assert plate_boxes(f) == [{"name": "Cube", "bbox": [10.0, 12.5, 40.0, 42.5]}, {"name": "Sphere", "bbox": [50.0, 50.0, 70.0, 70.0]}]
    sliced(f, boxes={"bbox_objects": [{"name": "x" * 500, "bbox": [0, 0, 1, 1]}] * 300})
    assert len(plate_boxes(f)) == 256 and len(plate_boxes(f)[0]["name"]) == 120
    for bad in ({"bbox_objects": "no"}, ["no"], {}):
        sliced(f, boxes=bad)
        assert plate_boxes(f) == []
    sliced(f)
    assert plate_boxes(f) == []
    (tmp_path / "junk.3mf").write_bytes(b"not a zip")
    assert plate_boxes(tmp_path / "junk.3mf") == []


@pytest.mark.parametrize("boxes", [1, True, None, "bad", {"name": "bad"},
                                   [{"name": "huge", "bbox": [0, 0, 10 ** 400, 1]}]])
def test_malformed_boxes_never_escape_the_optional_parser(tmp_path, caplog, boxes):
    f = tmp_path / "bad.3mf"
    sliced(f, boxes={"bbox_objects": boxes})
    with caplog.at_level("INFO", logger="makeros-hub.printers"):
        assert plate_boxes(f) == []
    assert "3mf plate boxes skipped" in caplog.text


@pytest.mark.parametrize("boxes", [1, [{"name": "huge", "bbox": [0, 0, 10 ** 400, 1]}]])
def test_malformed_first_plate_does_not_stop_the_heartbeat_dispatch_pass(tmp_path, boxes):
    manager = PrinterManager()
    first, second = FakeAdapter(), FakeAdapter()
    manager._adapters.update(p1=first, p2=second)
    assignments = []
    for number, metadata in ((1, {"bbox_objects": boxes}), (2, BOXES)):
        path = tmp_path / f"abcdef1{number}" / "part.3mf"
        path.parent.mkdir()
        _write_3mf_with_objects(path, plate=2, objects=[(286, "Cube"), (287, "Sphere")])
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("Metadata/plate_2.json", json.dumps(metadata))
        assignments.append(assignment(queueJobId=f"q{number}", printerId=f"p{number}", submissionUid=path.parent.name))
    reports = manager.dispatch_assignments(assignments, tmp_path)
    assert [r["state"] for r in reports] == ["uploading", "uploading"]
    assert [r["queueJobId"] for r in reports] == ["q1", "q2"]
    assert "plateBoxes" not in reports[0] and len(reports[1]["plateBoxes"]) == 2
    assert len(first.calls) == len(second.calls) == 1


def _slice(tmp_path, picture, boxes):
    reports = []

    def get(path, dest):
        Path(dest).write_bytes(MESH)
        return hashlib.sha256(MESH).hexdigest(), len(MESH)

    def run(uid):
        work = Path(tmp_path) / uid / ".slice"
        (work / "out").mkdir()
        (work / "out" / "result.json").write_text(json.dumps({"ok": True, "grams": 3.68, "seconds": 60, "supportUsed": False, "modelId": "N1", "plates": 1}))
        sliced(work / "out" / "sliced.3mf", picture=picture, boxes=boxes)
        return 0

    class Ok:
        status = 200
        body = {"ok": True}

    assert PrinterManager().fetch_uploads([fetch()], tmp_path, getter=get, reporter=lambda b: reports.append(b) or Ok(), slice_runner=run) == 1
    return reports[-1]


def test_a_print_it_slice_reports_its_plate_picture_and_boxes(tmp_path):
    body = _slice(tmp_path, png(), BOXES)
    assert base64.b64decode(body["slice"]["plateImage"]) == png()
    assert [b["name"] for b in body["slice"]["plateBoxes"]] == ["Cube", "Sphere"]
    assert body["slice"]["grams"] == 3.68


def test_a_slice_without_them_reports_as_before(tmp_path):
    body = _slice(tmp_path, None, None)
    assert "plateImage" not in body["slice"] and "plateBoxes" not in body["slice"]


def test_the_picture_never_pushes_the_report_past_the_sites_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "FETCHED_REPORT_BUDGET", 600)
    body = _slice(tmp_path, png(32, 32), BOXES)
    assert "plateImage" not in body["slice"] and len(body["slice"]["plateBoxes"]) == 2


def test_a_dispatched_plate_reports_its_objects_boxes_with_its_objects():
    with tempfile.TemporaryDirectory() as d:
        spool_file = Path(d) / "abcdef12" / "part.3mf"
        spool_file.parent.mkdir()
        _write_3mf_with_objects(spool_file, plate=2, objects=[(286, "Cube"), (287, "Sphere")])
        with zipfile.ZipFile(spool_file, "a") as z:
            z.writestr("Metadata/plate_2.json", json.dumps(BOXES))
        manager = PrinterManager()
        manager._adapters["p1"] = FakeAdapter({"ok": True})
        uploading = next(r for r in manager.dispatch_assignments([assignment()], d) if r["state"] == "uploading")
    assert uploading["objects"] == [{"id": 286, "name": "Cube"}, {"id": 287, "name": "Sphere"}]
    assert uploading["plateBoxes"] == [{"name": "Cube", "bbox": [10.0, 12.5, 40.0, 42.5]}, {"name": "Sphere", "bbox": [50.0, 50.0, 70.0, 70.0]}]


def test_the_uploading_report_reaches_the_site_with_its_boxes():
    from unittest import mock
    from makeros_hub import http
    from makeros_hub.agent import make_queue_status_reporter
    from makeros_hub.config import Config
    cfg = Config(cloud_url="https://cloud.invalid")
    boxes = [{"name": "Cube", "bbox": [10.0, 12.5, 40.0, 42.5]}]
    with mock.patch("makeros_hub.agent.post_json", return_value=http.Response(200, {"ok": True})) as post:
        make_queue_status_reporter(cfg, "cred")({"queueJobId": "q1", "state": "uploading", "objects": [{"id": 1, "name": "Cube"}, {"id": 2, "name": "B"}], "plateBoxes": boxes})
    assert post.call_args.args[1]["plateBoxes"] == boxes
    with mock.patch("makeros_hub.agent.post_json", return_value=http.Response(200, {"ok": True})) as post:
        make_queue_status_reporter(cfg, "cred")({"queueJobId": "q1", "state": "uploading"})
    assert "plateBoxes" not in post.call_args.args[1]


def _ps_slice():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ps_slice_pictures", Path(__file__).resolve().parents[1] / "slicer" / "ps_slice.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GCODE = (b"M83\n; FEATURE: Custom\nG1 X0 Y0 E5\n; FEATURE: Brim\nG1 Z0.2\nG1 X30 Y0 E0.5\n; FEATURE: Outer wall\nG1 X30 Y30 E0.5\n"
         b"G0 X0 Y0\nG1 X0 Y30 E0.5\n; FEATURE: Support\nG1 Z0.4\nG1 X10 Y10 E0.2\n; FEATURE: Prime tower\nG1 X90 Y90 E2\n")


def test_the_slicer_draws_the_plate_from_its_own_gcode_and_keeps_one_that_is_there(tmp_path):
    ps = _ps_slice()
    assert [s[0] for s in ps.gcode_segments(GCODE)] == ["brim", "outer wall", "outer wall", "support"]   # no start G-code, no prime tower
    absolute = b"M82\nG92 E0\n; FEATURE: Inner wall\nG1 X5 Y0 E1\nG1 X5 Y5 E1\nG1 X0 Y5 E2\n"   # absolute E: only a rising E extrudes
    assert len(ps.gcode_segments(absolute)) == 2
    pytest = __import__("pytest")
    pytest.importorskip("PIL")
    with zipfile.ZipFile(tmp_path / "g.3mf", "w") as z:
        z.writestr("Metadata/plate_1.gcode", GCODE)
    assert ps.add_plate_picture(str(tmp_path / "g.3mf"), printable_area=["0x0", "180x0", "180x180", "0x180"]) is True
    with zipfile.ZipFile(tmp_path / "g.3mf") as z:
        one, views = z.read("Metadata/plate_1.png"), z.read("Metadata/ps_views_1.png")
    assert one.startswith(b"\x89PNG") and plate_picture(tmp_path / "g.3mf") == views   # the member gets the four views; the printer keeps one
    assert ps.add_plate_picture(str(tmp_path / "g.3mf")) is False   # ones already there are kept
    with zipfile.ZipFile(tmp_path / "empty.3mf", "w") as z:
        z.writestr("Metadata/plate_1.gcode", b"; nothing printed\n")
    assert ps.add_plate_picture(str(tmp_path / "empty.3mf")) is False and plate_picture(tmp_path / "empty.3mf") is None


def test_the_four_views_are_rhinos_layout_in_one_rgb_png_the_site_takes(tmp_path):
    """Top, Perspective, Front, Right in a 2 x 2 grid (operator, 2026-10-27: "match the default rhino views behind it"): 8-bit RGB, at most
    1024 px and VIEWS_MAX bytes (the site's own limits, check-runner.ts pictureOk and plate-facts.ts); the walls are orange in Top, where
    the bed's outline shows, and the support is teal in Front."""
    PIL = __import__("pytest").importorskip("PIL.Image")
    import io
    ps = _ps_slice()
    assert [v[0] for v in ps._views(ps.gcode_segments(GCODE), [])] == ["Top", "Perspective", "Front", "Right"]
    assert ps._bed(["0x0", "180x0", "180x180", "0x180"]) == [(0, 0), (180, 0), (180, 180), (0, 180)] and ps._bed(["junk"]) == [] and ps._bed(None) == []
    png = ps.draw_plate_views(GCODE, ["0x0", "180x0", "180x180", "0x180"])
    img = PIL.open(io.BytesIO(png))
    assert img.mode == "RGB" and img.size == (2 * ps.VIEW_PANEL + 2, 2 * ps.VIEW_PANEL + 2) and max(img.size) <= 1024 and len(png) <= ps.VIEWS_MAX
    top, front = img.crop((0, 0, ps.VIEW_PANEL, ps.VIEW_PANEL)), img.crop((0, ps.VIEW_PANEL + 2, ps.VIEW_PANEL, 2 * ps.VIEW_PANEL + 2))
    def has(panel, rgb):
        return any(all(abs(a - b) < 40 for a, b in zip(px, rgb)) for px in panel.getdata())
    assert has(top, ps.FEATURE_COLOURS["outer wall"]) and has(top, (150, 150, 150)) and has(front, ps.FEATURE_COLOURS["support"])
    assert ps.draw_plate_views(b"; nothing printed\n") is None
