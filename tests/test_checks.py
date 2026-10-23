"""v0.81: members' file checks on the shop box (makeros_hub/checks.py; pstation Send to PS step 4). The site hands one check on a beat
that says the worker is free; the hub fetches it with its attempt token, runs the root-owned run-check on <spool>/<id>/.check, sends
Print it's slicer copy and posts the report, all inside the check's seconds, and leaves nothing behind. Here the runner is a stand-in
that writes what the ps-checker container writes; tests/test_run_check_box.py runs the real runner on a box."""

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from unittest import mock

import pytest

from makeros_hub import checks, http
from makeros_hub.agent import heartbeat_payload

CID = "11111111-2222-4333-8444-555555555555"
ATTEMPT = "66666666-7777-4888-9999-aaaaaaaaaaaa"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
REPORT = {"kind": "stl", "parts": {"count": 1}, "findings": [{"id": "looks-good"}]}
WHO = {"format": 1, "version": "abc123"}


@pytest.mark.parametrize("answer,rc", [
    (None, 124),
    ({"format": 2, "version": "test", "ok": True, "report": {"findings": []}}, 0),
    ({"format": 1, "version": "test", "ok": True, "report": {"findings": [{"id": "good", "level": "ok", "title": "Good"}]}}, 0),
    ({"format": 2, "version": "test", "ok": True, "report": {"findings": [{"id": "too-thin", "level": "fail", "title": "Too thin"}]}}, 0),
    ({"format": 2, "version": "test", "ok": True, "report": {"findings": [{"id": "open", "level": "fail", "title": "Not closed"}]}}, 0),
    ({"format": 2, "version": "test", "ok": True, "report": {"findings": [{"id": "future-failure", "level": "fail", "title": "Unresolved"}]}}, 0),
    ({"format": 2, "version": "test", "ok": True, "report": {"findings": [{"id": "good", "level": "ok", "title": "Good"}]}}, 137),
])
def test_prepared_check_never_clears_missing_old_failed_or_unresolved_results(tmp_path, monkeypatch, answer, rc):
    candidate = tmp_path / "candidate.stl"
    candidate.write_bytes(b"synthetic candidate")
    prepared = {"checked": True, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(), "settings": {"minWall": 0.84}}
    spool = tmp_path / "spool"
    def runner(uid):
        if answer is not None:
            out = spool / uid / ".check/out"
            out.mkdir()
            (out / "report.json").write_text(json.dumps(answer))
        return rc
    monkeypatch.setattr(checks, "run_check_job", runner)
    blocked = checks.check_prepared_model(spool, candidate, prepared, {"model": "A1 Mini", "fixes": {"orient": True}})
    assert blocked and prepared["checked"] is False
    assert not list(spool.iterdir())


def check(**over):
    c = {"checkId": CID, "attempt": ATTEMPT, "fileName": "model.stl", "sizeBytes": 10, "units": "file", "scale": 0.005, "models": ["P2S"],
         "minWall": 0.84, "picture": True, "stl": False, "path": f"/api/print/hub/check/{CID}", "seconds": 270}
    c.update(over)
    return c


class _Resp:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"ok": True} if body is None else body


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Box:
    """The hub's side of one run: a getter that writes the member's file, a runner that writes the container's outputs, and the posts,
    each told the seconds it has; a clock the stages can move (a slow fetch, a slow run)."""

    def __init__(self, tmp_path, *, report=None, rc=0, picture=True, stl=None, fetch_error=None, size=10, fetch_takes=0, run_takes=0):
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.report, self.rc, self.picture, self.stl, self.fetch_error, self.size = report, rc, picture, stl, fetch_error, size
        self.fetch_takes, self.run_takes = fetch_takes, run_takes
        self.clock = Clock()
        self.fetched, self.posted, self.bytes_posted, self.jobs, self.runs, self.slept = [], [], [], [], [], []

    def getter(self, path, dest, seconds):
        self.fetched.append((path, seconds))
        self.clock.now += self.fetch_takes
        if self.fetch_error:
            raise self.fetch_error
        Path(dest).write_bytes(b"s" * self.size)
        return "sha", self.size

    def runner(self, uid, seconds):
        self.runs.append(seconds)
        self.clock.now += self.run_takes
        work = self.spool / uid / ".check"
        self.jobs.append(json.loads((work / "job.json").read_text()))
        self.model_seen = sorted(p.name for p in work.iterdir())
        if self.report is not None:
            (work / "out").mkdir()
            (work / "out" / "report.json").write_text(json.dumps(self.report))
            if self.picture:
                (work / "out" / "picture.png").write_bytes(PNG)
            if self.stl is not None:
                (work / "out" / "model.stl").write_bytes(self.stl)
        return self.rc

    def run(self, items, post_status=200, post_bytes=None):
        return checks.run_checks(items, self.spool, getter=self.getter, runner=self.runner, clock=self.clock, sleep=self.sleep,
                                 post_json=lambda path, body, sec: (self.posted.append((path, body, sec)), _Resp(post_status))[1],
                                 post_bytes=post_bytes or (lambda path, data, sec: (self.bytes_posted.append((path, data)), 200)[1]))

    def sleep(self, s):
        self.slept.append(s)
        self.clock.now += s


def test_reads_a_check_strictly_and_fetches_only_its_own_address():
    assert checks.job_of(check())["job"] == {"name": "model.stl", "units": "file", "scale": 0.005, "models": ["P2S"], "minWall": 0.84,
                                             "picture": True, "stl": False}
    assert checks.job_of(check())["seconds"] == 270
    for bad in [check(path="/api/print/hub/file/x"), check(path=f"https://evil.example/api/print/hub/check/{CID}"), check(fileName="../x.stl"),
                check(fileName="part.stl"), check(sizeBytes=32 * 1024 * 1024 + 1), check(sizeBytes=True), check(units="furlong"),
                check(attempt="nope"), check(checkId="x"), check(models=["a"] * 9), check(picture="yes"), check(scale=0),
                check(scale=float("nan")), check(minWall=50), check(seconds=None), check(seconds=5), check(seconds=True), "x", None]:
        assert checks.job_of(bad) is None, bad


def test_checker_is_read_strictly():
    assert checks.parse_checker({"format": 1, "version": "abc123def456"}) == {"format": 1, "version": "abc123def456"}
    for bad in [{"format": "1", "version": "x"}, {"format": True, "version": "x"}, {"format": 1, "version": "a b"}, {"format": -1, "version": "x"},
                {"format": 1}, None, []]:
        assert checks.parse_checker(bad) is None


def test_runs_a_check_and_posts_the_report_with_its_picture_then_leaves_nothing(tmp_path):
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}})
    assert box.run([check()]) == 1
    assert box.fetched == [(f"/api/print/hub/check/{CID}?attempt={ATTEMPT}", 230)]   # the check's seconds, less what sending back needs
    assert box.jobs == [checks.job_of(check())["job"]]
    assert box.model_seen == ["job.json", "model.stl"]
    [(path, body, sec)] = box.posted
    assert path == f"/api/print/hub/check/{CID}/result"
    assert body == {"attempt": ATTEMPT, "checker": WHO, "outcome": {"ok": True, "report": REPORT, "sized": {"factor": 1}, "image": base64.b64encode(PNG).decode()}}
    assert box.bytes_posted == []
    assert list(box.spool.iterdir()) == []
    assert checks.CHECKER.value == WHO   # every check says what the image that ran it is


def test_print_it_sends_the_slicer_copy_before_the_result(tmp_path):
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}}, picture=False, stl=b"STL!")
    assert box.run([check(picture=False, stl=True)]) == 1
    assert box.bytes_posted == [(f"/api/print/hub/check/{CID}/stl?attempt={ATTEMPT}", b"STL!")]
    assert box.posted[0][1]["outcome"]["image"] is None


@pytest.mark.parametrize("rc,kind", [(124, "timeout"), (137, "memory"), (134, "memory"), (1, "broke")])
def test_a_stopped_run_says_why_by_its_exit_code_and_claims_no_identity(tmp_path, rc, kind):
    checks.CHECKER.value = {"format": 1, "version": "known"}   # what an earlier run said is never sent for this one
    box = Box(tmp_path, report=None, rc=rc)
    box.run([check()])
    assert box.posted[0][1] == {"attempt": ATTEMPT, "outcome": {"ok": False, "kind": kind}}


def test_a_report_without_its_own_identity_is_a_run_that_broke(tmp_path):
    checks.CHECKER.value = {"format": 1, "version": "known"}
    box = Box(tmp_path, report={"ok": True, "report": REPORT, "sized": {"factor": 1}})
    box.run([check()])
    assert box.posted[0][1] == {"attempt": ATTEMPT, "outcome": {"ok": False, "kind": "broke"}}


def test_what_the_checker_says_is_wrong_with_the_file_goes_back_as_it_is(tmp_path):
    box = Box(tmp_path, report={"ok": False, **WHO, "error": "This file is too detailed to check here.", "plain": True})
    box.run([check()])
    assert box.posted[0][1]["outcome"] == {"ok": False, "error": "This file is too detailed to check here.", "plain": True}
    assert box.posted[0][1]["checker"] == WHO


def test_outputs_are_read_only_as_regular_files_never_through_a_link(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text('{"ok": true, "format": 1, "version": "leak", "report": {}, "sized": {}}')
    box = Box(tmp_path, report=None)

    def runner(uid, seconds):
        out = box.spool / uid / ".check" / "out"
        out.mkdir()
        (out / "report.json").symlink_to(secret)
        os.mkfifo(out / "picture.png")
        return 0
    checks.run_checks([check()], box.spool, getter=box.getter, runner=runner, clock=box.clock, sleep=box.sleep,
                      post_json=lambda p, b, s: (box.posted.append(b), _Resp())[1], post_bytes=lambda p, d, s: 200)
    assert box.posted == [{"attempt": ATTEMPT, "outcome": {"ok": False, "kind": "broke"}}]
    assert checks.read_output(secret, 10) is None   # and a file over its cap is not read either


def test_one_deadline_covers_the_fetch_the_run_and_the_posts(tmp_path):
    # a slow fetch leaves the run only what is left, less what sending back needs
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}}, fetch_takes=100)
    assert box.run([check(seconds=200)]) == 1
    assert box.fetched[0][1] == 160 and box.runs == [60]
    assert 0 < box.posted[0][2] <= 100
    # a fetch that used nearly all of it: the check is not run and not reported (the site takes it back)
    (tmp_path / "b").mkdir()
    slow = Box(tmp_path / "b", report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}}, fetch_takes=160)
    assert slow.run([check(seconds=200)]) == 0
    assert slow.runs == [] and slow.posted == []
    # a run that used all of it is not reported late
    (tmp_path / "c").mkdir()
    late = Box(tmp_path / "c", report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}}, run_takes=300)
    assert late.run([check(seconds=200)]) == 0 and late.posted == []
    assert box.runs[0] <= checks.RUN_MAX_SEC


def test_a_busy_site_is_waited_for_within_the_deadline_and_not_past_it(tmp_path):
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}}, picture=False, stl=b"STL!")
    answers = [OSError("GET x: HTTP 503"), OSError("GET x: HTTP 503")]
    fetch = box.getter

    def getter(path, dest, seconds):
        if answers:
            raise answers.pop(0)
        return fetch(path, dest, seconds)
    box.getter = getter
    statuses = [503, 200]
    sent = []
    assert box.run([check(picture=False, stl=True)], post_bytes=lambda p, d, s: (sent.append(d), statuses.pop(0))[1]) == 1
    assert box.slept == [2, 2, 2] and sent == [b"STL!", b"STL!"]
    # always busy: it asks again only while there is time left to run the check after, then gives it up
    (tmp_path / "n").mkdir()
    never = Box(tmp_path / "n", report={"ok": True}, fetch_error=OSError("GET x: HTTP 503"))
    assert never.run([check(seconds=100)]) == 0
    assert never.jobs == [] and never.clock.now - 1000 <= 100 - checks.SEND_RESERVE_SEC


def test_a_check_it_cannot_fetch_is_dropped_and_the_site_takes_it_back(tmp_path):
    box = Box(tmp_path, report={"ok": True}, fetch_error=OSError("HTTP 410"))
    assert box.run([check()]) == 0
    assert box.posted == [] and box.jobs == [] and list(box.spool.iterdir()) == []
    (tmp_path / "b").mkdir()
    short = Box(tmp_path / "b", report={"ok": True}, size=9)   # fewer bytes than the site said
    assert short.run([check()]) == 0
    assert short.posted == [] and short.jobs == []


def test_a_malformed_check_never_holds_up_the_next_and_one_runs_a_beat(tmp_path):
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}})
    other = "22222222-3333-4444-8555-666666666666"
    assert box.run([check(path="/evil"), check(), check(checkId=other, path=f"/api/print/hub/check/{other}")]) == 1
    assert [p for p, _ in box.fetched] == [f"/api/print/hub/check/{CID}?attempt={ATTEMPT}"]


def test_a_report_the_site_refuses_counts_for_nothing(tmp_path):
    box = Box(tmp_path, report={"ok": True, **WHO, "report": REPORT, "sized": {"factor": 1}})
    assert box.run([check()], post_status=409) == 0
    assert list(box.spool.iterdir()) == []


def test_probe_asks_the_image_what_it_is_and_leaves_nothing(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()

    def runner(uid, seconds):
        work = spool / uid / ".check"
        assert json.loads((work / "job.json").read_text()) == {"probe": True}
        (work / "out").mkdir()
        (work / "out" / "report.json").write_text(json.dumps({"probe": True, "format": 1, "version": "abc"}))
        return 0
    assert checks.probe_checker(spool, runner=runner) == {"probe": True, "format": 1, "version": "abc"}
    assert list(spool.iterdir()) == []
    assert checks.probe_checker(spool, runner=lambda uid, s: 1) is None


def test_the_heartbeat_says_the_checker_and_whether_its_worker_is_free_only_when_known():
    assert "checker" not in heartbeat_payload([], [], None)
    checks.CHECKER.value = None
    assert checks.heartbeat_checker(True) is None
    checks.CHECKER.value = {"format": 1, "version": "abc"}
    assert checks.heartbeat_checker(False) == {"format": 1, "version": "abc", "free": False}
    assert heartbeat_payload([], [], None, checker={"format": 1, "version": "abc", "free": True, "extra": "x"})["checker"] == {"format": 1, "version": "abc", "free": True}


def test_run_check_stages_the_job_out_of_the_hubs_reach_and_kills_its_container():
    script = (Path(__file__).resolve().parents[1] / "checker" / "run-check").read_text()
    assert "--network none" in script and "--read-only" in script and "--memory 2g" in script and "--cap-drop ALL" in script
    assert 'case "$id" in ""|*[!0-9a-f]*)' in script and 'STAGE=/var/lib/ps-checker/run' in script and "setpriv --reuid" in script
    assert '--name "$name"' in script and 'docker rm -f "$name"' in script and "trap 'cleanup; exit 143' INT TERM HUP" in script
    assert '[ ! -L "$f" ]' in script and 'IMAGE=ps-checker:current; else' in script


def test_one_heartbeat_says_the_checker_and_hands_its_check_to_the_lane():
    """agent.run's wiring: with run-check installed and the image known, the beat carries `checker` with `free`, and the site's
    `checks` go to the check lane on its own thread; with no run-check, neither."""
    import test_v069

    for installed in (True, False):
        bodies, handed, done = [], [], threading.Event()

        def post(url, body, stop):
            bodies.append(body)
            stop.set()
            return http.Response(status=200, body={**test_v069.TestCommandsSurviveARestart.OK, "checks": [check()]})

        def lane(items, spool, **kw):
            handed.append(items)
            done.set()
            return 1

        checks.CHECKER.seen({"format": 1, "version": "abc123"})
        with mock.patch.object(checks, "checker_installed", return_value=installed), mock.patch.object(checks, "run_checks", side_effect=lane):
            test_v069.TestCommandsSurviveARestart.one_beat(post)
            assert done.wait(5) is installed
        assert ("checker" in bodies[0]) is installed
        if installed:
            assert bodies[0]["checker"] == {"format": 1, "version": "abc123", "free": True} and handed == [[check()]]
        else:
            assert handed == []
