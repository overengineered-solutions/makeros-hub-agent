"""v0.72: automatic slicing. A web upload may carry a member's checked mesh and a slice spec; the hub slices it on the shop
box through the root-owned runner, then treats the plate like any member's upload (operator, 2026-10-18)."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.printers.manager import PrinterManager, slice_spec_of

SLICED_INFO = b"""<?xml version="1.0"?><config><plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="N1"/>
<filament id="1" tray_info_idx="GFA00" type="PLA" color="#1E90FF" used_m="1.2" used_g="3.68"/></plate></config>"""


def sliced_plate() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICED_INFO)
        z.writestr("Metadata/plate_1.gcode", b"; sliced by the shop")
    return buf.getvalue()


MESH = b"solid cube\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid cube\n"
SPEC = {"model": "A1 Mini", "filament": {"type": "PLA", "colorHex": "#1E90FF", "trayInfoIdx": "GFA00", "extra": "x"},
        "fixes": {"orient": True, "brim": False}, "scale": 1.0, "evil": "rm -rf /"}


class _Resp:
    status = 200
    body = {"ok": True}


def fetch(mesh: bytes = MESH, **over):
    f = {"queueJobId": "11111111-2222-4333-8444-555555555555", "submissionUid": "abcdef1234567890", "fileName": "cube.3mf",
         "meshName": "cube.stl", "sha256": hashlib.sha256(mesh).hexdigest(), "sizeBytes": len(mesh), "plate": 1,
         "path": "/api/print/hub/file/1111", "slice": SPEC}
    f.update(over)
    return f


class TestSliceUploads(unittest.TestCase):
    def setUp(self):
        self.reports, self.runs, self.downloads = [], [], []

    def getter(self, data=MESH):
        def get(path, dest):
            self.downloads.append(path)
            Path(dest).write_bytes(data)
            return hashlib.sha256(data).hexdigest(), len(data)
        return get

    def reporter(self, body):
        self.reports.append(body)
        return _Resp()

    def runner(self, spool, result, plate=True):
        def run(uid):
            self.runs.append(uid)
            work = Path(spool) / uid / ".slice"
            job = json.loads((work / "job.json").read_text())
            self.assertEqual(job, {"model": "A1 Mini", "filament": {"type": "PLA", "colorHex": "#1E90FF", "trayInfoIdx": "GFA00"},
                                   "fixes": {"orient": True, "brim": False}, "scale": 1.0, "mesh": "cube.stl"})
            self.assertEqual((work / "cube.stl").read_bytes(), MESH)
            (work / "out").mkdir()
            (work / "out" / "result.json").write_text(json.dumps(result))
            if plate:
                (work / "out" / "sliced.3mf").write_bytes(sliced_plate())
            return 0 if result.get("ok") else 3
        return run

    def test_slices_then_reports_like_a_sliced_upload_and_reuses_a_finished_slice(self):
        with tempfile.TemporaryDirectory() as d:
            m = PrinterManager()
            ok = {"ok": True, "grams": 3.68, "seconds": 1174, "supportUsed": False, "filament": "Bambu PLA Basic @BBL A1M",
                  "process": "0.20mm Standard @BBL A1M", "modelId": "N1", "plates": 1}
            self.assertEqual(m.fetch_uploads([fetch()], d, getter=self.getter(), reporter=self.reporter, slice_runner=self.runner(d, ok)), 1)
            [body] = self.reports
            self.assertEqual(body["sha256"], hashlib.sha256(MESH).hexdigest())   # the cloud stored the mesh's sha
            self.assertEqual(body["printerModelId"], "N1")
            self.assertEqual(body["estGrams"], 4)
            self.assertEqual(body["requiredFilaments"][0].get("trayInfoIdx"), "GFA00")
            self.assertEqual(body["slice"]["grams"], 3.68)
            self.assertEqual((Path(d) / "abcdef1234567890" / "cube.3mf").read_bytes(), sliced_plate())
            # the report was lost: next beat the finished plate is reported again without downloading or slicing again
            self.assertEqual(m.fetch_uploads([fetch()], d, getter=self.getter(), reporter=self.reporter, slice_runner=self.runner(d, ok)), 1)
            self.assertEqual((len(self.runs), len(self.downloads), len(self.reports)), (1, 1, 2))

    def test_a_slice_the_slicer_refuses_is_reported_with_its_sentence_and_leaves_no_plate(self):
        with tempfile.TemporaryDirectory() as d:
            m = PrinterManager()
            bad = {"ok": False, "error": "the part does not fit on this printer's bed; choose a larger printer or scale it down"}
            self.assertEqual(m.fetch_uploads([fetch()], d, getter=self.getter(), reporter=self.reporter, slice_runner=self.runner(d, bad, plate=False)), 0)
            self.assertEqual(self.reports, [{"queueJobId": fetch()["queueJobId"], "sha256": fetch()["sha256"], "sliceError": bad["error"]}])
            self.assertFalse((Path(d) / "abcdef1234567890" / "cube.3mf").exists())

    def test_a_runner_that_cannot_start_says_the_slicer_is_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            def broken(uid):
                raise FileNotFoundError("sudo")
            PrinterManager().fetch_uploads([fetch()], d, getter=self.getter(), reporter=self.reporter, slice_runner=broken)
            self.assertEqual(self.reports[0]["sliceError"], "the shop's slicer is not available right now")

    def test_sha_mismatch_and_a_bad_mesh_name_are_reported_without_slicing(self):
        with tempfile.TemporaryDirectory() as d:
            m = PrinterManager()
            m.fetch_uploads([fetch()], d, getter=self.getter(b"other bytes"), reporter=self.reporter, slice_runner=self.runner(d, {"ok": True}))
            self.assertEqual(self.reports[-1], {"queueJobId": fetch()["queueJobId"], "sha256": hashlib.sha256(b"other bytes").hexdigest()})
            m.fetch_uploads([fetch(meshName="../etc/passwd")], d, getter=self.getter(), reporter=self.reporter, slice_runner=self.runner(d, {"ok": True}))
            self.assertEqual(self.reports[-1]["sliceError"], "the file name is not one the slicer accepts")
            self.assertEqual(self.runs, [])

    def test_only_the_slicer_fields_reach_the_job_file(self):
        self.assertEqual(slice_spec_of(SPEC), {"model": "A1 Mini", "filament": {"type": "PLA", "colorHex": "#1E90FF", "trayInfoIdx": "GFA00"},
                                               "fixes": {"orient": True, "brim": False}, "scale": 1.0})
        self.assertEqual(slice_spec_of({"model": "P2S", "filament": "x", "fixes": [], "scale": True}),
                         {"model": "P2S", "filament": {"type": None, "colorHex": None, "trayInfoIdx": None}, "fixes": {}})


if __name__ == "__main__":
    unittest.main()
