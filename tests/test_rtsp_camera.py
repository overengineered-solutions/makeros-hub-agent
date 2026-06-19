"""Tests for rtsp_camera — ffmpeg-backed X1/H2/P2S frame grab.

v0.42.0: the `-stimeout` regression is removed; a startup capability-probe picks
the socket-timeout flag this ffmpeg build actually accepts (preferring
`-rw_timeout`), degrading to the python deadline if none work. The runner
contract is `(cmd, timeout, max) -> (out, rc, stderr, timed_out)` so the
categorizer gets an authoritative deadline signal instead of a wall-clock guess.
"""

import sys
import unittest
from unittest import mock

from makeros_hub.printers import rtsp_camera

_JPEG = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"


def _runner_returning(out, rc, stderr=b"", timed_out=False):
    return lambda cmd, timeout, max_bytes: (out, rc, stderr, timed_out)


class TestCaptureFrame(unittest.TestCase):
    """capture_frame (back-compat bytes-only shim) via the injectable runner."""

    def setUp(self):
        # ffmpeg_argv calls _supported_timeout_flag() which would RUN ffmpeg;
        # pin it so argv is deterministic and no subprocess spawns in tests.
        self._probe = mock.patch.object(
            rtsp_camera, "_supported_timeout_flag", return_value=("-rw_timeout", "4000000")
        )
        self._probe.start()
        self.addCleanup(self._probe.stop)

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
    def test_argv_has_no_stimeout_and_probed_flag_before_input(self, _which):
        seen = {}

        def spy(cmd, timeout, max_bytes):
            seen["cmd"] = cmd
            return _JPEG, 0, b"", False

        rtsp_camera.capture_frame("1.2.3.4", "abcd1234", runner=spy)
        cmd = seen["cmd"]
        # The v0.41.0 regression flag must be GONE.
        self.assertNotIn("-stimeout", cmd)
        # The probed flag is spliced in, and BEFORE -i (ffmpeg ignores it after).
        self.assertIn("-rw_timeout", cmd)
        self.assertLess(cmd.index("-rw_timeout"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-rw_timeout") + 1], "4000000")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_argv_omits_flag_when_probe_returns_none(self, _which):
        # When no socket-timeout flag is supported, argv carries none (we rely
        # on the python deadline) — and crucially still has no -stimeout.
        with mock.patch.object(rtsp_camera, "_supported_timeout_flag", return_value=()):
            seen = {}

            def spy(cmd, timeout, max_bytes):
                seen["cmd"] = cmd
                return _JPEG, 0, b"", False

            rtsp_camera.capture_frame("h", "c", runner=spy)
            cmd = seen["cmd"]
            self.assertNotIn("-stimeout", cmd)
            self.assertNotIn("-rw_timeout", cmd)
            self.assertNotIn("-timeout", cmd)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_nonzero_exit_rejected_even_with_jpeg_stdout(self, _which):
        self.assertIsNone(
            rtsp_camera.capture_frame("h", "c", runner=_runner_returning(_JPEG, 1))
        )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_killed_run_rejected(self, _which):
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
            "h", "c", runner=lambda *a: called.append(1) or (b"", 0, b"", False)
        )
        self.assertIsNone(out)
        self.assertEqual(called, [])

    def test_blank_host_or_code_returns_none(self):
        self.assertIsNone(rtsp_camera.capture_frame("", "c"))
        self.assertIsNone(rtsp_camera.capture_frame("h", ""))


class TestCaptureFrameWithReason(unittest.TestCase):
    """capture_frame_with_reason — categorized failure surface."""

    def setUp(self):
        self._probe = mock.patch.object(
            rtsp_camera, "_supported_timeout_flag", return_value=("-rw_timeout", "4000000")
        )
        self._probe.start()
        self.addCleanup(self._probe.stop)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_success_has_no_reason(self, _which):
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(_JPEG, 0))
        self.assertEqual(r.jpeg, _JPEG)
        self.assertIsNone(r.reason)
        self.assertEqual(r.stderr_tail, "")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_categorized(self, _which):
        r = rtsp_camera.capture_frame_with_reason("h", "c")
        self.assertEqual(r.reason, "no-ffmpeg")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_ffmpeg_arg_error_is_categorized_not_unknown(self, _which):
        # The exact v0.41.0 outage signature must self-identify, never 'unknown'.
        stderr = b"Error splitting the argument list: Option not found\n"
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"", 1, stderr))
        self.assertEqual(r.reason, "ffmpeg-arg")
        self.assertIn("Option not found", r.stderr_tail)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_404_describe_is_liveview_off(self, _which):
        stderr = b"[rtsp @ 0x1] method DESCRIBE failed: 404 Not Found\n"
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"", 1, stderr))
        self.assertEqual(r.reason, "liveview-off")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_401_is_auth_fail(self, _which):
        stderr = b"[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\n"
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"", 1, stderr))
        self.assertEqual(r.reason, "auth-fail")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_no_route_is_unreachable(self, _which):
        stderr = b"[tcp @ 0x1] Connection to tcp://1.2.3.4:322 failed: No route to host\n"
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"", 1, stderr))
        self.assertEqual(r.reason, "unreachable")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_tls_handshake_is_tls_error(self, _which):
        stderr = b"[tls @ 0x1] Error during SSL handshake: sslv3 alert handshake failure\n"
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"", 1, stderr))
        self.assertEqual(r.reason, "tls-error")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_deadline_flag_is_timeout(self, _which):
        # timed_out=True is authoritative — even with empty stderr + rc None.
        r = rtsp_camera.capture_frame_with_reason(
            "h", "c", runner=_runner_returning(b"", None, b"", True)
        )
        self.assertEqual(r.reason, "timeout")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_zero_exit_garbage_is_bad_jpeg(self, _which):
        r = rtsp_camera.capture_frame_with_reason("h", "c", runner=_runner_returning(b"nope", 0))
        self.assertEqual(r.reason, "bad-jpeg")

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_access_code_redacted_from_stderr_tail(self, _which):
        stderr = b"Connection refused for rtsps://bblp:s3cret-code@1.2.3.4:322\n"
        r = rtsp_camera.capture_frame_with_reason(
            "h", "s3cret-code", runner=_runner_returning(b"", 1, stderr)
        )
        self.assertNotIn("s3cret-code", r.stderr_tail)
        self.assertIn("***", r.stderr_tail)


class TestCategorizeStderr(unittest.TestCase):
    def test_ffmpeg_arg_wins_over_everything(self):
        # Even if stderr also mentions 401, an arg-parse error is OUR bug first.
        self.assertEqual(
            rtsp_camera._categorize_stderr("Option not found 401 unauthorized", 1, False),
            "ffmpeg-arg",
        )

    def test_timed_out_flag_beats_stderr(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("401 unauthorized", 1, True), "timeout"
        )

    def test_connection_timed_out_is_unreachable(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("Connection timed out", 1, False), "unreachable"
        )

    def test_operation_timed_out_is_timeout(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("Operation timed out", 1, False), "timeout"
        )

    def test_rc_none_no_stderr_is_timeout(self):
        self.assertEqual(rtsp_camera._categorize_stderr("", None, False), "timeout")

    def test_unknown_when_no_match(self):
        self.assertEqual(
            rtsp_camera._categorize_stderr("something weird", 1, False), "unknown"
        )


class TestSupportedTimeoutFlag(unittest.TestCase):
    """The capability probe — run ffmpeg with each candidate, keep the first the
    build accepts. This is the safeguard against another -stimeout-style break."""

    def setUp(self):
        rtsp_camera._supported_timeout_flag.cache_clear()
        self.addCleanup(rtsp_camera._supported_timeout_flag.cache_clear)

    @mock.patch.object(rtsp_camera.shutil, "which", return_value=None)
    def test_no_ffmpeg_returns_empty(self, _which):
        self.assertEqual(rtsp_camera._supported_timeout_flag(), ())

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_prefers_rw_timeout_when_accepted(self, _which):
        def fake_run(argv, **kw):
            # rw_timeout accepted (no arg-parse error in stderr).
            return mock.Mock(stderr="frame=0", returncode=1)

        with mock.patch.object(rtsp_camera.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                rtsp_camera._supported_timeout_flag(), ("-rw_timeout", "4000000")
            )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_falls_through_to_timeout_when_rw_rejected(self, _which):
        calls = {"n": 0}

        def fake_run(argv, **kw):
            calls["n"] += 1
            # First candidate (-rw_timeout) rejected, second (-timeout) accepted.
            if "-rw_timeout" in argv:
                return mock.Mock(stderr="Option not found", returncode=1)
            return mock.Mock(stderr="frame=0", returncode=1)

        with mock.patch.object(rtsp_camera.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                rtsp_camera._supported_timeout_flag(), ("-timeout", "4000000")
            )

    @mock.patch.object(rtsp_camera.shutil, "which", return_value="/usr/bin/ffmpeg")
    def test_returns_empty_when_all_rejected(self, _which):
        with mock.patch.object(
            rtsp_camera.subprocess,
            "run",
            return_value=mock.Mock(stderr="Option not found", returncode=1),
        ):
            self.assertEqual(rtsp_camera._supported_timeout_flag(), ())


class TestRunFfmpegBounded(unittest.TestCase):
    """Exercise the REAL Popen+select bounded reader using python as a stand-in
    'ffmpeg'. Now returns a 4-tuple (out, rc, stderr, timed_out)."""

    @staticmethod
    def _emit(n: int) -> list[str]:
        return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'A'*{n})"]

    def test_reads_full_small_output_with_rc0(self):
        out, rc, stderr, timed_out = rtsp_camera._run_ffmpeg(
            self._emit(1000), timeout=5.0, max_bytes=1_000_000
        )
        self.assertEqual(len(out), 1000)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, b"")
        self.assertFalse(timed_out)

    def test_over_cap_is_bounded_and_killed(self):
        out, rc, _stderr, _to = rtsp_camera._run_ffmpeg(
            self._emit(2 * 1024 * 1024), timeout=5.0, max_bytes=64 * 1024
        )
        self.assertLessEqual(len(out), 64 * 1024 + 65536)
        self.assertIsNone(rc)

    def test_deadline_sets_timed_out(self):
        # A child that sleeps past the deadline → timed_out True, rc None.
        sleeper = [sys.executable, "-c", "import time; time.sleep(5)"]
        out, rc, _stderr, timed_out = rtsp_camera._run_ffmpeg(
            sleeper, timeout=0.3, max_bytes=1024
        )
        self.assertEqual(out, b"")
        self.assertIsNone(rc)
        self.assertTrue(timed_out)

    def test_stderr_captured(self):
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('boom\\n'); "
            "sys.stdout.buffer.write(b'\\xff\\xd8\\xff\\xe0xx\\xff\\xd9')",
        ]
        out, rc, stderr, _to = rtsp_camera._run_ffmpeg(cmd, timeout=5.0, max_bytes=1_000_000)
        self.assertEqual(rc, 0)
        self.assertEqual(out[:3], b"\xff\xd8\xff")
        self.assertEqual(stderr.strip(), b"boom")

    def test_bad_binary_returns_empty_none_with_oserror_detail(self):
        out, rc, stderr, timed_out = rtsp_camera._run_ffmpeg(
            ["/nonexistent/ffmpeg-xyz"], timeout=2.0, max_bytes=1024
        )
        self.assertEqual(out, b"")
        self.assertIsNone(rc)
        self.assertTrue(stderr)
        self.assertFalse(timed_out)


if __name__ == "__main__":
    unittest.main()
