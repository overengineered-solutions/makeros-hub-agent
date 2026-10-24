"""Gate -> hub -> pstation, field names end to end (contract 2026-10-14).

The real gate (laser_gate, branch laser-gate) runs on loopback in front of its fake Ruida controller; the hub's real
LaserHub polls its /status, has it /check files and /store a load; the heartbeat body the hub builds is then read the way
pstation reads it. PSTATION below mirrors pstation's ingest rules (laser-control c008d5d, src/lib/laser.ts:
ingestLaserHeartbeat, ingestLaserRuns, ingestLaserFileChecks; hub-commands.ts ingestCommandResults). Change both together.

Skipped until the gate is in this tree (merge laser-gate), or run it with the gate checkout on PYTHONPATH.
"""
import hashlib
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fx = pytest.importorskip("laser_gate_fixtures", reason="the gate (laser_gate) is not in this tree yet")

from makeros_hub import laser  # noqa: E402
from makeros_hub.agent import heartbeat_payload  # noqa: E402
from makeros_hub.laser import LaserConfig, LaserHub  # noqa: E402
from makeros_hub.printers.manager import PrinterManager  # noqa: E402

LASER_ID = "11111111-2222-4333-8444-555555555555"
SLUG = "omtech-pro-80w"
FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PUSH_TOKEN = "p" * 40

# --- pstation's reading of the heartbeat (keep in step with src/lib/laser.ts) ---
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ISO_INSTANT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?(\.\d+)?(Z|[+-]\d\d:?\d\d)$")
RUN_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LASER_STATES = {"off", "on_idle", "cutting", "unknown"}
LASER_LINKS = {"up", "down", "not_configured"}
SWITCH_STATES = {"on", "off", "unavailable", "unknown"}
HUB_END_REASONS = {"idle", "switch_off", "telemetry_lost"}


def num_or_none(v):
    return v is None or (type(v) in (int, float))


def pstation_accepts_laser(entry):
    """What ingestLaserHeartbeat needs to write the row (it skips the entry otherwise), plus the optional fields it reads."""
    g = entry.get("gate")
    assert UUID.match(entry["machineId"])
    assert entry["state"] in LASER_STATES and entry["haLink"] in LASER_LINKS and entry["gateLink"] in LASER_LINKS
    assert ISO_INSTANT.match(entry["stateSince"])
    assert entry["switch"] is None or entry["switch"] in SWITCH_STATES
    assert num_or_none(entry["currentA"]) and num_or_none(entry["powerW"])
    assert entry["haSeenAt"] is None or ISO_INSTANT.match(entry["haSeenAt"])
    assert entry["gateDetail"] is None or isinstance(entry["gateDetail"], str)
    assert g is None or (set(g) >= {"moving", "fileCount", "seenAt"} and (g["moving"] is None or isinstance(g["moving"], bool))
                         and (g["fileCount"] is None or type(g["fileCount"]) is int)
                         and (g["seenAt"] is None or ISO_INSTANT.match(g["seenAt"])))
    for r in entry["runs"]:
        assert RUN_KEY.match(r["runKey"]) and ISO_INSTANT.match(r["startedAt"]) and ISO_INSTANT.match(r["lastCuttingAt"])
        assert (r["endedAt"] is None) == (r["endReason"] is None)
        assert r["endedAt"] is None or ISO_INSTANT.match(r["endedAt"])
        assert r["endReason"] is None or r["endReason"] in HUB_END_REASONS
    return True


def pstation_accepts_check(c):
    assert UUID.match(c["fileId"]) and re.fullmatch(r"[0-9a-f]{64}", c["sha256"])
    assert c["verdict"] in {"checked", "refused", "retry"}
    if c["verdict"] == "checked":
        assert isinstance(c["result"], dict)
        assert set(c["result"]) == {"checkVersion", "preparedSha256", "sizeBytes", "maxPowerPct", "widthMm", "heightMm", "layers"}
        assert laser._checked(c["result"])
    if c["verdict"] == "refused":
        assert isinstance(c["reason"], str) and c["reason"].strip() and len(c["reason"]) <= 300
        assert isinstance(c.get("code"), str)
    return True


class GateHubPstation(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.secrets = Path(d.name)
        for name, value in ((laser.GATE_TOKEN_FILE, fx.TOKEN), (laser.PUSH_TOKEN_FILE, PUSH_TOKEN)):
            (self.secrets / name).write_text(value)
            (self.secrets / name).chmod(0o600)
        self.fake = fx.FakeController()
        self.addCleanup(self.fake.close)
        self.gate = fx.gate_for(self.fake)
        self.addCleanup(self.fake.assert_clean, self.gate.reads)   # every datagram the gate sent passed its guard
        self.srv = fx.Running(self.gate)
        self.addCleanup(self.srv.close)
        self.files = {}

        def fetch(path, dest):   # the cloud's GET /api/print/hub/laser-file/<id>
            data = self.files[path]
            Path(dest).write_bytes(data)
            return hashlib.sha256(data).hexdigest(), len(data)

        cfg = LaserConfig(ha_push_allowed_ip="127.0.0.1", ha_push_listen=("127.0.0.1", 0),
                          gate_url=f"http://127.0.0.1:{self.srv.port}")
        self.hub = LaserHub(cfg, fetcher=fetch, secrets_dir=self.secrets)
        self.hub.set_lasers([{"id": LASER_ID, "slug": SLUG, "name": "OMTech Pro 80W"}])

    def offer(self, data, file_id=FILE_ID, cap=50):
        path = laser.LASER_FILE_PATH + file_id
        self.files[path] = data
        return {"fileId": file_id, "machineId": LASER_ID, "path": path, "sha256": hashlib.sha256(data).hexdigest(),
                "sizeBytes": len(data), "maxPowerPct": cap, "profile": [{"speed": 20, "min": 20, "max": 30}]}

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)
        self.assertTrue(predicate())

    def test_status_checks_runs_and_load_reach_pstation_in_its_shape(self):
        self.hub.push({"laser": SLUG, "switch": "on", "currentA": "2.4", "powerW": "300"})
        refused_file = fx.swizzle(fx.insert_before(fx.square_plain(), 0x88, b"\xD9\x00\x00")[0])
        self.hub.offer_checks([self.offer(fx.square()),
                               self.offer(refused_file, file_id="aaaaaaaa-bbbb-4ccc-8ddd-000000000001")])
        self.wait(lambda: len(self.hub.report(poll=False).file_checks) == 2)
        report = self.hub.report()   # polls the real gate /status
        payload = heartbeat_payload(lasers=report.lasers, laser_file_checks=report.file_checks)

        [entry] = payload["lasers"]
        self.assertTrue(pstation_accepts_laser(entry))
        self.assertEqual((entry["state"], entry["haLink"], entry["gateLink"], entry["gateDetail"]), ("cutting", "up", "up", None))
        self.assertEqual((entry["gate"]["moving"], entry["gate"]["fileCount"]), (False, 5))
        self.assertEqual(len(entry["runs"]), 1)

        by_id = {c["fileId"]: c for c in payload["laserFileChecks"]}
        for c in by_id.values():
            self.assertTrue(pstation_accepts_check(c))
        checked = by_id[FILE_ID]
        self.assertEqual((checked["verdict"], checked["result"]["widthMm"], checked["result"]["heightMm"]), ("checked", 20.0, 20.0))
        refused = by_id["aaaaaaaa-bbbb-4ccc-8ddd-000000000001"]
        self.assertEqual((refused["verdict"], refused["code"]), ("refused", "command_not_allowed"))
        self.assertIn("D9 00", refused["reason"])

        # Load through the real gate: the fake controller's stored-file count steps by one
        data = fx.square()
        o = self.offer(data)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manager = PrinterManager(laser=self.hub)
        params = {"expiresAt": expires, "fileId": FILE_ID, "path": o["path"], "sha256": o["sha256"],
                  "sizeBytes": o["sizeBytes"], "code": "MD0042", "maxPowerPct": 50,
                  "profile": o["profile"], "preparedSha256": checked["result"]["preparedSha256"]}
        self.assertEqual(manager.dispatch_commands([{"requestId": "22222222-0000-4000-8000-000000000001",
                                                     "printerId": LASER_ID, "command": "laser_load", "params": params}]), [])
        self.wait(lambda: not self.hub._load_running)
        [result] = self.hub.take_command_results()
        self.assertEqual(result, {"requestId": "22222222-0000-4000-8000-000000000001", "command": "laser_load", "status": "ok"})
        self.assertEqual(self.fake.mem[fx.FILE_COUNT], 6)
        self.assertTrue(self.fake.jobs()[0].startswith(b"\xE8\x02\xE7\x01MD0042\x00"))

    def test_gate_refusals_become_the_details_pstation_words(self):
        self.fake.mem[0x0200] = 1 << 15   # the head is moving
        o = self.offer(fx.square())
        expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"expiresAt": expires, "fileId": FILE_ID, "path": o["path"], "sha256": o["sha256"],
                  "sizeBytes": o["sizeBytes"], "code": "MD0043", "maxPowerPct": 50}
        self.assertIsNone(self.hub.dispatch("r1", LASER_ID, "laser_load", params))
        self.wait(lambda: not self.hub._load_running)
        self.assertEqual(self.hub.take_command_results()[0]["detail"], "gate:head_moving")
        entry = self.hub.report().lasers[0]
        self.assertEqual((entry["gateLink"], entry["gate"]["moving"]), ("up", True))

        self.fake.mem[0x0200] = 0
        params["maxPowerPct"] = 10   # the square's layer runs 20-30 %; the gate caps min and max, and counts layers from 1
        self.hub.dispatch("r2", LASER_ID, "laser_load", params)
        self.wait(lambda: not self.hub._load_running)
        detail = self.hub.take_command_results()[0]["detail"]
        self.assertEqual(detail, "gate:refused: layer 1 power 20.0% is over the 10.0% cap")
        self.assertEqual(self.fake.jobs(), [])   # nothing but status reads reached the controller

    def test_a_gate_that_cannot_reach_the_controller(self):
        self.fake.close()
        # a closed port: the gate answers /status with controller "down"
        gate = fx.gate_for(None)
        srv = fx.Running(gate)
        self.addCleanup(srv.close)
        self.hub.cfg = LaserConfig(ha_push_allowed_ip="127.0.0.1", ha_push_listen=("127.0.0.1", 0),
                                   gate_url=f"http://127.0.0.1:{srv.port}")
        entry = self.hub.report().lasers[0]
        self.assertTrue(pstation_accepts_laser(entry))
        self.assertEqual((entry["gateLink"], entry["gateDetail"]), ("down", "controller_unreachable"))


if __name__ == "__main__":
    unittest.main()
