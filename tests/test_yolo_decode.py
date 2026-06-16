"""Pure-numpy decode tests. Don't need onnxruntime/Pillow — just numpy.

Numpy is a transitive dep of onnxruntime (mentioned in deps), but it's also a
trivial transitive of many other things — and unittest discover skips an
ImportError-on-collect file gracefully. Skip the suite if numpy is absent.
"""

import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover - CI has numpy; the skip is a defensive net
    np = None


@unittest.skipIf(np is None, "numpy not installed")
class TestDecodeFailureProbability(unittest.TestCase):
    def setUp(self):
        # Make a (1, 7, 8400) array of zeros — every detection is "nothing
        # here". Decoding should yield p=0 (all class maxes = 0).
        from makeros_hub.printers import yolo_decode

        self.NUM_ANCHORS = yolo_decode.NUM_ANCHORS
        self.decode = yolo_decode.decode_failure_probability

    def _empty(self):
        return np.zeros((1, 7, self.NUM_ANCHORS), dtype=np.float32)

    def test_all_zeros_returns_zero(self):
        out = self._empty()
        self.assertEqual(self.decode(out), 0.0)

    def test_single_spaghetti_detection_dominates(self):
        # Put 0.9 in the spaghetti (class 0) channel at anchor 0.
        out = self._empty()
        out[0, 4, 0] = 0.9
        p = self.decode(out)
        # spaghetti weight = 1.0 → p = 0.9
        self.assertAlmostEqual(p, 0.9, places=5)

    def test_stringing_weighted_lower_than_spaghetti(self):
        out = self._empty()
        out[0, 5, 0] = 0.9  # stringing
        p = self.decode(out)
        # stringing weight = 0.7 → p ≈ 0.63
        self.assertAlmostEqual(p, 0.63, places=5)

    def test_zits_weighted_lowest(self):
        out = self._empty()
        out[0, 6, 0] = 0.9
        p = self.decode(out)
        # zits weight = 0.5 → p ≈ 0.45
        self.assertAlmostEqual(p, 0.45, places=5)

    def test_max_across_classes(self):
        # When multiple classes fire, return the strongest weighted signal.
        out = self._empty()
        out[0, 4, 0] = 0.5   # spaghetti × 1.0 = 0.5
        out[0, 5, 0] = 0.9   # stringing × 0.7 = 0.63 ← wins
        out[0, 6, 0] = 0.95  # zits × 0.5 = 0.475
        self.assertAlmostEqual(self.decode(out), 0.63, places=5)

    def test_max_across_anchors_per_class(self):
        # The per-class max is taken across ALL 8400 anchors, then weighted.
        out = self._empty()
        out[0, 4, 1234] = 0.8
        out[0, 4, 5678] = 0.95  # this should win
        self.assertAlmostEqual(self.decode(out), 0.95, places=5)

    def test_accepts_unbatched_shape(self):
        # Some exports lose the batch dim. (7, 8400) must work too.
        out = np.zeros((7, self.NUM_ANCHORS), dtype=np.float32)
        out[4, 0] = 0.7
        self.assertAlmostEqual(self.decode(out), 0.7, places=5)

    def test_rejects_wrong_channel_count(self):
        # An (1, 6, 8400) output means the model isn't ours.
        out = np.zeros((1, 6, self.NUM_ANCHORS), dtype=np.float32)
        with self.assertRaises(ValueError):
            self.decode(out)

    def test_rejects_wrong_dimensions(self):
        out = np.zeros((7,), dtype=np.float32)
        with self.assertRaises(ValueError):
            self.decode(out)

    def test_nan_replaced_with_zero_not_propagated(self):
        # A single NaN must not poison the max; pure-numpy `.max()` would
        # propagate NaN forever.
        out = self._empty()
        out[0, 4, 0] = np.nan
        out[0, 4, 1] = 0.4
        self.assertAlmostEqual(self.decode(out), 0.4, places=5)

    def test_inf_replaced_with_zero(self):
        out = self._empty()
        out[0, 5, 0] = np.inf
        out[0, 6, 0] = 0.6
        # stringing inf → treated as 0; zits 0.6 × 0.5 = 0.3
        self.assertAlmostEqual(self.decode(out), 0.3, places=5)

    def test_out_of_range_class_score_clamped(self):
        # A sigmoid output that landed at 1.0001 (numerical artifact) must
        # not push weighted past 1.0.
        out = self._empty()
        out[0, 4, 0] = 1.0001
        self.assertEqual(self.decode(out), 1.0)

    def test_negative_class_score_clamped_to_zero(self):
        out = self._empty()
        out[0, 4, 0] = -0.5
        self.assertEqual(self.decode(out), 0.0)

    def test_custom_class_weights(self):
        out = self._empty()
        out[0, 4, 0] = 0.5
        out[0, 5, 0] = 0.5
        out[0, 6, 0] = 0.5
        # Equal weights — all three contribute 0.25; max is 0.25.
        p = self.decode(out, class_weights=(0.5, 0.5, 0.5))
        self.assertAlmostEqual(p, 0.25, places=5)

    def test_return_type_is_python_float(self):
        # The wire layer needs a plain float (`json.dumps` doesn't like
        # numpy scalars without extra wrangling).
        out = self._empty()
        out[0, 4, 0] = 0.5
        result = self.decode(out)
        self.assertIsInstance(result, float)


@unittest.skipIf(np is None, "numpy not installed")
class TestOutOfRangeCallback(unittest.TestCase):
    """Adversarial finding #8: when class scores fall outside the [0,1]
    sigmoid range, surface a callback so the agent can log once. Otherwise
    a model exported without sigmoid'd class heads silently degrades."""

    def setUp(self):
        from makeros_hub.printers import yolo_decode

        self.NUM_ANCHORS = yolo_decode.NUM_ANCHORS
        self.decode = yolo_decode.decode_failure_probability

    def _out(self):
        return np.zeros((1, 7, self.NUM_ANCHORS), dtype=np.float32)

    def test_no_callback_when_in_range(self):
        out = self._out()
        out[0, 4, 0] = 0.9
        observed = []
        self.decode(out, on_out_of_range=lambda mx: observed.append(mx))
        self.assertEqual(observed, [])

    def test_callback_fires_when_class_above_one(self):
        out = self._out()
        out[0, 4, 0] = 5.0  # raw logit
        observed = []
        p = self.decode(out, on_out_of_range=lambda mx: observed.append(mx))
        self.assertEqual(p, 1.0)  # clamped
        self.assertEqual(len(observed), 1)
        self.assertAlmostEqual(observed[0], 5.0, places=3)

    def test_callback_fires_when_class_below_zero(self):
        out = self._out()
        out[0, 4, 0] = -2.0
        observed = []
        self.decode(out, on_out_of_range=lambda mx: observed.append(mx))
        self.assertEqual(len(observed), 1)

    def test_callback_not_fired_for_tiny_slop(self):
        # 1.001 is within the loose tolerance (numerical artifact).
        out = self._out()
        out[0, 4, 0] = 1.001
        observed = []
        self.decode(out, on_out_of_range=lambda mx: observed.append(mx))
        self.assertEqual(observed, [])


if __name__ == "__main__":
    unittest.main()
