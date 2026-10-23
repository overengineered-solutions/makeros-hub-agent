"""v0.72: automatic slicing. A web upload may carry a member's checked mesh and a slice spec; the hub slices it on the shop
box through the root-owned runner, then treats the plate like any member's upload (operator, 2026-10-18)."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.agent import heartbeat_payload
from makeros_hub.printers.manager import SLICER_CAPS, PrinterManager, parse_slicer_caps, probe_slicer, slice_spec_of

SLICED_INFO = b"""<?xml version="1.0"?><config><plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="N1"/>
<filament id="1" tray_info_idx="GFA00" type="PLA" color="#1E90FF" used_m="1.2" used_g="3.68"/></plate></config>"""


def sliced_plate() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("Metadata/slice_info.config"), SLICED_INFO)
        z.writestr(zipfile.ZipInfo("Metadata/plate_1.gcode"), b"; sliced by the shop")
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
        choice = {"version": 1, "thicken": True, "moveLargeGaps": False}
        self.assertEqual(slice_spec_of({**SPEC, "prepare": choice})["prepare"], choice)
        self.assertEqual(parse_slicer_caps({"scaleMin": 0.001, "scaleMax": 1000, "prepareVersion": 1})["prepareVersion"], 1)
        self.assertNotIn("prepareVersion", parse_slicer_caps({"scaleMin": 0.001, "scaleMax": 1000, "prepareVersion": True}))
        self.assertEqual(slice_spec_of(SPEC), {"model": "A1 Mini", "filament": {"type": "PLA", "colorHex": "#1E90FF", "trayInfoIdx": "GFA00"},
                                               "fixes": {"orient": True, "brim": False}, "scale": 1.0})
        self.assertEqual(slice_spec_of({"model": "P2S", "filament": "x", "fixes": [], "scale": True}),
                         {"model": "P2S", "filament": {"type": None, "colorHex": None, "trayInfoIdx": None}, "fixes": {}})
        # v0.82: the H2D's nozzle reaches the job file only as "left" or "right"
        self.assertEqual(slice_spec_of({**SPEC, "model": "H2D", "nozzle": "left"})["nozzle"], "left")
        for odd in (1, "LEFT", None, {"x": 1}):
            self.assertNotIn("nozzle", slice_spec_of({**SPEC, "model": "H2D", "nozzle": odd}))


class TestSlicerReady(unittest.TestCase):
    def test_the_heartbeat_says_whether_this_box_can_slice(self):
        self.assertIs(heartbeat_payload(slicer_ready=True)["slicerReady"], True)
        self.assertIs(heartbeat_payload(slicer_ready=False)["slicerReady"], False)
        self.assertNotIn("slicerReady", heartbeat_payload())

    def test_heartbeat_names_the_camera_gateway_and_live_view_stats_v075(self):
        p = heartbeat_payload(camera_gateway=True, camera_live_stats=[{"printerId": "p", "sent": 3, "failed": 0, "postMs": 80}])
        self.assertIs(p["cameraGateway"], True)
        self.assertEqual(p["cameraLiveStats"], [{"printerId": "p", "sent": 3, "failed": 0, "postMs": 80}])
        self.assertIs(heartbeat_payload(camera_gateway=False)["cameraGateway"], False)
        self.assertNotIn("cameraLiveStats", heartbeat_payload(camera_live_stats=[]))
        self.assertNotIn("cameraGateway", heartbeat_payload())


CAPS = {"scaleMin": 0.001, "scaleMax": 1000.0, "lineWidth": {"A1 Mini": 0.42, "P2S": 0.42}}


class TestSlicerCaps(unittest.TestCase):
    """Send to PS step 2 (review): the site gates sizes on what the installed ps-slicer image says it takes, read from the image
    by a probe job and from every slice result, never from the agent's version (an agent and its image can differ)."""

    def test_the_probe_asks_the_image_through_the_runner_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            def runner(uid):
                work = Path(d) / uid / ".slice"
                seen.append(json.loads((work / "job.json").read_text()))
                (work / "out").mkdir()
                (work / "out" / "result.json").write_text(json.dumps({"ok": True, "probe": True, **CAPS}))
                return 0
            self.assertEqual(parse_slicer_caps(probe_slicer(d, runner)), CAPS)
            self.assertEqual(seen, [{"probe": True}])
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_an_image_before_step_2_or_a_runner_that_cannot_start_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            def old(uid):   # the old image refuses a probe as a slice request without a mesh
                work = Path(d) / uid / ".slice"
                (work / "out").mkdir()
                (work / "out" / "result.json").write_text(json.dumps({"ok": False, "error": "the file name is not one the slicer accepts"}))
                return 3
            def broken(uid):
                raise FileNotFoundError("sudo")
            self.assertIsNone(parse_slicer_caps(probe_slicer(d, old)))
            self.assertIsNone(probe_slicer(d, broken))
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_caps_are_read_strictly(self):
        self.assertEqual(parse_slicer_caps({**CAPS, "lineWidth": {"A1 Mini": 0.42, "x" * 41: 0.4, "P2S": "0.42", "X1C": 9, "H2D": True}}),
                         {"scaleMin": 0.001, "scaleMax": 1000.0, "lineWidth": {"A1 Mini": 0.42}})
        for bad in ({}, {"scaleMin": "0.001", "scaleMax": 1000}, {"scaleMin": 0, "scaleMax": 1}, {"scaleMin": 2, "scaleMax": 1},
                    {"scaleMin": 0.001, "scaleMax": float("inf")}, {"scaleMin": float("nan"), "scaleMax": 1}, {"scaleMin": True, "scaleMax": 1},
                    {"scaleMin": 0.001, "scaleMax": 1e7}, None, "caps"):
            self.assertIsNone(parse_slicer_caps(bad), bad)
        self.assertEqual(parse_slicer_caps({"scaleMin": 0.05, "scaleMax": 20}), {"scaleMin": 0.05, "scaleMax": 20.0, "lineWidth": {}})

    def test_every_slice_result_refreshes_what_the_image_takes_and_its_settings_reach_the_site(self):
        with tempfile.TemporaryDirectory() as d:
            settings = {"sparse_infill_pattern": "rectilinear", "sparse_infill_density": "15%", "wall_loops": "2"}
            ok = {"ok": True, "grams": 3.68, "seconds": 1174, "supportUsed": False, "filament": "Bambu PLA Basic @BBL A1M",
                  "process": "0.20mm Standard @BBL A1M", "settings": settings, "modelId": "N1", "plates": 1, **CAPS}
            reports = []
            def runner(uid, result=ok):
                work = Path(d) / uid / ".slice"
                (work / "out").mkdir()
                (work / "out" / "result.json").write_text(json.dumps(result))
                if result.get("ok"):
                    (work / "out" / "sliced.3mf").write_bytes(sliced_plate())
                return 0 if result.get("ok") else 3
            def get(path, dest):
                Path(dest).write_bytes(MESH)
                return hashlib.sha256(MESH).hexdigest(), len(MESH)
            def report(body):
                reports.append(body)
                return _Resp()
            SLICER_CAPS.seen(None)
            PrinterManager().fetch_uploads([fetch()], d, getter=get, reporter=report, slice_runner=runner)
            self.assertEqual(reports[-1]["slice"]["settings"], settings)
            self.assertEqual(SLICER_CAPS.value, CAPS)
            # the image is rolled back under the agent: its next answer says nothing, and the box is unknown again
            PrinterManager().fetch_uploads([fetch(submissionUid="0123456789abcdef")], d, getter=get, reporter=report,
                                           slice_runner=lambda uid: runner(uid, {"ok": False, "error": "the size change is outside what the slicer accepts"}))
            self.assertIsNone(SLICER_CAPS.value)

    def test_the_heartbeat_carries_the_images_answer_only_while_the_box_can_slice(self):
        self.assertEqual(heartbeat_payload(slicer_ready=True, slicer_caps=CAPS)["slicerCaps"], CAPS)
        self.assertNotIn("slicerCaps", heartbeat_payload(slicer_ready=False, slicer_caps=CAPS))
        self.assertNotIn("slicerCaps", heartbeat_payload(slicer_ready=True, slicer_caps=None))


if __name__ == "__main__":
    unittest.main()


def test_prepare_round_trip_refusal_and_cache_binds_choices(tmp_path):
    m = PrinterManager()
    reports, jobs = [], []
    request = {"version": 1, "thicken": True, "moveLargeGaps": False}
    proposal = {"version": 1, "changes": [], "blocked": []}

    def get(path, dest):
        Path(dest).write_bytes(MESH)
        return hashlib.sha256(MESH).hexdigest(), len(MESH)

    def report(body):
        reports.append(body)
        return _Resp()

    def runner(uid):
        work = tmp_path / uid / ".slice"
        jobs.append(json.loads((work / "job.json").read_text()))
        (work / "out").mkdir()
        (work / "out/result.json").write_text(json.dumps({"ok": True, "prepare": proposal, "grams": 4}))
        (work / "out/sliced.3mf").write_bytes(sliced_plate())
        return 0

    sent = fetch(slice={**SPEC, "prepare": request})
    for _ in range(2):
        assert m.fetch_uploads([sent], tmp_path, getter=get, reporter=report, slice_runner=runner) == 1
    assert len(jobs) == 1 and jobs[0]["prepare"] == request
    assert reports[-1]["slice"]["prepare"] == proposal
    # Same mesh, different chosen repairs: the old plate and report cannot be reused.
    sent["slice"] = {**SPEC, "prepare": {**request, "moveLargeGaps": True}}
    assert m.fetch_uploads([sent], tmp_path, getter=get, reporter=report, slice_runner=runner) == 1
    assert len(jobs) == 2

    def refuse(uid):
        work = tmp_path / uid / ".slice"
        (work / "out").mkdir()
        (work / "out/result.json").write_text(json.dumps({"ok": False, "error": "Pick a bigger scale.", "prepare": proposal}))
        return 3
    sent["slice"] = {**SPEC, "prepare": {**request, "thicken": False}}
    assert m.fetch_uploads([sent], tmp_path, getter=get, reporter=report, slice_runner=refuse) == 0
    assert reports[-1]["slice"]["prepare"] == proposal
    assert reports[-1]["sliceError"] == "Pick a bigger scale."
