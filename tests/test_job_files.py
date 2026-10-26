import gzip
import os
import threading
from unittest import mock

from makeros_hub import job_files
from makeros_hub.http import TransportError


def _drain():
    while not job_files._queue.empty():
        job_files._queue.get_nowait()


def test_sends_are_copied_only_while_the_site_asks():
    _drain()
    job_files.want(False)
    assert job_files.offer("job-1", "/tmp/x.3mf") is False
    job_files.want(True)
    assert job_files.offer("job-1", "/tmp/x.3mf") is True
    assert job_files.offer("", "/tmp/x.3mf") is False and job_files.offer(None, "/tmp/x.3mf") is False
    assert job_files._queue.get_nowait() == ("job-1", "/tmp/x.3mf")
    job_files.want(False)


def test_upload_outcomes(tmp_path):
    f = tmp_path / "plate.3mf"
    f.write_bytes(b"PK\x03\x04plate")
    seen = []

    def poster(status):
        def p(url, data, bearer=None, timeout=None, content_encoding=None, content_length=None):
            assert content_encoding == "gzip"
            compressed = data.read(content_length)
            assert len(compressed) == content_length
            seen.append((url, gzip.decompress(compressed), bearer))
            if status == "net":
                raise TransportError("down")
            return status
        return p

    assert job_files.upload_one("u", str(f), "tok", poster=poster(204)) is True
    assert seen[-1] == ("u", b"PK\x03\x04plate", "tok")
    assert job_files.upload_one("u", str(f), "tok", poster=poster(500)) is False      # try again
    assert job_files.upload_one("u", str(f), "tok", poster=poster("net")) is False    # try again
    assert job_files.upload_one("u", str(f), "tok", poster=poster(409)) is True       # different bytes: never retry
    assert job_files.upload_one("u", str(f), "tok", poster=poster(404)) is True       # not ours any more
    assert job_files.upload_one("u", str(tmp_path / "gone.3mf"), "tok", poster=poster(204)) is True   # printed and cleaned


def test_the_worker_retries_then_stops(tmp_path):
    _drain()
    f = tmp_path / "plate.3mf"
    f.write_bytes(b"PK\x03\x04")
    calls = []
    stop = threading.Event()

    def poster(url, data, bearer=None, timeout=None, **kwargs):
        calls.append(url)
        if len(calls) < 2:
            return 503
        stop.set()
        return 204

    job_files.want(True)
    job_files.offer("job-9", str(f))
    job_files.run(lambda j: f"https://site/api/print/hub/job-file/{j}", "tok", stop, poster=poster, pause=lambda s: None)
    assert calls == ["https://site/api/print/hub/job-file/job-9"] * 2
    job_files.want(False)


def test_the_copy_limit_applies_to_compressed_bytes_and_cleans_staging(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    path = spool / "tree.gcode"
    monkeypatch.setattr(job_files, "MAX_BYTES", 80)
    path.write_bytes(b"G1\n" * 10000)
    posted = mock.Mock(return_value=204)
    assert job_files.upload_one("u", str(path), "tok", poster=posted)
    assert posted.call_args.kwargs["content_length"] <= 80
    posted.reset_mock()
    path.write_bytes(os.urandom(30000))
    assert job_files.upload_one("u", str(path), "tok", poster=posted)
    posted.assert_not_called()
    assert list(spool.iterdir()) == [path]
    monkeypatch.setattr(job_files, "MAX_RAW_FILE_BYTES", 10)
    assert job_files.upload_one("u", str(path), "tok", poster=posted)
    posted.assert_not_called()


def test_compression_disk_failure_is_retried(tmp_path, monkeypatch):
    path = tmp_path / "tree.gcode"
    path.write_bytes(b"G1\n")
    with mock.patch.object(job_files.tempfile, "TemporaryFile", side_effect=OSError("disk full")):
        assert job_files.upload_one("u", str(path), "tok") is False
