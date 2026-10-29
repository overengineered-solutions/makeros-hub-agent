"""Stdlib-only tests for the OrcaSlicer ingest: the pure multipart parser, and
a live round-trip against the threaded server with a fake cloud-submit.
Run: python3 -m unittest discover -s tests"""

import hashlib
import io
import json
import tempfile
import time
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

from makeros_hub.multipart import MultipartError, UploadTooLarge, boundary_from_content_type, parse_multipart, stream_multipart
from makeros_hub import ingest
from makeros_hub.ingest import IngestServer


def build_multipart(boundary: str, file_name: str, file_data: bytes, fields: dict) -> bytes:
    parts = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


class TestBoundary(unittest.TestCase):
    def test_extracts_boundary(self):
        self.assertEqual(
            boundary_from_content_type("multipart/form-data; boundary=----abc123"), "----abc123"
        )
        self.assertEqual(
            boundary_from_content_type('multipart/form-data; boundary="q u o t e d"'), "q u o t e d"
        )

    def test_non_multipart_is_none(self):
        self.assertIsNone(boundary_from_content_type("application/json"))
        self.assertIsNone(boundary_from_content_type(None))


class TestParseMultipart(unittest.TestCase):
    def test_boundary_like_bytes_and_trailing_newline_survive_exactly(self):
        data = b"prefix--EXACTBOUNDmiddle\r\n--EXACTBOUNDnot-a-delimiter\r\nlast\r\n"
        form = parse_multipart(build_multipart("EXACTBOUND", "part.3mf", data, {}), "EXACTBOUND")
        self.assertEqual(form.file.data, data)

    def test_parses_file_and_fields_binary_safe(self):
        # Include CRLF + the boundary-ish bytes inside the payload to prove the
        # parser doesn't corrupt binary data.
        payload = b"\x00\x01\r\n--nottheboundary\x02PK\x03\x04binary3mf"
        body = build_multipart("XBOUND", "cube.gcode.3mf", payload, {"print": "true", "path": ""})
        form = parse_multipart(body, "XBOUND")
        self.assertIsNotNone(form.file)
        self.assertEqual(form.file.filename, "cube.gcode.3mf")
        self.assertEqual(form.file.data, payload)  # byte-exact, no corruption
        self.assertEqual(form.fields["print"], "true")
        self.assertEqual(form.fields["path"], "")

    def test_no_file_part(self):
        body = build_multipart("B", "", b"", {"print": "false"})
        # build_multipart always adds a file part with empty filename → treated
        # as not-a-file (empty filename). So fields present, file None.
        form = parse_multipart(body, "B")
        self.assertEqual(form.fields.get("print"), "false")
        self.assertIsNone(form.file)


class TestStreamingMultipart(unittest.TestCase):
    def test_every_boundary_split_and_boundary_lookalike_is_binary_safe(self):
        payload = b"\x00prefix--Bmiddle\r\n--Bnot-a-delimiter\r\n--B--nope\r\nlast\r\n"
        for closing in (b"--B--", b"--B--\r\n"):
            body = build_multipart("B", "part.gcode", payload, {"print": "true"}).rsplit(b"--B--", 1)[0] + closing
            for step in range(1, len(body) + 1):
                with self.subTest(closing=closing, step=step):
                    out = io.BytesIO()
                    result = stream_multipart((body[i:i + step] for i in range(0, len(body), step)), "B", out, len(payload))
                    self.assertEqual(result, ("part.gcode", {"print": "true"}, hashlib.sha256(payload).hexdigest(), len(payload)))
                    self.assertEqual(out.getvalue(), payload)

    def test_malformed_or_unbounded_forms_are_refused(self):
        valid = build_multipart("B", "part.gcode", b"file", {})
        cases = [valid[:-7], valid + b"junk", b"preamble" + valid,
                 valid.replace(b'filename="part.gcode"', b'filename="part.gcode"; padding="' + b"x" * 8192 + b'"'),
                 build_multipart("B", "part.gcode", b"file", {"print": "x" * 1025}),
                 build_multipart("B", "part.gcode", b"file", {str(i): "x" for i in range(64)}),
                 valid.replace(b'name="file"', b'name="other"'),
                 valid[:-7] + valid]
        for body in cases:
            with self.subTest(body=body[:100]), self.assertRaises(MultipartError):
                stream_multipart((body[i:i + 17] for i in range(0, len(body), 17)), "B", io.BytesIO(), 100)
        with self.assertRaises(UploadTooLarge):
            stream_multipart([valid], "B", io.BytesIO(), 3)


class TestIngestServer(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()

        def fake_submit(**kwargs):
            self.calls.append(kwargs)
            tok = kwargs["member_token"]
            if tok == "bad":
                return {"status": "bad_token"}
            if tok == "ineligible":
                return {"status": "rejected", "reason": "membership_paused"}
            return {"status": "queued", "jobId": "job_123"}

        # Port 0 → OS picks a free ephemeral port.
        self.server = IngestServer(
            fake_submit, port=0, spool_dir=Path(self.tmp.name), max_bytes=10 * 1024 * 1024
        )
        # ThreadingHTTPServer bound on 0 → read back the real port.
        self.port = self.server._server.server_address[1]
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_version_endpoint_is_octoprint_shaped(self):
        with urllib.request.urlopen(self._url("/api/version"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            body = json.loads(r.read())
        self.assertIn("api", body)
        self.assertTrue(body["text"].startswith("OctoPrint"))  # slicer's validity check

    def _upload(self, token, print_flag="true", data=b"PK\x03\x04sliced", file_name="cube.gcode.3mf"):
        boundary = "----makerosTEST"
        body = build_multipart(boundary, file_name, data, {"print": print_flag, "path": ""})
        req = urllib.request.Request(
            self._url("/api/files/local"),
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Api-Key": token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_upload_queued_returns_201_and_calls_cloud_and_spools_file(self):
        status, body = self._upload("good-token")
        self.assertEqual(status, 201)
        self.assertEqual(body["files"]["local"]["origin"], "local")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["member_token"], "good-token")
        self.assertTrue(call["print_now"])
        self.assertEqual(call["file_size"], len(b"PK\x03\x04sliced"))
        # File was spooled hub-local under the submission uid.
        spooled = list(Path(self.tmp.name).rglob("cube.gcode.3mf"))
        self.assertEqual(len(spooled), 1)
        self.assertEqual(spooled[0].read_bytes(), b"PK\x03\x04sliced")

    def test_gcode_upload_hands_its_sliced_metadata_to_the_cloud(self):
        from test_gcode_meta import sv08_gcode

        status, _ = self._upload("good-token", data=sv08_gcode().encode(), file_name="cube.gcode")
        self.assertEqual(status, 201)
        call = self.calls[-1]
        self.assertEqual(call["printer_model"], "Sovol SV08 MAX")
        self.assertEqual(call["est_grams"], 5)
        self.assertEqual(call["required_filaments"], [{"slot": 1, "type": "PLA", "color": "FFFFFFFF", "usedG": 4.53}])

    def test_3mf_upload_hands_no_metadata(self):
        self._upload("good-token")
        self.assertFalse({"printer_model", "est_grams", "required_filaments"} & set(self.calls[-1]))

    def test_unreadable_metadata_never_fails_the_upload(self):
        with mock.patch("makeros_hub.ingest.read_gcode_meta", side_effect=ValueError("garbled")):
            status, _ = self._upload("good-token", data=b"G28\n", file_name="cube.gcode")
        self.assertEqual(status, 201)
        self.assertNotIn("printer_model", self.calls[-1])

    def test_missing_api_key_is_403(self):
        boundary = "----b"
        body = build_multipart(boundary, "x.3mf", b"data", {"print": "false"})
        req = urllib.request.Request(
            self._url("/api/files/local"),
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_bad_token_maps_to_403(self):
        status, _ = self._upload("bad")
        self.assertEqual(status, 403)

    def test_rejection_is_visible_because_no_portal_job_was_created(self):
        status, body = self._upload("ineligible")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "print_rejected")

    def test_unknown_receipt_keeps_file_and_never_reports_success(self):
        handler = self.server._server.RequestHandlerClass
        for outcome in ({}, {"status": "queued"}, {"status": "error"}, None):
            with mock.patch.object(handler, "submit_fn", staticmethod(lambda **kwargs: outcome)):
                status, body = self._upload("good")
                self.assertEqual(status, 502)
                self.assertEqual(body["error"], "submission_unconfirmed")
        self.assertEqual(len(list(Path(self.tmp.name).rglob("*.3mf"))), 4)

    def test_busy_and_disk_failures_never_reach_cloud(self):
        handler = self.server._server.RequestHandlerClass
        with handler.upload_slot:
            self.assertEqual(self._upload("good")[0], 503)
        with mock.patch("makeros_hub.ingest.os.fsync", side_effect=OSError("disk error")):
            self.assertEqual(self._upload("good")[0], 507)
        self.assertEqual(self.calls, [])
        with mock.patch("makeros_hub.ingest.shutil.disk_usage", return_value=mock.Mock(free=0)):
            self.assertEqual(self._upload("good")[0], 507)
        self.assertEqual(self.calls, [])

    def test_raw_limit_excludes_form_overhead_and_removes_partial_files(self):
        handler = self.server._server.RequestHandlerClass
        with mock.patch.object(handler, "max_bytes", 8):
            self.assertEqual(self._upload("good", data=b"12345678")[0], 201)
            status, body = self._upload("good", data=b"123456789")
            self.assertEqual((status, body), (413, {"error": "file_too_large", "maxBytes": 8}))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)
        self.assertFalse(list(Path(self.tmp.name).rglob("*.tmp")))

    def test_truncated_or_timed_out_uploads_remove_the_partial_spool(self):
        handler = self.server._server.RequestHandlerClass
        for error, status in ((EOFError, 400), (TimeoutError, 408)):
            def broken_read(_handler, _length):
                yield build_multipart("----makerosTEST", "part.gcode", b"G1" * 100, {})[:-30]
                raise error("stopped")
            with mock.patch.object(handler, "_read_chunks", broken_read):
                self.assertEqual(self._upload("good")[0], status)
            self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_connection_threads_are_bounded_before_headers(self):
        slots = self.server._server.connections
        limit = ingest._BoundedServer.MAX_CONNECTIONS
        self.assertGreaterEqual(limit, 32)   # 4 let a few dead tailnet connections refuse every member (2026-10-29)
        for _ in range(limit):
            slots.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self._url("/api/version"), timeout=5)
            self.assertEqual(caught.exception.code, 503)
        finally:
            for _ in range(limit):
                slots.release()

    def test_a_silent_connection_frees_its_slot(self):
        import socket
        with mock.patch.object(self.server._server.RequestHandlerClass, "REQUEST_LINE_TIMEOUT", 0.3):
            silent = [socket.create_connection(("127.0.0.1", self.server._server.server_address[1])) for _ in range(40)]
            try:
                time.sleep(1.5)   # every silent connection timed out and gave its slot back
                with urllib.request.urlopen(self._url("/api/version"), timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                for s in silent:
                    s.close()

    def test_rejected_discards_the_spooled_file(self):
        # A rejected job's file won't print — it must not leak on disk.
        self._upload("ineligible")
        spooled = list(Path(self.tmp.name).rglob("*.3mf"))
        self.assertEqual(spooled, [])

    def test_bad_token_discards_the_spooled_file(self):
        self._upload("bad")
        spooled = list(Path(self.tmp.name).rglob("*.3mf"))
        self.assertEqual(spooled, [])


if __name__ == "__main__":
    unittest.main()
