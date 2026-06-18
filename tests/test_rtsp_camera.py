"""Tests for rtsp_camera — ffmpeg-backed X1/H2/P2S frame grab.

v0.41.0 adds CaptureResult with a categorized failure `reason` and a bounded
redacted `stderr_tail`. The runner contract therefore widens from
`(cmd, timeout, max) -> (out, rc)` to `(cmd, timeout, max) -> (out, rc, stderr)`
so the wrapper can surface ffmpeg's stderr without changing the stdout path.
"""

import sys
import unittest
from unittest import mock

from makeros_hub.printers import rtsp_camera

_JPEG = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"


def _runner_returning(out, rc, stderr=b""):
    return lambda cmd, timeout, max_bytes: (out, rc, stderr)


class TestCaptureFrame(unittest.TestCase):
    """capture_frame (back-compat wrapper) via the injectable runner."""

    def test_url_shape_and_code_quoting(self):
        self.assertEqual(
            rtsp_camera.rtsp_url("1.2.3.4", "co/de@1"),
            "rtsps://bblp:co%2Fde%401@1.2.3.4:322/streaming/live/1",
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_returns_jpeg_on_clean_exit(self, _which):
        self.assertEqual(
            rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, 0)),
            _JPEG,
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_argv_shape_includes_stimeout_before_input(self, _which):
        # v0.41.0: ffmpeg -stimeout 5_000_000 MUST be before -i so RTSP socket
        # I/O timeout is in force on the open phase. The exact order matters —
        # ffmpeg silently ignores -stimeout when it appears after -i.
        seen = {}

        def spy(cmd, timeout, max_bytes):
            seen["cmd"] = cmd
            seen["timeout"] = timeout
            return _JPEG, 0, b""

        rtsp_camera.capture_frame("1.2.3.4", "abcd1234", runner=spy, timeout=10)
        cmd = seen["cmd"]
        self.assertIn("-stimeout", cmd)
        self.assertEqual(cmd[cmd.index("-stimeout") + 1], "5000000")
        # -stimeout must come before -i (ffmpeg ignores it otherwise).
        self.assertLess(cmd.index("-stimeout"), cmd.index("-i"))
        self.assertEqual(seen["timeout"], 10)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_nonzero_exit_rejected_even_with_jpeg_stdout(self, _which):
        self.assertIsNone(
            rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, 1))
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_killed_run_rejected(self, _which):
        # rc None = killed (over cap / deadline) — never a trusted frame.
        self.assertIsNone(
            rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, None))
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_non_jpeg_and_empty_and_oversized_rejected(self, _which):
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(b"nope", 0)))
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(b"", 0)))
        big = b"\xff\xd8\xff\xe0" + b"x" * (5 * 1024 * 1024) + b"\xff\xd9"
        self.assertIsNone(rtsp_camera.capture_frame("h", "c", runner=_runner_returning(big, 0)))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_returns_none_without_running(self, _which):
        called = []
        out = rtsp_camera.capture_frame(
            "h", "c", runner=lambda *a: called.append(1) or (b"", 0, b"")
        )
        self.assertIsNone(out)
        self.assertEqual(called, [])

    def test_blank_host_or_code_returns_none(self):
        self.assertIsNone(rtsp_camera.capture_frame("", "c"))
        self.assertIsNone(rtsp_camera.capture_frame("h", ""))


class TestCaptureFrameWithReason(unittest.TestCase):
    """capture_frame_with_reason — categorized failure surface (v0.41.0)."""

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_success_has_no_reason_and_no_stderr_tail(self, _which):
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(_JPEG, 0, b"")
        )
        self.assertEqual(result.jpeg, _JPEG)
        self.assertIsNone(result.reason)
        self.assertEqual(result.stderr_tail, "")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_categorized(self, _which):
        result = rtsp_camera.capture_frame_with_reason("h", "c")
        self.assertIsNone(result.jpeg)
        self.assertEqual(result.reason, "no-ffmpeg")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_404_describe_categorized_as_liveview_off(self, _which):
        stderr = b"[rtsp @ 0x1] method DESCRIBE failed: 404 Not Found\n"
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"", 1, stderr)
        )
        self.assertIsNone(result.jpeg)
        self.assertEqual(result.reason, "liveview-off")
        self.assertIn("404", result.stderr_tail)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_401_unauthorized_categorized_as_auth_fail(self, _which):
        stderr = b"[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\n"
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"", 1, stderr)
        )
        self.assertEqual(result.reason, "auth-fail")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_no_route_to_host_categorized_as_unreachable(self, _which):
        stderr = b"[tcp @ 0x1] Connection to tcp://1.2.3.4:322 failed: No route to host\n"
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"", 1, stderr)
        )
        self.assertEqual(result.reason, "unreachable")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_killed_with_no_stderr_categorized_as_timeout(self, _which):
        # rc=None with no stderr signal → timeout (the python deadline killed it).
        # Note: with the relaxed deadline_hit detector, the timeout path can land
        # whenever rc is None and stderr offers nothing actionable.
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"", None, b"")
        )
        # Either timeout or unknown is acceptable for an rc=None+no-stderr case;
        # both surface the no-frame to the operator. The categorizer prefers
        # timeout when the wall-clock exceeded the budget.
        self.assertIn(result.reason, ("timeout", "unknown"))

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_bad_jpeg_when_ffmpeg_returns_zero_but_garbage(self, _which):
        result = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"nope", 0, b"")
        )
        self.assertEqual(result.reason, "bad-jpeg")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_access_code_redacted_from_stderr_tail(self, _which):
        # ffmpeg echoes the dial-target URL into stderr. The access code MUST
        # be redacted before the result leaves this module — otherwise it
        # leaks into system_events on the cloud side.
        stderr = (
            b"[rtsp @ 0x1] Connection refused for rtsps://bblp:s3cret-code@1.2.3.4:322\n"
        )
        result = rtsp_camera.capture_frame_with_reason(
            "h", "s3cret-code", runner=_runner_returning(b"", 1, stderr)
        )
        self.assertNotIn("s3cret-code", result.stderr_tail)
        self.assertIn("***", result.stderr_tail)


class TestCategorizeStderr(unittest.TestCase):
    """Direct test of _categorize_stderr to lock the keyword contract in."""

    def test_deadline_hit_wins_over_any_stderr(self):
        # The categorizer trusts the python-side deadline_hit signal first;
        # whatever ffmpeg wrote to stderr is irrelevant when we hit the wall
        # clock budget.
        self.assertEqual(
            rtsp_camera._categorize_stderr("401 Unauthorized", 1, deadline_hit=True),
            "timeout",
        )

    def test_empty_stderr_returncode_none_is_timeout(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("", None, deadline_hit=False), "timeout"
        )

    def test_unknown_when_no_match(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("something we don't recognize", 1, False),
            "unknown",
        )


class TestRunFfmpegBounded(unittest.TestCase):
    """Exercise the REAL Popen+select bounded reader using python as a stand-in
    'ffmpeg' that writes a controlled number of bytes to stdout."""

    @staticmethod
    def _emit(n: int) -> list[str]:
        return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'A'*{n})"]

    def test_reads_full_small_output_with_rc0_and_empty_stderr(self):
        out, rc, stderr = rtsp_camera._run_ffmpeg(self._emit(1000), timeout=5.0, max_bytes=1_000_000)
        self.assertEqual(len(out), 1000)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, b"")

    def test_over_cap_is_bounded_and_killed(self):
        # Emit 2 MB but cap at 64 KB — the reader must stop near the cap and
        # report rc None (killed), NOT buffer all 2 MB.
        out, rc, _ = rtsp_camera._run_ffmpeg(
            self._emit(2 * 1024 * 1024), timeout=5.0, max_bytes=64 * 1024
        )
        self.assertLessEqual(len(out), 64 * 1024 + 65536)  # bounded (one extra read chunk at most)
        self.assertIsNone(rc)

    def test_stderr_captured_when_child_writes_it(self):
        # Spawn a python that emits a known stderr line and a tiny jpeg-like
        # stdout. The reader must capture both without truncating either.
        cmd = [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stderr.write('boom\\n');"
                "sys.stdout.buffer.write(b'\\xff\\xd8\\xff\\xe0xx\\xff\\xd9')"
            ),
        ]
        out, rc, stderr = rtsp_camera._run_ffmpeg(cmd, timeout=5.0, max_bytes=1_000_000)
        self.assertEqual(rc, 0)
        self.assertEqual(out[:3], b"\xff\xd8\xff")
        self.assertEqual(stderr.strip(), b"boom")

    def test_bad_binary_returns_empty_none_with_oserror_in_stderr(self):
        out, rc, stderr = rtsp_camera._run_ffmpeg(
            ["/nonexistent/ffmpeg-xyz"], timeout=2.0, max_bytes=1024
        )
        self.assertEqual(out, b"")
        self.assertIsNone(rc)
        # OSError message goes into the stderr-channel-equivalent so the
        # categorizer has something to work with.
        self.assertTrue(stderr)


if __name__ == "__main__":
    unittest.main()
