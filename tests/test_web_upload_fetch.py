"""v0.57 (design B4): web uploads — the hub fetches a member's .3mf from the cloud into its spool, verifies the sha,
parses the sent plate and reports `fetched`."""

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from makeros_hub.printers.manager import PrinterManager

SLICE_INFO = b"""<?xml version="1.0"?><config><plate><metadata key="index" value="1"/>
<filament id="1" tray_info_idx="GFL99" type="PLA" color="#FFFFFF" used_m="1.0" used_g="3.4"/>
<filament id="2" tray_info_idx="GFG00" type="PETG" color="#000000" used_m="2.0" used_g="7.25"/></plate>
<plate><metadata key="index" value="2"/><filament id="3" type="ABS" color="#FF0000" used_g="1.5"/></plate></config>"""


def three_mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
        z.writestr("Metadata/plate_1.gcode", b"; gcode")
    return buf.getvalue()


class _Resp:
    def __init__(self, ok=True):
        self.status = 200
        self.body = {"ok": ok}


class TestFetchUploads(unittest.TestCase):
    def _fetch(self, data: bytes, plate=1):
        return {"queueJobId": "11111111-2222-4333-8444-555555555555", "submissionUid": "abcdef1234567890", "fileName": "part.3mf",
                "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data), "plate": plate, "path": "/api/print/hub/file/1111"}

    def test_downloads_verifies_parses_the_sent_plate_and_reports(self):
        data = three_mf()
        reports = []
        calls = []

        def getter(path, dest):
            calls.append(path)
            Path(dest).write_bytes(data)
            return hashlib.sha256(data).hexdigest(), len(data)

        with tempfile.TemporaryDirectory() as d:
            m = PrinterManager()
            n = m.fetch_uploads([self._fetch(data, plate=1)], d, getter=getter, reporter=lambda b: (reports.append(b), _Resp())[1])
            self.assertEqual(n, 1)
            self.assertEqual(calls, ["/api/print/hub/file/1111"])
            self.assertTrue((Path(d) / "abcdef1234567890" / "part.3mf").exists())
            self.assertFalse(list((Path(d) / "abcdef1234567890").glob(".*.tmp")))
            [r] = reports
            self.assertEqual(r["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual([f["slot"] for f in r["requiredFilaments"]], [1, 2])          # plate 1 only — not plate 2's ABS
            self.assertEqual(r["requiredFilaments"][0]["trayInfoIdx"], "GFL99")
            self.assertEqual(r["estGrams"], 11)                                             # ceil(3.4 + 7.25)
            # already in the spool: no second download, still reported (idempotent)
            n = m.fetch_uploads([self._fetch(data)], d, getter=getter, reporter=lambda b: (reports.append(b), _Resp())[1])
            self.assertEqual((n, len(calls), len(reports)), (1, 1, 2))

    def test_sha_mismatch_is_reported_and_nothing_is_kept(self):
        data = three_mf()
        reports = []

        def getter(path, dest):
            Path(dest).write_bytes(b"not the file")
            return hashlib.sha256(b"not the file").hexdigest(), 12

        with tempfile.TemporaryDirectory() as d:
            n = PrinterManager().fetch_uploads([self._fetch(data)], d, getter=getter, reporter=lambda b: (reports.append(b), _Resp(False))[1])
            self.assertEqual(n, 0)
            self.assertEqual(reports, [{"queueJobId": "11111111-2222-4333-8444-555555555555", "sha256": hashlib.sha256(b"not the file").hexdigest()}])
            self.assertEqual(list((Path(d) / "abcdef1234567890").iterdir()), [])

    def test_malformed_or_foreign_entries_are_skipped_and_one_per_beat(self):
        data = three_mf()
        got = []

        def getter(path, dest):
            got.append(path); Path(dest).write_bytes(data); return hashlib.sha256(data).hexdigest(), len(data)

        with tempfile.TemporaryDirectory() as d:
            m = PrinterManager()
            bad_uid = dict(self._fetch(data), submissionUid="../etc")
            bad_path = dict(self._fetch(data), path="https://evil.example/x")
            two = [dict(self._fetch(data), submissionUid="aaaaaaaa"), dict(self._fetch(data), submissionUid="bbbbbbbb")]
            self.assertEqual(m.fetch_uploads([bad_uid, bad_path], d, getter=getter, reporter=lambda b: _Resp()), 0)
            self.assertEqual(got, [])
            self.assertEqual(m.fetch_uploads(two, d, getter=getter, reporter=lambda b: _Resp()), 1)   # one per beat
            self.assertEqual(len(got), 1)

    def test_a_failing_download_never_raises(self):
        def getter(path, dest):
            raise OSError("boom")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(PrinterManager().fetch_uploads([self._fetch(b"x")], d, getter=getter, reporter=lambda b: _Resp()), 0)
