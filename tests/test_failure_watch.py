import unittest

from makeros_hub.printers.failure_watch import (
    EWMA_ALPHA,
    EWMA_SPAN,
    FailureWatchSmoother,
    _to_bps,
    collect_failure_samples,
    stub_detector,
)


def _target(pid: str, **over):
    base = {
        "printerId": pid,
        "cameraEnabled": True,
        "aiFailureWatchEnabled": True,
        "aiFailureSensitivity": "medium",
    }
    base.update(over)
    return base


def _status(state: str = "printing"):
    return {"state": state}


class TestStubDetector(unittest.TestCase):
    def test_stub_returns_zero(self):
        self.assertEqual(stub_detector(b"\xff\xd8notarealjpeg"), 0.0)


class TestToBps(unittest.TestCase):
    def test_clamps_negative_to_zero(self):
        self.assertEqual(_to_bps(-0.1), 0)

    def test_clamps_over_one_to_ten_thousand(self):
        self.assertEqual(_to_bps(1.5), 10_000)
        self.assertEqual(_to_bps(99.0), 10_000)

    def test_legal_values_round_to_int_bps(self):
        self.assertEqual(_to_bps(0.0), 0)
        self.assertEqual(_to_bps(0.5), 5_000)
        self.assertEqual(_to_bps(1.0), 10_000)
        # Python's round() uses banker's rounding (0.5 → even); both 1234 + 1235 are valid
        self.assertIn(_to_bps(0.12345), (1234, 1235))

    def test_nan_to_zero(self):
        self.assertEqual(_to_bps(float("nan")), 0)


class TestEwmaSmoother(unittest.TestCase):
    def test_cold_start_returns_raw(self):
        s = FailureWatchSmoother()
        self.assertAlmostEqual(s.update("p1", 0.8, now=0.0), 0.8)

    def test_subsequent_sample_applies_alpha(self):
        s = FailureWatchSmoother()
        s.update("p1", 0.0, now=0.0)  # prev=0
        # Next sample at raw=1: smoothed = α*1 + (1-α)*0 = α
        self.assertAlmostEqual(s.update("p1", 1.0, now=1.0), EWMA_ALPHA)

    def test_stable_at_constant_input(self):
        s = FailureWatchSmoother()
        for i in range(20):
            v = s.update("p1", 0.7, now=float(i))
        self.assertAlmostEqual(v, 0.7, places=4)

    def test_per_printer_state_is_independent(self):
        s = FailureWatchSmoother()
        s.update("p1", 0.9, now=0.0)
        # p2 cold-starts independent of p1
        self.assertAlmostEqual(s.update("p2", 0.1, now=0.0), 0.1)

    def test_stale_gap_cold_starts(self):
        s = FailureWatchSmoother(stale_after_sec=5.0)
        s.update("p1", 0.0, now=0.0)
        # 60 s later: gap exceeds stale_after_sec → cold-start
        self.assertAlmostEqual(s.update("p1", 0.95, now=60.0), 0.95)

    def test_forget_drops_only_unknown_ids(self):
        s = FailureWatchSmoother()
        s.update("p1", 0.5, now=0.0)
        s.update("p2", 0.5, now=0.0)
        s.forget({"p1"})  # keep p1, drop p2
        # p2 cold-starts on next call
        self.assertAlmostEqual(s.update("p2", 0.9, now=1.0), 0.9)
        # p1 retains state
        v = s.update("p1", 0.5, now=1.0)
        self.assertAlmostEqual(v, 0.5)

    def test_alpha_validation(self):
        with self.assertRaises(ValueError):
            FailureWatchSmoother(alpha=0.0)
        with self.assertRaises(ValueError):
            FailureWatchSmoother(alpha=-0.1)
        with self.assertRaises(ValueError):
            FailureWatchSmoother(alpha=1.5)

    def test_ewma_span_constant_matches_alpha(self):
        # α = 2/(span+1) — the agreed Obico-style preset.
        self.assertAlmostEqual(EWMA_ALPHA, 2.0 / (EWMA_SPAN + 1))


class TestCollectFailureSamples(unittest.TestCase):
    def test_empty_targets_no_samples(self):
        smoother = FailureWatchSmoother()
        samples, dropped = collect_failure_samples([], {}, {}, smoother, now=0.0)
        self.assertEqual(samples, [])
        self.assertEqual(dropped, 0)

    def test_skips_non_printing_states(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1")]
        # Idle: no sample, no inference call
        for state in ("idle", "paused", "offline", "error", None):
            samples, dropped = collect_failure_samples(
                targets,
                {"p1": _status(state=state) if state else {}},
                {"p1": b"\xff\xd8" + b"\x00" * 100},
                smoother,
                now=0.0,
            )
            self.assertEqual(samples, [], f"state={state}")
            self.assertEqual(dropped, 0, f"state={state}")

    def test_skips_when_camera_disabled(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1", cameraEnabled=False)]
        samples, _ = collect_failure_samples(
            targets, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=0.0
        )
        self.assertEqual(samples, [])

    def test_skips_when_failure_watch_disabled(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1", aiFailureWatchEnabled=False)]
        samples, _ = collect_failure_samples(
            targets, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=0.0
        )
        self.assertEqual(samples, [])

    def test_skips_when_no_frame(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1")]
        samples, _ = collect_failure_samples(
            targets, {"p1": _status()}, {}, smoother, now=0.0
        )
        self.assertEqual(samples, [])

    def test_emits_sample_with_raw_and_smoothed(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1")]
        # Detector that returns a fixed value
        det = lambda _b: 0.8  # noqa: E731
        samples, dropped = collect_failure_samples(
            targets,
            {"p1": _status()},
            {"p1": b"\xff\xd8" + b"\x00" * 100},
            smoother,
            now=0.0,
            detector=det,
        )
        self.assertEqual(len(samples), 1)
        s = samples[0]
        self.assertEqual(s["printerId"], "p1")
        self.assertEqual(s["rawPBps"], 8000)
        # Cold start: smoothed == raw
        self.assertEqual(s["smoothedPBps"], 8000)
        self.assertEqual(dropped, 0)

    def test_smoothing_state_persists_across_calls(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1")]
        # First call: cold start at 0.0
        det0 = lambda _b: 0.0  # noqa: E731
        collect_failure_samples(
            targets, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=0.0, detector=det0
        )
        # Second call: raw=1.0 → smoothed ≈ α
        det1 = lambda _b: 1.0  # noqa: E731
        samples, _ = collect_failure_samples(
            targets, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=1.0, detector=det1
        )
        # α ≈ 0.154 → ~1538 bps
        self.assertEqual(samples[0]["rawPBps"], 10_000)
        self.assertAlmostEqual(samples[0]["smoothedPBps"] / 10_000, EWMA_ALPHA, places=3)

    def test_detector_raise_counts_as_dropped_not_fatal(self):
        smoother = FailureWatchSmoother()
        targets = [_target("p1"), _target("p2")]
        # p1 raises; p2 returns fine
        def det(b):
            if b == b"raise":
                raise RuntimeError("simulated")
            return 0.5
        samples, dropped = collect_failure_samples(
            targets,
            {"p1": _status(), "p2": _status()},
            {"p1": b"raise", "p2": b"\xff\xd8"},
            smoother,
            now=0.0,
            detector=det,
        )
        self.assertEqual(dropped, 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["printerId"], "p2")

    def test_forget_drops_state_for_disabled_printer(self):
        smoother = FailureWatchSmoother()
        targets_on = [_target("p1")]
        det = lambda _b: 0.8  # noqa: E731
        # Build up state
        collect_failure_samples(
            targets_on, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=0.0, detector=det
        )
        # Now disable p1 — collect should not emit, AND state should be dropped
        targets_off = [_target("p1", aiFailureWatchEnabled=False)]
        collect_failure_samples(
            targets_off, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=1.0, detector=det
        )
        # Re-enable: cold start because forget dropped the row
        collect_failure_samples(
            targets_on, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=2.0, detector=det
        )
        samples, _ = collect_failure_samples(
            targets_on, {"p1": _status()}, {"p1": b"\xff\xd8"}, smoother, now=3.0, detector=det
        )
        # After cold start at 0.8 then a second 0.8 sample, smoothed = 0.8 again.
        self.assertEqual(samples[0]["rawPBps"], 8000)
        self.assertEqual(samples[0]["smoothedPBps"], 8000)


if __name__ == "__main__":
    unittest.main()
