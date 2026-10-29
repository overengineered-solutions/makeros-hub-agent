"""Lasers (v0.68.0), contract section 6 HUB: H1-H11 (H12 is test_version_sync at 0.68.0).

Every peer here is a fake on 127.0.0.1 with an ephemeral port: a fake Home Assistant webhook, a fake gate, a fake
cloud fetcher. Nothing contacts the shop box, Home Assistant, the gate or the controller.
"""
import ast
import hashlib
import http.client
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from makeros_hub import agent, config, http as hub_http, laser
from makeros_hub.agent import _pull_config, heartbeat_payload
from makeros_hub.config import Config
from makeros_hub.laser import LaserConfig, LaserHub, RunTracker, derive_state, parse_laser_config
from makeros_hub.printers.manager import PrinterManager

ROOT = Path(__file__).resolve().parents[1]
LASER_ID = "11111111-2222-4333-8444-555555555555"
SLUG = "omtech-pro-80w"
FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
GATE_TOKEN = "g" * 40
PUSH_TOKEN = "p" * 40
BODY = b"\x01\x02rd-file-bytes" * 10
SHA = hashlib.sha256(BODY).hexdigest()
LASER_KEYS = {"machineId", "state", "stateSince", "switch", "currentA", "powerW", "haLink", "haSeenAt", "gateLink",
              "gateDetail", "gate", "runs"}


def acked(rep):
    """The laserAck of a site that stored everything `rep` carried."""
    return {"runKeys": [k for keys in rep.run_keys.values() for k in keys], "fileIds": [c["fileId"] for c in rep.check_items]}


def iso_in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """A tiny HTTP peer on 127.0.0.1. `routes[(method, path)] = (status, json)` or a callable(handler, body) -> that."""

    def __init__(self, routes=None):
        self.routes, self.calls = dict(routes or {}), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = self.path.split("?", 1)[0]
                fake.calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
                route = fake.routes.get((self.command, path), (404, {"error": "not_found"}))
                status, obj = route(self, body) if callable(route) else route
                raw = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.secrets = Path(d.name)

    def secret(self, name, value, mode=0o600):
        p = self.secrets / name
        p.write_text(value)
        os.chmod(p, mode)

    def server(self, routes=None):
        s = FakeServer(routes)
        self.addCleanup(s.close)
        return s

    def hub(self, gate_url=None, fetcher=None, lasers=((LASER_ID, SLUG),), allowed="127.0.0.1", **over):
        cfg = LaserConfig(ha_push_allowed_ip=allowed, ha_push_listen=("127.0.0.1", 0), gate_url=gate_url, **over)
        h = LaserHub(cfg, fetcher=fetcher, secrets_dir=self.secrets)
        h.set_lasers([{"id": i, "slug": s, "name": s.upper()} for i, s in lasers])
        return h

    def gate(self, routes=None):
        g = self.server(routes)
        self.secret(laser.GATE_TOKEN_FILE, GATE_TOKEN)
        return g

    @staticmethod
    def fetcher(data=BODY, wait=None):
        def fetch(path, dest):
            if wait is not None:
                wait.wait(5)
            Path(dest).write_bytes(data)
            return hashlib.sha256(data).hexdigest(), len(data)
        return fetch

    @staticmethod
    def wait_for(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False


# --- H1 -------------------------------------------------------------------------------------------------------------

class H1DeriveState(unittest.TestCase):
    def test_table(self):
        rows = [
            # configured, push age, switch, currentA -> haLink, state
            (False, 1, "on", 3.0, "not_configured", "unknown"),
            (True, None, "on", 3.0, "down", "unknown"),
            (True, 76, "on", 3.0, "down", "unknown"),
            (True, 75, "on", 3.0, "up", "cutting"),
            (True, 1, "unavailable", 3.0, "up", "unknown"),
            (True, 1, "unknown", 3.0, "up", "unknown"),
            (True, 1, None, 3.0, "up", "unknown"),
            (True, 1, "off", 3.0, "up", "off"),
            (True, 1, "on", None, "up", "unknown"),
            (True, 1, "on", 2.0, "up", "cutting"),
            (True, 1, "on", 1.99, "up", "on_idle"),
            (True, 1, "on", 1.66, "up", "on_idle"),
            (True, 1, "on", 0.5, "up", "on_idle"),
            (True, 1, "on", 0.49, "up", "off"),
            (True, 1, "on", 0.0, "up", "off"),
        ]
        for configured, age, switch, amps, link, state in rows:
            with self.subTest(configured=configured, age=age, switch=switch, amps=amps):
                self.assertEqual(derive_state(configured=configured, push_age_s=age, switch=switch, current_a=amps),
                                 (link, state))


class H1HubState(Base):
    def test_no_token_is_not_configured_and_silence_is_down(self):
        hub = self.hub()
        self.assertTrue(hub.push({"laser": SLUG, "switch": "on", "currentA": "2.5"}, now=1000, mono=100))
        entry = hub.report(now=1000, mono=100, poll=False).lasers[0]
        self.assertEqual((entry["haLink"], entry["state"]), ("not_configured", "unknown"))
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub.push({"laser": SLUG, "switch": "on", "currentA": "2.5"}, now=1000, mono=100)
        self.assertEqual(hub.report(now=1001, mono=101, poll=False).lasers[0]["state"], "cutting")
        entry = hub.report(now=1076, mono=176, poll=False).lasers[0]
        self.assertEqual((entry["haLink"], entry["state"]), ("down", "unknown"))
        self.assertEqual(entry["stateSince"], laser.iso_z(1076))

    def test_no_laser_table_is_not_configured(self):
        hub = LaserHub(None, secrets_dir=self.secrets)
        hub.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "x"}])
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        entry = hub.report(poll=False).lasers[0]
        self.assertEqual((entry["haLink"], entry["gateLink"], entry["state"]), ("not_configured", "not_configured", "unknown"))

    def test_a_fifo_secret_never_blocks(self):
        fifo = self.secrets / laser.PUSH_TOKEN_FILE
        os.mkfifo(fifo, 0o600)
        done = []
        t = threading.Thread(target=lambda: done.append(laser.read_secret(fifo)), daemon=True)
        t.start()
        t.join(2)
        self.assertEqual(done, [None])

    def test_a_readable_by_others_token_is_not_configured(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN, mode=0o644)
        hub = self.hub()
        hub.push({"laser": SLUG, "switch": "on", "currentA": 1}, now=1, mono=1)
        self.assertEqual(hub.report(now=1, mono=1, poll=False).lasers[0]["haLink"], "not_configured")


# --- H2 -------------------------------------------------------------------------------------------------------------

class H2Runs(Base):
    def test_bursts_20s_apart_merge_into_one_run(self):
        r = RunTracker(60)
        r.sample("cutting", 0)
        r.sample("on_idle", 10)
        r.tick("on_idle", 15)
        r.sample("cutting", 20)
        r.sample("on_idle", 30)
        r.sample("cutting", 40)
        self.assertEqual(r.closed, [])
        self.assertEqual((r.open["startedAt"], r.open["lastCuttingAt"]), (0, 40))

    def test_61s_gap_closes_idle_at_the_last_cutting_reading(self):
        r = RunTracker(60)
        r.sample("cutting", 100)
        r.sample("on_idle", 130)
        r.tick("on_idle", 159)
        self.assertIsNotNone(r.open)
        r.tick("on_idle", 161)
        self.assertIsNone(r.open)
        self.assertEqual([(c["endedAt"], c["endReason"]) for c in r.closed], [(100, "idle")])

    def test_switch_off_and_silence(self):
        r = RunTracker(60)
        r.sample("cutting", 0)
        r.sample("off", 5)
        r.sample("cutting", 10)
        r.tick("unknown", 12)
        self.assertEqual([c["endReason"] for c in r.closed], ["switch_off", "telemetry_lost"])
        self.assertEqual([c["endedAt"] for c in r.closed], [0, 10])

    def test_a_tick_never_opens_a_run(self):
        r = RunTracker(60)
        r.tick("cutting", 0)
        self.assertIsNone(r.open)

    def test_closed_runs_ride_every_beat_until_acked_with_a_stable_key(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1000, mono=10)
        open_key = hub.report(now=1001, mono=11, poll=False).lasers[0]["runs"][0]["runKey"]
        hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=1010, mono=20)
        first = hub.report(now=1011, mono=21, poll=False)
        second = hub.report(now=1041, mono=51, poll=False)
        for rep in (first, second):
            self.assertEqual(rep.lasers[0]["runs"], [{"runKey": open_key, "startedAt": laser.iso_z(1000),
                                                      "lastCuttingAt": laser.iso_z(1000),
                                                      "endedAt": laser.iso_z(1000), "endReason": "switch_off"}])
        hub.ack(second, acked(second))
        self.assertEqual(hub.report(now=1071, mono=61, poll=False).lasers[0]["runs"], [])

    def test_an_ack_keeps_runs_closed_after_that_beat_was_built(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1000, mono=10)
        rep = hub.report(now=1001, mono=11, poll=False)   # carries only the open run
        hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=1010, mono=20)
        hub.ack(rep, {"runKeys": [r["runKey"] for r in rep.lasers[0]["runs"]], "fileIds": []})   # the site acks open runs too
        self.assertEqual(len(hub.report(now=1011, mono=21, poll=False).lasers[0]["runs"]), 1)

    def test_runs_the_cloud_has_not_acked_survive_a_restart(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1000, mono=10)
        hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=1010, mono=20)    # run A closes
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1020, mono=30)     # run B opens
        before = {r["runKey"]: r for r in hub.report(now=1021, mono=31, poll=False).lasers[0]["runs"]}
        self.assertEqual(len(before), 2)
        # the hub restarts; a report before config-down arrives must not drop the saved runs
        reborn = LaserHub(hub.cfg, secrets_dir=self.secrets)
        self.assertIsNone(reborn.report(now=1030, poll=False).lasers)
        reborn.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "X"}, {"id": "11111111-1111-4111-8111-111111111111",
                                                                        "slug": "other", "name": "Y"}])
        # cutting before the first report is a new run, never B continued (it may be another member's session)
        reborn.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=5000, mono=4)
        after = reborn.report(now=5001, mono=5, poll=False)
        (fresh,) = [r for r in after.lasers[0]["runs"] if r["endedAt"] is None]
        self.assertNotIn(fresh["runKey"], before)
        self.assertEqual(fresh["startedAt"], laser.iso_z(5000))
        runs = {r["runKey"]: r for r in after.lasers[0]["runs"] if r["endedAt"] is not None}
        self.assertEqual(set(runs), set(before))
        (a,) = [r for k, r in runs.items() if before[k]["endedAt"] is not None]
        self.assertEqual(a, next(r for r in before.values() if r["endedAt"] is not None))
        (b,) = [r for k, r in runs.items() if before[k]["endedAt"] is None]
        self.assertEqual((b["startedAt"], b["endedAt"], b["endReason"]),
                         (laser.iso_z(1020), laser.iso_z(1020), "telemetry_lost"))
        self.assertEqual(after.lasers[1]["runs"], [])
        reborn.ack(after, acked(after))
        self.assertEqual(LaserHub(hub.cfg, secrets_dir=self.secrets)._unclaimed_runs[LASER_ID]["closed"], [])

    def test_a_new_run_never_pushes_the_newest_closed_run_out_of_the_report(self):
        # 31 unacked closed runs and B open at a restart: B comes back closed, and cutting opens C before any report
        closed = [{"runKey": f"{i:032x}", "startedAt": i * 10, "lastCuttingAt": i * 10, "endedAt": i * 10,
                   "endReason": "idle"} for i in range(31)]
        r = RunTracker(60)
        r.load({"open": {"runKey": "b" * 32, "startedAt": 1000, "lastCuttingAt": 4600}, "closed": closed})
        r.sample("cutting", 9000)
        rep = r.report()
        self.assertEqual((len(rep), rep[0]["endedAt"]), (32, None))
        self.assertEqual(rep[-1], {"runKey": "b" * 32, "startedAt": laser.iso_z(1000), "lastCuttingAt": laser.iso_z(4600),
                                   "endedAt": laser.iso_z(4600), "endReason": "telemetry_lost"})

    def test_at_most_32_kept_oldest_dropped(self):
        r = RunTracker(60)
        for i in range(40):
            r.sample("cutting", i * 10)
            r.sample("off", i * 10 + 1)
        self.assertEqual(len(r.closed), 32)
        self.assertEqual(r.closed[0]["startedAt"], 80)
        r.sample("cutting", 1000)
        rep = r.report()
        self.assertEqual(len(rep), 32)
        self.assertIsNone(rep[0]["endedAt"])   # the open run always rides


class H2Ack(Base):
    """v0.68.1 (codex release review of pstation aa9433b): a 200 drops only what its laserAck names."""
    F1, F2 = FILE_ID, "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"

    def two_of_each(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        for t in (1000, 1020):
            hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=t, mono=t)
            hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=t + 5, mono=t + 5)
        hub._check_results = [{"fileId": f, "sha256": SHA, "verdict": "retry", "detail": "gate_unreachable"}
                              for f in (self.F1, self.F2)]
        rep = hub.report(now=1100, mono=1100, poll=False)
        self.assertEqual((len(rep.run_keys[LASER_ID]), len(rep.check_items)), (2, 2))
        return hub, rep

    def left(self, hub):
        rep = hub.report(now=1200, mono=1200, poll=False)
        saved = LaserHub(hub.cfg, secrets_dir=self.secrets)._unclaimed_runs[LASER_ID]["closed"]
        self.assertEqual([r["runKey"] for r in saved], rep.run_keys[LASER_ID])   # what is kept is kept on disk too
        return rep.run_keys[LASER_ID], [c["fileId"] for c in rep.check_items]

    def test_an_empty_ack_keeps_both(self):
        hub, rep = self.two_of_each()
        hub.ack(rep, {"runKeys": [], "fileIds": []})
        self.assertEqual(self.left(hub), (rep.run_keys[LASER_ID], [self.F1, self.F2]))

    def test_a_partial_ack_drops_only_what_it_names(self):
        hub, rep = self.two_of_each()
        first, second = rep.run_keys[LASER_ID]
        hub.ack(rep, {"runKeys": [first, "f" * 32, 7], "fileIds": [self.F2, None]})
        self.assertEqual(self.left(hub), ([second], [self.F1]))

    def test_no_laser_ack_from_an_older_site_keeps_everything(self):
        hub, rep = self.two_of_each()
        first = rep.run_keys[LASER_ID][0]
        for ack in (None, "ok", [], {"runKeys": "abc", "fileIds": {}}, {"runKeys": [first]},
                    {"runKeys": [first], "fileIds": "bad"}, {"runKeys": "bad", "fileIds": [self.F1]}):
            with self.subTest(ack=ack):
                hub.ack(rep, ack)
                self.assertEqual(self.left(hub), (rep.run_keys[LASER_ID], [self.F1, self.F2]))

    def test_a_verdict_that_arrived_after_the_beat_stays(self):
        hub, rep = self.two_of_each()
        late = {"fileId": self.F1, "sha256": SHA, "verdict": "checked", "result": {}}
        hub._check_results.append(late)
        hub.ack(rep, acked(rep))
        self.assertEqual(hub._check_results, [late])

    def test_a_site_that_never_acks_holds_at_most_32_runs_and_32_verdicts(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        hub.check_one = lambda o: {"fileId": o["fileId"], "sha256": SHA, "verdict": "retry", "detail": "x"}
        for i in range(40):
            hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1000 + i * 10, mono=1 + i * 10)
            hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=1001 + i * 10, mono=2 + i * 10)
            hub._check_queue = [offer(fileId=f"{i:08d}-bbbb-4ccc-8ddd-eeeeeeeeeeee")]
            hub._check_worker()
            rep = hub.report(now=1005 + i * 10, mono=6 + i * 10, poll=False)
            hub.ack(rep, {"runKeys": [], "fileIds": []})
        self.assertEqual((len(hub._lasers[LASER_ID].runs.closed), len(hub._check_results)), (32, 32))
        self.assertEqual(len(rep.lasers[0]["runs"]), 32)
        self.assertEqual(len(rep.file_checks), 16)


# --- H3 -------------------------------------------------------------------------------------------------------------

class H3PushListener(Base):
    def listen(self, allowed="127.0.0.1"):
        hub = self.hub(allowed=allowed)
        hub.start()
        self.addCleanup(hub.stop)
        return hub, hub._server.server_address[1]

    @staticmethod
    def post(port, body, token=PUSH_TOKEN, path="/ha/laser", method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        c.request(method, path, body=raw if method == "POST" else None, headers=headers)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    def test_wrong_peer_is_403_before_the_body_is_read(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        _, port = self.listen(allowed="127.0.0.2")
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(b"POST /ha/laser HTTP/1.1\r\nHost: x\r\nContent-Length: 100000\r\n\r\n")   # no body follows
            reply = s.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.0 403"), reply)

    def test_no_token_file_is_503(self):
        _, port = self.listen()
        self.assertEqual(self.post(port, {"laser": SLUG}), (503, {"error": "not_configured"}))

    def test_wrong_or_missing_bearer_is_401(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        _, port = self.listen()
        self.assertEqual(self.post(port, {"laser": SLUG}, token="x" * 40), (401, {"error": "unauthorized"}))
        self.assertEqual(self.post(port, {"laser": SLUG}, token=None), (401, {"error": "unauthorized"}))

    def test_bad_bodies_are_400_and_unknown_slugs_404(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        _, port = self.listen()
        for body in (b"[1, 2]", b"not json", json.dumps({"laser": SLUG, "pad": "x" * 5000}).encode(), b"{}",
                     json.dumps({"laser": 7}).encode()):
            with self.subTest(body=body[:20]):
                self.assertEqual(self.post(port, body), (400, {"error": "bad_request"}))
        self.assertEqual(self.post(port, {"laser": "some-other-laser"}), (404, {"error": "unknown_laser"}))
        self.assertEqual(self.post(port, {}, path="/elsewhere"), (404, {"error": "not_found"}))
        self.assertEqual(self.post(port, {}, method="GET"), (405, {"error": "method_not_allowed"}))

    def test_200_records_the_reading_and_unavailable_parses_to_null(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub, port = self.listen()
        body = {"laser": SLUG, "switch": "on", "currentA": "1.66", "powerW": "180.5"}
        self.assertEqual(self.post(port, body), (200, {"ok": True}))
        entry = hub.report(poll=False).lasers[0]
        self.assertEqual((entry["state"], entry["currentA"], entry["powerW"], entry["haLink"]), ("on_idle", 1.66, 180.5, "up"))
        self.assertIsNotNone(entry["haSeenAt"])
        body = {"laser": SLUG, "switch": "unavailable", "currentA": "unavailable", "powerW": "unknown"}
        self.assertEqual(self.post(port, body), (200, {"ok": True}))
        entry = hub.report(poll=False).lasers[0]
        self.assertEqual((entry["switch"], entry["currentA"], entry["powerW"], entry["state"]),
                         ("unavailable", None, None, "unknown"))

    def test_readings_are_parsed_and_clamped(self):
        for raw, want in (("", None), ("nan", None), ("inf", None), (True, None), ("-3", 0.0), (250, 100.0), (" 2.5 ", 2.5),
                          (10 ** 400, None), ([1], None), ({"a": 1}, None)):
            with self.subTest(raw=raw):
                hub = self.hub()
                hub.push({"laser": SLUG, "switch": "on", "currentA": raw})
                self.assertEqual(hub.report(poll=False).lasers[0]["currentA"], want)


# --- H4 / H5 ----------------------------------------------------------------------------------------------------------

class H4Dispatch(Base):
    def manager(self, hub=None):
        return PrinterManager(laser=hub or self.hub())

    def run_cmd(self, manager, command="laser_power_on", params=None, printer=LASER_ID, rid="r1"):
        if params is None:
            params = {"expiresAt": iso_in(120)}
        return manager.dispatch_commands([{"requestId": rid, "printerId": printer, "command": command, "params": params}])

    def detail(self, *a, **k):
        reports = self.run_cmd(*a, **k)
        self.assertEqual(len(reports), 1)
        return reports[0].get("detail") if reports[0]["status"] == "failed" else "ok"

    def test_a_laser_row_takes_the_laser_branch_not_printer_unavailable(self):
        m = self.manager()
        self.assertEqual(self.detail(m), "ha_not_configured")
        self.assertEqual(self.detail(PrinterManager()), "laser_unavailable")   # the default hub knows no lasers

    def test_detail_order_and_no_http_call(self):
        ha = self.server({("POST", "/api/webhook/abc"): (200, None)})
        self.secret(laser.WEBHOOK_URL_FILE, ha.url + "/api/webhook/abc")
        m = self.manager()
        self.assertEqual(self.detail(m, command="laser_power_off"), "unknown_command")
        self.assertEqual(self.detail(m, command="laser_frobnicate", params={}), "unknown_command")
        self.assertEqual(self.detail(m, params={}), "bad_params")
        self.assertEqual(self.detail(m, params={"expiresAt": "soon"}), "bad_params")
        self.assertEqual(self.detail(m, params={"expiresAt": "2026-10-14T12:00:00"}), "bad_params")   # no zone
        self.assertEqual(self.detail(m, params={"expiresAt": iso_in(-1)}), "expired")
        self.assertEqual(self.detail(m, printer="99999999-2222-4333-8444-555555555555"), "laser_unavailable")
        self.assertEqual(self.detail(m, printer="99999999-2222-4333-8444-555555555555", params={"expiresAt": iso_in(-5)}),
                         "expired")
        self.assertEqual(ha.calls, [])

    def test_load_params_are_checked_after_the_common_ones(self):
        m = self.manager()
        good = {"expiresAt": iso_in(120), "fileId": FILE_ID, "path": laser.LASER_FILE_PATH + FILE_ID, "sha256": SHA,
                "sizeBytes": len(BODY), "code": "AB1234", "maxPowerPct": 60}
        for k, bad in (("code", "ab1234"), ("code", "ABC123"), ("path", "/api/print/hub/file/x"), ("sha256", "A" * 64),
                       ("sizeBytes", 0), ("sizeBytes", 5 * 1024 * 1024), ("maxPowerPct", 101), ("maxPowerPct", True),
                       ("fileId", "../x")):
            with self.subTest(k=k, bad=bad):
                self.assertEqual(self.detail(m, command="laser_load", params={**good, k: bad}), "bad_params")
        self.assertEqual(self.detail(m, command="laser_load", params=good), "gate_not_configured")


class H5HomeAssistant(Base):
    def test_200_is_ok_and_posts_only_request_id_and_slug(self):
        ha = self.server({("POST", "/api/webhook/secret-id-123"): (200, None)})
        self.secret(laser.WEBHOOK_URL_FILE, ha.url + "/api/webhook/secret-id-123")
        with self.assertLogs("makeros-hub.laser", level=logging.INFO) as logs:
            reports = PrinterManager(laser=self.hub()).dispatch_commands(
                [{"requestId": "r7", "printerId": LASER_ID, "command": "laser_power_on", "params": {"expiresAt": iso_in(60)}}])
        self.assertEqual(reports, [{"requestId": "r7", "command": "laser_power_on", "status": "ok"}])
        self.assertEqual(json.loads(ha.calls[0]["body"]), {"requestId": "r7", "laser": SLUG})
        self.assertNotIn("secret-id-123", "\n".join(logs.output))

    def test_a_redelivered_ok_is_not_resent(self):
        ha = self.server({("POST", "/api/webhook/abc"): (200, None)})
        self.secret(laser.WEBHOOK_URL_FILE, ha.url + "/api/webhook/abc")
        m = PrinterManager(laser=self.hub())
        row = {"requestId": "r8", "printerId": LASER_ID, "command": "laser_power_on", "params": {"expiresAt": iso_in(60)}}
        self.assertEqual(m.dispatch_commands([row])[0]["status"], "ok")
        self.assertEqual(m.dispatch_commands([row])[0]["status"], "ok")
        self.assertEqual(len(ha.calls), 1)

    def test_failures(self):
        ha = self.server({("POST", "/api/webhook/abc"): (500, {"x": 1})})
        hub = self.hub()
        cases = [
            (ha.url + "/api/webhook/abc", 0o600, "ha_rejected"),
            (f"http://127.0.0.1:{closed_port()}/api/webhook/abc", 0o600, "ha_unreachable"),
            (ha.url + "/api/webhook/abc", 0o644, "ha_not_configured"),
            (ha.url + "/api/webhook/", 0o600, "ha_not_configured"),
            (ha.url + "/api/webhook/abc?x=1", 0o600, "ha_not_configured"),
            ("", 0o600, "ha_not_configured"),
        ]
        for n, (url, mode, want) in enumerate(cases):   # a requestId is attempted once, so each case has its own
            with self.subTest(want=want, mode=oct(mode)):
                self.secret(laser.WEBHOOK_URL_FILE, url, mode)
                self.assertEqual(hub.dispatch(f"r{n}", LASER_ID, "laser_power_on", {"expiresAt": iso_in(60)})["detail"], want)
        (self.secrets / laser.WEBHOOK_URL_FILE).unlink()
        self.assertEqual(hub.dispatch("r-last", LASER_ID, "laser_power_on", {"expiresAt": iso_in(60)})["detail"],
                         "ha_not_configured")


class H5Once(Base):
    """codex 2026-10-14: one HA attempt per requestId, a total deadline, and no turn-on without a [laser] table."""

    def dropping_ha(self):
        """Reads each request and closes without answering: HA got it, the hub never hears back."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)
        hits = []

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    hits.append(conn.recv(65536))
        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}", hits

    def test_a_lost_answer_is_never_followed_by_a_second_post(self):
        url, hits = self.dropping_ha()
        self.secret(laser.WEBHOOK_URL_FILE, url + "/api/webhook/abc")
        m = PrinterManager(laser=self.hub())
        row = {"requestId": "r9", "printerId": LASER_ID, "command": "laser_power_on", "params": {"expiresAt": iso_in(60)}}
        self.assertEqual(m.dispatch_commands([row])[0]["detail"], "ha_unreachable")
        self.assertEqual(m.dispatch_commands([row]), [])
        self.assertEqual(len(hits), 1)

    def test_no_laser_table_never_calls_ha(self):
        ha = self.server({("POST", "/api/webhook/abc"): (200, None)})
        self.secret(laser.WEBHOOK_URL_FILE, ha.url + "/api/webhook/abc")
        hub = LaserHub(None, secrets_dir=self.secrets)
        hub.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "x"}])
        self.assertEqual(hub.dispatch("r", LASER_ID, "laser_power_on", {"expiresAt": iso_in(60)})["detail"],
                         "ha_not_configured")
        self.assertEqual(ha.calls, [])

    def test_a_dripping_peer_cannot_hold_the_caller_past_the_deadline(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)

        def drip():
            conn, _ = srv.accept()
            with conn:
                for b in b"HTTP/1.0 200 OK\r\nX-Slow: " + b"x" * 20:
                    try:
                        conn.sendall(bytes([b]))
                    except OSError:
                        return
                    time.sleep(0.3)
        threading.Thread(target=drip, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(laser.Unreachable):
            laser.request("GET", f"http://127.0.0.1:{srv.getsockname()[1]}/status", timeout=1)
        self.assertLess(time.monotonic() - started, 2)


# --- H6 -------------------------------------------------------------------------------------------------------------

def load_params(**over):
    p = {"expiresAt": iso_in(120), "fileId": FILE_ID, "path": laser.LASER_FILE_PATH + FILE_ID, "sha256": SHA,
         "sizeBytes": len(BODY), "code": "AB1234", "maxPowerPct": 60}
    p.update(over)
    return p


class H6Load(Base):
    def store_gate(self, status=200, obj=None):
        return self.gate({("POST", "/store"): (status, obj if obj is not None else {"ok": True, "name": "AB1234"})})

    def result(self, hub):
        self.assertTrue(self.wait_for(lambda: not hub._load_running))
        return hub.take_command_results()

    def test_happy_path_returns_at_once_and_reports_on_a_later_drain(self):
        gate = self.store_gate()
        release = threading.Event()
        hub = self.hub(gate.url, fetcher=self.fetcher(wait=release))
        m = PrinterManager(laser=hub)
        started = time.monotonic()
        params = load_params(expiresAt=iso_in(30))
        reports = m.dispatch_commands([{"requestId": "L1", "printerId": LASER_ID, "command": "laser_load",
                                        "params": params}])
        self.assertEqual(reports, [])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(hub.take_command_results(), [])
        release.set()
        self.assertEqual(self.result(hub), [{"requestId": "L1", "command": "laser_load", "status": "ok"}])
        call = gate.calls[0]
        self.assertTrue(call["path"].startswith(f"/store?name=AB1234&maxPowerPct=60&sha256={SHA}&expiresAt="))
        expires = float(call["path"].split("expiresAt=")[1])
        self.assertGreater(expires, time.time())
        self.assertEqual(expires, laser.parse_instant(params["expiresAt"]))
        self.assertLessEqual(expires, time.time() + laser.STORE_TIMEOUT_S - 1)
        self.assertEqual(call["body"], BODY)
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {GATE_TOKEN}")
        self.assertEqual(m.dispatch_commands([{"requestId": "L1", "printerId": LASER_ID, "command": "laser_load",
                                               "params": load_params()}]), [])   # a redelivery stores nothing twice
        self.assertEqual(len(gate.calls), 1)

    def test_sha_mismatch_never_calls_the_gate(self):
        gate = self.store_gate()
        hub = self.hub(gate.url, fetcher=self.fetcher(data=BODY + b"x"))
        self.assertIsNone(hub.dispatch("L2", LASER_ID, "laser_load", load_params()))
        self.assertEqual(self.result(hub)[0]["detail"], "sha_mismatch")
        self.assertEqual(gate.calls, [])

    def test_fetch_failure(self):
        gate = self.store_gate()

        def broken(path, dest):
            raise hub_http.TransportError("GET http://cloud/x: HTTP 404")
        hub = self.hub(gate.url, fetcher=broken)
        hub.dispatch("L3", LASER_ID, "laser_load", load_params())
        self.assertEqual(self.result(hub)[0]["detail"], "fetch_failed")
        self.assertEqual(gate.calls, [])

    def test_gate_errors(self):
        long_reason = "command D9 00 at byte 12 isn't allowed " * 10
        cases = [((409, {"error": "head_moving"}), "gate:head_moving"),
                 ((422, {"error": "refused", "code": "command_not_allowed", "reason": long_reason, "offset": 12}),
                  ("gate:refused: " + long_reason)[:200]),
                 ((502, {"error": "count_mismatch", "before": 5, "after": 5}), "gate:count_mismatch"),
                 ((500, None), "gate:http_500")]
        for (status, obj), want in cases:
            with self.subTest(want=want[:30]):
                gate = self.gate({("POST", "/store"): (status, obj)})
                hub = self.hub(gate.url, fetcher=self.fetcher())
                hub.dispatch("L4", LASER_ID, "laser_load", load_params())
                detail = self.result(hub)[0]["detail"]
                self.assertEqual(detail, want)
                self.assertLessEqual(len(detail), 200)

    def test_gate_unreachable_and_not_configured(self):
        self.secret(laser.GATE_TOKEN_FILE, GATE_TOKEN)
        hub = self.hub(f"http://127.0.0.1:{closed_port()}", fetcher=self.fetcher())
        hub.dispatch("L5", LASER_ID, "laser_load", load_params())
        self.assertEqual(self.result(hub)[0]["detail"], "gate_unreachable")
        os.chmod(self.secrets / laser.GATE_TOKEN_FILE, 0o640)
        self.assertEqual(hub.dispatch("L6", LASER_ID, "laser_load", load_params())["detail"], "gate_not_configured")

    def test_a_second_load_while_one_runs_is_gate_busy(self):
        gate = self.store_gate()
        release = threading.Event()
        hub = self.hub(gate.url, fetcher=self.fetcher(wait=release))
        self.assertIsNone(hub.dispatch("L7", LASER_ID, "laser_load", load_params()))
        self.assertEqual(hub.dispatch("L8", LASER_ID, "laser_load", load_params())["detail"], "gate_busy")
        release.set()
        self.assertEqual(self.result(hub), [{"requestId": "L7", "command": "laser_load", "status": "ok"}])

    def test_a_request_that_expires_during_the_fetch_is_never_stored(self):
        gate = self.store_gate()
        expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)

        def slow(path, dest):
            while datetime.now(timezone.utc) <= expires:
                time.sleep(0.05)
            return self.fetcher()(path, dest)
        hub = self.hub(gate.url, fetcher=slow)
        hub.dispatch("L9", LASER_ID, "laser_load", load_params(expiresAt=expires.strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertEqual(self.result(hub)[0]["detail"], "expired")
        self.assertEqual(gate.calls, [])

    def test_a_redelivered_accepted_load_gets_no_second_result_even_after_expiry(self):
        gate = self.store_gate()
        release = threading.Event()
        hub = self.hub(gate.url, fetcher=self.fetcher(wait=release))
        m = PrinterManager(laser=hub)
        row = {"requestId": "L13", "printerId": LASER_ID, "command": "laser_load", "params": load_params()}
        self.assertEqual(m.dispatch_commands([row]), [])
        late = {**row, "params": load_params(expiresAt=iso_in(-10))}
        self.assertEqual(m.dispatch_commands([late]), [])
        self.assertEqual(m.dispatch_commands([{**row, "requestId": f"x{i}", "command": "pause"} for i in range(16)] + [late])[-1:],
                         [{"requestId": "x15", "command": "pause", "status": "failed", "detail": "printer_unavailable"}])
        release.set()
        self.assertEqual(self.result(hub), [{"requestId": "L13", "command": "laser_load", "status": "ok"}])

    def test_only_the_laser_the_gate_serves(self):
        other = "22222222-2222-4333-8444-555555555555"
        gate = self.store_gate()
        hub = self.hub(gate.url, fetcher=self.fetcher(), lasers=((LASER_ID, SLUG), (other, "second-laser")))
        self.assertEqual(hub.dispatch("L10", LASER_ID, "laser_load", load_params())["detail"], "gate_not_configured")
        hub = self.hub(gate.url, fetcher=self.fetcher(), lasers=((LASER_ID, SLUG), (other, "second-laser")), gate_laser=SLUG)
        self.assertEqual(hub.dispatch("L11", other, "laser_load", load_params())["detail"], "gate_not_configured")
        self.assertIsNone(hub.dispatch("L12", LASER_ID, "laser_load", load_params()))
        self.assertEqual(self.result(hub)[0]["status"], "ok")


# --- H7 -------------------------------------------------------------------------------------------------------------

def offer(**over):
    o = {"fileId": FILE_ID, "machineId": LASER_ID, "path": laser.LASER_FILE_PATH + FILE_ID, "sha256": SHA,
         "sizeBytes": len(BODY), "maxPowerPct": 40}
    o.update(over)
    return o


CHECKED = {"verdict": "checked", "sha256": SHA, "sizeBytes": len(BODY), "maxPowerPct": 40, "widthMm": 20.0,
           "heightMm": 20.0, "layers": [{"index": 0, "maxPowerPct": 30.0, "minPowerPct": 20.0, "speedMmS": 20.0}]}


class H7Checks(Base):
    def checks(self, hub, n=1):
        self.assertTrue(self.wait_for(lambda: len(hub.report(poll=False).file_checks) >= n and not hub._checking))
        return hub.report(poll=False).file_checks

    def test_material_check_needs_a_complete_receipt_for_this_file(self):
        gate = self.gate({("POST", "/check"): (200, CHECKED)})
        hub = self.hub(gate.url, fetcher=self.fetcher())
        o = offer(profile=[{"speed": 20, "min": 20, "max": 30}])
        self.assertEqual(hub.check_one(o)["verdict"], "retry")
        good = {**CHECKED, "checkVersion": 1, "preparedSha256": "a" * 64}
        gate.routes[("POST", "/check")] = (200, good)
        self.assertEqual(hub.check_one(o)["result"]["preparedSha256"], "a" * 64)
        gate.routes[("POST", "/check")] = (200, {**good, "sha256": "0" * 64})
        self.assertEqual(hub.check_one(o)["verdict"], "retry")

    def test_checked_and_refused_come_through(self):
        gate = self.gate({("POST", "/check"): (200, CHECKED)})
        hub = self.hub(gate.url, fetcher=self.fetcher())
        self.assertEqual(hub.offer_checks([offer()]), 1)
        got = self.checks(hub)
        self.assertEqual(got, [{"fileId": FILE_ID, "sha256": SHA, "verdict": "checked",
                                "result": {k: CHECKED[k] for k in ("sizeBytes", "maxPowerPct", "widthMm", "heightMm", "layers")}}])
        self.assertEqual(gate.calls[0]["path"], "/check?maxPowerPct=40")
        self.assertEqual(gate.calls[0]["body"], BODY)
        rep = hub.report(poll=False)
        hub.ack(rep, acked(rep))
        self.assertEqual(hub.report(poll=False).file_checks, [])

        refused = {"verdict": "refused", "code": "power_over_cap", "reason": "layer 0 power 90.0% is over the 40.0% cap",
                   "offset": None, "sha256": SHA, "sizeBytes": len(BODY)}
        gate.routes[("POST", "/check")] = (200, refused)
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub), [{"fileId": FILE_ID, "sha256": SHA, "verdict": "refused",
                                             "reason": refused["reason"], "code": "power_over_cap"}])

    def test_retries(self):
        gate = self.gate({("POST", "/check"): (503, {"error": "not_calibrated", "detail": "min_power_pct is not set (T5)"})})
        hub = self.hub(gate.url, fetcher=self.fetcher())
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub)[0], {"fileId": FILE_ID, "sha256": SHA, "verdict": "retry", "detail": "gate:not_calibrated"})

        hub = self.hub(None, fetcher=self.fetcher())
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub)[0]["detail"], "gate_not_configured")

        hub = self.hub(f"http://127.0.0.1:{closed_port()}", fetcher=self.fetcher())
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub)[0]["detail"], "gate_unreachable")

        def broken(path, dest):
            raise OSError("disk")
        hub = self.hub(gate.url, fetcher=broken)
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub)[0]["detail"], "fetch_failed")

    def test_a_damaged_download_is_refused_without_asking_the_gate(self):
        gate = self.gate({("POST", "/check"): (200, CHECKED)})
        hub = self.hub(gate.url, fetcher=self.fetcher(data=b"other bytes"))
        hub.offer_checks([offer()])
        self.assertEqual(self.checks(hub)[0], {"fileId": FILE_ID, "sha256": SHA, "verdict": "refused", "code": "sha_mismatch",
                                               "reason": "the file didn't arrive at the shop intact; upload it again"})
        self.assertEqual(gate.calls, [])

    def test_malformed_offers_are_skipped_and_at_most_3_taken(self):
        hub = self.hub(None, fetcher=self.fetcher())
        bad = [offer(machineId=[]), offer(machineId={"a": 1}), offer(fileId=None), offer(path=None),
               offer(path="/api/print/hub/file/x"), offer(machineId="99999999-2222-4333-8444-555555555555"),
               offer(sha256="x"), offer(maxPowerPct=0), offer(sizeBytes=5 * 1024 * 1024), "x"]
        self.assertEqual(hub.offer_checks(bad), 0)
        ids = [f"aaaaaaaa-bbbb-4ccc-8ddd-00000000000{i}" for i in range(5)]
        self.assertEqual(hub.offer_checks([offer(fileId=i, path=laser.LASER_FILE_PATH + i) for i in ids]), 3)


# --- H8 -------------------------------------------------------------------------------------------------------------

class H8GateStatus(Base):
    STATUS = {"gateVersion": "0.1.0", "controller": "up", "statusWord": 0x8000, "moving": True, "fileCount": 5,
              "seenAt": "2026-10-14T20:00:00Z", "storeEnabled": False,
              "calibrated": {"minPowerPct": False, "fileCountAddr": False}, "busy": False}

    def test_mapping(self):
        gate = self.gate({("GET", "/status"): (200, self.STATUS)})
        hub = self.hub(gate.url)
        self.assertEqual(hub.poll_gate(), ("up", None, {"idle": None, "moving": True, "statusWord": 0x8000, "fileCount": 5,
                                                         "seenAt": "2026-10-14T20:00:00Z"}))
        self.assertEqual(gate.calls[0]["headers"]["Authorization"], f"Bearer {GATE_TOKEN}")
        gate.routes[("GET", "/status")] = (200, {**self.STATUS, "controller": "down", "statusWord": None, "moving": None,
                                                 "fileCount": None})
        self.assertEqual(hub.poll_gate(), ("down", "controller_unreachable",
                                           {"idle": None, "moving": None, "statusWord": None, "fileCount": None, "seenAt": "2026-10-14T20:00:00Z"}))
        gate.routes[("GET", "/status")] = (401, {"error": "unauthorized"})
        self.assertEqual(hub.poll_gate(), ("down", "gate_unauthorized", None))
        gate.routes[("GET", "/status")] = (500, None)
        self.assertEqual(hub.poll_gate(), ("down", "gate_error", None))
        self.assertEqual(self.hub(f"http://127.0.0.1:{closed_port()}").poll_gate(), ("down", "gate_unreachable", None))
        self.assertEqual(self.hub(None).poll_gate(), ("not_configured", None, None))
        os.chmod(self.secrets / laser.GATE_TOKEN_FILE, 0o644)
        self.assertEqual(hub.poll_gate(), ("not_configured", None, None))
        (self.secrets / laser.GATE_TOKEN_FILE).unlink()
        self.assertEqual(hub.poll_gate(), ("not_configured", None, None))

    def test_the_report_carries_the_poll_for_the_served_laser_only(self):
        gate = self.gate({("GET", "/status"): (200, self.STATUS)})
        other = "22222222-2222-4333-8444-555555555555"
        hub = self.hub(gate.url, lasers=((LASER_ID, SLUG), (other, "second-laser")), gate_laser=SLUG)
        by_id = {e["machineId"]: e for e in hub.report().lasers}
        self.assertEqual((by_id[LASER_ID]["gateLink"], by_id[LASER_ID]["gate"]["fileCount"]), ("up", 5))
        self.assertEqual((by_id[other]["gateLink"], by_id[other]["gate"]), ("not_configured", None))


# --- H9 -------------------------------------------------------------------------------------------------------------

class H9Payload(Base):
    def test_exact_keys_caps_and_omission(self):
        ids = [f"{i:08d}-2222-4333-8444-555555555555" for i in range(10)]
        hub = self.hub(lasers=[(i, f"laser-{n}") for n, i in enumerate(ids)])
        rep = hub.report(poll=False)
        self.assertEqual(len(rep.lasers), 8)
        for entry in rep.lasers:
            self.assertEqual(set(entry), LASER_KEYS)
        payload = heartbeat_payload(lasers=rep.lasers, laser_file_checks=[{"fileId": str(n)} for n in range(20)])
        self.assertEqual(len(payload["lasers"]), 8)
        self.assertEqual(len(payload["laserFileChecks"]), 16)
        self.assertNotIn("lasers", heartbeat_payload())
        self.assertNotIn("laserFileChecks", heartbeat_payload())
        self.assertIsNone(self.hub(lasers=()).report(poll=False).lasers)

    def test_runs_capped_at_32_and_checks_at_16(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        for i in range(40):
            hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=1000 + i * 10, mono=1 + i * 10)
            hub.push({"laser": SLUG, "switch": "off", "currentA": 0}, now=1001 + i * 10, mono=2 + i * 10)
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3}, now=2000, mono=1001)
        self.assertEqual(len(hub.report(now=2001, mono=1002, poll=False).lasers[0]["runs"]), 32)
        hub._check_results = [{"fileId": str(n)} for n in range(20)]
        self.assertEqual(len(hub.report(poll=False).file_checks), 16)


class H9RunLoop(Base):
    """The agent loop: lasers ride the heartbeat, a load's result joins commandResults, and a 200 acks and takes offers."""

    def test_one_beat(self):
        stop = threading.Event()
        sent, offered, acks = [], [], []

        class Nothing:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def stop_sync(self):
                pass

            def current_model(self):
                return None

        def pull(cfg, credential, manager, **_k):
            manager.laser.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "OMTech"}])
            manager.laser._command_results.append({"requestId": "L1", "command": "laser_load", "status": "ok"})
            manager.laser.offer_checks = lambda offers: offered.append(offers) or 0
            manager.laser.ack = lambda rep, laser_ack: acks.append((rep, laser_ack))
            return None

        def post(url, body, **_k):
            sent.append(body)
            stop.set()
            return hub_http.Response(200, {"hubId": "h", "configVersion": "v", "nextPollMs": 30000, "applied": 0,
                                           "laserChecks": [offer()], "laserAck": {"runKeys": [], "fileIds": []}})

        with mock.patch("makeros_hub.agent.read_credential", return_value="cred"), \
                mock.patch("makeros_hub.agent._pull_config", side_effect=pull), \
                mock.patch("makeros_hub.agent.IngestServer", Nothing), \
                mock.patch("makeros_hub.agent.VirtualPrinterManager", Nothing), \
                mock.patch("makeros_hub.agent.tailscale_binary_exists", return_value=False), \
                mock.patch("makeros_hub.agent.post_json", side_effect=post), \
                mock.patch("makeros_hub.agent._drain_vprinter_submissions_safely", return_value=0):
            agent.run(Config(cloud_url="https://host.example", heartbeat_sec=5), _stop_event=stop, _install_signals=False)
        [body] = sent
        self.assertFalse(any(key.startswith("moonraker") for key in body))
        self.assertEqual([e["machineId"] for e in body["lasers"]], [LASER_ID])
        self.assertEqual((body["lasers"][0]["haLink"], body["lasers"][0]["gateLink"]), ("not_configured", "not_configured"))
        self.assertIn({"requestId": "L1", "command": "laser_load", "status": "ok"}, body["commandResults"])
        self.assertEqual(offered, [[offer()]])
        [(rep, laser_ack)] = acks
        self.assertEqual((rep.lasers, laser_ack), (body["lasers"], {"runKeys": [], "fileIds": []}))


# --- H10 ------------------------------------------------------------------------------------------------------------

class H10ConfigDown(Base):
    def test_lasers_parsed_malformed_skipped_printers_unaffected(self):
        agent._reset_restart_nonce_state()   # no restartNonce here; never let another test's nonce exit the run
        manager = PrinterManager(laser=self.hub(lasers=()))
        body = {"version": "v1", "printers": [], "lasers": [
            {"id": LASER_ID, "slug": SLUG, "name": "OMTech"},
            {"id": "not-a-uuid", "slug": "x", "name": "x"},
            {"id": "33333333-2222-4333-8444-555555555555", "slug": "Bad Slug", "name": "x"},
            {"id": "44444444-2222-4333-8444-555555555555", "slug": "ok-slug"},
            "junk"]}
        with mock.patch("makeros_hub.agent.get_json", return_value=hub_http.Response(200, body)):
            _pull_config(Config(cloud_url="https://host.example"), "cred", manager, tailscale_reconciler=lambda _c: {})
        self.assertEqual({k: v.slug for k, v in manager.laser._lasers.items()}, {LASER_ID: SLUG})
        self.assertEqual(manager.config_version, "v1")
        with mock.patch("makeros_hub.agent.get_json", return_value=hub_http.Response(200, {"version": "v2", "printers": []})):
            _pull_config(Config(cloud_url="https://host.example"), "cred", manager, tailscale_reconciler=lambda _c: {})
        self.assertEqual(manager.laser._lasers, {})

    def test_a_laser_that_stays_keeps_its_state(self):
        self.secret(laser.PUSH_TOKEN_FILE, PUSH_TOKEN)
        hub = self.hub()
        hub.push({"laser": SLUG, "switch": "on", "currentA": 3})
        hub.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "renamed"}])
        self.assertIsNotNone(hub._lasers[LASER_ID].runs.open)

    def test_laser_table(self):
        self.assertIsNone(parse_laser_config(None))
        cfg = parse_laser_config({"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://127.0.0.1:7650/"})
        self.assertEqual((cfg.ha_push_listen, cfg.gate_url, cfg.cutting_amps, cfg.on_amps, cfg.run_gap_s),
                         (("0.0.0.0", 8788), "http://127.0.0.1:7650", 2.0, 0.5, 60.0))
        self.assertEqual(parse_laser_config({"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://10.0.3.1:7650"}).gate_url,
                         "http://10.0.3.1:7650")
        for bad in ({}, {"ha_push_allowed_ip": "ha.local"}, {"ha_push_allowed_ip": "192.168.1.213", "gate_ur": "x"},
                    {"ha_push_allowed_ip": "192.168.1.213", "gate_url": "https://127.0.0.1:7650"},
                    {"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://8.8.8.8:7650"},
                    {"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://100.64.0.5:7650"},
                    {"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://127.0.0.1"},
                    {"ha_push_allowed_ip": "192.168.1.213", "gate_url": "http://user:pw@127.0.0.1:7650"},
                    {"ha_push_allowed_ip": "192.168.1.213", "ha_push_listen": "0.0.0.0"},
                    {"ha_push_allowed_ip": "192.168.1.213", "cutting_amps": 0.4},
                    {"ha_push_allowed_ip": "192.168.1.213", "run_gap_s": True},
                    {"ha_push_allowed_ip": "192.168.1.213", "run_gap_s": 10 ** 400}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_laser_config(bad)

    def test_a_bad_laser_table_never_stops_the_agent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('cloud_url = "https://host.example"\n[laser]\nha_push_allowed_ip = "nope"\n')
            with mock.patch.object(config, "CONFIG_PATH", path), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MAKEROS_HUB_CLOUD_URL", None)
                cfg = config.load_config()
            self.assertEqual((cfg.cloud_url, cfg.laser), ("https://host.example", None))
            path.write_text('cloud_url = "https://host.example"\n[laser]\nha_push_allowed_ip = "192.168.1.213"\n')
            with mock.patch.object(config, "CONFIG_PATH", path):
                self.assertEqual(config.load_config().laser.ha_push_allowed_ip, "192.168.1.213")


# --- H11 ------------------------------------------------------------------------------------------------------------

class H11Static(unittest.TestCase):
    def test_the_gate_calls_allow_a_slow_check(self):
        # the gate's note: a worst-case file takes ~10 s on a desktop and longer on a Pi; /store checks it again first
        self.assertGreaterEqual(laser.CHECK_TIMEOUT_S, 60)
        self.assertGreaterEqual(laser.STORE_TIMEOUT_S, laser.CHECK_TIMEOUT_S)

    def test_no_off_verb_and_no_gate_import_in_the_agent(self):
        for f in (ROOT / "makeros_hub").rglob("*.py"):
            text = f.read_text()
            with self.subTest(file=f.name):
                self.assertNotIn("turn_off", text)
                self.assertNotIn("laser_power_off", text)
                for node in ast.walk(ast.parse(text)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                    self.assertFalse([n for n in names if n.split(".")[0] == "laser_gate"], names)

    def test_the_ha_doc_has_no_off_action_and_no_webhook_id(self):
        doc = (ROOT / "docs" / "laser-ha.md").read_text()
        self.assertNotIn("turn_off", doc)
        self.assertIn('webhook_id: "<minted by the operator; never committed>"', doc)
        # a recent sighting counts only while the camera reads off now, never on unavailable/unknown or last_changed
        self.assertIn("(p[0].state == 'off' and 0 <= age < 120)", doc)
        self.assertNotIn("last_changed).total_seconds()", doc)


if __name__ == "__main__":
    unittest.main()
