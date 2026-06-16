"""Tests for rtsp_camera — ffmpeg-backed X1/H2/P2S frame grab."""

import sys
import unittest
from unittest import mock

from makeros_hub.printers import rtsp_camera

_JPEG = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"


def _runner_returning(out, rc):
    return lambda cmd, timeout, max_bytes: (out, rc)


class TestCaptureFrame(unittest.TestCase):
    """capture_frame contract via the injectable runner (cmd, timeout, max) -> (bytes, rc)."""

    def test_url_shape_and_code_quoting(self):
        self.assertEqual(
            rtsp_camera.rtsp_url("1.2.3.4", "co/de@1"),
            "rtsps://bblp:co%2Fde%401@1.2.3.4:322/streaming/live/1",
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_returns_jpeg_on_clean_exit(self, _which):
        self.assertEqual(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, 0)), _JPEG)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_argv_shape_and_timeout(self, _which):
        seen = {}

        def spy(cmd, timeout, max_bytes):
            seen["cmd"] = cmd
            seen["timeout"] = timeout
            return _JPEG, 0

        rtsp_camera.capture_frame("1.2.3.4", "abcd1234", runner=spy, timeout=10)
        self.assertEqual(
            seen["cmd"],
            [
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", "rtsps://bblp:abcd1234@1.2.3.4:322/streaming/live/1",
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
        )
        self.assertEqual(seen["timeout"], 10)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_nonzero_exit_rejected_even_with_jpeg_stdout(self, _which):
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, 1)))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_killed_run_rejected(self, _which):
        # rc None = killed (over cap / deadline) — never a trusted frame.
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, None)))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_non_jpeg_and_empty_and_oversized_rejected(self, _which):
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(b"nope", 0)))
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(b"", 0)))
        big = b"\xff\xd8\xff\xe0" + b"x" * (5 * 1024 * 1024) + b"\xff\xd9"
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(big, 0)))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_returns_none_without_running(self, _which):
        called = []
        out = rtsp_camera.capture_frame("h", "c", runner=lambda *a: called.append(1) or (b"", 0))
        self.assertIsNone(out)
        self.assertEqual(called, [])

    def test_blank_host_or_code_returns_none(self):
        self.assertIsNone(rtsp_camera.capture_frame("", "c"))
        self.assertIsNone(rtsp_camera.capture_frame("h", ""))


class TestRunFfmpegBounded(unittest.TestCase):
    """Exercise the REAL Popen+select bounded reader using python as a stand-in
    'ffmpeg' that writes a controlled number of bytes to stdout."""

    @staticmethod
    def _emit(n: int) -> list[str]:
        return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'A'*{n})"]

    def test_reads_full_small_output_with_rc0(self):
        out, rc = rtsp_camera._run_ffmpeg(self._emit(1000), timeout=5.0, max_bytes=1_000_000)
        self.assertEqual(len(out), 1000)
        self.assertEqual(rc, 0)

    def test_over_cap_is_bounded_and_killed(self):
        # Emit 2 MB but cap at 64 KB — the reader must stop near the cap and
        # report rc None (killed), NOT buffer all 2 MB.
        out, rc = rtsp_camera._run_ffmpeg(self._emit(2 * 1024 * 1024), timeout=5.0, max_bytes=64 * 1024)
        self.assertLessEqual(len(out), 64 * 1024 + 65536)  # bounded (one extra read chunk at most)
        self.assertIsNone(rc)

    def test_bad_binary_returns_empty_none(self):
        out, rc = rtsp_camera._run_ffmpeg(["/nonexistent/ffmpeg-xyz"], timeout=2.0, max_bytes=1024)
        self.assertEqual(out, b"")
        self.assertIsNone(rc)


if __name__ == "__main__":
    unittest.main()
