"""v0.81: members' file checks on the shop box (makeros_hub/checks.py; pstation Send to PS step 4). The site hands one check a beat; the
hub fetches it with its attempt token, runs the root-owned run-check on <spool>/<id>/.check, sends Print it's slicer copy and posts the
report, and leaves nothing behind. Here the runner is a stand-in that writes what the ps-checker container writes."""

import base64
import json
from pathlib import Path

import pytest

from makeros_hub import checks
from makeros_hub.agent import heartbeat_payload

CID = "11111111-2222-4333-8444-555555555555"
ATTEMPT = "66666666-7777-4888-9999-aaaaaaaaaaaa"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
REPORT = {"kind": "stl", "parts": {"count": 1}, "findings": [{"id": "looks-good"}]}


def check(**over):
    c = {"checkId": CID, "attempt": ATTEMPT, "fileName": "model.stl", "sizeBytes": 10, "units": "file", "scale": 0.005, "models": ["P2S"],
         "minWall": 0.84, "picture": True, "stl": False, "path": f"/api/print/hub/check/{CID}"}
    c.update(over)
    return c


class _Resp:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"ok": True} if body is None else body


class Box:
    """The hub's side of one run: a getter that writes the member's file, a runner that writes the container's outputs, and the posts."""

    def __init__(self, tmp_path, *, report=None, rc=0, picture=True, stl=None, fetch_error=None, size=10):
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.report, self.rc, self.picture, self.stl, self.fetch_error, self.size = report, rc, picture, stl, fetch_error, size
        self.fetched, self.posted, self.bytes_posted, self.jobs = [], [], [], []

    def getter(self, path, dest):
        self.fetched.append(path)
        if self.fetch_error:
            raise self.fetch_error
        Path(dest).write_bytes(b"s" * self.size)
        return "sha", self.size

    def runner(self, uid):
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

    def run(self, items, post_status=200):
        return checks.run_checks(items, self.spool, getter=self.getter, runner=self.runner,
                                 post_json=lambda path, body: (self.posted.append((path, body)), _Resp(post_status))[1],
                                 post_bytes=lambda path, data: (self.bytes_posted.append((path, data)), 200)[1])


def test_reads_a_check_strictly_and_fetches_only_its_own_address():
    assert checks.job_of(check())["job"] == {"name": "model.stl", "units": "file", "scale": 0.005, "models": ["P2S"], "minWall": 0.84,
                                             "picture": True, "stl": False}
    for bad in [check(path="/api/print/hub/file/x"), check(path=f"https://evil.example/api/print/hub/check/{CID}"), check(fileName="../x.stl"),
                check(fileName="part.stl"), check(sizeBytes=32 * 1024 * 1024 + 1), check(sizeBytes=True), check(units="furlong"),
                check(attempt="nope"), check(checkId="x"), check(models=["a"] * 9), check(picture="yes"), check(scale=0),
                check(scale=float("nan")), check(minWall=50), "x", None]:
        assert checks.job_of(bad) is None, bad


def test_checker_is_read_strictly():
    assert checks.parse_checker({"format": 1, "version": "abc123def456"}) == {"format": 1, "version": "abc123def456"}
    for bad in [{"format": "1", "version": "x"}, {"format": True, "version": "x"}, {"format": 1, "version": "a b"}, {"format": -1, "version": "x"},
                {"format": 1}, None, []]:
        assert checks.parse_checker(bad) is None


def test_runs_a_check_and_posts_the_report_with_its_picture_then_leaves_nothing(tmp_path):
    box = Box(tmp_path, report={"ok": True, "format": 1, "version": "abc123", "report": REPORT, "sized": {"factor": 1}})
    assert box.run([check()]) == 1
    assert box.fetched == [f"/api/print/hub/check/{CID}?attempt={ATTEMPT}"]
    assert box.jobs == [checks.job_of(check())["job"]]
    assert box.model_seen == ["job.json", "model.stl"]
    [(path, body)] = box.posted
    assert path == f"/api/print/hub/check/{CID}/result"
    assert body == {"attempt": ATTEMPT, "checker": {"format": 1, "version": "abc123"},
                    "outcome": {"ok": True, "report": REPORT, "sized": {"factor": 1}, "image": base64.b64encode(PNG).decode()}}
    assert box.bytes_posted == []
    assert list(box.spool.iterdir()) == []
    assert checks.CHECKER.value == {"format": 1, "version": "abc123"}   # every check says what the image that ran it is


def test_print_it_sends_the_slicer_copy_before_the_result(tmp_path):
    box = Box(tmp_path, report={"ok": True, "format": 1, "version": "v", "report": REPORT, "sized": {"factor": 1}}, picture=False, stl=b"STL!")
    assert box.run([check(picture=False, stl=True)]) == 1
    assert box.bytes_posted == [(f"/api/print/hub/check/{CID}/stl?attempt={ATTEMPT}", b"STL!")]
    assert box.posted[0][1]["outcome"]["image"] is None


@pytest.mark.parametrize("rc,kind", [(124, "timeout"), (137, "memory"), (134, "memory"), (1, "broke")])
def test_a_checker_that_stops_says_why_by_its_exit_code(tmp_path, rc, kind):
    checks.CHECKER.value = {"format": 1, "version": "known"}
    box = Box(tmp_path, report=None, rc=rc)
    box.run([check()])
    assert box.posted[0][1]["outcome"] == {"ok": False, "kind": kind}
    assert box.posted[0][1]["checker"] == {"format": 1, "version": "known"}


def test_what_the_checker_says_is_wrong_with_the_file_goes_back_as_it_is(tmp_path):
    box = Box(tmp_path, report={"ok": False, "format": 1, "version": "v", "error": "This file is too detailed to check here.", "plain": True})
    box.run([check()])
    assert box.posted[0][1]["outcome"] == {"ok": False, "error": "This file is too detailed to check here.", "plain": True}


def test_a_check_it_cannot_fetch_is_dropped_and_the_site_takes_it_back(tmp_path):
    box = Box(tmp_path, report={"ok": True}, fetch_error=OSError("HTTP 410"))
    assert box.run([check()]) == 0
    assert box.posted == [] and box.jobs == [] and list(box.spool.iterdir()) == []
    (tmp_path / "b").mkdir()
    short = Box(tmp_path / "b", report={"ok": True}, size=9)   # fewer bytes than the site said
    assert short.run([check()]) == 0
    assert short.posted == [] and short.jobs == []


def test_a_malformed_check_never_holds_up_the_next_and_one_runs_a_beat(tmp_path):
    box = Box(tmp_path, report={"ok": True, "format": 1, "version": "v", "report": REPORT, "sized": {"factor": 1}})
    other = "22222222-3333-4444-8555-666666666666"
    assert box.run([check(path="/evil"), check(), check(checkId=other, path=f"/api/print/hub/check/{other}")]) == 1
    assert box.fetched == [f"/api/print/hub/check/{CID}?attempt={ATTEMPT}"]


def test_a_busy_site_is_waited_for_and_not_given_up_on(tmp_path):
    """The site's one upload slot held by a member's upload answers 503: the fetch and the slicer's copy wait 2 s a try, a minute at most."""
    box = Box(tmp_path, report={"ok": True, "format": 1, "version": "v", "report": REPORT, "sized": {"factor": 1}}, picture=False, stl=b"STL!")
    answers = [OSError("GET x: HTTP 503"), OSError("GET x: HTTP 503")]
    fetch = box.getter
    def getter(path, dest):
        if answers:
            raise answers.pop(0)
        return fetch(path, dest)
    statuses = [503, 200]
    sent, slept = [], []
    assert checks.run_checks([check(picture=False, stl=True)], box.spool, getter=getter, runner=box.runner, sleep=slept.append,
                             post_json=lambda path, body: _Resp(),
                             post_bytes=lambda path, data: (sent.append(data), statuses.pop(0))[1]) == 1
    assert slept == [2, 2, 2] and sent == [b"STL!", b"STL!"]
    (tmp_path / "n").mkdir()
    never = Box(tmp_path / "n", report={"ok": True})
    tries = []
    def busy(path, dest):
        tries.append(path)
        raise OSError("GET x: HTTP 503")
    assert checks.run_checks([check()], never.spool, getter=busy, runner=never.runner, sleep=lambda s: None,
                             post_json=lambda p, b: _Resp(), post_bytes=lambda p, d: 200) == 0
    assert len(tries) == checks.BUSY_TRIES and never.jobs == []


def test_a_report_the_site_refuses_counts_for_nothing(tmp_path):
    box = Box(tmp_path, report={"ok": True, "format": 1, "version": "v", "report": REPORT, "sized": {"factor": 1}})
    assert box.run([check()], post_status=409) == 0
    assert list(box.spool.iterdir()) == []


def test_probe_asks_the_image_what_it_is_and_leaves_nothing(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    def runner(uid):
        work = spool / uid / ".check"
        assert json.loads((work / "job.json").read_text()) == {"probe": True}
        (work / "out").mkdir()
        (work / "out" / "report.json").write_text(json.dumps({"probe": True, "format": 1, "version": "abc"}))
        return 0
    assert checks.probe_checker(spool, runner=runner) == {"probe": True, "format": 1, "version": "abc"}
    assert list(spool.iterdir()) == []
    assert checks.probe_checker(spool, runner=lambda uid: 1) is None


def test_the_heartbeat_says_the_checker_only_when_it_is_known():
    assert "checker" not in heartbeat_payload([], [], None)
    assert heartbeat_payload([], [], None, checker={"format": 1, "version": "abc", "extra": "x"})["checker"] == {"format": 1, "version": "abc"}


def test_run_check_takes_only_a_plain_hex_job_folder_owned_by_the_hub():
    script = (Path(__file__).resolve().parents[1] / "checker" / "run-check").read_text()
    assert "--network none" in script and "--read-only" in script and "--memory 2g" in script and "--cap-drop ALL" in script
    assert 'case "$id" in ""|*[!0-9a-f]*)' in script and 'realpath -e' in script and 'stat -c %u' in script
    assert "timeout 150 docker run" in script


def test_one_heartbeat_says_the_checker_and_hands_its_check_to_the_lane():
    """agent.run's wiring: with run-check installed and the image known, the beat carries `checker` and the site's `checks` go to
    the check lane on its own thread; with no run-check, neither."""
    import threading
    from unittest import mock

    from makeros_hub import http
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
            assert bodies[0]["checker"] == {"format": 1, "version": "abc123"} and handed == [[check()]]
        else:
            assert handed == []
