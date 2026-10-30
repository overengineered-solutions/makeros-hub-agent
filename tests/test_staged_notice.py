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

from makeros_hub.moonraker import NOTICE_TTL, STAGE_TTL
import zipfile

import pytest

from makeros_hub import agent, moonraker
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
    cfg = Config(cloud_url="https://site.invalid")
    monkeypatch.setattr(test_moonraker, "IngestServer", partial(IngestServer,
                        moonraker_staged=agent.make_staged_notice(cfg, "synthetic-hub")))
    monkeypatch.setattr(door, "submit", agent.make_cloud_submit(cfg, "synthetic-hub"))
    door.restart()
    yield site
    site.release.set()


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
    with door.store.locked() as db:   # past the stage TTL, inside the notice TTL: the pending notice keeps the file
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
    with door.store.locked() as db:
        db.execute("CREATE TRIGGER lose_ack BEFORE UPDATE OF notified_at ON uploads "
                   "BEGIN SELECT RAISE(FAIL,'synthetic disk failure'); END")
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    assert row_for(door, path)["notified_at"] is None
    assert len(site.uploads) == 1
    with door.store.locked() as db:
        db.execute("DROP TRIGGER lose_ack")
    retry(door)
    eventually(lambda: row_for(door, path)["notified_at"] is not None)
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
    assert (directory / "part.gcode").read_bytes() == b"x"


def test_notice_never_acknowledged_expires_after_the_notice_ttl(door, site, caplog):
    """Retention stays bounded when the site never acknowledges (a hub released ahead of its site build)."""
    site.mode = "down"
    path = door.stage()
    eventually(lambda: "staged notice unconfirmed" in caplog.text)
    eventually(lambda: not door.store._notifying)
    with door.store.locked() as db:
        db.execute("UPDATE uploads SET created=?", (time.time() - NOTICE_TTL - 60,))
    door.restart()
    assert not (door.path / path).exists()
    assert row_for(door, path) is None
