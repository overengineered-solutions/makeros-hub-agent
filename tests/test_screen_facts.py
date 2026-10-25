import copy
import http.server
import json
import threading
import time
import urllib.parse
from unittest.mock import patch

import pytest

from makeros_hub.agent import heartbeat_payload
from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.screen import ScreenLink, event_of, facts_of

FACTS = {"who": "Sage E.", "job": "Bracket", "job_id": "", "next": "Rowan E. + 2 more", "warnings": [],
         "finish_at": None, "next_zones": [1, 4], "staff_until": None}


def event(n=1, action="plate_clear", **extra):
    return {"v": 1, "id": f"176140000000{n}-abc123", "at": 1761400000.0, "action": action, "job_id": "", **extra}


@pytest.fixture
def moonraker():
    class State:
        events = {}
        writes = []
        deleted = []
        bad_reply = False
        slow = False

    state = State()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            if state.slow:
                time.sleep(0.2)
            if self.command == "GET":
                params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            else:
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert params["namespace"] == "pstation"
            key = params["key"]
            if self.command == "POST":
                assert key == "screen"
                state.writes.append(params["value"])
                value = params["value"]
            elif self.command == "GET":
                assert key == "screen_events"
                value = copy.deepcopy(state.events)
            else:
                assert isinstance(key, list) and key[0] == "screen_events"
                state.deleted.append(key[1])
                value = state.events.pop(key[1], None)
            body = json.dumps({} if state.bad_reply else {"result": {"namespace": "pstation", "key": key, "value": value}}).encode()
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        do_GET = do_POST = do_DELETE = handle_request

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    link = ScreenLink("printer-a", f"http://127.0.0.1:{server.server_port}")
    # Exercise real HTTP synchronously; the worker's scheduling has its own test below.
    link._thread = thread
    yield link, state
    link.stop()
    server.shutdown()
    server.server_close()


def test_write_changes_and_refreshes_within_sixty_seconds_without_masking_a_dead_site(moonraker):
    link, mr = moonraker
    with patch("makeros_hub.printers.screen.time.monotonic", return_value=100), patch("makeros_hub.printers.screen.time.time", return_value=1761400000):
        link.offer(FACTS)
        link._sync()
        link._sync()
    assert len(mr.writes) == 1 and mr.writes[0]["updated"] == 1761400000
    with patch("makeros_hub.printers.screen.time.monotonic", return_value=144):
        link.offer(FACTS)
        link._sync()
    assert len(mr.writes) == 1
    with patch("makeros_hub.printers.screen.time.monotonic", return_value=145):
        link._sync()
    assert len(mr.writes) == 2
    with patch("makeros_hub.printers.screen.time.monotonic", return_value=161):
        link.offer(dict(FACTS, next=""))
        link._sync()
    assert len(mr.writes) == 3 and mr.writes[-1]["next"] == ""
    with patch("makeros_hub.printers.screen.time.monotonic", return_value=260):
        link._sync()
    assert len(mr.writes) == 3
    link.offer(None)  # older site / omitted facts never renew stale names
    link._sync()
    assert len(mr.writes) == 3


def test_events_stay_durable_until_explicit_ack_and_delete_only_that_id(moonraker):
    link, mr = moonraker
    first = event()
    mr.events[first["id"]] = first
    link._sync()
    assert link.pending() == [dict(first, printerId="printer-a")]
    assert not mr.deleted
    second = event(2, "report_problem")
    mr.events[second["id"]] = second  # arrived while the heartbeat was in flight
    link.ack([first["id"], second["id"]])  # unsent/unknown id is not ackable
    link._sync()
    assert mr.deleted == [first["id"]] and list(mr.events) == [second["id"]]
    replacement = ScreenLink(link.printer_id, link.base)  # a process restart reloads from Moonraker
    replacement._sync()
    assert replacement.pending() == [dict(second, printerId="printer-a")]


@pytest.mark.parametrize("action", ["plate_clear", "report_problem", "control", "paused_long", "power_loss"])
def test_all_screen_actions_are_reports_never_replayed_motion(moonraker, action):
    link, mr = moonraker
    e = event(action=action, control="calibrate:mesh", result="ran", by="Sage Example", job="/private/file.gcode", reason="private text")
    mr.events[e["id"]] = e
    link._sync()
    sent = link.pending()[0]
    assert sent["action"] == action and not any(k in sent for k in ("by", "job", "reason"))
    link.ack([e["id"]])
    link._sync()
    assert not mr.events


def test_unknown_and_malformed_events_are_deleted_without_poisoning_valid_events(moonraker):
    link, mr = moonraker
    for n, val in enumerate([event(action="home"), {"id": "broken"}, None, ["not an event"]]):
        mr.events[f"bad.{n}"] = val
    good = event(5)
    mr.events[good["id"]] = good
    link._sync()
    assert set(mr.deleted) == {f"bad.{n}" for n in range(4)}
    assert [e["id"] for e in link.pending()] == [good["id"]]


def test_valid_event_id_with_unknown_action_is_deleted(moonraker):
    link, mr = moonraker
    e = event(action="home")
    mr.events[e["id"]] = e
    link._sync()
    assert not link.pending() and mr.deleted == [e["id"]]


def test_requests_timeout_and_validate_success_bodies(moonraker):
    link, mr = moonraker
    mr.bad_reply = True
    with pytest.raises(ValueError):
        link._sync()
    mr.bad_reply, mr.slow = False, True
    with patch("makeros_hub.printers.screen.TIMEOUT_SEC", 0.02), pytest.raises(TimeoutError):
        link._sync()


def test_a_lost_delete_reply_does_not_replay_or_retain_the_ack(moonraker):
    link, mr = moonraker
    e = event()
    mr.events[e["id"]] = e
    link._sync()
    link.ack([e["id"]])
    request = link._request

    def lost_reply(method, key, value=None):
        result = request(method, key, value)
        if method == "DELETE":
            raise TimeoutError()
        return result

    with patch.object(link, "_request", lost_reply), pytest.raises(TimeoutError):
        link._sync()
    assert not link.pending() and not mr.events
    link._sync()
    assert not link._acked and not link._pending


def test_a_corrupt_parent_is_never_deleted_over_a_concurrent_new_tap(moonraker):
    link, mr = moonraker
    mr.events = []
    with pytest.raises(ValueError):
        link._sync()
    assert not mr.deleted


def test_worker_never_blocks_the_caller_and_logs_once_per_outage(caplog):
    link = ScreenLink("printer-a", "http://unused.invalid")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fail():
        calls.append(1)
        entered.set()
        release.wait(1)
        raise TimeoutError()

    with patch.object(link, "_sync", fail), patch("makeros_hub.printers.screen.POLL_SEC", 0.01):
        started = time.monotonic()
        link.offer(FACTS)
        assert time.monotonic() - started < 0.1
        assert entered.wait(1)
        link.offer(dict(FACTS, next=""))
        assert link.pending() == []
        release.set()
        deadline = time.monotonic() + 1
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        link.stop()
        link._thread.join(1)
    assert len(calls) >= 3
    assert caplog.text.count("link unavailable") == 1


def test_manager_scopes_facts_and_acknowledges_only_sent_events(moonraker):
    link, mr = moonraker
    manager = PrinterManager()
    manager._adapters = {"printer-a": type("Adapter", (), {"screen": link})(), "bambu": object()}
    e = event()
    mr.events[e["id"]] = e
    link._sync()
    sent = manager.screen_events()
    assert heartbeat_payload(screen_events=sent)["screenEvents"] == sent
    assert "screenEvents" not in heartbeat_payload()
    manager.update_screens({"screenFacts": [dict(FACTS, printerId="other")], "screenEventsAck": [{"printerId": "other", "id": e["id"]}]}, sent)
    assert link._facts is None and link.pending() == sent
    manager.update_screens({"screenFacts": [dict(FACTS, printerId="printer-a")], "screenEventsAck": [{"printerId": "printer-a", "id": e["id"]}]}, [])
    assert link.pending() == sent
    manager.update_screens({"screenEventsAck": [{"printerId": "printer-a", "id": e["id"]}]}, sent)
    link._sync()
    assert not mr.events


def test_facts_and_events_reject_bad_types_and_never_unlock_staff():
    assert facts_of(dict(FACTS, staff_until=9999999999))["staff_until"] is None
    for value in [None, {}, dict(FACTS, next_zones=[True]), dict(FACTS, finish_at=float("nan")), dict(FACTS, warnings="bad")]:
        with pytest.raises(ValueError):
            facts_of(value)
    for e in [event(at=True), event(v=True), event(job_id="someone"), event(control="G28\n"), event(minutes=-1)]:
        with pytest.raises(ValueError):
            event_of(e["id"], e)
