import unittest
from unittest import mock

from makeros_hub.printers.manager import PrinterManager
from makeros_hub.printers.camera import CameraScheduler, collect_camera_frames


class TestCameraTargets(unittest.TestCase):
    """`camera_targets()` exposes per-printer camera-routing facts (incl. the
    admin `cameraEnabled` flag from config-down) for the heartbeat capturer.
    Uses vendor='other' so no Bambu/MQTT adapter is started."""

    def test_config_camera_identity_survives_manager_and_capture(self):
        config = {"id": "p", "vendor": "bambu", "model": "A1 mini", "host": "192.0.2.2",
                  "serial": "test", "cameraSnapshotUrl": "http://pi:8080/snapshot", "cameraEnabled": True}
        manager = PrinterManager()
        with mock.patch.object(manager, "_reconcile_bambu"):
            manager.reconcile([config], version="v1")
        # This fixed vector is also asserted by pstation against its stored printer facts.
        expected = "3560a72d7d65e1a4c913b515a6c1accbd2a004da910f4752cc62128cbfb69c90"
        for jpeg in (b"jpeg", None):
            frames, failures = collect_camera_frames(
                manager.camera_targets(), {}, CameraScheduler(), now=0, capture=lambda _: jpeg,
            )
            self.assertEqual((frames + failures)[0]["sourceId"], expected)

    def test_targets_carry_camera_enabled_and_routing(self):
        m = PrinterManager()
        m.reconcile(
            [
                {
                    "id": "p1",
                    "vendor": "other",
                    "model": "Ender",
                    "moonrakerUrl": "http://k:7125",
                    "cameraEnabled": True,
                },
                {"id": "p2", "vendor": "other", "model": "X", "cameraEnabled": False},
            ],
            version="v1",
        )
        targets = {t["printerId"]: t for t in m.camera_targets()}
        self.assertEqual(targets["p1"]["cameraEnabled"], True)
        self.assertEqual(targets["p2"]["cameraEnabled"], False)
        self.assertEqual(targets["p1"]["moonrakerUrl"], "http://k:7125")
        self.assertEqual(targets["p1"]["model"], "Ender")

    def test_missing_camera_enabled_defaults_false(self):
        m = PrinterManager()
        m.reconcile([{"id": "p1", "vendor": "other"}], version="v1")
        self.assertEqual(m.camera_targets()[0]["cameraEnabled"], False)

    def test_reconcile_rebuilds_targets_no_stale(self):
        m = PrinterManager()
        m.reconcile([{"id": "p1", "vendor": "other", "cameraEnabled": True}], version="v1")
        m.reconcile([{"id": "p2", "vendor": "other"}], version="v2")
        self.assertEqual({t["printerId"] for t in m.camera_targets()}, {"p2"})


if __name__ == "__main__":
    unittest.main()
