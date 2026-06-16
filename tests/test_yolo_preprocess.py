"""Preprocess tests — letterbox math (pure stdlib) + prepare_input (numpy)."""

import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class TestLetterboxDims(unittest.TestCase):
    """Pure stdlib — no deps, always runs."""

    def setUp(self):
        from makeros_hub.printers.yolo_preprocess import letterbox_dims

        self.letterbox_dims = letterbox_dims

    def test_square_input_no_pad(self):
        # 640×640 -> no scaling, no padding.
        w, h, pl, pt, s = self.letterbox_dims(640, 640)
        self.assertEqual((w, h, pl, pt), (640, 640, 0, 0))
        self.assertEqual(s, 1.0)

    def test_wide_landscape_pads_top_bottom(self):
        # 1280×720 -> scale=0.5, resized=640×360, pad top/bottom (640-360)/2 = 140
        w, h, pl, pt, s = self.letterbox_dims(1280, 720)
        self.assertEqual((w, h), (640, 360))
        self.assertEqual(pl, 0)
        self.assertEqual(pt, 140)
        self.assertAlmostEqual(s, 0.5)

    def test_tall_portrait_pads_left_right(self):
        # 720×1280 -> scale=0.5, resized=360×640, pad left/right (640-360)/2 = 140
        w, h, pl, pt, s = self.letterbox_dims(720, 1280)
        self.assertEqual((w, h), (360, 640))
        self.assertEqual(pl, 140)
        self.assertEqual(pt, 0)

    def test_small_input_scales_up(self):
        # 320×320 -> scale=2, resized=640×640, no pad
        w, h, pl, pt, s = self.letterbox_dims(320, 320)
        self.assertEqual((w, h, pl, pt), (640, 640, 0, 0))
        self.assertEqual(s, 2.0)

    def test_odd_dimensions_pad_centers_off_by_one(self):
        # 641×640 -> scale ≈ 0.9984, resized=640×639, pad top=0 bot=1 (// 2)
        w, h, pl, pt, s = self.letterbox_dims(641, 640)
        self.assertEqual((w, pl), (640, 0))
        # pad_top = (640 - 639) // 2 = 0; bottom pad is 1 (implicit).
        self.assertEqual(pt, 0)

    def test_rejects_zero_dim(self):
        with self.assertRaises(ValueError):
            self.letterbox_dims(0, 480)
        with self.assertRaises(ValueError):
            self.letterbox_dims(640, 0)

    def test_rejects_negative_dim(self):
        with self.assertRaises(ValueError):
            self.letterbox_dims(-1, 480)

    def test_custom_dst_size(self):
        # 1280×720 letterboxed to 320 instead of 640.
        w, h, pl, pt, s = self.letterbox_dims(1280, 720, dst=320)
        self.assertEqual((w, h), (320, 180))
        self.assertEqual(pl, 0)
        self.assertEqual(pt, 70)


@unittest.skipIf(np is None, "numpy not installed")
class TestPrepareInput(unittest.TestCase):
    def setUp(self):
        from makeros_hub.printers.yolo_preprocess import prepare_input

        self.prepare_input = prepare_input

    def test_square_input_shape(self):
        src = np.zeros((640, 640, 3), dtype=np.uint8)
        out = self.prepare_input(src)
        self.assertEqual(out.shape, (1, 3, 640, 640))
        self.assertEqual(out.dtype, np.float32)

    def test_landscape_input_shape(self):
        src = np.zeros((720, 1280, 3), dtype=np.uint8)
        out = self.prepare_input(src)
        self.assertEqual(out.shape, (1, 3, 640, 640))

    def test_normalized_to_unit_range(self):
        # All-white input → all-1.0 after /255.
        src = np.full((100, 200, 3), 255, dtype=np.uint8)
        out = self.prepare_input(src)
        self.assertGreaterEqual(out.min(), 114 / 255.0)  # 114 is the pad
        self.assertLessEqual(out.max(), 1.0)

    def test_pad_fill_is_114_over_255(self):
        # Tall portrait → padded left/right with 114.
        src = np.full((640, 100, 3), 255, dtype=np.uint8)
        out = self.prepare_input(src)
        # Left edge column (pad zone) should be 114/255.
        left_pad_value = out[0, 0, 0, 0]
        self.assertAlmostEqual(float(left_pad_value), 114 / 255.0, places=3)

    def test_rejects_grayscale_input(self):
        src = np.zeros((100, 100), dtype=np.uint8)
        with self.assertRaises(ValueError):
            self.prepare_input(src)

    def test_rejects_rgba_input(self):
        src = np.zeros((100, 100, 4), dtype=np.uint8)
        with self.assertRaises(ValueError):
            self.prepare_input(src)


@unittest.skipIf(np is None, "numpy not installed")
class TestPrepareInputFromRgbArray(unittest.TestCase):
    """The shared HWC→CHW+normalize stage used by BOTH the pure-numpy test
    path AND the Pillow-resize production path. Eliminating the prior
    divergence flagged by the adversarial review."""

    def setUp(self):
        from makeros_hub.printers.yolo_preprocess import prepare_input_from_rgb_array

        self.prepare = prepare_input_from_rgb_array

    def test_correct_canvas_shape_produces_batched_tensor(self):
        canvas = np.zeros((640, 640, 3), dtype=np.uint8)
        out = self.prepare(canvas)
        self.assertEqual(out.shape, (1, 3, 640, 640))
        self.assertEqual(out.dtype, np.float32)
        self.assertEqual(float(out.max()), 0.0)

    def test_all_white_canvas_normalizes_to_one(self):
        canvas = np.full((640, 640, 3), 255, dtype=np.uint8)
        out = self.prepare(canvas)
        self.assertAlmostEqual(float(out.max()), 1.0, places=5)
        self.assertAlmostEqual(float(out.min()), 1.0, places=5)

    def test_rejects_non_canvas_shape(self):
        with self.assertRaises(ValueError):
            self.prepare(np.zeros((100, 100, 3), dtype=np.uint8))
        with self.assertRaises(ValueError):
            self.prepare(np.zeros((640, 640), dtype=np.uint8))

    def test_chw_ordering_correct(self):
        # Different value per channel → after CHW transpose, each channel
        # plane should be constant.
        canvas = np.zeros((640, 640, 3), dtype=np.uint8)
        canvas[..., 0] = 100  # R
        canvas[..., 1] = 150  # G
        canvas[..., 2] = 200  # B
        out = self.prepare(canvas)
        self.assertAlmostEqual(float(out[0, 0].mean()), 100 / 255.0, places=3)
        self.assertAlmostEqual(float(out[0, 1].mean()), 150 / 255.0, places=3)
        self.assertAlmostEqual(float(out[0, 2].mean()), 200 / 255.0, places=3)


if __name__ == "__main__":
    unittest.main()
