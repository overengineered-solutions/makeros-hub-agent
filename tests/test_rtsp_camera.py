"""Tests for rtsp_camera.capture_frame — ffmpeg-backed X1/H2/P2S frame grab."""

import subprocess
import unittest
from unittest import mock

from makeros_hub.printers import rtsp_camera

_JPEG = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"


class FakeProc:
    def __init__(self, stdout=b""):
        self.stdout = stdout


def _run_ok(*a, **k):
    return FakeProc(_JPEG)


class TestRtspCapture(unittest.TestCase):
    def test_url_shape_and_code_quoting(self):
        url = rtsp_camera.rtsp_url("1.2.3.4", "co/de@1")
        self.assertEqual(url, "rtsps://bblp:co%2Fde%401@1.2.3.4:322/streaming/live/1")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_returns_jpeg_on_clean_run(self, _which):
        out = rtsp_camera.capture_frame("1.2.3.4", "abcd1234", runner=_run_ok)
        self.assertEqual(out, _JPEG)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_returns_none(self, _which):
        called = []
        out = rtsp_camera.capture_frame("1.2.3.4", "abcd1234", runner=lambda *a, **k: called.append(1))
        self.assertIsNone(out)
        self.assertEqual(called, [])  # never invoked the runner

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_non_jpeg_output_rejected(self, _which):
        out = rtsp_camera.capture_frame("h", "c", runner=lambda *a, **k: FakeProc(b"not a jpeg"))
        self.assertIsNone(out)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_empty_output_rejected(self, _which):
        out = rtsp_camera.capture_frame("h", "c", runner=lambda *a, **k: FakeProc(b""))
        self.assertIsNone(out)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_oversized_output_rejected(self, _which):
        big = b"\xff\xd8\xff\xe0" + (b"x" * (5 * 1024 * 1024)) + b"\xff\xd9"
        out = rtsp_camera.capture_frame("h", "c", runner=lambda *a, **k: FakeProc(big))
        self.assertIsNone(out)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_timeout_returns_none(self, _which):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)

        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=boom))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_oserror_returns_none(self, _which):
        def boom(*a, **k):
            raise OSError("ffmpeg vanished")

        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=boom))

    def test_blank_host_or_code_returns_none(self):
        self.assertIsNone(rtsp_camera.capture_frame("", "c"))
        self.assertIsNone(rtsp_camera.capture_frame("h", ""))


if __name__ == "__main__":
    unittest.main()
