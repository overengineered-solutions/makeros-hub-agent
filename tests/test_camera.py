import unittest
from unittest import mock

from makeros_hub.printers import camera


class FakeResp:
    def __init__(self, data: bytes, headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self, _n: int = -1) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _bambu(**over):
    return {"vendor": "bambu", "model": "A1 mini", "host": "1.2.3.4", "accessCode": "AbCd1234", **over}


class TestSourceKind(unittest.TestCase):
    def test_external_camera_overrides_bambu_without_needing_printer_credentials(self):
        for model in ("A1 mini", "H2D"):
            printer = _bambu(model=model, host="", accessCode="", cameraSnapshotUrl=" http://pi:8080/snapshot ")
            self.assertEqual(camera.camera_source_kind(printer), "http-snapshot")
            jpeg = b"\xff\xd8\xff\xe0camera\xff\xd9"
            with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(jpeg)) as fetch, \
                 mock.patch.object(camera, "_bambu_capture_frame") as lan, \
                 mock.patch.object(camera, "_rtsp_capture_frame") as rtsp:
                self.assertEqual(camera.capture_printer_frame(printer), jpeg)
                self.assertEqual(camera.capture_printer_frame_with_reason(printer), (jpeg, None, ""))
                self.assertEqual(fetch.call_args.args[0].full_url, "http://pi:8080/snapshot")
                lan.assert_not_called()
                rtsp.assert_not_called()
        self.assertEqual(camera.camera_source_kind(_bambu(cameraSnapshotUrl=" ")), "bambu-lan")

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
    def test_disconnected_ustreamer_placeholder_is_not_a_camera_frame(self):
        jpeg = b"\xff\xd8\xff\xe0placeholder\xff\xd9"
        for online in ("false", "garbage"):
            with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(jpeg, {"X-UStreamer-Online": online})):
                self.assertEqual(camera._http_snapshot_with_reason("http://pi/snapshot")[:2], (None, "unreachable"))
        with mock.patch.object(camera.urllib.request, "urlopen", return_value=FakeResp(jpeg, {"X-UStreamer-Online": "true"})):
            self.assertEqual(camera.http_snapshot("http://pi/snapshot"), jpeg)

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


JPEG = b"\xff\xd8\xff\xe0x\xff\xd9"
A1 = {"vendor": "bambu", "model": "A1 mini", "host": "1.2.3.4", "accessCode": "c"}
RTSP = {"vendor": "bambu", "model": "X1C", "host": "1.2.3.5", "accessCode": "c"}
HTTP = {"vendor": "klipper", "moonrakerUrl": "http://k:7125"}


def _t(pid, kind=A1, **over):
    return {"printerId": pid, **kind, **over}


def _beat(s, targets, now, status=None, **kw):
    """One beat in which the site answers 200 for every frame (what the agent's push applies)."""
    frames, failures = camera.collect_camera_frames(targets, status or {}, s, now, **kw)
    for f in frames:
        s.accepted(f["printerId"], now, bytes.fromhex(f["sha256"]))
    return frames, failures


def _due(s, targets, now, status=None, want=None):
    return [t["printerId"] for t in s.plan(targets, status or {}, now, want)]


class TestCameraScheduler(unittest.TestCase):
    """v0.69: a frame counts only once the SITE accepted it; real failures open a breaker; a reconnect resets it."""

    def test_first_sighting_then_throttled_then_due(self):
        s, t, st = camera.CameraScheduler(), [_t("p")], {"p": {"state": "printing", "progressPct": 50}}
        self.assertEqual(_due(s, t, 0.0, st), ["p"])
        s.accepted("p", 0.0, b"x")
        self.assertEqual(_due(s, t, 10.0, st), [])
        self.assertEqual(_due(s, t, camera.CameraScheduler.MID_PRINT_S, st), ["p"])

    def test_state_change_forces_capture(self):
        s, t = camera.CameraScheduler(), [_t("p")]
        _due(s, t, 0.0, {"p": {"state": "idle"}})
        s.accepted("p", 0.0, b"x")
        self.assertEqual(_due(s, t, 1.0, {"p": {"state": "printing", "progressPct": 1}}), ["p"])

    def test_want_puts_an_idle_printer_on_the_dense_tier_and_cameraDense_is_the_fallback(self):
        s, st = camera.CameraScheduler(), {"p": {"state": "idle"}}
        _due(s, [_t("p")], 0.0, st)
        s.accepted("p", 0.0, b"x")
        later = camera.CameraScheduler.FIRST_OR_LAST_LAYER_S
        self.assertEqual(_due(s, [_t("p")], later, st, want={"p"}), ["p"])
        self.assertEqual(_due(s, [_t("p")], later, st, want=set()), [])                   # listed nowhere: idle tier
        self.assertEqual(_due(s, [_t("p", cameraDense=True)], later, st, want=set()), [])  # want wins over the flag
        self.assertEqual(_due(s, [_t("p", cameraDense=True)], later, st, want=None), ["p"])  # older site: the flag
        self.assertEqual(_due(s, [_t("p")], camera.CameraScheduler.IDLE_S, st, want=None), ["p"])

    def test_one_miss_retries_next_beat_then_the_breaker_backs_off_and_caps(self):
        s, t, st = camera.CameraScheduler(), [_t("p")], {"p": {"state": "idle"}}
        _due(s, t, 0.0, st)
        s.failed("p", 0.0)
        self.assertEqual(_due(s, t, 40.0, st), ["p"])   # one free miss: a camera waking after a reboot
        now, waits = 40.0, []
        for _ in range(6):
            s.failed("p", now)
            waits.append(s._cams["p"].retry_at - now)
            self.assertEqual(_due(s, t, s._cams["p"].retry_at - 0.1, st), [])
            now = s._cams["p"].retry_at
            self.assertEqual(_due(s, t, now, st), ["p"])
        self.assertEqual(waits, [60.0, 120.0, 240.0, 480.0, 600.0, 600.0])
        p = camera.CameraScheduler()
        _due(p, t, 0.0, {"p": {"state": "printing", "progressPct": 50}})
        for i in range(4):
            p.failed("p", float(i))
        self.assertEqual(p._cams["p"].retry_at - 3.0, 120.0)   # capped at 120 s while printing

    def test_an_open_breaker_wins_over_want(self):
        s, t, st = camera.CameraScheduler(), [_t("p")], {"p": {"state": "idle"}}
        _due(s, t, 0.0, st)
        s.failed("p", 0.0); s.failed("p", 0.0)
        self.assertEqual(_due(s, t, 30.0, st, want={"p"}), [])

    def test_state_change_reconnect_source_change_and_success_reset_the_breaker(self):
        cases = {
            "state": lambda t, st: (t, {"p": {"state": "printing", "progressPct": 1}}),
            "reconnect": lambda t, st: (t, {"p": {"state": "idle", "mqttConnectedAtMs": 2}}),   # a power-cycle: idle -> idle
            "source": lambda t, st: ([dict(t[0], host="1.2.3.9")], st),
        }
        for name, change in cases.items():
            with self.subTest(change=name):
                s, t, st = camera.CameraScheduler(), [_t("p")], {"p": {"state": "idle", "mqttConnectedAtMs": 1}}
                _due(s, t, 0.0, st)
                s.failed("p", 0.0); s.failed("p", 0.0)
                self.assertEqual(_due(s, t, 10.0, st), [])
                self.assertEqual(_due(s, *change(t, st)[:1], 10.0, change(t, st)[1]), ["p"])
                self.assertEqual(s._cams["p"].streak, 0)
        s, t, st = camera.CameraScheduler(), [_t("p")], {"p": {"state": "idle"}}
        _due(s, t, 0.0, st)
        s.failed("p", 0.0); s.accepted("p", 1.0, b"x"); s.failed("p", 2.0)
        self.assertEqual(s._cams["p"].retry_at, 0.0)   # a success reset the streak: this is a first miss again

    def test_first_layer_is_denser_than_mid(self):
        s = camera.CameraScheduler()
        self.assertEqual(s._interval("printing", 2), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("printing", 50), camera.CameraScheduler.MID_PRINT_S)
        self.assertEqual(s._interval("printing", 99), camera.CameraScheduler.FIRST_OR_LAST_LAYER_S)
        self.assertEqual(s._interval("idle", None), camera.CameraScheduler.IDLE_S)

    def test_forget_drops_absent_printers_and_a_reenable_is_due_at_once(self):
        s, t = camera.CameraScheduler(), [_t("a"), _t("b")]
        _due(s, t, 0.0)
        s.accepted("a", 0.0, b"x"); s.accepted("b", 0.0, b"x")
        s.failed("b", 0.0); s.failed("b", 0.0)
        s.forget({"a"})
        self.assertEqual(set(s._cams), {"a"})
        self.assertEqual(_due(s, t, 1.0), ["b"])

    def test_a_printer_without_a_camera_path_is_never_planned(self):
        s = camera.CameraScheduler()
        self.assertEqual(_due(s, [_t("p", host=""), {"printerId": "o", "vendor": "other"}], 0.0), [])


class TestCollectCameraFrames(unittest.TestCase):
    def test_source_identity_and_change_capture_immediately(self):
        target = {"printerId": "p", "vendor": "bambu", "model": "A1 mini", "host": "192.0.2.2", "serial": "test", "accessCode": "c", "cameraSnapshotUrl": "http://pi:8080/snapshot"}
        # Same fixed wire vector asserted in pstation's DB suite.
        self.assertEqual(camera.camera_source_id(target), "3560a72d7d65e1a4c913b515a6c1accbd2a004da910f4752cc62128cbfb69c90")
        scheduler = camera.CameraScheduler()
        frames, _ = _beat(scheduler, [target], now=0, capture=lambda _: b"jpeg0")
        self.assertEqual(frames[0]["sourceId"], camera.camera_source_id(target))
        self.assertEqual(_beat(scheduler, [target], now=1, capture=lambda _: b"jpeg"), ([], []))
        for i, change in enumerate(({"cameraSnapshotUrl": ""}, {"host": "192.0.2.4"}, {"moonrakerUrl": "http://sovol:7125"}, {"model": "H2D"}), 2):
            target = dict(target, **change)
            frames, failures = _beat(scheduler, [target], now=i, capture=lambda _, i=i: b"jpeg%d" % i)
            self.assertEqual(len(frames), 1)
            self.assertEqual(failures, [])
            self.assertEqual(frames[0]["sourceId"], camera.camera_source_id(target))
        target = dict(target, cameraSnapshotUrl="http://offline/snapshot")
        frames, failures = _beat(scheduler, [target], now=7, capture=lambda _: None)
        self.assertEqual(frames, [])
        self.assertEqual(failures[0]["sourceId"], camera.camera_source_id(target))
        scheduler.forget(set())
        self.assertEqual(scheduler._cams, {})

    def _targets(self):
        return [_t("p1"), _t("p2")]

    def test_captures_due_printers_and_base64s(self):
        s = camera.CameraScheduler()
        frames, failures = camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=lambda _t: JPEG)
        self.assertEqual({f["printerId"] for f in frames}, {"p1", "p2"})
        self.assertEqual(failures, [])
        import base64 as b64
        import hashlib
        self.assertEqual(b64.b64decode(frames[0]["jpegBase64"]), JPEG)
        self.assertEqual(frames[0]["sha256"], hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(s.stats["ok"], 0)   # not captured until the site says so

    def test_none_frame_is_reported_as_failure_dict_shape(self):
        s = camera.CameraScheduler()
        frames, failures = camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=lambda _t: None)
        self.assertEqual(frames, [])
        self.assertEqual({f["printerId"] for f in failures}, {"p1", "p2"})
        for f in failures:
            self.assertEqual(set(f), {"printerId", "reason", "stderrTail", "sourceId"})
        self.assertEqual(s.stats["fail"], {"unknown": 2})

    def test_capture_exception_is_failure_not_propagated(self):
        s = camera.CameraScheduler()

        def boom(_t):
            raise RuntimeError("camera exploded")

        frames, failures = camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=boom)
        self.assertEqual(frames, [])
        self.assertEqual({f["printerId"] for f in failures}, {"p1", "p2"})
        self.assertTrue(all(f["reason"] == "unknown" for f in failures))

    def test_nothing_due_returns_empty_no_failures(self):
        s = camera.CameraScheduler()
        st = {"p1": {"state": "printing", "progressPct": 50}, "p2": {"state": "printing", "progressPct": 50}}
        _beat(s, self._targets(), 0.0, st, capture=lambda t: JPEG + t["printerId"].encode())
        self.assertEqual(_beat(s, self._targets(), 1.0, st, capture=lambda _t: JPEG), ([], []))

    def test_partial_failure_reports_only_the_failed(self):
        s = camera.CameraScheduler()
        frames, failures = camera.collect_camera_frames(
            self._targets(), {}, s, now=0.0, capture=lambda t: JPEG if t["printerId"] == "p1" else None)
        self.assertEqual([f["printerId"] for f in frames], ["p1"])
        self.assertEqual([f["printerId"] for f in failures], ["p2"])

    def test_a_started_capture_that_outlives_the_beat_is_a_timeout_and_the_beat_is_bounded(self):
        import time as _time
        s = camera.CameraScheduler()

        def cap(t):
            if t["printerId"] == "p1":
                return (JPEG, None, "")
            _time.sleep(0.4)   # an HTTP camera trickling past the whole budget
            return (JPEG, None, "")

        t0 = _time.monotonic()
        frames, failures = camera.collect_camera_frames([_t("p1", HTTP), _t("p2", HTTP)], {}, s, now=0.0,
                                                        capture_with_reason=cap, budget=0.1)
        wall = _time.monotonic() - t0
        self.assertLess(wall, 0.2, f"heartbeat blocked by slow capture: {wall:.3f}s")
        self.assertEqual([f["printerId"] for f in frames], ["p1"])
        self.assertEqual([(f["printerId"], f["reason"]) for f in failures], [("p2", "timeout")])

    def test_a_capture_that_never_started_is_silent_and_goes_first_next_beat(self):
        import time as _time
        s = camera.CameraScheduler()
        targets = [_t("r%d" % i, RTSP) for i in range(10)]
        with mock.patch.object(camera, "_RTSP_DEADLINE_S", 0.6875):   # = 5.5 s of an 8 s beat, at 1/8 scale
            frames, failures = camera.collect_camera_frames(
                targets, {}, s, now=0.0, budget=1.0, ff_slots=4,
                capture_with_reason=lambda t: (_time.sleep(0.4), (JPEG + t["printerId"].encode(), None, ""))[1])
        self.assertEqual(len(frames), 4)
        self.assertEqual(failures, [])                       # never posted as "Capture failed"
        self.assertEqual(s.stats["notStarted"], 6)
        captured = {f["printerId"] for f in frames}
        self.assertEqual(set(_due(s, targets, 40.0)[:6]), {t["printerId"] for t in targets} - captured)

    def test_the_ffmpeg_lane_never_runs_more_than_its_slots(self):
        import threading
        import time as _time
        live, peak, lock = [0], [0], threading.Lock()

        def cap(t):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            _time.sleep(0.03)
            with lock:
                live[0] -= 1
            return (JPEG + t["printerId"].encode(), None, "")

        with mock.patch.object(camera, "_RTSP_DEADLINE_S", 0.05):
            frames, _ = camera.collect_camera_frames([_t("r%d" % i, RTSP) for i in range(10)], {}, camera.CameraScheduler(),
                                                     0.0, budget=2.0, ff_slots=3, capture_with_reason=cap)
        self.assertEqual(len(frames), 10)
        self.assertEqual(peak[0], 3)

    def test_dead_a1s_never_starve_rtsp_and_http_cameras(self):
        # the 2026-10-14 incident at 1/8 time scale: 7 A1s that each hang for their whole 5 s deadline used to hold the
        # shared 4-slot pool, so healthy X1C/P2S/H2D/SV08 cameras showed "Capture failed"
        import time as _time

        def cap(t):
            if t["model"] == "A1 mini":
                _time.sleep(0.625)
                return (None, "timeout", "timed out in stream")
            _time.sleep(0.25 if t["model"] == "X1C" else 0.05)
            return (JPEG + t["printerId"].encode(), None, "")

        targets = [_t("a%d" % i) for i in range(7)] + [_t("r%d" % i, RTSP) for i in range(8)] + [_t("h", HTTP, model="SV08")]
        s = camera.CameraScheduler()
        with mock.patch.object(camera, "_RTSP_DEADLINE_S", 0.6875):
            frames, failures = camera.collect_camera_frames(targets, {}, s, 0.0, budget=1.0, capture_with_reason=cap)
        self.assertEqual({f["printerId"] for f in frames}, {"r%d" % i for i in range(8)} | {"h"})
        self.assertEqual({f["printerId"] for f in failures}, {"a%d" % i for i in range(7)})
        self.assertEqual(s.stats["notStarted"], 0)

    def test_frozen_and_too_large_frames_are_failures_and_never_pushed(self):
        s = camera.CameraScheduler()
        _beat(s, [_t("p")], 0.0, capture=lambda _t: JPEG)
        frames, failures = camera.collect_camera_frames([_t("p")], {}, s, camera.CameraScheduler.IDLE_S, capture=lambda _t: JPEG)
        self.assertEqual((frames, [f["reason"] for f in failures]), ([], ["frozen"]))
        big = b"\xff\xd8\xff" + b"x" * 1_600_000
        frames, failures = camera.collect_camera_frames([_t("q")], {}, s, 0.0, capture=lambda _t: big)
        self.assertEqual((frames, [f["reason"] for f in failures]), ([], ["too-large"]))

    def test_failed_capture_does_not_mark_scheduler(self):
        s = camera.CameraScheduler()
        camera.collect_camera_frames(self._targets(), {}, s, now=0.0, capture=lambda _t: None)
        self.assertEqual(_due(s, self._targets(), 40.0), ["p1", "p2"])   # a first miss is retried next beat


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
