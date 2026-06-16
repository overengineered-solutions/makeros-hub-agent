"""Tests for BambuAdapter.send_command — LAN control commands (pause/resume/stop)."""

import json
import sys
import types
import unittest

# The agent test env doesn't install paho-mqtt (the adapter module is normally
# not imported under test). Stub the bits bambu.py touches at import + in
# send_command so this test runs anywhere; guarded so the real paho wins on a hub.
if "paho.mqtt.client" not in sys.modules:  # pragma: no cover - env shim
    _mqtt = types.ModuleType("paho.mqtt.client")
    _mqtt.MQTT_ERR_SUCCESS = 0
    _mqtt.MQTTv311 = 4
    _mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _mqtt.Client = type("Client", (), {})
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_mqtt.client = _mqtt
    _paho.mqtt = _paho_mqtt
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _mqtt

from makeros_hub.printers.bambu import BambuAdapter  # noqa: E402


class FakeInfo:
    def __init__(self, rc=0):
        self.rc = rc


class FakeClient:
    def __init__(self, connected=True, rc=0):
        self._connected = connected
        self._rc = rc
        self.published = []

    def is_connected(self):
        return self._connected

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        return FakeInfo(self._rc)


def make_adapter():
    return BambuAdapter(
        "p1", host="1.2.3.4", serial="SER123", access_code="code", model="A1 Mini"
    )


class TestSendCommand(unittest.TestCase):
    def test_publishes_correct_payload_for_each_command(self):
        for command in ("pause", "resume", "stop"):
            adapter = make_adapter()
            adapter._client = FakeClient(connected=True)
            adapter._connack = "ok"
            result = adapter.send_command(command)
            self.assertEqual(result, {"ok": True}, command)
            self.assertEqual(len(adapter._client.published), 1, command)
            topic, payload = adapter._client.published[0]
            self.assertEqual(topic, "device/SER123/request")
            doc = json.loads(payload)
            self.assertEqual(doc["print"]["command"], command)
            # pybambu's proven shape: param "" + a string sequence_id.
            self.assertEqual(doc["print"]["param"], "")
            self.assertIsInstance(doc["print"]["sequence_id"], str)

    def test_ams_dry_publishes_drying_payload(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True)
        adapter._connack = "ok"
        result = adapter.send_command(
            "ams_dry", {"amsId": 2, "temp": 50, "durationHours": 8}
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(adapter._client.published), 1)
        topic, payload = adapter._client.published[0]
        self.assertEqual(topic, "device/SER123/request")
        doc = json.loads(payload)["print"]
        seq = doc.pop("sequence_id")
        self.assertIsInstance(seq, str)
        self.assertTrue(seq)  # unique, non-empty per call (no static reuse)
        # Full wire shape verified verbatim against BambuStudio's
        # DevFilaSystemCtrl.cpp: cooling_temp is the post-dry cool target and the
        # source-of-truth client sends 0 (the >=45 floor is on `temp`, enforced
        # cloud-side); duration is in HOURS; mode 1 = timed dry.
        self.assertEqual(
            doc,
            {
                "command": "ams_filament_drying",
                "ams_id": 2,
                "mode": 1,
                "temp": 50,
                "cooling_temp": 0,
                "duration": 8,
                "humidity": 0,
                "rotate_tray": False,
                "filament": "",
                "close_power_conflict": False,
            },
        )

    def test_ams_dry_without_params_rejected(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True)
        adapter._connack = "ok"
        # Missing params entirely.
        self.assertEqual(
            adapter.send_command("ams_dry"),
            {"ok": False, "reason": "invalid_dry_params"},
        )
        # Present but malformed (amsId not an int).
        self.assertEqual(
            adapter.send_command("ams_dry", {"amsId": "x", "temp": 50, "durationHours": 8}),
            {"ok": False, "reason": "invalid_dry_params"},
        )
        # bool is a subclass of int — amsId=True must NOT pass as 1.
        self.assertEqual(
            adapter.send_command("ams_dry", {"amsId": True, "temp": 50, "durationHours": 8}),
            {"ok": False, "reason": "invalid_dry_params"},
        )
        # non-positive temp / duration rejected.
        self.assertEqual(
            adapter.send_command("ams_dry", {"amsId": 0, "temp": 0, "durationHours": 8}),
            {"ok": False, "reason": "invalid_dry_params"},
        )
        self.assertEqual(
            adapter.send_command("ams_dry", {"amsId": 0, "temp": 50, "durationHours": 0}),
            {"ok": False, "reason": "invalid_dry_params"},
        )
        self.assertEqual(adapter._client.published, [])

    def test_skip_objects_publishes_obj_list(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True)
        adapter._connack = "ok"
        result = adapter.send_command("skip_objects", {"objList": [286, 287]})
        self.assertEqual(result, {"ok": True})
        topic, payload = adapter._client.published[0]
        self.assertEqual(topic, "device/SER123/request")
        doc = json.loads(payload)["print"]
        seq = doc.pop("sequence_id")
        self.assertIsInstance(seq, str)
        self.assertTrue(seq)
        # Verified vs BambuStudio command_task_partskip: command + int obj_list.
        self.assertEqual(doc, {"command": "skip_objects", "obj_list": [286, 287]})

    def test_skip_objects_dedups_obj_list_preserving_order(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True)
        adapter._connack = "ok"
        result = adapter.send_command("skip_objects", {"objList": [287, 286, 287, 286]})
        self.assertEqual(result, {"ok": True})
        doc = json.loads(adapter._client.published[0][1])["print"]
        self.assertEqual(doc["obj_list"], [287, 286])  # deduped, order preserved

    def test_skip_objects_invalid_params_rejected(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True)
        adapter._connack = "ok"
        for bad in (
            None,
            {"objList": []},  # empty
            {"objList": "286"},  # not a list
            {"objList": [286, "287"]},  # non-int element
            {"objList": [286, True]},  # bool element (bool ⊂ int)
            {"objList": [286, -1]},  # negative id
            {"objList": list(range(65))},  # over the 64 ceiling
        ):
            self.assertEqual(
                adapter.send_command("skip_objects", bad),
                {"ok": False, "reason": "invalid_skip_params"},
                bad,
            )
        self.assertEqual(adapter._client.published, [])

    def test_unsupported_command_rejected_without_publish(self):
        adapter = make_adapter()
        adapter._client = FakeClient()
        adapter._connack = "ok"
        result = adapter.send_command("frobnicate")
        self.assertEqual(result, {"ok": False, "reason": "unsupported_command"})
        self.assertEqual(adapter._client.published, [])

    def test_not_connected_does_not_publish(self):
        adapter = make_adapter()
        adapter._client = None
        result = adapter.send_command("stop")
        self.assertEqual(result, {"ok": False, "reason": "not_connected"})

    def test_publish_rc_failure_reports_command_failed(self):
        adapter = make_adapter()
        adapter._client = FakeClient(connected=True, rc=1)
        adapter._connack = "ok"
        result = adapter.send_command("resume")
        self.assertEqual(result, {"ok": False, "reason": "command_failed"})


if __name__ == "__main__":
    unittest.main()
