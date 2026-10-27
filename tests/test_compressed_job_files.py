"""Raw OrcaSlicer -> ingest -> gzip site copy -> another hub -> plain Moonraker, with bounded memory."""

import gzip
import hashlib
import io
import json
import queue
import threading
import tracemalloc
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from makeros_hub import config, http, job_files
from makeros_hub.agent import heartbeat_payload, make_cloud_submit
from makeros_hub.ingest import IngestServer
from makeros_hub.multipart import boundary_from_content_type, stream_multipart
from makeros_hub.printers import klipper, manager as manager_module
from makeros_hub.printers.manager import PrinterManager
from test_gcode_meta import sv08_gcode

JOB = "0123abcd-0000-4000-8000-000000000001"
GCODE = sv08_gcode(model="PS Giga").replace("; CONFIG_BLOCK_START", "; CONFIG_BLOCK_START\n; nozzle_diameter = 0.4").encode()


def fingerprint(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(256 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Small HTTP fakes; even the fake printer writes to disk instead of buffering uploads."""
    site = SimpleNamespace(submit=None, encoding="gzip", uploads=[], starts=[], stored=tmp_path / "site.gz",
                           printed=tmp_path / "printed.gcode")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def chunks(self):
            remaining = int(self.headers["Content-Length"])
            while remaining:
                chunk = self.rfile.read(min(remaining, 256 * 1024))
                assert chunk
                remaining -= len(chunk)
                yield chunk

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/print/hub/submit":
                site.submit = json.loads(b"".join(self.chunks()))
                return self.reply(200, {"ok": True, "jobId": JOB})
            if path.startswith("/api/print/hub/job-file/"):
                assert self.headers["Content-Encoding"] == "gzip"
                assert 0 < int(self.headers["Content-Length"]) <= job_files.MAX_BYTES
                assert self.headers.get("Transfer-Encoding") is None
                with site.stored.open("wb") as out:
                    for chunk in self.chunks():
                        out.write(chunk)
                with gzip.open(site.stored, "rb") as raw:
                    sha, size = fingerprint(raw)
                assert (sha, size) == (site.submit["fileSha256"], site.submit["fileSizeBytes"])
                return self.reply(204, {})
            if path == "/server/files/upload":
                assert self.headers.get("Content-Encoding") is None
                with site.printed.open("wb") as out:
                    name, fields, sha, size = stream_multipart(self.chunks(), boundary_from_content_type(
                        self.headers["Content-Type"]), out, http.MAX_RAW_FILE_BYTES)
                site.uploads.append((sha, size))
                assert fields["checksum"] == sha and "print" not in fields
                return self.reply(201, {"item": {"path": fields["path"] + "/" + name}})
            if path == "/printer/print/start":
                site.starts.append(self.path)
                return self.reply(200, {"result": "ok"})
            raise AssertionError(path)

        def do_GET(self):
            if self.path.startswith("/api/print/hub/file/"):
                assert self.headers.get("Accept-Encoding") == "gzip"
                self.send_response(200)
                if site.encoding:
                    self.send_header("Content-Encoding", site.encoding)
                self.send_header("Content-Length", str(site.stored.stat().st_size))
                self.end_headers()
                with site.stored.open("rb") as src:
                    while chunk := src.read(256 * 1024):
                        self.wfile.write(chunk)
                return
            if self.path.startswith("/server/files/directory"):
                return self.reply(200, {"result": {"dirs": [], "files": []}})
            if self.path.startswith("/printer/objects/query"):
                return self.reply(200, {"result": {"status": {
                    "configfile": {"settings": {"extruder": {"nozzle_diameter": 0.4}}},
                    "webhooks": {"state": "ready"}, "print_stats": {"state": "standby"},
                    "idle_timeout": {"state": "Ready"}}}})
            raise AssertionError(self.path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(klipper, "QUEUE_PROGRESS_DIR", tmp_path / "progress")
    monkeypatch.setattr(klipper, "TERMINAL_JOBS_DIR", tmp_path / "terminal")
    monkeypatch.setattr(manager_module, "DISPATCHED_STATE_PATH", tmp_path / "dispatched.json")
    site.manager = PrinterManager()
    site.manager._adapters["giga"] = klipper.KlipperAdapter("giga", site.url, model="Elegoo OrangeStorm Giga")
    site.spool = tmp_path / "fetch-spool"
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()


def fetch_and_dispatch(site, sha, size, uid="a" * 32):
    fetch = {"queueJobId": JOB, "submissionUid": uid, "fileName": "tree.gcode", "sha256": sha,
             "sizeBytes": size, "path": "/api/print/hub/file/" + JOB}
    reports = []
    done = site.manager.fetch_uploads([fetch], site.spool,
        getter=lambda path, dest: http.get_to_file(site.url + path, dest, max_bytes=http.MAX_RAW_FILE_BYTES),
        reporter=lambda body: (reports.append(body), http.Response(200, {"ok": True}))[1])
    dispatched = site.manager.dispatch_assignments([{**fetch, "printerId": "giga"}], site.spool)
    return done, reports, dispatched


def test_more_than_64_mib_goes_from_orca_to_another_hub_and_plain_moonraker(rig, tmp_path, monkeypatch):
    source = tmp_path / "tree.gcode"
    block = b"G1 X100 Y100 E0.005\n" * 4096
    with source.open("wb") as out:
        while out.tell() <= 65 * 1024 * 1024:
            out.write(block)
        out.write(GCODE)
    with source.open("rb") as src:
        expected = fingerprint(src)
    assert expected[1] > job_files.MAX_BYTES
    cfg = config.Config(cloud_url=rig.url)
    assert cfg.max_upload_mb == 1024
    assert heartbeat_payload()["agentVersion"] == "0.86.0"
    monkeypatch.setattr(job_files, "_queue", queue.Queue())
    monkeypatch.setattr(job_files, "_wanted", threading.Event())
    job_files.want(True)
    ingest = IngestServer(make_cloud_submit(cfg, "test-token"), port=0,
                          spool_dir=tmp_path / "ingest", max_bytes=cfg.max_upload_mb * 1024 * 1024)
    ingest.start()
    head = b'--TREE\r\nContent-Disposition: form-data; name="file"; filename="tree.gcode"\r\n\r\n'
    tail = b'\r\n--TREE\r\nContent-Disposition: form-data; name="print"\r\n\r\ntrue\r\n--TREE--\r\n'

    def body():
        yield head
        with source.open("rb") as src:
            yield from iter(lambda: src.read(256 * 1024), b"")
        yield tail

    tracemalloc.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{ingest._server.server_port}/api/files/local",
            data=body(), headers={"Content-Type": "multipart/form-data; boundary=TREE", "X-Api-Key": "test-member",
                                  "Content-Length": str(len(head) + expected[1] + len(tail))})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 201
        assert (rig.submit["fileSha256"], rig.submit["fileSizeBytes"]) == expected
        assert rig.submit["printerModel"] == "PS Giga"
        assert rig.submit["requiredFilaments"][0]["nozzleDiameterMm"] == 0.4
        job, spooled = job_files._queue.get_nowait()
        assert job == JOB
        assert job_files.upload_one(cfg.job_file_url(job), spooled, "test-token")
        assert rig.stored.stat().st_size < job_files.MAX_BYTES
        done, reports, dispatched = fetch_and_dispatch(rig, *expected, uid=rig.submit["hubSubmissionUid"])
        assert done == 1 and reports[0]["sha256"] == expected[0]
        assert dispatched == [{"queueJobId": JOB, "state": "uploading"}]
        assert rig.uploads == [expected] and len(rig.starts) == 1
        # Includes intake, raw metadata, compression, gunzip, verification, and the actual Klipper upload transport.
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 24 * 1024 * 1024, f"transfer buffered {peak} bytes"
    finally:
        tracemalloc.stop()
        ingest.stop()


def test_uncompressed_site_file_still_prints(rig):
    rig.encoding = None
    rig.stored.write_bytes(GCODE)
    expected = hashlib.sha256(GCODE).hexdigest(), len(GCODE)
    done, _, dispatched = fetch_and_dispatch(rig, *expected)
    assert done == 1 and dispatched == [{"queueJobId": JOB, "state": "uploading"}]
    assert rig.uploads == [expected] and rig.printed.read_bytes() == GCODE


@pytest.mark.parametrize("failure", ["truncated", "crc", "not_gzip", "hash", "size"])
def test_corrupt_or_mismatched_files_never_reach_moonraker(rig, failure):
    compressed = gzip.compress(GCODE)
    sha, size = hashlib.sha256(GCODE).hexdigest(), len(GCODE)
    if failure == "truncated":
        compressed = compressed[:-5]
    elif failure == "crc":
        compressed = compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:]
    elif failure == "not_gzip":
        compressed = GCODE
    elif failure == "hash":
        sha = "0" * 64
    else:
        size += 1
    rig.stored.write_bytes(compressed)
    done, _, dispatched = fetch_and_dispatch(rig, sha, size)
    assert done == 0 and dispatched == [{"queueJobId": JOB, "state": "held", "reason": "file_not_found"}]
    assert rig.uploads == rig.starts == []
    assert list(rig.spool.rglob("*.tmp")) == []


def test_gunzip_caps_raw_size_not_compressed_size(tmp_path, monkeypatch):
    compressed = gzip.compress(b"G1\n" * 10000)
    response = io.BytesIO(compressed)
    response.status, response.headers = 200, {"Content-Encoding": "gzip"}
    monkeypatch.setattr(http._OPENER, "open", lambda *args, **kwargs: response)
    with pytest.raises(http.TransportError, match="body exceeds 1024 bytes"):
        http.get_to_file("https://site/file", tmp_path / "f", max_bytes=1024)
    assert (tmp_path / "f").stat().st_size <= 1024


def test_raw_upload_config_override_survives(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://site"\nmax_upload_mb = 77\n')
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv("MAKEROS_HUB_MAX_UPLOAD_MB", raising=False)
    assert config.load_config().max_upload_mb == 77
    monkeypatch.setenv("MAKEROS_HUB_MAX_UPLOAD_MB", "88")
    assert config.load_config().max_upload_mb == 88
