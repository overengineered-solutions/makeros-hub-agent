import threading

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
        def p(url, data, bearer=None, timeout=None):
            seen.append((url, data, bearer))
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

    def poster(url, data, bearer=None, timeout=None):
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
