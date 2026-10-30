"""Local door sockets and an injected site transport; no live services."""
import base64
from datetime import datetime
from functools import partial
import hashlib
import io
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from makeros_hub.moonraker import STAGE_TTL, NOTICE_TTL
import zipfile

import pytest

from makeros_hub import agent, http, moonraker
from makeros_hub.config import Config
from makeros_hub.http import Response, TransportError
from makeros_hub.ingest import IngestServer
import test_moonraker
from test_agent import _captured_job, _FakeVPrinterOutbox
from test_moonraker import door, gcode, threemf
from test_plate_facts import png


def eventually(check):
    until = time.monotonic() + 1
    while not check():
        assert time.monotonic() < until, "local worker did not finish within one second"
        time.sleep(.005)


class Site:
    def __init__(self):
        self.calls, self.uploads, self.jobs = [], {}, {}
        self.mode = "ok"
        self.entered, self.release = threading.Event(), threading.Event()
        self.copies, self.puts = {}, []
        self.copy_mode = "ok"
        self.copy_entered, self.copy_release = threading.Event(), threading.Event()

    def open(self, request, timeout):
        assert request.get_method() == "PUT"
        assert request.get_header("Authorization") == "Bearer synthetic-hub"
        assert request.get_header("Content-type") == "application/octet-stream"
        data = request.data.read()
        assert request.get_header("Content-length") == str(len(data))
        uid = request.full_url.split("/")[-2].removeprefix("upload-")
        assert request.full_url == f"https://site.invalid/api/print/hub/vp-staged/upload-{uid}/file"
        assert hashlib.sha256(data).hexdigest() == self.uploads[uid]["fileSha256"]
        self.puts.append((uid, data))
        self.copy_entered.set()
        if self.copy_mode == "blocked":
            assert self.copy_release.wait(5)
        if self.copy_mode == "down":
            raise TransportError("synthetic-hub alice must never reach the log")
        if isinstance(self.copy_mode, int):
            response = io.BytesIO(b'{}')
            response.status = self.copy_mode
            return response
        self.copies.setdefault(uid, data)
        if self.copy_mode == "lost_reply":
            raise TransportError("synthetic lost file reply")
        response = io.BytesIO(b'{"ok":true}')
        response.status = 200
        return response

    def post(self, url, body, **kwargs):
        self.calls.append((url, body.copy(), kwargs))
        uid = body.get("submissionUid", body.get("hubSubmissionUid"))
        if url.endswith("/vp-staged"):
            self.entered.set()
            if self.mode == "blocked":
                assert self.release.wait(5)
            if self.mode == "down":
                raise TransportError("synthetic member token: " + body["memberToken"])
            if isinstance(self.mode, Response):
                return self.mode
            self.uploads.setdefault(uid, body.copy())
            if self.mode == "lost_reply":
                raise TransportError("synthetic reply lost")
            return Response(200, {"ok": True, "uploadId": "upload-" + uid})
        assert url.endswith("/submit")
        self.jobs.setdefault(uid, {"ok": True, "jobId": "job-" + uid,
                                  "submissionProtocol": moonraker.PROTOCOL,
                                  "reason": "Waiting for the requested build plate."})
        return Response(200, self.jobs[uid])


@pytest.fixture
def site(door, monkeypatch):
    site = Site()
    monkeypatch.setattr(agent, "post_json", site.post)
    monkeypatch.setattr(http._OPENER, "open", site.open)
    cfg = Config(cloud_url="https://site.invalid")
    monkeypatch.setattr(test_moonraker, "IngestServer", partial(IngestServer,
                        moonraker_staged=agent.make_staged_notice(cfg, "synthetic-hub"),
                        moonraker_copy=agent.make_staged_file_copy(cfg, "synthetic-hub")))
    monkeypatch.setattr(door, "submit", agent.make_cloud_submit(cfg, "synthetic-hub"))
    door.restart()
    yield site
    site.release.set()
    site.copy_release.set()
    eventually(lambda: not door.store._notifying)


def row_for(door, path):
    with door.store.locked() as db:
        return db.execute("SELECT * FROM uploads WHERE uid=?", (path.split("/")[0],)).fetchone()


def retry(door):
    door.store._last_clean = 0
    door.server._server.service_actions()


@pytest.mark.parametrize("pool", ["sv08-max", "p2s"])
def test_notice_immediately_after_reply_has_durable_facts_and_selected_picture(door, site, monkeypatch, pool):
    data, name = gcode(), "part.gcode"
    if pool == "p2s":
        data = io.BytesIO(threemf(plates=(2,)))
        with zipfile.ZipFile(data, "a") as archive:
            archive.writestr("Metadata/plate_2.png", png())
            archive.writestr("Metadata/plate_1.png", png(2, 2))
        data, name = data.getvalue(), "part.3mf"
        door.server._server.RequestHandlerClass.model_pool = pool
    handler = door.server._server.RequestHandlerClass
    original = handler._json
    replies = []

    def reply(self, status, body):
        assert not site.calls
        # A maintenance tick between staging and the response must not send early.
        retry(door)
        if door.store._retry_worker:
            door.store._retry_worker.join(timeout=1)
        assert not site.calls
        original(self, status, body)
        replies.append(status)

    monkeypatch.setattr(handler, "_json", reply)
    site.mode = "blocked"
    path = door.stage(name=name, data=data)
    assert site.entered.wait(1) and replies == [201]
    assert site.puts == []
    assert site.jobs == {}  # Orca has its reply while the site request is still blocked.
    url, body, options = site.calls[0]
    assert url == "https://site.invalid/api/print/hub/vp-staged"
    assert options == {"bearer": "synthetic-hub", "timeout": 8, "retries": 0}
    assert body["submissionUid"] == path.split("/")[0]
    assert body["memberToken"] == "alice"
    assert body["fileName"] == name and body["fileSizeBytes"] == len(data)
    assert body["fileSha256"] == hashlib.sha256(data).hexdigest()
    assert body["printerModel"] == body["fileFacts"]["printerModel"]
    assert datetime.fromisoformat(body["stagedAt"]).utcoffset().total_seconds() == 0
    if pool == "p2s":
        assert body["fileFacts"]["plate"] == 2
        assert base64.b64decode(body["plateImage"]) == png()
    else:
        assert "plateImage" not in body
    assert row_for(door, path)["notified_at"] is None
    site.release.set()
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    assert row_for(door, path)["notice"] is None
    assert door.store.db_path.stat().st_mode & 0o777 == 0o600
    retry(door)
    assert len(site.calls) == 1


@pytest.mark.parametrize("mode", ["down", "lost_reply"])
def test_failed_notice_survives_restart_expiry_and_retries_once(door, site, caplog, mode):
    site.mode = mode
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    assert "alice" not in caplog.text and "synthetic-hub" not in caplog.text
    assert row_for(door, path)["notified_at"] is None
    retry(door)
    eventually(lambda: len(site.calls) == 2)
    eventually(lambda: not door.store._notifying)
    assert caplog.text.count("staged notice unconfirmed") == 1
    with door.store.locked() as db:   # The pending notice keeps the file past the stage TTL.
        db.execute("UPDATE uploads SET created=?", (time.time() - STAGE_TTL - 60,))
    door.restart()
    assert (door.path / path).exists()
    site.mode = "ok"
    retry(door)
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    assert len(site.uploads) == 1 and site.jobs == {}
    notices = [body for _, body, _ in site.calls]
    assert all(body == notices[0] for body in notices)
    retry(door)
    assert (door.path / path).exists()  # Give it normal retention after delayed acknowledgement.
    assert len(site.calls) == 3
    assert door.start(path)[0] == 200


@pytest.mark.parametrize("response", [Response(500, {}), Response(401, {}), Response(200, {}),
                                      Response(200, {"ok": True, "uploadId": " "}),
                                      Response(200, {"ok": False, "uploadId": "x"})])
def test_invalid_notice_ack_keeps_file_and_retry(door, site, caplog, response):
    site.mode = response
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    assert row_for(door, path)["notified_at"] is None
    assert (door.path / path).exists()
    site.mode = "ok"
    retry(door)
    eventually(lambda: row_for(door, path)["notified_at"] is not None)


def test_same_names_are_member_scoped_and_start_reuses_noticed_uid(door, site):
    first = door.stage()
    second = door.stage(token="bob")
    eventually(lambda: row_for(door, first)["notified_at"] and row_for(door, second)["notified_at"])
    assert first != second and len(site.uploads) == 2
    assert door.start(first, token="bob")[0] == 404
    for path, token in ((first, "alice"), (second, "bob")):
        assert door.start(path, token=token)[0] == 200
        assert door.start(path, token=token)[0] == 200
        uid = path.split("/")[0]
        assert site.uploads[uid]["memberToken"] == token
        assert uid in site.jobs
    assert len(site.jobs) == 2 and len(site.calls) == 4


def test_slow_notice_and_retry_do_not_block_start_or_listener(door, site):
    site.mode = "blocked"
    path = door.stage()
    assert site.entered.wait(1)
    retry(door)
    assert door.request("/server/info")[0] == 200
    assert door.start(path)[0] == 200
    assert len(site.calls) == 2  # One notice in flight, one start; no concurrent duplicate notice.
    site.release.set()
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    assert len(site.uploads) == len(site.jobs) == 1


def test_retry_worker_leaves_listener_and_other_uploads_free(door, site, caplog):
    site.mode = "down"
    first = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    site.mode = "blocked"
    site.entered.clear()
    retry(door)
    assert site.entered.wait(1)
    assert door.request("/server/info")[0] == 200
    second = door.stage(token="bob")
    assert first != second
    site.release.set()
    eventually(lambda: row_for(door, first)["notified_at"] and row_for(door, second)["notified_at"])
    assert len(site.uploads) == 2


def test_lost_local_notice_receipt_retries_without_duplicate_site_record(door, site, caplog):
    door.restart(pool="p2s")
    with door.store.locked() as db:
        db.execute("CREATE TRIGGER lose_ack BEFORE UPDATE OF notified_at ON uploads "
                   "BEGIN SELECT RAISE(FAIL,'synthetic disk failure'); END")
    path = door.stage(name="part.3mf", data=threemf())
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    assert row_for(door, path)["notified_at"] is None
    assert len(site.uploads) == 1
    assert site.puts == []  # The notice receipt must be durable before copying.
    with door.store.locked() as db:
        db.execute("DROP TRIGGER lose_ack")
    retry(door)
    eventually(lambda: row_for(door, path)["copied_at"] is not None)
    assert len(site.calls) == 2 and len(site.uploads) == 1


def test_pending_notice_survives_job_release_then_cleanup_finishes(door, site, caplog):
    site.mode = "down"
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    job = door.start(path)[1]["result"]["jobId"]
    assert door.server.reconcile_moonraker([], [job])
    assert (door.path / path).exists() and row_for(door, path)["notice"] is not None
    door.restart()
    site.mode = "ok"
    retry(door)
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    retry(door)
    assert row_for(door, path) is None and not (door.path / path).exists()


def test_vp_submit_queued_with_plate_wait_is_accepted(monkeypatch):
    job = _captured_job()
    pending, outbox = queue.Queue(), _FakeVPrinterOutbox()
    pending.put(job)
    offers = []
    monkeypatch.setattr(agent.job_files, "offer", lambda *args: offers.append(args))

    def site(url, body, **kwargs):
        assert url == "https://site.invalid/api/print/hub/vp-submit"
        return Response(200, {"ok": True, "jobId": "job", "state": "queued",
                              "reason": "Waiting for the requested build plate."})

    assert agent._drain_vprinter_submissions(pending, Config(cloud_url="https://site.invalid"),
        "synthetic-hub", model="P2S", outbox=outbox, poster=site) == 1
    assert pending.empty() and outbox.removed == [job.submission_uid]
    assert offers == [("job", job.file_path)]


@pytest.mark.parametrize("reason", ["invalid_token", "This nozzle is not supported by the selected printer."])
def test_start_token_and_nozzle_refusals_still_apply(door, site, reason):
    path = door.stage()
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    site.jobs[path.split("/")[0]] = {"ok": False, "reason": reason}
    assert door.start(path)[0] == 403
    assert (door.path / path).exists()


def test_old_upload_table_migrates_without_discarding_staged_file(tmp_path):
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    (directory / "part.gcode").write_bytes(b"x")
    db = sqlite3.connect(tmp_path / ".moonraker.sqlite3")
    db.execute("CREATE TABLE uploads (uid TEXT PRIMARY KEY, member TEXT, pool TEXT, name TEXT, sha TEXT, "
               "size INTEGER, created REAL, state TEXT, facts TEXT, job TEXT)")
    db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?)",
               ("a" * 32, "alice", "sv08-max", "part.gcode", "b" * 64, 1, time.time(), "staged", "{}", None))
    db.commit()
    db.close()
    store = moonraker.StagedUploads(tmp_path)
    with store.locked() as db:
        row = db.execute("SELECT * FROM uploads").fetchone()
        assert row["notified_at"] is None and row["notice"] is None and row["state"] == "staged"
        assert row["copy_upload_id"] is None and row["copied_at"] is None
    assert (directory / "part.gcode").read_bytes() == b"x"


def test_notice_never_acknowledged_survives_a_prolonged_site_outage(door, site, caplog):
    site.mode = "down"
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    with door.store.locked() as db:
        db.execute("UPDATE uploads SET created=?", (time.time() - NOTICE_TTL + 60,))
    door.restart()
    assert (door.path / path).exists()
    assert row_for(door, path)["notice"] is not None
    site.mode = "ok"
    retry(door)
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
    assert len(site.calls) == 2 and site.calls[0][1] == site.calls[1][1]


def test_original_3mf_copy_after_reply_does_not_block_or_repeat(door, site):
    door.restart(pool="p2s")
    site.copy_mode = "blocked"
    data = threemf()
    path = door.stage(name="part.3MF", data=data)
    assert site.copy_entered.wait(1)  # Orca already has its upload response.
    assert row_for(door, path)["notified_at"] is not None
    assert row_for(door, path)["copied_at"] is None
    with door.store.locked(blocking=False):
        pass  # The PUT must not hold the file-operation lock.
    retry(door)
    if door.store._retry_worker:
        door.store._retry_worker.join(1)
    assert door.start(path)[0] == 200
    assert door.start(path)[0] == 200
    assert len(site.puts) == len(site.jobs) == 1
    site.copy_release.set()
    eventually(lambda: row_for(door, path)["copied_at"] is not None)
    door.restart()
    retry(door)
    assert site.puts == [(path.split("/")[0], data)]
    assert len(site.calls) == 2  # One notice and one Start.


@pytest.mark.parametrize("mode", ["down", "lost_reply", 500, 401])
def test_failed_copy_retained_past_expiry_and_job_release_then_retried(door, site, caplog, mode):
    door.restart(pool="p2s")
    site.copy_mode = mode
    path = door.stage(name="part.3mf", data=threemf())
    eventually(lambda: "staged file copy unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    retry(door)
    door.store._retry_worker.join(1)
    assert len(site.puts) == 2
    assert caplog.text.count("staged file copy unconfirmed") == 1
    assert "alice" not in caplog.text and "synthetic-hub" not in caplog.text
    with door.store.locked() as db:
        db.execute("UPDATE uploads SET created=?,notified_at=?", (time.time() - NOTICE_TTL + 60,) * 2)
    door.restart()
    assert (door.path / path).exists()
    job = door.start(path)[1]["result"]["jobId"]
    assert door.server.reconcile_moonraker([], [job])
    assert (door.path / path).exists()
    door.restart()
    site.copy_mode = "ok"
    retry(door)
    eventually(lambda: row_for(door, path)["copied_at"] is not None)
    assert len(site.puts) == 3 and len(site.copies) == len(site.uploads) == len(site.jobs) == 1
    assert len(site.calls) == 2
    retry(door)
    assert row_for(door, path) is None and not (door.path / path).exists()


@pytest.mark.parametrize("name,size,expected", [
    ("part.gcode", 10, False), ("part.3mf", 64 * 1024**2, True),
    ("part.3mf", 64 * 1024**2 + 1, False),
])
def test_staged_copy_size_and_extension_limits(tmp_path, name, size, expected):
    copy = Mock()
    store = moonraker.StagedUploads(tmp_path, notify_staged=lambda _: {"ok": True, "uploadId": "upload"},
                                   copy_staged=copy)
    uid = "a" * 32
    directory = tmp_path / uid
    directory.mkdir()
    with (directory / name).open("wb") as stream:
        stream.truncate(size)
    with store.locked() as db:
        db.execute("INSERT INTO uploads(uid,member,pool,name,size,created,state,notice) "
                   "VALUES(?,?,?,?,?,?,'staged','{}')", (uid, "alice", "p2s", name, size, time.time()))
    store.notify(uid)
    store.notify(uid)
    assert copy.call_count == int(expected)
    with store.locked() as db:
        assert (db.execute("SELECT copied_at FROM uploads").fetchone()[0] is not None) == expected


def test_lost_local_copy_receipt_retries_same_file(door, site, caplog):
    door.restart(pool="p2s")
    with door.store.locked() as db:
        db.execute("CREATE TRIGGER lose_copy BEFORE UPDATE OF copied_at ON uploads "
                   "BEGIN SELECT RAISE(FAIL,'synthetic disk failure'); END")
    path = door.stage(name="part.3mf", data=threemf())
    eventually(lambda: "staged file copy unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    assert row_for(door, path)["copied_at"] is None
    with door.store.locked() as db:
        db.execute("DROP TRIGGER lose_copy")
    retry(door)
    eventually(lambda: row_for(door, path)["copied_at"] is not None)
    assert len(site.puts) == 2 and site.puts[0] == site.puts[1]
    assert len(site.copies) == len(site.calls) == 1


def test_heartbeat_releases_only_staged_uploads(door, monkeypatch):
    paths = [door.stage() for _ in range(3)]
    job = door.start(paths[1])[1]["result"]["jobId"]
    door.submit_mode = "lost_reply"
    assert door.start(paths[2])[0] == 502
    uids = [p.split("/")[0] for p in paths]
    stop = threading.Event()
    beats = []

    def heartbeat(url, body, **kwargs):
        beats.append(body)
        stop.set()
        return Response(200, {"hubId": "h", "configVersion": "v", "nextPollMs": 30000, "applied": 0,
                              "moonrakerReleasedUploads": [*uids, uids[0], "f" * 32]})

    # Run the real heartbeat with the real staged store; all services and transports are fakes.
    monkeypatch.setattr(agent, "read_credential", lambda: "synthetic-hub")
    monkeypatch.setattr(agent, "_surface_last_bootstrap", lambda *_: None)
    monkeypatch.setattr(agent, "_pull_config", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "IngestServer", lambda *a, **kw: door.server)
    monkeypatch.setattr(door.server, "start", lambda: None)
    monkeypatch.setattr(door.server, "stop", lambda: None)
    monkeypatch.setattr(agent, "VirtualPrinterManager", lambda **kw:
                        SimpleNamespace(current_model=lambda: None, stop_sync=lambda: None))
    monkeypatch.setattr(agent, "tailscale_binary_exists", lambda: False)
    monkeypatch.setattr(agent, "post_json", heartbeat)
    monkeypatch.setattr(agent, "start_telemetry", lambda *a: None)
    for module in (agent.wake_lane, agent.job_files, agent.toolpaths):
        monkeypatch.setattr(module, "start", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "prime_camera_capabilities", lambda: None)
    monkeypatch.setattr(agent, "probe_slicer", lambda: False)
    monkeypatch.setattr(agent, "build_detector_async", lambda **kw: agent.DetectorHolder())
    assert agent.run(Config(cloud_url="https://site.invalid"), _stop_event=stop, _install_signals=False,
                     _outbox=_FakeVPrinterOutbox()) == 0
    assert len(beats) == 1
    assert row_for(door, paths[0]) is None and not (door.path / paths[0]).exists()
    assert all(row_for(door, path) is not None and (door.path / path).exists() for path in paths[1:])
    assert door.server.reconcile_moonraker([], released_uploads=uids)
    assert door.server.reconcile_moonraker([], [job])
    assert row_for(door, paths[1]) is None
    assert row_for(door, paths[2])["state"] == "submitting"


@pytest.mark.parametrize("pending", ["notice", "file copy"])
def test_staged_release_cancels_pending_work_and_recovers_interrupted_deletion(door, site, monkeypatch, caplog, pending):
    door.restart(pool="p2s")
    if pending == "notice":
        site.mode = "down"
    else:
        site.copy_mode = "down"
    path = door.stage(name="part.3mf", data=threemf())
    eventually(lambda: f"staged {pending} unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    uid = path.split("/")[0]
    with monkeypatch.context() as patch:
        patch.setattr(moonraker.shutil, "rmtree", Mock(side_effect=OSError("synthetic failure")))
        assert not door.server.reconcile_moonraker([], released_uploads=[uid])
    assert row_for(door, path)["released"] == 1
    assert door.start(path)[0] == 404
    door.restart()
    assert row_for(door, path) is None and not (door.path / path).exists()
    assert len(site.calls) == 1


def test_notice_never_acknowledged_expires_after_the_notice_ttl(door, site, caplog):
    """Retention stays bounded when the site never acknowledges (a hub released ahead of its site build)."""
    site.mode = "down"
    path = door.stage()
    eventually(lambda: "unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    with door.store.locked() as db:
        db.execute("UPDATE uploads SET created=?", (time.time() - NOTICE_TTL - 60,))
    door.restart()
    assert not (door.path / path).exists()
    assert row_for(door, path) is None
