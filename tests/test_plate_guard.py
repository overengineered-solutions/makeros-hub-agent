"""v0.80.1 (2026-10-22): the hub never tells a printer to start a plate its file does not hold, and never guesses one.

OrcaSlicer 2.4.2's Export plate sliced file keeps the plate's own number, so plate 2 of a project exports as a file holding only
Metadata/plate_2.gcode; sent as plate 1, a printer took the command, stopped on its screen and the hub reported
start_not_observed, six times for one member. After ChatGPT's review: canonical names only, duplicates and damaged entries
refused, an OrcaSlicer send that names no plate is never printed as plate 1, and a file that breaks the check holds its own
assignment, never the pass. The fixtures are synthetic (make_plates.py) and byte-equal to the site's test/fixtures/plates.
"""

import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.threemf_objects import gcode_plates
from makeros_hub.vprinter.capture import (
    UploadRecord,
    assemble_captured_job,
    build_vp_submit_body,
    parse_project_file_command,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plates"
# the same pins as the site's test/unit/_zip.ts PLATE_FIXTURES: both sides read these very bytes
PINNED = {
    "single-plate-2.3mf": ("badcf43d1854f6f4314d21dc6a456e4858157998330bab0fa926351b726923dd", [2]),
    "send-plate-3.3mf": ("2fe4d3ad77d8eace02756d49e713b1fd957ef42a927193db884770c7c0df213d", [3]),
    "part_P3.3mf": ("0735ce27c392d2bcb00e7ff3767db2273cf27568971733ad162260f7cafbff48", [3]),
    "three-plates.3mf": ("a309f3e9e5e61b07de9923fa5175015320d3b93ae2fa85bb11db41f3d9263734", [1, 2, 3]),
}
INFO = '<config><plate><metadata key="index" value="{n}"/></plate></config>'


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def start_print(self, local_path, file_name, **kwargs):
        self.calls.append((file_name, kwargs.get("plate")))
        return {"ok": True}


def assignment(job, name, plate=None, uid="abcdef12", **extra):
    a = {"queueJobId": job, "printerId": "p1", "submissionUid": uid, "fileName": name, "assignmentSeq": 1, **extra}
    if plate is not None:
        a["plate"] = plate
    return a


class TestPlateGuard(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "abcdef12").mkdir()
        self.manager = PrinterManager()
        self.adapter = FakeAdapter()
        self.manager._adapters["p1"] = self.adapter

    def stage(self, name, data=None, src=None):
        path = self.dir / "abcdef12" / name
        if src is not None:
            shutil.copy(src, path)
        else:
            path.write_bytes(data)
        return path

    def zip_of(self, entries, method=zipfile.ZIP_DEFLATED):
        path = self.dir / "built.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            for name, body in entries:
                info = zipfile.ZipInfo(name)
                info.compress_type = method
                zf.writestr(info, body)
        return path.read_bytes()

    def dispatch(self, *assignments):
        return self.manager.dispatch_assignments(list(assignments), self.dir)

    def test_the_members_shapes_print_their_own_plate_and_the_old_default_is_refused(self):
        for i, (name, (sha, plates)) in enumerate(PINNED.items()):
            src = FIXTURES / name
            self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), sha, name)
            self.stage(name, src=src)
            self.assertEqual(gcode_plates(self.dir / "abcdef12" / name), plates, name)
            if 1 not in plates:   # what the site's upload form used to send
                [r] = self.dispatch(assignment(f"old{i}", name, 1))
                self.assertEqual(r["reason"], f"plate_missing: the file has no plate 1; it holds plate {', '.join(map(str, plates))}")
            # the plate the site reads from the file now (the member's pick for three-plates)
            [r] = self.dispatch(assignment(f"new{i}", name, plates[-1]))
            self.assertEqual(r["state"], "uploading", name)
        self.assertEqual([c[1] for c in self.adapter.calls], [2, 3, 3, 3])

    def test_an_orca_send_that_names_no_plate_prints_a_lone_plate_and_never_guesses_among_several(self):
        # the whole path: the captured command names no plate, the submit body carries none, the site sends none (it omits a
        # null plate since 2026-10-22), and dispatch decides from the file
        for name, want in (("single-plate-2.3mf", ("uploading", 2)), ("three-plates.3mf", ("held", None))):
            path = self.stage(name, src=FIXTURES / name)
            intent = parse_project_file_command({"print": {"command": "project_file", "file": name, "url": f"ftp:///{name}"}}, "m1")
            upload = UploadRecord("m1", name, path, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
            body = build_vp_submit_body(assemble_captured_job(upload, intent), model="X1C")
            sent = assignment(f"vp-{name}", name, body.get("plate"))
            [r] = self.dispatch(sent)
            self.assertEqual(r["state"], want[0], name)
            if want[1] is not None:
                self.assertEqual(body["plate"], want[1])
                self.assertEqual(self.adapter.calls[-1], (name, want[1]))
            else:
                self.assertNotIn("plate", body)
                self.assertEqual(r["reason"], "plate_unchosen: no plate was named; the file holds plate 1, 2, 3")
        # a file name that merely looks like a plate ("part_plate_3.3mf" in the url) names none
        self.assertIsNone(parse_project_file_command({"print": {"command": "project_file", "url": "ftp:///part_plate_3.3mf"}}, "m1").plate)
        self.assertEqual(parse_project_file_command({"print": {"command": "project_file", "param": "Metadata/plate_3.gcode"}}, "m1").plate, 3)
        self.assertIsNone(parse_project_file_command({"print": {"command": "project_file", "param": "Metadata/plate_03.gcode"}}, "m1").plate)

    def test_padded_enormous_duplicate_and_damaged_entries_are_refused(self):
        info2 = INFO.format(n=2)
        cases = {
            # a padded name is not plate 2: the printer would be asked for plate_2.gcode, which is not there
            "padded": (self.zip_of([("Metadata/plate_02.gcode", "G28"), ("Metadata/slice_info.config", info2)]), 2, "plate_missing"),
            "enormous": (self.zip_of([(f"Metadata/plate_{'9' * 5000}.gcode", "G28")]), 2, "plate_missing"),
            "out of range": (self.zip_of([("Metadata/plate_65.gcode", "G28")]), 2, "plate_missing"),
        }
        dup = self.zip_of([("Metadata/plate_2.gcode", "G28"), ("Metadata/plate_X.gcode", "G29")])
        cases["duplicate"] = (dup.replace(b"Metadata/plate_X.gcode", b"Metadata/plate_2.gcode"), 2, "plate_unreadable: the file lists an entry twice")
        good = self.zip_of([("[Content_Types].xml", "<Types/>"), ("Metadata/plate_2.gcode", "G28\n" * 50), ("Metadata/slice_info.config", info2)])
        local = good.index(b"Metadata/plate_2.gcode") - 30
        broken = bytearray(good); broken[local:local + 4] = b"XXXX"
        cases["broken local header"] = (bytes(broken), 2, "plate_unreadable: plate 2's entry in the file is damaged")
        method = bytearray(good); method[local + 8:local + 10] = (0).to_bytes(2, "little")   # the local header says stored
        cases["local header disagrees"] = (bytes(method), 2, "plate_unreadable: plate 2's entry in the file is damaged")
        cases["damaged zip"] = (good[: len(good) // 2], 2, "plate_unreadable: the file is a damaged zip")
        cases["unsupported compression"] = (self.zip_of([("Metadata/plate_2.gcode", "G28")], zipfile.ZIP_BZIP2), 2,
                                            "plate_unreadable: plate 2 uses a compression the printer does not read")
        for i, (label, (data, plate, reason)) in enumerate(cases.items()):
            self.stage(f"c{i}.3mf", data)
            [r] = self.dispatch(assignment(f"c{i}", f"c{i}.3mf", plate))
            self.assertEqual(r["state"], "held", label)
            self.assertTrue(r["reason"].startswith(reason), (label, r["reason"]))
        self.assertEqual(self.adapter.calls, [])

    def test_a_file_that_breaks_the_check_holds_its_own_assignment_and_the_pass_goes_on(self):
        self.stage("bad.3mf", src=FIXTURES / "single-plate-2.3mf")
        self.stage("good.3mf", src=FIXTURES / "single-plate-2.3mf")
        real = gcode_plates

        def flaky(path):
            if Path(path).name == "bad.3mf":
                raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
            return real(path)

        self.manager._adapters["p2"] = FakeAdapter()
        with mock.patch("makeros_hub.printers.manager.gcode_plates", side_effect=flaky):
            reports = self.dispatch(assignment("bad", "bad.3mf", 2), assignment("good", "good.3mf", 2, printerId="p2"))
        self.assertEqual([(r["queueJobId"], r["state"]) for r in reports], [("bad", "held"), ("good", "uploading")])
        self.assertEqual(reports[0]["reason"], "plate_unreadable: the file could not be read")

    def test_a_klipper_gcode_has_no_plates_to_check(self):
        self.stage("part.gcode", b"G28\nG1 Z5\n")
        [r] = self.dispatch(assignment("k1", "part.gcode"))
        self.assertEqual(r["state"], "uploading")
        self.assertEqual(self.adapter.calls, [("part.gcode", 1)])


if __name__ == "__main__":
    unittest.main()
