"""Optional previews never gate a fetched slice, and their bounded retry survives a process restart."""
import io
import json
import logging
import shutil
import subprocess
import threading
import zipfile

import pytest

from makeros_hub import http, toolpath, toolpaths
from makeros_hub.config import Config
from makeros_hub.printers import manager as manager_mod
from tests.test_toolpath import PREFIX, decode
from tests import test_v072
from tests.test_v072 import fetch


def pending(tmp_path, *, accepted=True, code=PREFIX + b"G1 X1 E1\n"):
    sliced = tmp_path / "abcdef12" / "sliced.3mf"
    sliced.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(sliced, "w") as archive:
        archive.writestr("Metadata/plate_2.gcode", code)
    sha = toolpath.sliced_sha256(sliced)
    path = toolpaths.prepare("job-1", sliced, 2, sha)
    if accepted:
        toolpaths.accept(path, now=100)
    return path, sliced


class Upload:
    def __init__(self, status=204, body=None):
        self.response = http.Response(status, body or {})
        self.calls = []

    def __call__(self, url, data, **kwargs):
        self.calls.append((url, data.read(), kwargs))
        return self.response


def builder(sliced, plate, sha, output):
    assert toolpath.sliced_sha256(sliced) == sha
    toolpath.build(sliced, plate, output)


def process(path, upload, now=100, build=builder):
    toolpaths.process(path, lambda job: f"https://site.invalid/api/print/hub/toolpath/{job}", "test-only-key",
                      poster=upload, now=now, builder=build)


def test_upload_headers_and_gzip_match_the_accepted_slice(tmp_path):
    path, sliced = pending(tmp_path)
    original = sliced.read_bytes()
    upload = Upload()
    process(path, upload)
    assert toolpaths.read(path)["state"] == "stored"
    [(url, body, headers)] = upload.calls
    assert url == "https://site.invalid/api/print/hub/toolpath/job-1"
    assert headers == {"bearer": "test-only-key", "timeout": 30.0, "content_type": "application/vnd.ps.toolpath",
                       "content_encoding": "gzip", "content_length": len(body), "response_body": True,
                       "extra_headers": {"X-PS-Sliced-Sha256": toolpath.sliced_sha256(sliced), "X-PS-Plate": "2"}}
    assert decode(body)["points"] == 2 and sliced.read_bytes() == original
    assert not path.with_suffix(".gz").exists()
    process(path, upload, now=1000)
    assert len(upload.calls) == 1


@pytest.mark.parametrize("status,body,state", [(204, {}, "stored"), (404, {}, "stopped"), (409, {"error": "slice_mismatch"}, "stopped"),
                                              (400, {}, "failed"), (413, {}, "failed"), (401, {}, "failed"), (302, {}, "failed"),
                                              (409, {"error": "unexpected"}, "failed")])
def test_terminal_answers_stop_once_even_after_restart(tmp_path, caplog, status, body, state):
    path, _ = pending(tmp_path)
    upload = Upload(status, body)
    process(path, upload)
    assert toolpaths.read(path)["state"] == state
    toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "test-only-key", now=500, poster=upload, builder=builder)
    assert len(upload.calls) == 1
    if status == 404:
        assert not caplog.records
    if status in (400, 413):
        assert f"upload refused ({status})" in caplog.text


def test_not_ready_backoff_survives_restart_without_regenerating(tmp_path):
    path, _ = pending(tmp_path)
    upload = Upload(409, {"error": "not_ready"})
    process(path, upload)
    assert path.with_suffix(".gz").exists()
    assert toolpaths.read(path)["next"] == 110
    def never_build(*args):
        pytest.fail("persisted packet regenerated after restart")
    # A fresh sweep has no in-memory queue or remembered backoff to help it.
    toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "key", now=109, poster=upload, builder=never_build)
    assert len(upload.calls) == 1
    toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "key", now=110, poster=upload, builder=never_build)
    assert len(upload.calls) == 2 and toolpaths.read(path)["next"] == 130
    upload.response = http.Response(204, {})
    process(path, upload, now=130, build=never_build)
    assert len(upload.calls) == 3 and toolpaths.read(path)["state"] == "stored"


def test_not_ready_expires_after_an_hour_and_cannot_be_rearmed(tmp_path, caplog):
    path, sliced = pending(tmp_path)
    upload = Upload(409, {"error": "not_ready"})
    for now in range(100, 3700, 300):
        process(path, upload, now=now)
    assert max(toolpaths.read(path)["next"] - now, 0) <= 300
    calls = len(upload.calls)
    toolpaths.accept(toolpaths.prepare("job-1", sliced, 2, toolpath.sliced_sha256(sliced)), now=3699)
    process(path, upload, now=3700)
    assert len(upload.calls) == calls and toolpaths.read(path)["state"] == "failed"
    assert "upload retry expired" in caplog.text


def test_generation_cannot_push_a_new_upload_past_the_retry_deadline(tmp_path, monkeypatch):
    path, _ = pending(tmp_path)
    clock = [0]
    monkeypatch.setattr(toolpaths.time, "monotonic", lambda: clock[0])
    def slow(*args):
        args[-1].write_bytes(b"packet")
        clock[0] = 3601
    upload = Upload()
    process(path, upload, build=slow)
    assert not upload.calls and toolpaths.read(path)["state"] == "failed"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_site_failure_retries(tmp_path, status):
    path, _ = pending(tmp_path)
    upload = Upload(status)
    process(path, upload)
    assert toolpaths.read(path)["state"] == "pending"
    process(path, upload, now=110)
    assert len(upload.calls) == 2


def test_connection_failure_reserves_its_backoff_before_posting(tmp_path):
    path, _ = pending(tmp_path)
    def offline(*args, **kwargs):
        assert toolpaths.read(path)["next"] == 110
        raise http.TransportError("do not log key or arbitrary response contents")
    process(path, offline)
    assert toolpaths.read(path)["state"] == "pending"
    process(path, Upload(), now=110)
    assert toolpaths.read(path)["state"] == "stored"


def test_dormant_request_never_uploads_before_report_acceptance(tmp_path):
    path, _ = pending(tmp_path, accepted=False)
    upload = Upload()
    process(path, upload)
    assert not upload.calls and not path.with_suffix(".gz").exists()
    toolpaths.accept(path, now=100)
    process(path, upload)
    assert len(upload.calls) == 1


def test_cleanup_with_deleted_spool_and_changed_slice(tmp_path):
    path, sliced = pending(tmp_path)
    upload = Upload(409, {"error": "not_ready"})
    process(path, upload)
    sliced.write_bytes(b"replacement slice")
    process(path, upload, now=110)
    assert toolpaths.read(path)["state"] == "stopped" and len(upload.calls) == 1
    sliced.unlink()
    process(path, upload, now=120)
    assert not list(path.parent.glob(".toolpath-*"))
    path, sliced = pending(tmp_path)
    process(path, upload)
    shutil.rmtree(sliced.parent)
    toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "key", poster=upload, now=500)
    assert not sliced.parent.exists()


def test_new_assignment_reuploads_identical_bytes_after_site_invalidation(tmp_path):
    path, sliced = pending(tmp_path)
    upload = Upload()
    process(path, upload)
    replacement = toolpaths.prepare("job-1", sliced, 2, toolpath.sliced_sha256(sliced), assignment_seq=2)
    assert replacement != path
    toolpaths.accept(replacement, now=200)
    process(replacement, upload, now=200)
    assert toolpaths.read(replacement)["state"] == "stored" and len(upload.calls) == 2
    assert upload.calls[0][1] == upload.calls[1][1]


@pytest.mark.parametrize("failure", [toolpaths.PreviewFailed("failed", 17), toolpaths.PreviewFailed("too_large", 23),
                                     subprocess.TimeoutExpired("preview", 180), MemoryError(), OSError("private file name")])
def test_preview_failure_is_contained_and_diagnosable(tmp_path, caplog, failure):
    path, sliced = pending(tmp_path)
    original = sliced.read_bytes()
    def fail(*args):
        args[-1].write_bytes(b"partial preview")
        raise failure
    upload = Upload()
    process(path, upload, build=fail)
    reason = failure.reason if isinstance(failure, toolpaths.PreviewFailed) else "failed"
    assert toolpaths.read(path)["state"] == "stored" and toolpaths.read(path)["unavailable"] == reason
    assert sliced.read_bytes() == original and "use the slice pictures" in caplog.text
    [(url, body, headers)] = upload.calls
    assert url == "https://site.invalid/api/print/hub/toolpath/job-1" and body == b""
    assert headers == {"bearer": "test-only-key", "timeout": 30.0, "content_type": "application/vnd.ps.toolpath",
                       "content_encoding": None, "content_length": 0, "response_body": True,
                       "extra_headers": {"X-PS-Sliced-Sha256": toolpath.sliced_sha256(sliced), "X-PS-Plate": "2",
                                         "X-PS-Toolpath-Unavailable": reason}}
    assert not path.with_suffix(".tmp").exists() and not path.with_suffix(".gz").exists()
    assert "private file name" not in caplog.text
    if isinstance(failure, toolpaths.PreviewFailed):
        assert f"line {failure.line}" in caplog.text


@pytest.mark.parametrize("reason", ["failed", "too_large"])
def test_unavailable_reason_is_durable_before_post_and_retried_after_restart(tmp_path, reason):
    path, _ = pending(tmp_path)
    def fail(*args):
        raise toolpaths.PreviewFailed(reason, 7)
    def offline(*args, **kwargs):
        state = toolpaths.read(path)
        assert state["state"] == "pending" and state["unavailable"] == reason and state["next"] == 110
        raise http.TransportError("offline")
    process(path, offline, build=fail)
    upload = Upload(409, {"error": "not_ready"})
    def never_build(*args):
        pytest.fail("an unavailable preview was regenerated")
    for now, calls in ((109, 0), (110, 1), (129, 1), (130, 2)):
        toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "key", now=now, poster=upload, builder=never_build)
        assert len(upload.calls) == calls
    assert toolpaths.read(path)["next"] == 170 and toolpaths.read(path)["expires"] == 3700
    upload.response = http.Response(204, {})
    process(path, upload, now=170, build=never_build)
    assert toolpaths.read(path)["state"] == "stored"
    process(path, upload, now=1000, build=never_build)
    assert len(upload.calls) == 3
    assert all(body == b"" and headers["content_length"] == 0 and headers["extra_headers"]["X-PS-Toolpath-Unavailable"] == reason
               for _, body, headers in upload.calls)


@pytest.mark.parametrize("status,body,terminal", [(404, {}, "stopped"), (409, {"error": "slice_mismatch"}, "stopped"),
                                                (400, {}, "failed"), (413, {}, "failed"), (500, {}, None), (429, {}, None)])
def test_unavailable_reports_use_the_upload_response_rules(tmp_path, caplog, status, body, terminal):
    path, _ = pending(tmp_path)
    upload = Upload(status, body)
    def fail(*args):
        raise toolpaths.PreviewFailed("failed", 7)
    process(path, upload, build=fail)
    caplog.clear()
    process(path, upload, now=110, build=lambda *a: pytest.fail("regenerated failed preview"))
    assert len(upload.calls) == (1 if terminal else 2)
    assert toolpaths.read(path)["state"] == (terminal or "pending")
    if status == 404:
        assert not caplog.records


def test_unavailable_report_expires_and_cleans_up_with_the_spool(tmp_path):
    path, sliced = pending(tmp_path)
    upload = Upload(409, {"error": "not_ready"})
    def fail(*args):
        raise toolpaths.PreviewFailed("too_large", 7)
    process(path, upload, build=fail)
    process(path, upload, now=3700)
    assert toolpaths.read(path)["state"] == "failed" and len(upload.calls) == 1
    sliced.unlink()
    process(path, upload, now=4000)
    assert not list(path.parent.glob(".toolpath-*"))


def test_oversized_packet_is_reported_unavailable(tmp_path, monkeypatch):
    path, _ = pending(tmp_path)
    monkeypatch.setattr(toolpaths, "MAX_BYTES", 1)
    upload = Upload()
    process(path, upload)
    assert toolpaths.read(path)["state"] == "stored"
    assert upload.calls[0][1] == b"" and upload.calls[0][2]["extra_headers"]["X-PS-Toolpath-Unavailable"] == "too_large"


def test_unavailable_report_cannot_describe_a_replacement_slice(tmp_path):
    path, sliced = pending(tmp_path)
    upload = Upload()
    def replaced(*args):
        sliced.write_bytes(b"new slice")
        raise toolpaths.PreviewFailed("failed", 7)
    process(path, upload, build=replaced)
    assert not upload.calls and toolpaths.read(path)["state"] == "stopped"


def test_killed_generator_reports_unavailable_without_leaking_stderr(tmp_path, monkeypatch, caplog):
    path, _ = pending(tmp_path)
    monkeypatch.setattr(toolpaths.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, -9, b"", b"private file name"))
    upload = Upload()
    process(path, upload, build=toolpaths.generate)
    assert toolpaths.read(path)["state"] == "stored"
    assert upload.calls[0][2]["extra_headers"]["X-PS-Toolpath-Unavailable"] == "failed"
    assert "private file name" not in caplog.text


def test_preview_failure_enters_the_heartbeats_existing_diagnostic_log_ring(tmp_path):
    from makeros_hub.diagnostics import LogRingHandler
    ring = LogRingHandler()
    root = logging.getLogger()
    path, _ = pending(tmp_path)
    def fail(*args):
        raise toolpaths.PreviewFailed("failed", 17)
    root.addHandler(ring)
    try:
        process(path, Upload(), build=fail)
    finally:
        root.removeHandler(ring)
    assert any(entry["name"] == "makeros-hub.toolpaths" and "line 17" in entry["message"] and "job-1" in entry["message"]
               for entry in ring.snapshot())


def test_real_child_builds_the_selected_plate_and_reports_bad_motion_line(tmp_path):
    path, sliced = pending(tmp_path)
    process(path, Upload(), build=toolpaths.generate)
    assert toolpaths.read(path)["state"] == "stored"
    with zipfile.ZipFile(sliced, "w") as archive:
        archive.writestr("Metadata/plate_2.gcode", PREFIX + b"G1 Xnan\n")
    with pytest.raises(toolpaths.PreviewFailed) as error:
        toolpaths.generate(sliced, 2, toolpath.sliced_sha256(sliced), path.with_suffix(".tmp"))
    assert error.value.reason == "failed" and error.value.line == 7
    path = toolpaths.prepare("job-1", sliced, 2, toolpath.sliced_sha256(sliced))
    toolpaths.accept(path, now=100)
    upload = Upload()
    process(path, upload, build=toolpaths.generate)
    assert toolpaths.read(path)["state"] == "stored"
    assert upload.calls[0][1] == b"" and upload.calls[0][2]["extra_headers"]["X-PS-Toolpath-Unavailable"] == "failed"


def test_child_launch_is_bounded_and_low_priority_is_only_in_the_child(monkeypatch, tmp_path):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(toolpaths.subprocess, "run", run)
    toolpaths.generate(tmp_path / "slice", 1, "a" * 64, tmp_path / "out")
    assert calls[0][0][1:3] == ["-m", "makeros_hub.toolpaths"]
    assert calls[0][1]["timeout"] == toolpath.DEADLINE_SECONDS + 5
    assert "preexec_fn" not in calls[0][1]  # the multithreaded hub must not run Python after fork


def test_worker_runs_off_the_callers_thread(monkeypatch, tmp_path):
    stop, entered = threading.Event(), threading.Event()
    callers = []
    def sweep(*args):
        callers.append(threading.get_ident())
        entered.set()
        stop.wait(2)
    monkeypatch.setattr(toolpaths, "sweep", sweep)
    worker = toolpaths.start(Config(cloud_url="https://site.invalid"), "key", stop, tmp_path)
    assert entered.wait(2) and callers != [threading.get_ident()] and worker.daemon
    stop.set()
    worker.join(2)
    assert not worker.is_alive()


def test_worker_start_and_scan_failures_are_optional(monkeypatch, tmp_path, caplog):
    stop = threading.Event()
    def failed_scan(*args):
        stop.set()
        raise OSError("private path")
    monkeypatch.setattr(toolpaths, "sweep", failed_scan)
    worker = toolpaths.start(Config(cloud_url="https://site.invalid"), "key", stop, tmp_path)
    worker.join(2)
    assert "toolpath worker failed" in caplog.text and "private path" not in caplog.text
    def failed_start(*args):
        raise RuntimeError("no more threads")
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    assert toolpaths.start(Config(cloud_url="https://site.invalid"), "key", stop, tmp_path) is None
    assert "worker could not start" in caplog.text


@pytest.mark.parametrize("change", [{"file": "../other-member.3mf"}, {"file": "/private"}, {"plate": 0}, {"plate": True},
                                   {"sha256": "bad"}, {"jobId": "../other"}, {"accepted": 1}, {"expires": float("nan")},
                                   {"attempt": -1}, {"state": "anything"}, {"file": []}, {"jobId": 3}, {"assignmentSeq": True},
                                   {"unavailable": "other"}, {"unavailable": []}, {"unavailable": None}])
def test_durable_state_is_validated_before_file_or_network_access(tmp_path, change, caplog):
    path, _ = pending(tmp_path)
    state = json.loads(path.read_text())
    path.write_text(json.dumps({**state, **change}))
    upload = Upload()
    toolpaths.sweep(tmp_path, lambda job: "https://site.invalid", "key", now=100, poster=upload)
    assert not upload.calls and "unreadable pending preview" in caplog.text


def test_fetched_report_hash_and_ack_arm_only_the_matching_preview(tmp_path):
    harness = test_v072.TestSliceUploads()
    harness.setUp()
    manager = manager_mod.PrinterManager()
    ack = False
    def report(body):
        path = next(tmp_path.glob("*/.toolpath-*.json"))
        assert not toolpaths.read(path)["accepted"]
        sliced = tmp_path / fetch()["submissionUid"] / fetch()["fileName"]
        assert body["slice"]["slicedSha256"] == toolpath.sliced_sha256(sliced)
        assert body["sha256"] == fetch()["sha256"]
        return http.Response(200, {"ok": ack})
    kwargs = {"getter": harness.getter(), "reporter": report, "slice_runner": harness.runner(tmp_path, {"ok": True})}
    assert manager.fetch_uploads([fetch(assignmentSeq=3)], tmp_path, **kwargs) == 0
    path = next(tmp_path.glob("*/.toolpath-*.json"))
    assert not toolpaths.read(path)["accepted"]
    ack = True
    assert manager.fetch_uploads([fetch(assignmentSeq=3)], tmp_path, **kwargs) == 1
    assert toolpaths.read(path)["accepted"]
    assert toolpaths.read(path)["assignmentSeq"] == 3
    assert len(harness.runs) == 1


def test_optional_queue_storage_failure_never_fails_the_fetched_slice(tmp_path, monkeypatch, caplog):
    harness = test_v072.TestSliceUploads()
    harness.setUp()
    monkeypatch.setattr(toolpaths, "write_json_durably", lambda *a: False)
    assert manager_mod.PrinterManager().fetch_uploads([fetch()], tmp_path, getter=harness.getter(), reporter=harness.reporter,
                                                     slice_runner=harness.runner(tmp_path, {"ok": True})) == 1
    assert harness.reports[-1]["slice"]["slicedSha256"] and "could not queue preview" in caplog.text


def test_split_manifest_gets_the_staged_hash_without_claiming_one_pieces_picture(tmp_path, monkeypatch):
    _, sliced = pending(tmp_path)
    monkeypatch.setattr(manager_mod, "plate_picture", lambda *args: pytest.fail("split manifest has no single plate picture"))
    body = {"slice": {"pieces": [{"index": 0}, {"index": 1}]}}
    manager_mod._add_plate_facts(body, sliced, 2)
    assert body["slice"]["slicedSha256"] == toolpath.sliced_sha256(sliced)


def test_post_bytes_keeps_old_status_contract_and_can_read_conflict_details(monkeypatch):
    import urllib.error
    seen = []
    def open_request(request, timeout):
        seen.append(request)
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error":"not_ready"}'))
    monkeypatch.setattr(http._OPENER, "open", open_request)
    assert http.post_bytes("https://site.invalid", b"old") == 409
    response = http.post_bytes("https://site.invalid", b"new", bearer="key", response_body=True,
                               extra_headers={"X-PS-Plate": "2", "X-PS-Sliced-Sha256": "a" * 64},
                               content_type="application/vnd.ps.toolpath", content_encoding="gzip")
    assert response == http.Response(409, {"error": "not_ready"})
    assert seen[-1].get_header("X-ps-plate") == "2" and seen[-1].get_header("Content-encoding") == "gzip"
    assert seen[-1].get_header("Authorization") == "Bearer key"
    http.post_bytes("https://site.invalid", io.BytesIO(), bearer="key", response_body=True, content_length=0,
                    extra_headers={"X-PS-Plate": "2", "X-PS-Sliced-Sha256": "a" * 64, "X-PS-Toolpath-Unavailable": "failed"},
                    content_type="application/vnd.ps.toolpath")
    assert seen[-1].get_header("Content-length") == "0" and seen[-1].data.read() == b""
    assert seen[-1].get_header("X-ps-toolpath-unavailable") == "failed" and not seen[-1].has_header("Content-encoding")
