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
    """v0.41.0: should_capture is side-effect-free for time-tracking — it only
    updates last_state for state-change detection. The caller must invoke
    mark_captured(printer_id, now) AFTER a successful frame to stamp the
    last-capture timer; failed captures stay due next beat so flaky printers
    self-heal at heartbeat cadence instead of going dark for IDLE_S."""

    def test_first_sighting_then_throttled_then_due(self):
        s = camera.CameraScheduler()
        # first ever -> capture
        self.assertTrue(s.should_capture("p", "printing", 50, now=0.0))
        # The first should_capture has NOT stamped the timer (mark-on-success
        # contract). Simulate the caller acknowledging the success.
        s.mark_captured("p", now=0.0)
        # Same state, within mid-print interval -> no
        self.assertFalse(s.should_capture("p", "printing", 50, now=10.0))
        # Past the mid-print interval -> yes again
        self.assertTrue(
            s.should_capture("p", "printing", 50, now=0.0 + camera.CameraScheduler.MID_PRINT_S)
        )

    def test_state_change_forces_capture(self):
        s = camera.CameraScheduler()
        self.assertTrue(s.should_capture("p", "idle", None, now=0.0))
        s.mark_captured("p", now=0.0)
        # idle -> printing a moment later: state change beats the idle interval
        self.assertTrue(s.should_capture("p", "printing", 1, now=1.0))

    def test_failed_capture_stays_due_next_beat(self):
        # The 2026-06-17 bug this fixes: pre-v0.41.0 every attempt stamped
        # last_capture, so a Liveview-off printer went dark for IDLE_S=600s
        # between retries. Now the caller only stamps on success → failed
        # printers retry every beat.
        s = camera.CameraScheduler()
        self.assertTrue(s.should_capture("p", "idle", None, now=0.0))
        # No mark_captured call (capture failed). Next beat at +40s:
        self.assertTrue(s.should_capture("p", "idle", None, now=40.0))
        # Still failing — next beat still due.
        self.assertTrue(s.should_capture("p", "idle", None, now=80.0))

    def test_first_layer_is_denser_than_mid(self):
        s = camera.CameraScheduler()
        self.assertEqual(s._interval("printing", 2), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("printing", 50), camera.CameraScheduler.MID_PRINT_S)
        self.assertEqual(s._interval("printing", 99), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("idle", None), camera.CameraScheduler.IDLE_S)

    def test_forget_drops_absent_printers(self):
        s = camera.CameraScheduler()
        s.should_capture("a", "idle", None, now=0.0)
        s.mark_captured("a", now=0.0)
        s.should_capture("b", "idle", None, now=0.0)
        s.mark_captured("b", now=0.0)
        s.forget({"a"})
        self.assertIn("a", s._last_capture)
        self.assertNotIn("b", s._last_capture)


class TestCollectCameraFrames(unittest.TestCase):
    def _targets(self):
        return [{"printerId": "p1", "vendor": "bambu"}, {"printerId": "p2", "vendor": "bambu"}]

    def test_captures_due_printers_and_base64s(self):
        s = camera.CameraScheduler()  # fresh -> both due (first sighting)
        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=lambda _t: b"\xff\xd8\xff\xe0x\xff\xd9"
        )
        self.assertEqual({f["printerId"] for f in frames}, {"p1", "p2"})
        self.assertEqual(failures, [])  # all due captured -> no failures
        import base64 as b64
        self.assertEqual(b64.b64decode(frames[0]["jpegBase64"]), b"\xff\xd8\xff\xe0x\xff\xd9")

    def test_none_frame_is_reported_as_failure_dict_shape(self):
        s = camera.CameraScheduler()
        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=lambda _t: None
        )
        self.assertEqual(frames, [])
        # v0.41.0: failures is list[dict{printerId,reason,stderrTail}].
        self.assertEqual({f["printerId"] for f in failures}, {"p1", "p2"})
        for f in failures:
            self.assertIn("reason", f)
            self.assertIn("stderrTail", f)

    def test_capture_exception_is_failure_not_propagated(self):
        s = camera.CameraScheduler()

        def boom(_t):
            raise RuntimeError("camera exploded")

        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=boom
        )
        self.assertEqual(frames, [])
        # Each printer has a failure dict; reason is 'unknown' for arbitrary
        # exceptions (the legacy bytes-capture path doesn't categorize).
        self.assertEqual({f["printerId"] for f in failures}, {"p1", "p2"})
        self.assertTrue(all(f["reason"] == "unknown" for f in failures))

    def test_nothing_due_returns_empty_no_failures(self):
        s = camera.CameraScheduler()
        # prime both via should_capture + mark_captured so they're no longer
        # first-sighting (v0.41.0 split — mark separately).
        for pid in ("p1", "p2"):
            s.should_capture(pid, "printing", 50, now=0.0)
            s.mark_captured(pid, now=0.0)
        frames, failures = camera.collect_camera_frames(
            self._targets(),
            {"p1": {"state": "printing", "progressPct": 50}, "p2": {"state": "printing", "progressPct": 50}},
            s,
            now=1.0,  # within mid-print interval -> nothing due
            capture=lambda _t: b"\xff\xd8\xff\xe0x\xff\xd9",
        )
        self.assertEqual(frames, [])
        # nothing due is NOT a silent drop — failures only counts due-but-failed.
        self.assertEqual(failures, [])

    def test_partial_failure_reports_only_the_failed(self):
        s = camera.CameraScheduler()
        # p1 captures, p2 returns None
        def cap(t):
            return b"\xff\xd8\xff\xe0x\xff\xd9" if t["printerId"] == "p1" else None

        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=cap
        )
        self.assertEqual([f["printerId"] for f in frames], ["p1"])
        self.assertEqual([f["printerId"] for f in failures], ["p2"])

    def test_timed_out_printer_appears_in_failures_without_blocking(self):
        # Codex review HIGH: the load-bearing path is the as_completed timeout
        # branch — a slow/unreachable camera must not stall the heartbeat AND
        # must appear in failures. Verified by wall-clock + result. v0.41.0:
        # the timed-out printer's failure dict carries reason='timeout'.
        import time as _t
        s = camera.CameraScheduler()
        def cap(t):
            if t["printerId"] == "p1":
                return b"\xff\xd8\xff\xe0x\xff\xd9"
            _t.sleep(0.3)  # well past overall_timeout
            return b"\xff\xd8\xff\xe0x\xff\xd9"

        t0 = _t.monotonic()
        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=cap, overall_timeout=0.05, max_workers=2
        )
        wall = _t.monotonic() - t0
        # The slow printer's worker is still running, but shutdown(wait=False,
        # cancel_futures=True) returns immediately — heartbeat is bounded.
        self.assertLess(wall, 0.25, f"heartbeat blocked by slow capture: {wall:.3f}s")
        self.assertEqual([f["printerId"] for f in frames], ["p1"])
        self.assertEqual([f["printerId"] for f in failures], ["p2"])
        self.assertEqual(failures[0]["reason"], "timeout")

    def test_failed_capture_does_not_mark_scheduler(self):
        # Direct regression test for the 2026-06-17 scheduler bug: pre-v0.41.0,
        # a failed capture still stamped last_capture so a Liveview-off printer
        # went dark for IDLE_S=600s between retries. Now the caller only marks
        # on success → a printer whose first capture returns None stays due
        # next beat.
        s = camera.CameraScheduler()
        camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=lambda _t: None
        )
        # Neither printer was successfully captured → scheduler has not stamped
        # either, so both are still due on the next beat.
        self.assertTrue(s.should_capture("p1", "idle", None, now=40.0))
        self.assertTrue(s.should_capture("p2", "idle", None, now=40.0))


class TestReasonRouting(unittest.TestCase):
    """v0.42.0: capture_printer_frame_with_reason surfaces categorized reasons
    from each transport (RTSP already did; :6000 + http now do too)."""

    def _bambu(self, **over):
        return {"vendor": "bambu", "model": "A1 mini", "host": "1.2.3.4", "accessCode": "c", **over}

    def test_bambu_lan_surfaces_reason_on_failure(self):
        from makeros_hub.printers.bambu_camera import CaptureResult as BR

        with mock.patch.object(
            camera, "_bambu_capture_with_reason",
            return_value=BR(None, "liveview-off", "LAN-mode off"),
        ):
            jpeg, reason, detail = camera.capture_printer_frame_with_reason(self._bambu())
        self.assertIsNone(jpeg)
        self.assertEqual(reason, "liveview-off")
        self.assertEqual(detail, "LAN-mode off")

    def test_bambu_lan_success(self):
        from makeros_hub.printers.bambu_camera import CaptureResult as BR

        with mock.patch.object(
            camera, "_bambu_capture_with_reason", return_value=BR(b"\xff\xd8\xff\xe0x\xff\xd9", None, "")
        ):
            jpeg, reason, _ = camera.capture_printer_frame_with_reason(self._bambu())
        self.assertTrue(jpeg)
        self.assertIsNone(reason)

    def test_http_snapshot_http_error_is_categorized(self):
        import urllib.error

        printer = {"vendor": "klipper", "moonrakerUrl": "http://1.2.3.4:7125"}
        err = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
        with mock.patch.object(camera.urllib.request, "urlopen", side_effect=err):
            jpeg, reason, detail = camera.capture_printer_frame_with_reason(printer)
        self.assertIsNone(jpeg)
        self.assertEqual(reason, "auth-fail")
        self.assertIn("403", detail)

    def test_no_camera_source(self):
        jpeg, reason, _ = camera.capture_printer_frame_with_reason({"vendor": "other"})
        self.assertIsNone(jpeg)
        self.assertEqual(reason, "no-camera-source")


class TestSchedulerBoundary(unittest.TestCase):
    def test_exact_5_and_95_are_dense(self):
        s = camera.CameraScheduler()
        self.assertEqual(s._interval("printing", 5), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("printing", 95), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        # Strictly between stays sparse.
        self.assertEqual(s._interval("printing", 50), camera.CameraScheduler.MID_PRINT_S)


if __name__ == "__main__":
    unittest.main()
