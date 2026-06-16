import unittest
from unittest import mock

from makeros_hub.printers import camera


class FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, _n: int = -1) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _bambu(**over):
    return {"vendor": "bambu", "model": "A1 mini", "host": "1.2.3.4", "accessCode": "AbCd1234", **over}


class TestSourceKind(unittest.TestCase):
    def test_bambu_a1_is_lan(self):
        self.assertEqual(camera.camera_source_kind(_bambu()), "bambu-lan")

    def test_bambu_rtsp_class_models(self):
        # X1/H2/P2S stream RTSPS:322 → bambu-rtsp (ffmpeg path).
        for model in ("X1 Carbon", "X1C", "X1E", "H2D", "H2S", "P2S", "X2D"):
            self.assertEqual(
                camera.camera_source_kind(_bambu(model=model)), "bambu-rtsp", model
            )

    def test_bambu_image_class_models(self):
        # A1/P1 use the :6000 raw-JPEG path → bambu-lan.
        for model in ("A1 mini", "A1", "P1P", "P1S"):
            self.assertEqual(
                camera.camera_source_kind(_bambu(model=model)), "bambu-lan", model
            )

    def test_bambu_without_creds_is_none(self):
        self.assertIsNone(camera.camera_source_kind(_bambu(host="", accessCode="")))
        # even an RTSP-class model needs host+code before we route it
        self.assertIsNone(camera.camera_source_kind(_bambu(model="X1C", host="")))

    def test_klipper_with_explicit_url_is_http(self):
        self.assertEqual(
            camera.camera_source_kind(
                {"vendor": "klipper", "cameraSnapshotUrl": "http://k/webcam/?action=snapshot"}
            ),
            "http-snapshot",
        )

    def test_klipper_with_moonraker_url_is_http(self):
        self.assertEqual(
            camera.camera_source_kind({"vendor": "klipper", "moonrakerUrl": "http://k:7125"}),
            "http-snapshot",
        )

    def test_other_without_any_url_is_none(self):
        self.assertIsNone(camera.camera_source_kind({"vendor": "other"}))


class TestSnapshotUrl(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(
            camera._snapshot_url({"cameraSnapshotUrl": "http://x/snap.jpg", "moonrakerUrl": "http://y"}),
            "http://x/snap.jpg",
        )

    def test_derives_from_moonraker(self):
        self.assertEqual(
            camera._snapshot_url({"moonrakerUrl": "http://k:7125/"}),
            "http://k:7125/webcam/?action=snapshot",
        )

    def test_adds_scheme_for_bare_host(self):
        self.assertEqual(
            camera._snapshot_url({"host": "10.0.0.5"}),
            "http://10.0.0.5/webcam/?action=snapshot",
        )


class TestHttpSnapshot(unittest.TestCase):
    def test_returns_jpeg_bytes(self):
        jpeg = b"\xff\xd8\xff\xe0body\xff\xd9"
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(jpeg)):
            self.assertEqual(camera.http_snapshot("http://k/snap"), jpeg)

    def test_rejects_non_jpeg(self):
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(b"<html>nope")):
            self.assertIsNone(camera.http_snapshot("http://k/snap"))

    def test_rejects_oversized(self):
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(b"\xff\xd8" + b"x" * 50)):
            self.assertIsNone(camera.http_snapshot("http://k/snap", max_bytes=10))

    def test_none_on_error(self):
        with mock.patch.object(camera.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIsNone(camera.http_snapshot("http://k/snap"))

    def test_empty_url(self):
        self.assertIsNone(camera.http_snapshot(""))

    def test_rejects_non_http_scheme(self):
        # file:// / ftp:// etc. must never be fetched (no urlopen call at all).
        with mock.patch.object(camera.urllib.request, "urlopen") as m:
            self.assertIsNone(camera.http_snapshot("file:///etc/passwd"))
            self.assertIsNone(camera.http_snapshot("ftp://host/x.jpg"))
            m.assert_not_called()


class TestCapturePrinterFrame(unittest.TestCase):
    def test_bambu_routes_to_lan_capture(self):
        with mock.patch.object(camera, "_bambu_capture_frame", return_value=b"BAMBU") as m:
            self.assertEqual(camera.capture_printer_frame(_bambu()), b"BAMBU")
            m.assert_called_once_with("1.2.3.4", "AbCd1234")

    def test_bambu_rtsp_class_routes_to_rtsp_capture(self):
        # X1C/H2D/P2S go to the ffmpeg RTSP grabber, NOT the :6000 one.
        with mock.patch.object(camera, "_rtsp_capture_frame", return_value=b"RTSP") as rt, \
            mock.patch.object(camera, "_bambu_capture_frame") as lan:
            for model in ("X1C", "H2D", "P2S"):
                self.assertEqual(camera.capture_printer_frame(_bambu(model=model)), b"RTSP", model)
            lan.assert_not_called()
            rt.assert_called_with("1.2.3.4", "AbCd1234")

    def test_klipper_routes_to_http(self):
        jpeg = b"\xff\xd8\xff\xe0k\xff\xd9"
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(jpeg)):
            self.assertEqual(
                camera.capture_printer_frame({"vendor": "klipper", "moonrakerUrl": "http://k:7125"}),
                jpeg,
            )

    def test_no_camera_path_returns_none(self):
        self.assertIsNone(camera.capture_printer_frame({"vendor": "other"}))


class TestCameraScheduler(unittest.TestCase):
    def test_first_sighting_then_throttled_then_due(self):
        s = camera.CameraScheduler()
        # first ever -> capture
        self.assertTrue(s.should_capture("p", "printing", 50, now=0.0))
        # same state, within mid-print interval -> no
        self.assertFalse(s.should_capture("p", "printing", 50, now=10.0))
        # past the mid-print interval -> yes
        self.assertTrue(s.should_capture("p", "printing", 50, now=0.0 + camera.CameraScheduler.MID_PRINT_S))

    def test_state_change_forces_capture(self):
        s = camera.CameraScheduler()
        self.assertTrue(s.should_capture("p", "idle", None, now=0.0))
        # idle -> printing a moment later: state change beats the idle interval
        self.assertTrue(s.should_capture("p", "printing", 1, now=1.0))

    def test_first_layer_is_denser_than_mid(self):
        s = camera.CameraScheduler()
        self.assertEqual(s._interval("printing", 2), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("printing", 50), camera.CameraScheduler.MID_PRINT_S)
        self.assertEqual(s._interval("printing", 99), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("idle", None), camera.CameraScheduler.IDLE_S)

    def test_forget_drops_absent_printers(self):
        s = camera.CameraScheduler()
        s.should_capture("a", "idle", None, now=0.0)
        s.should_capture("b", "idle", None, now=0.0)
        s.forget({"a"})
        self.assertIn("a", s._last_capture)
        self.assertNotIn("b", s._last_capture)


class TestCollectCameraFrames(unittest.TestCase):
    def _targets(self):
        return [{"printerId": "p1", "vendor": "bambu"}, {"printerId": "p2", "vendor": "bambu"}]

    def test_captures_due_printers_and_base64s(self):
        s = camera.CameraScheduler()  # fresh -> both due (first sighting)
        frames = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=lambda _t: b"\xff\xd8\xff\xe0x\xff\xd9"
        )
        self.assertEqual({f["printerId"] for f in frames}, {"p1", "p2"})
        import base64 as b64
        self.assertEqual(b64.b64decode(frames[0]["jpegBase64"]), b"\xff\xd8\xff\xe0x\xff\xd9")

    def test_none_frame_is_skipped(self):
        s = camera.CameraScheduler()
        frames = camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=lambda _t: None)
        self.assertEqual(frames, [])

    def test_capture_exception_never_propagates(self):
        s = camera.CameraScheduler()

        def boom(_t):
            raise RuntimeError("camera exploded")

        frames = camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=boom)
        self.assertEqual(frames, [])

    def test_nothing_due_returns_empty(self):
        s = camera.CameraScheduler()
        # prime both so they're not first-sighting
        for pid in ("p1", "p2"):
            s.should_capture(pid, "printing", 50, now=0.0)
        frames = camera.collect_camera_frames(
            self._targets(),
            {"p1": {"state": "printing", "progressPct": 50}, "p2": {"state": "printing", "progressPct": 50}},
            s,
            now=1.0,  # within mid-print interval -> nothing due
            capture=lambda _t: b"\xff\xd8\xff\xe0x\xff\xd9",
        )
        self.assertEqual(frames, [])


if __name__ == "__main__":
    unittest.main()
