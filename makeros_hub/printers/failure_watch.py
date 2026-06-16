"""AI failure-watch (spaghetti detection) — the agent half.

Inference + EWMA smoothing run LOCALLY on the hub; the cloud is the
threshold/notify authority. The agent ships both raw and smoothed `p` per
sample so the cloud can record the series for tuning and apply the
sensitivity-keyed threshold consistently across all sources.

V1 SCOPE — framework only:

  * Pluggable Detector interface: any callable that takes JPEG bytes and
    returns 0..1 confidence. Default detector is the no-op stub (always
    returns 0.0) — it makes the wire path testable end-to-end without
    bundling a model file. The real ONNX-backed detector lands in a
    follow-up (YoloV11s-3D-Print-Failure-Detection, MIT, ~20 MB ONNX).
  * Local EWMA smoothing (span≈12, α=2/13 ≈ 0.154) per printer so the
    cloud sees a stable smoothed series even though raw output bounces.
  * Stateful across beats (last smoothed p per printer); reconcile-aware
    (forget removed printers so a re-enable starts cold).
  * R4.5 silent-zero guard: if collect_failure_samples sees due printers
    but produces zero samples (every detect raised), the caller can log
    that — the function returns explicit (samples, dropped) counts.

Default-off everywhere: a printer with cameraEnabled=false OR
aiFailureWatchEnabled=false is never inferred on, even if a frame for it
landed in the heartbeat by another path.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger(__name__)

# Match Obico's "Detective" pattern: EWMA span=12 → α ≈ 2/(12+1).
# A short span keeps the series responsive to a real failure starting; the
# cloud threshold defines the false-positive floor.
EWMA_SPAN = 12
EWMA_ALPHA = 2.0 / (EWMA_SPAN + 1)

# Skip inference unless the printer is actively printing — idle frames are
# pure cost with zero failure signal. (Cloud will drop them anyway if state
# isn't printing, but skipping here saves the ONNX run.)
_INFER_STATES = frozenset({"printing"})


class Detector(Protocol):
    """A failure detector: takes JPEG bytes, returns a confidence in [0, 1].
    Implementations MUST NOT raise — return 0.0 on any unrecoverable error so
    one bad inference can't sink the beat. The default `stub_detector` is
    the example; a real ONNX detector ships in a follow-up."""

    def __call__(self, jpeg: bytes) -> float: ...


def stub_detector(jpeg: bytes) -> float:  # noqa: ARG001 - signature contract
    """Default no-op detector — always 0.0. Makes the wire path testable
    end-to-end before the real ONNX inference lands. Operator sees
    `samples=N, notified=0` in the cloud admin until the real model is
    wired (PR2)."""
    return 0.0


class FailureWatchSmoother:
    """Per-printer EWMA smoothing. Pure given monotonic `now`; state is
    last smoothed value + last-seen time per printer. Reconciler-aware:
    `forget` drops state for printers no longer present (or disabled).

    A printer's series resets when:
      * it's seen for the first time (cold start: smoothed = raw)
      * `forget` drops it (re-enable starts cold)
      * the inter-sample gap exceeds `stale_after_sec` — a fresh print
        shouldn't inherit the prior print's smoothed tail.
    """

    DEFAULT_STALE_AFTER_SEC = 600.0  # 10 min gap → cold start

    def __init__(self, alpha: float = EWMA_ALPHA, stale_after_sec: float | None = None) -> None:
        if not (0.0 < alpha <= 1.0):
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._stale_after_sec = (
            float(stale_after_sec)
            if stale_after_sec is not None
            else self.DEFAULT_STALE_AFTER_SEC
        )
        self._prev: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    def update(self, printer_id: str, raw_p: float, now: float) -> float:
        # Clamp at the boundary so a malformed model can't store nonsense.
        raw = max(0.0, min(1.0, float(raw_p)))
        prev = self._prev.get(printer_id)
        last = self._last_seen.get(printer_id)
        if prev is None or last is None or (now - last) > self._stale_after_sec:
            smoothed = raw  # cold start
        else:
            smoothed = self._alpha * raw + (1.0 - self._alpha) * prev
        self._prev[printer_id] = smoothed
        self._last_seen[printer_id] = now
        return smoothed

    def forget(self, keep_ids: set[str]) -> None:
        """Drop tracking for printers no longer eligible — so a re-enable
        starts cold (no inherited tail from a prior session)."""
        for d in (self._prev, self._last_seen):
            for pid in [p for p in d if p not in keep_ids]:
                d.pop(pid, None)


def _to_bps(p: float) -> int:
    """0..1 → 0..10000 (basis points). Clamped + rounded to int so the wire
    surface matches the cloud schema (integer column)."""
    if p != p or p < 0.0:  # NaN check + negative
        return 0
    if p > 1.0:
        return 10_000
    return int(round(p * 10_000))


def collect_failure_samples(
    targets: list[dict[str, Any]],
    status_by_id: dict[str, dict[str, Any]],
    frames_by_id: dict[str, bytes],
    smoother: FailureWatchSmoother,
    *,
    now: Optional[float] = None,
    detector: Detector = stub_detector,
) -> tuple[list[dict[str, Any]], int]:
    """For every eligible printer that produced a frame this beat, run
    inference, smooth, and emit a sample. Returns `(samples, dropped)`:

      samples — wire shape: {printerId, capturedAt, rawPBps, smoothedPBps}.
                Sent on the heartbeat as `failureSamples[]`. capturedAt is
                omitted; the cloud stamps receipt time (matches camera).

      dropped — count of eligible targets whose detector raised. Caller
                may surface this to local diagnostics (R4.5 loudness).

    Eligibility requires BOTH cameraEnabled AND aiFailureWatchEnabled to be
    strictly True (config-down semantics). State is also gated to printing
    only — idle/paused frames are pure cost.
    """
    if now is None:
        now = time.monotonic()
    samples: list[dict[str, Any]] = []
    dropped = 0
    eligible_ids: set[str] = set()
    for t in targets:
        pid = t.get("printerId")
        if not isinstance(pid, str):
            continue
        if not t.get("cameraEnabled") or not t.get("aiFailureWatchEnabled"):
            continue
        status = status_by_id.get(pid) or {}
        state = status.get("state")
        if state not in _INFER_STATES:
            continue
        jpeg = frames_by_id.get(pid)
        if not jpeg:
            continue
        eligible_ids.add(pid)
        try:
            raw = float(detector(jpeg))
        except Exception:  # noqa: BLE001 - detector contract says no raise; defense in depth
            dropped += 1
            continue
        smoothed = smoother.update(pid, raw, now)
        samples.append(
            {
                "printerId": pid,
                "rawPBps": _to_bps(raw),
                "smoothedPBps": _to_bps(smoothed),
            }
        )
    smoother.forget(eligible_ids)
    return samples, dropped
