"""Runtime wrapper tests — downloader, soft-import, build_detector factory.

The InferenceSession itself isn't tested here (would need a real ONNX model +
onnxruntime installed); instead we cover the lifecycle around it: download
with sha-pin, atomic replace, retry on URL error, soft-import gate, factory
behavior when MODEL_URL/SHA are unset.
"""

import hashlib
import io
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock


class TestRuntimeAvailable(unittest.TestCase):
    def test_with_real_environment(self):
        # In this test process, runtime_available reflects whether the deps
        # are actually installed. Both outcomes are valid — assert it returns
        # a bool, and that mocking importlib changes the verdict.
        from makeros_hub.printers.onnx_detector import runtime_available

        self.assertIsInstance(runtime_available(), bool)

    def test_false_when_any_dep_missing(self):
        # If we patch find_spec to return None for any of the 3 deps,
        # runtime_available must say False.
        for missing in ("onnxruntime", "PIL", "numpy"):
            def side_effect(name, _missing=missing):
                return None if name == _missing else mock.MagicMock()

            with mock.patch(
                "makeros_hub.printers.onnx_detector.importlib.util.find_spec",
                side_effect=side_effect,
            ):
                from makeros_hub.printers.onnx_detector import runtime_available

                self.assertFalse(runtime_available(), f"missing={missing}")

    def test_true_when_all_present(self):
        with mock.patch(
            "makeros_hub.printers.onnx_detector.importlib.util.find_spec",
            return_value=mock.MagicMock(),
        ):
            from makeros_hub.printers.onnx_detector import runtime_available

            self.assertTrue(runtime_available())


class TestDownloadModel(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onnx-test-")
        self.payload = b"fake-onnx-bytes-for-test" * 1024  # ~24 KB
        self.expected_sha = hashlib.sha256(self.payload).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fake_urlopen(self, *_args, **_kw):
        """Returns a context-managed BytesIO so the chunked read works."""
        from contextlib import contextmanager

        @contextmanager
        def cm():
            stream = io.BytesIO(self.payload)
            yield stream

        return cm()

    def test_downloads_and_caches_to_sha_pathed_file(self):
        from makeros_hub.printers import onnx_detector

        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=self._fake_urlopen):
            path = onnx_detector.download_model(
                "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
            )
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(f"{self.expected_sha}.onnx"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.payload)

    def test_cache_hit_skips_redownload(self):
        from makeros_hub.printers import onnx_detector

        # Pre-populate the cache with a matching file.
        dest = os.path.join(self.tmpdir, f"{self.expected_sha}.onnx")
        with open(dest, "wb") as f:
            f.write(self.payload)

        with mock.patch.object(onnx_detector.urllib.request, "urlopen") as urlopen:
            path = onnx_detector.download_model(
                "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
            )
            urlopen.assert_not_called()  # never touched the network
        self.assertEqual(path, dest)

    def test_corrupted_cache_triggers_redownload(self):
        from makeros_hub.printers import onnx_detector

        # Plant a wrong-sha file at the expected location.
        dest = os.path.join(self.tmpdir, f"{self.expected_sha}.onnx")
        with open(dest, "wb") as f:
            f.write(b"corrupted contents that don't match the sha")

        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=self._fake_urlopen):
            path = onnx_detector.download_model(
                "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
            )
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.payload)

    def test_sha_mismatch_after_download_raises(self):
        from makeros_hub.printers import onnx_detector

        wrong_sha = "0" * 64
        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=self._fake_urlopen):
            with self.assertRaises(ValueError) as ctx:
                onnx_detector.download_model(
                    "https://example/test.onnx", wrong_sha, cache_dir=self.tmpdir
                )
            self.assertIn("sha256 mismatch", str(ctx.exception))
        # No file left at the destination on mismatch.
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, f"{wrong_sha}.onnx")))

    def test_retries_once_on_url_error(self):
        from makeros_hub.printers import onnx_detector

        call_count = {"n": 0}

        def flaky_urlopen(*_args, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.URLError("network down")
            return self._fake_urlopen()

        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=flaky_urlopen):
            path = onnx_detector.download_model(
                "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
            )
        self.assertEqual(call_count["n"], 2)
        self.assertTrue(os.path.exists(path))

    def test_persistent_url_error_raises_after_retries(self):
        from makeros_hub.printers import onnx_detector

        with mock.patch.object(
            onnx_detector.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("network down"),
        ):
            with self.assertRaises(urllib.error.URLError):
                onnx_detector.download_model(
                    "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
                )

    def test_empty_url_rejected(self):
        from makeros_hub.printers import onnx_detector

        with self.assertRaises(ValueError):
            onnx_detector.download_model("", "a" * 64, cache_dir=self.tmpdir)

    def test_empty_sha_rejected(self):
        from makeros_hub.printers import onnx_detector

        with self.assertRaises(ValueError):
            onnx_detector.download_model("https://example/x.onnx", "", cache_dir=self.tmpdir)

    def test_atomic_replace_no_partial_files_left(self):
        # After a successful download, no `.partial.*` stragglers should
        # remain in the cache dir — only the final sha-named file.
        from makeros_hub.printers import onnx_detector

        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=self._fake_urlopen):
            onnx_detector.download_model(
                "https://example/test.onnx", self.expected_sha, cache_dir=self.tmpdir
            )
        files = os.listdir(self.tmpdir)
        partials = [f for f in files if f.startswith(".partial.")]
        self.assertEqual(partials, [], f"expected no .partial.* stragglers, got {partials}")


class TestBuildDetector(unittest.TestCase):
    def test_returns_none_when_url_empty(self):
        from makeros_hub.printers.onnx_detector import build_detector

        # Empty URL → None (stub fallback), no download attempted.
        det = build_detector(url="", sha256="a" * 64, cache_dir=tempfile.mkdtemp())
        self.assertIsNone(det)

    def test_returns_none_when_sha_empty(self):
        from makeros_hub.printers.onnx_detector import build_detector

        det = build_detector(url="https://example/x.onnx", sha256="", cache_dir=tempfile.mkdtemp())
        self.assertIsNone(det)

    def test_returns_none_when_runtime_unavailable(self):
        with mock.patch(
            "makeros_hub.printers.onnx_detector.runtime_available",
            return_value=False,
        ):
            from makeros_hub.printers.onnx_detector import build_detector

            det = build_detector(
                url="https://example/x.onnx",
                sha256="a" * 64,
                cache_dir=tempfile.mkdtemp(),
            )
            self.assertIsNone(det)

    def test_returns_none_on_download_failure(self):
        with mock.patch(
            "makeros_hub.printers.onnx_detector.runtime_available",
            return_value=True,
        ):
            with mock.patch(
                "makeros_hub.printers.onnx_detector.download_model",
                side_effect=urllib.error.URLError("offline"),
            ):
                from makeros_hub.printers.onnx_detector import build_detector

                det = build_detector(
                    url="https://example/x.onnx",
                    sha256="a" * 64,
                    cache_dir=tempfile.mkdtemp(),
                )
                self.assertIsNone(det)


class TestDetectorHolder(unittest.TestCase):
    def test_starts_empty(self):
        from makeros_hub.printers.onnx_detector import DetectorHolder

        h = DetectorHolder()
        self.assertIsNone(h.detector())
        self.assertFalse(h.ready)
        self.assertIsNone(h.boot_outcome)

    def test_set_stub_outcome(self):
        from makeros_hub.printers.onnx_detector import DetectorHolder

        h = DetectorHolder()
        h.set(None, "stub_no_url")
        self.assertIsNone(h.detector())
        self.assertTrue(h.ready)
        self.assertEqual(h.boot_outcome, "stub_no_url")

    def test_set_active(self):
        from makeros_hub.printers.onnx_detector import DetectorHolder

        h = DetectorHolder()
        fake_det = mock.MagicMock()
        h.set(fake_det, "active")
        self.assertIs(h.detector(), fake_det)
        self.assertTrue(h.ready)
        self.assertEqual(h.boot_outcome, "active")

    def test_close_calls_inner_close(self):
        from makeros_hub.printers.onnx_detector import DetectorHolder

        h = DetectorHolder()
        fake_det = mock.MagicMock()
        h.set(fake_det, "active")
        h.close()
        fake_det.close.assert_called_once()
        self.assertIsNone(h.detector())

    def test_close_when_empty_is_noop(self):
        from makeros_hub.printers.onnx_detector import DetectorHolder

        h = DetectorHolder()
        h.close()  # must not raise


class TestBuildDetectorAsync(unittest.TestCase):
    """The async factory spawns a thread that calls build_detector(); we use
    a synchronous wait + on_outcome callback to assert it lands."""

    def test_stub_no_url_when_env_unset(self):
        from makeros_hub.printers.onnx_detector import build_detector_async

        # Clear envs
        with mock.patch.dict(os.environ, {"MAKEROS_HUB_MODEL_URL": "", "MAKEROS_HUB_MODEL_SHA256": ""}, clear=False):
            outcome_ref = []
            h = build_detector_async(on_outcome=lambda o, s: outcome_ref.append((o, s)))
            # Boot is fast (no URL → early return)
            import time as _t
            for _ in range(20):
                if h.ready:
                    break
                _t.sleep(0.05)
            self.assertTrue(h.ready)
            self.assertEqual(h.boot_outcome, "stub_no_url")
            self.assertIsNone(h.detector())
            self.assertEqual(outcome_ref[0][0], "stub_no_url")
            self.assertIsNone(outcome_ref[0][1])

    def test_stub_no_deps_when_runtime_unavailable(self):
        from makeros_hub.printers.onnx_detector import build_detector_async

        with mock.patch.dict(
            os.environ,
            {
                "MAKEROS_HUB_MODEL_URL": "https://example/x.onnx",
                "MAKEROS_HUB_MODEL_SHA256": "a" * 64,
            },
            clear=False,
        ):
            with mock.patch(
                "makeros_hub.printers.onnx_detector.runtime_available",
                return_value=False,
            ):
                outcome_ref = []
                h = build_detector_async(on_outcome=lambda o, s: outcome_ref.append((o, s)))
                import time as _t
                for _ in range(20):
                    if h.ready:
                        break
                    _t.sleep(0.05)
                self.assertEqual(h.boot_outcome, "stub_no_deps")

    def test_callback_exception_does_not_break_holder(self):
        from makeros_hub.printers.onnx_detector import build_detector_async

        with mock.patch.dict(os.environ, {"MAKEROS_HUB_MODEL_URL": "", "MAKEROS_HUB_MODEL_SHA256": ""}, clear=False):
            def bad_cb(_o, _s):
                raise RuntimeError("test")

            h = build_detector_async(on_outcome=bad_cb)
            import time as _t
            for _ in range(20):
                if h.ready:
                    break
                _t.sleep(0.05)
            self.assertTrue(h.ready)


class TestSweepPartials(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onnx-sweep-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_old_partial_swept(self):
        from makeros_hub.printers.onnx_detector import _sweep_partials

        partial = os.path.join(self.tmpdir, ".partial.abc.onnx")
        with open(partial, "wb") as f:
            f.write(b"junk")
        # Backdate the file
        old_ts = os.path.getmtime(partial) - 7200
        os.utime(partial, (old_ts, old_ts))

        _sweep_partials(self.tmpdir, max_age_sec=3600)
        self.assertFalse(os.path.exists(partial))

    def test_fresh_partial_preserved(self):
        from makeros_hub.printers.onnx_detector import _sweep_partials

        partial = os.path.join(self.tmpdir, ".partial.fresh.onnx")
        with open(partial, "wb") as f:
            f.write(b"in-flight")

        _sweep_partials(self.tmpdir, max_age_sec=3600)
        self.assertTrue(os.path.exists(partial))

    def test_missing_dir_silently_handled(self):
        from makeros_hub.printers.onnx_detector import _sweep_partials

        _sweep_partials("/nonexistent/path/xyz")  # must not raise


class TestBuildDetectorReadsFreshEnv(unittest.TestCase):
    def test_no_args_reads_env_at_call_time(self):
        """Sanity check that env vars are read inside build_detector() — not at
        import. This was a confirmed adversarial-review finding."""
        from makeros_hub.printers import onnx_detector

        # Empty env: returns None (stub).
        with mock.patch.dict(os.environ, {"MAKEROS_HUB_MODEL_URL": "", "MAKEROS_HUB_MODEL_SHA256": ""}, clear=False):
            self.assertIsNone(onnx_detector.build_detector())


class TestIncompleteReadRetried(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onnx-inc-")
        self.payload = b"fake-onnx-data" * 1024
        self.expected_sha = hashlib.sha256(self.payload).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_incomplete_read_is_retried(self):
        """An http.client.IncompleteRead mid-chunk must be retried, not treated
        as a permanent failure. This was an adversarial-review finding."""
        from makeros_hub.printers import onnx_detector
        import http.client
        import io as _io
        from contextlib import contextmanager

        call_count = {"n": 0}

        def flaky_urlopen(*_args, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise http.client.IncompleteRead(b"partial")

            @contextmanager
            def cm():
                yield _io.BytesIO(self.payload)

            return cm()

        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=flaky_urlopen):
            path = onnx_detector.download_model(
                "https://example/test.onnx",
                self.expected_sha,
                cache_dir=self.tmpdir,
                retries=1,
            )
        self.assertTrue(os.path.exists(path))
        self.assertEqual(call_count["n"], 2)

    def test_partial_unlinked_on_sha_mismatch(self):
        """A sha mismatch must clean up the .partial.* file — not leak it
        into the cache dir. Adversarial-review finding (sha read mid-failure)."""
        from makeros_hub.printers import onnx_detector
        import io as _io
        from contextlib import contextmanager

        def good_urlopen(*_args, **_kw):
            @contextmanager
            def cm():
                yield _io.BytesIO(self.payload)

            return cm()

        wrong_sha = "0" * 64
        with mock.patch.object(onnx_detector.urllib.request, "urlopen", side_effect=good_urlopen):
            with self.assertRaises(ValueError):
                onnx_detector.download_model(
                    "https://example/test.onnx", wrong_sha, cache_dir=self.tmpdir, retries=0
                )
        # No .partial.* stragglers
        partials = [f for f in os.listdir(self.tmpdir) if f.startswith(".partial.")]
        self.assertEqual(partials, [])


if __name__ == "__main__":
    unittest.main()
