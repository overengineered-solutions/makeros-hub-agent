"""Pure-numpy YOLOv11 output decoder → single failure probability in [0, 1].

The HF model `ApatheticWithoutTheA/YoloV11s-3D-Print-Failure-Detection`
detects three classes IN ORDER: spaghetti, stringing, zits. YOLOv11 detect
exports as `output0` of shape `(1, 4 + num_classes, 8400)` where channels
0..3 are (cx, cy, w, h) in 640-space and channels 4..6 are sigmoid class
scores (NO separate objectness channel — class score IS the score).

For our scalar-probability use case (the cloud applies the threshold + the
notify decision), we don't need NMS — we take per-class max-confidence
across all 8400 anchors and combine into a single p_fail with class weights
reflecting how badly each failure mode hurts a print:

  spaghetti = 1.0   (catastrophic — wasted filament + bed)
  stringing = 0.7   (cosmetic but signals retraction tuning)
  zits      = 0.5   (cosmetic)

These weights live as constants here; the cloud tunes the sensitivity
threshold against the resulting smoothed p. Pure: same array in → same float
out, no IO, no state.
"""

from __future__ import annotations

from typing import Callable, Optional


CLASS_NAMES = ("spaghetti", "stringing", "zits")
# Default class weights — spaghetti dominates (it's the failure that costs the
# operator real money in wasted filament + plate damage); stringing/zits are
# cosmetic but useful as a soft signal. Cloud retuning lives in
# lib/print/failure-watch.ts (thresholds), so tweaking here is rare.
DEFAULT_CLASS_WEIGHTS = (1.0, 0.7, 0.5)

# YOLOv11 detect output channel layout.
NUM_CLASSES = 3
NUM_CHANNELS = 4 + NUM_CLASSES  # cx, cy, w, h, c0, c1, c2
NUM_ANCHORS = 8400  # 80² + 40² + 20² for 640 input


def decode_failure_probability(
    output0,
    class_weights: tuple[float, float, float] = DEFAULT_CLASS_WEIGHTS,
    on_out_of_range: Optional[Callable[[float], None]] = None,
) -> float:
    """`output0` is the raw model output, expected shape `(1, 7, 8400)`
    (or `(7, 8400)` — both accepted). Returns a single probability in
    [0, 1] = max over classes of (per-class-max-conf × class_weight).

    Defensive — non-finite values are skipped (treated as 0), out-of-range
    inputs clamped at the boundary, malformed shapes raise. The agent's
    `collect_failure_samples` catches detector exceptions and counts them
    to diagnostics, so a raise here surfaces as a dropped sample, not a
    crashed beat.
    """
    import numpy as np

    arr = np.asarray(output0)
    # Accept either (1, 7, 8400) or (7, 8400) — the runtime wrapper strips
    # the batch dim before passing, but a model that wasn't exported with
    # `--dynamic` keeps it. We don't care; either layout works.
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim != 2:
        raise ValueError(f"expected (1, C, N) or (C, N), got shape {arr.shape}")
    if arr.shape[0] != NUM_CHANNELS:
        raise ValueError(
            f"expected {NUM_CHANNELS} channels (4 box + {NUM_CLASSES} class), got {arr.shape[0]}"
        )

    # Replace any non-finite values with 0 so a single NaN can't poison the
    # max. (np.isfinite-and-mask is cheaper than np.nan_to_num and clearer
    # at this scale.)
    mask = np.isfinite(arr)
    if not mask.all():
        arr = np.where(mask, arr, 0.0)

    # Per-class max over the 8400 anchor predictions. Shape: (NUM_CLASSES,).
    class_max = arr[4:NUM_CHANNELS, :].max(axis=1)

    # If any per-class max is materially outside the expected sigmoid range
    # [0, 1] (allowing a small float-precision slop), the model was likely
    # exported without sigmoid'd class heads — let the caller log that
    # actionably (not on every frame). Threshold is loose so genuine 1.0001
    # numerical artifacts don't trip.
    if on_out_of_range is not None:
        max_observed = float(class_max.max())
        if max_observed > 1.01 or float(class_max.min()) < -0.01:
            on_out_of_range(max_observed)

    # Weight + reduce. Clamp each class score to [0, 1] first so a sigmoid
    # that landed at 1.0001 doesn't push weighted past 1.0; clamp at the end
    # too so weight=1.0 + score=0.999 = 0.999 (already there) but a future
    # weight > 1 would still return ≤1.0.
    weighted = [
        max(0.0, min(1.0, float(class_max[i]))) * float(class_weights[i])
        for i in range(NUM_CLASSES)
    ]
    return max(0.0, min(1.0, max(weighted)))
