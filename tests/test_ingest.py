"""Stdlib-only tests for the OrcaSlicer ingest: the pure multipart parser, and
a live round-trip against the threaded server with a fake cloud-submit.
Run: python3 -m unittest discover -s tests"""

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from makeros_hub.multipart import boundary_from_content_type, parse_multipart
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

    def _upload(self, token, print_flag="true", data=b"PK\x03\x04sliced"):
        boundary = "----makerosTEST"
        body = build_multipart(boundary, "cube.gcode.3mf", data, {"print": print_flag, "path": ""})
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

    def test_rejected_still_returns_201_upload_ok(self):
        # Eligibility rejection is surfaced in the portal, not as an upload error.
        status, body = self._upload("ineligible")
        self.assertEqual(status, 201)
        self.assertTrue(body["done"])


if __name__ == "__main__":
    unittest.main()
