"""Pure-numpy YOLO preprocessing: JPEG bytes → (1, 3, 640, 640) float32 tensor.

Separated from the runtime wrapper so the math is unit-testable WITHOUT
onnxruntime + Pillow installed in CI. The wrapper module imports these and
combines them with `PIL.Image.open(BytesIO(jpg))`; here we accept the already-
decoded RGB uint8 array and do the resize-via-letterbox + normalize +
HWC→CHW + add-batch in pure numpy.

Letterboxing (preserve aspect ratio, gray-pad to a square) is the Ultralytics
convention for YOLOv8/v11 inference and the only safe choice if we ever want
to map detections back to the original frame for bounding-box rendering. The
gray pad is 114 — the value Ultralytics's default `LetterBox` uses.

Everything here is pure: same input → same output, no IO, no state.
"""

from __future__ import annotations

from typing import Tuple


# Hardcoded — YOLOv11 detect-export ships at 640×640. Don't make this a knob;
# the export step has to match this for the loaded ONNX to accept the tensor.
INPUT_SIZE = 640

# Ultralytics LetterBox default fill color. Gray (114) avoids skewing the
# brightness distribution that the network was trained on.
LETTERBOX_FILL = 114


def letterbox_dims(src_w: int, src_h: int, dst: int = INPUT_SIZE) -> Tuple[int, int, int, int, float]:
    """Compute the letterbox geometry for a (src_w, src_h) frame into a dst×dst
    canvas. Returns (resized_w, resized_h, pad_left, pad_top, scale) where:

      - resized_w/h are the post-scale dimensions (one is <= dst, the other = dst)
      - pad_left/top are the left + top gray-pad offsets
      - scale is the multiplier applied to source coords

    Pure math — no array dep. Verified against Ultralytics's LetterBox
    (https://github.com/ultralytics/ultralytics/blob/main/ultralytics/data/augment.py).
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"invalid source dims: {src_w}x{src_h}")
    scale = min(dst / src_w, dst / src_h)
    new_w = int(round(src_w * scale))
    new_h = int(round(src_h * scale))
    # Center-pad: half the remainder on each side. Off-by-one defers to top/left
    # when odd — same as Ultralytics's `dw // 2`, `dh // 2`.
    pad_left = (dst - new_w) // 2
    pad_top = (dst - new_h) // 2
    return new_w, new_h, pad_left, pad_top, scale


def prepare_input(rgb_hwc_uint8) -> "_NDArrayLike":
    """RGB HWC uint8 (any size) → (1, 3, 640, 640) float32 in [0, 1].

    Pure-numpy convenience for tests: letterbox-resize via nearest-neighbor
    (cheap, deterministic, no Pillow dep), then delegate to
    `prepare_input_from_rgb_array` for the canonical normalize + CHW path.

    The PRODUCTION path uses Pillow's BILINEAR for the resize (sharper at the
    640× scale-down) and then calls `prepare_input_from_rgb_array` on the
    canvas — so both paths share the SAME final tensor stage. This was a
    real concern: the prior shape had two divergent normalize/CHW paths and
    the tests exercised the wrong one.
    """
    import numpy as np

    arr = np.asarray(rgb_hwc_uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HWC RGB, got shape {arr.shape}")
    src_h, src_w = arr.shape[0], arr.shape[1]
    new_w, new_h, pad_l, pad_t, _ = letterbox_dims(src_w, src_h)

    # Nearest-neighbor resize (numpy-only).
    ys = (np.arange(new_h) * src_h / new_h).astype(np.int64)
    xs = (np.arange(new_w) * src_w / new_w).astype(np.int64)
    resized = arr[ys[:, None], xs[None, :], :]

    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), LETTERBOX_FILL, dtype=np.uint8)
    canvas[pad_t : pad_t + new_h, pad_l : pad_l + new_w, :] = resized
    return prepare_input_from_rgb_array(canvas)


def prepare_input_from_rgb_array(canvas_hwc_uint8) -> "_NDArrayLike":
    """Canonical normalize + CHW for an ALREADY-letterboxed canvas
    (shape `(640, 640, 3)` HWC uint8).

    Steps (in order):
      1. Transpose HWC → CHW.
      2. Cast to float32 + divide by 255.
      3. Prepend the batch dim.

    Shared by BOTH the pure-numpy test path and the Pillow-resize production
    path — eliminates the divergence the adversarial review flagged.
    """
    import numpy as np

    arr = np.asarray(canvas_hwc_uint8)
    if arr.ndim != 3 or arr.shape != (INPUT_SIZE, INPUT_SIZE, 3):
        raise ValueError(
            f"expected ({INPUT_SIZE}, {INPUT_SIZE}, 3) HWC canvas, got shape {arr.shape}"
        )
    chw = arr.transpose(2, 0, 1)
    tensor = chw.astype(np.float32) / 255.0
    return tensor[None, ...]  # (1, 3, 640, 640)


# Type stub — numpy may not be importable in some test environments; the
# letterbox_dims function above is genuinely numpy-free.
class _NDArrayLike:  # pragma: no cover - typing alias only
    pass
