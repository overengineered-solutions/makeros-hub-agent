"""ONNX YOLO failure detector — the runtime half.

The PURE pieces (preprocess + decode) live in sibling modules so they
unit-test without `onnxruntime`/`Pillow`/`numpy` in CI. This file holds the
DEPENDS-ON-RUNTIME pieces:

  - soft-import gate (graceful no-op when onnxruntime/Pillow aren't installed —
    older hubs predate the v0.34+ install.sh that adds the wheels)
  - InferenceSession lifecycle (create-once, warm-up at boot, reuse forever,
    close() at SIGTERM so the memory arena releases deterministically)
  - download + sha256-verify of the .onnx weights file (one-time at boot,
    with hardened retry / partial-file cleanup / cache GC)
  - the Detector callable the agent's `collect_failure_samples` accepts
  - DetectorHolder — a thread-safe one-slot mailbox so the heavy boot can
    run on a background thread without blocking the first heartbeat

Default-off everywhere. The detector only ACTIVATES when `MODEL_URL` +
`MODEL_SHA256` env vars are both set (the cloud has both; the operator pins
them once the .onnx is uploaded to our mirror per docs/failure-watch-onnx.md).
Without them, `build_detector()` returns None and `failure_watch.py` falls
back to the stub — exact same wire path, samples all land as p=0.0, which is
the safe default before model + sensitivity are calibrated.

Concurrency: ORT's `InferenceSession.run()` is documented thread-safe across
concurrent calls on the same session (the intra-op pool fans out per-call;
shared session state is serialized internally). We instantiate one session
at agent boot and reuse it forever. The agent's failure-watch path currently
runs serially after camera collection — but a future parallelization is
session-safe.
"""

from __future__ import annotations

import collections
import glob
import hashlib
import http.client
import importlib.util
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from typing import Callable, Optional, Tuple

from .yolo_decode import DEFAULT_CLASS_WEIGHTS, decode_failure_probability
from .yolo_preprocess import INPUT_SIZE, prepare_input_from_rgb_array

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants — env-overridable. We DO NOT snapshot these at
# import time; build_detector() re-reads on every call so a systemd drop-in
# env change is picked up on agent restart without code redeploy.
# ---------------------------------------------------------------------------

# Where the agent writes the cached .onnx weights. /var/lib/makeros-hub
# survives `venv` recreation on OTA (the venv is wiped each update; state is
# not). Pathed by sha so a future model upgrade is atomic — new sha = new
# file, old file can be GC'd later.
DEFAULT_MODEL_CACHE_DIR = "/var/lib/makeros-hub/models"

# Boot path uses a TIGHT timeout so a slow CDN / DNS hiccup / captive portal
# can't push first-heartbeat past the cloud's offline threshold (~90s). The
# download lives on a background thread (see build_detector_async) so the
# inline timeout matters less than it used to, but a tight bound is still
# the right default. 15s × 2 attempts = 30s worst case.
DEFAULT_DOWNLOAD_TIMEOUT_SEC = 15
DEFAULT_DOWNLOAD_RETRIES = 1

# Detector knobs. The CPU defaults are tuned for Pi 5 (4 cores) — leaving 2
# cores free for the heartbeat loop, vp live-mirror, and HTTP server.
DEFAULT_INTRA_OP_NUM_THREADS = 2
DEFAULT_INTER_OP_NUM_THREADS = 1

# Per-call latency ring. Bounded so memory is constant across millions of
# beats. The detector exposes `latency_summary()` so the agent can drop a
# (median_ms, max_ms) summary into the heartbeat diagnostics.
LATENCY_RING_SIZE = 16

# How often to re-log "stub mode active" — the operator pinning a model
# should reset this. Bounded so a hub stuck in stub for weeks doesn't
# spam the diag surface.
STUB_REWARN_INTERVAL_SEC = 24 * 60 * 60  # 24 h

# Cleanup window for stragglers in the cache dir.
PARTIAL_CLEANUP_AGE_SEC = 60 * 60  # 1 h

_INPUT_NAME_FALLBACK = "images"
_OUTPUT_NAME_FALLBACK = "output0"


def _env_url() -> str:
    return os.environ.get("MAKEROS_HUB_MODEL_URL", "").strip()


def _env_sha() -> str:
    return os.environ.get("MAKEROS_HUB_MODEL_SHA256", "").strip().lower()


def _env_cache_dir() -> str:
    return os.environ.get("MAKEROS_HUB_MODEL_CACHE_DIR", DEFAULT_MODEL_CACHE_DIR)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("detector: invalid int env %s=%r; using default %d", name, raw, default)
        return default


def _env_class_weights() -> Tuple[float, float, float]:
    """Parse `MAKEROS_HUB_CLASS_WEIGHTS` as `1.0,0.7,0.5` (3 comma-separated
    floats); fall back to DEFAULT_CLASS_WEIGHTS on parse failure. Env-tuneable
    so an operator can rebalance the spaghetti/stringing/zits weights without
    a code redeploy."""
    raw = os.environ.get("MAKEROS_HUB_CLASS_WEIGHTS", "").strip()
    if not raw:
        return DEFAULT_CLASS_WEIGHTS
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        log.warning("detector: invalid MAKEROS_HUB_CLASS_WEIGHTS=%r; using defaults", raw)
        return DEFAULT_CLASS_WEIGHTS
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        log.warning("detector: invalid MAKEROS_HUB_CLASS_WEIGHTS=%r; using defaults", raw)
        return DEFAULT_CLASS_WEIGHTS


# ---------------------------------------------------------------------------
# Soft-import gate
# ---------------------------------------------------------------------------

def runtime_available() -> bool:
    """True when both onnxruntime + Pillow + numpy are importable.

    Hubs older than v0.34 don't have onnxruntime/Pillow in the venv — they
    were never auto-installed. `build_detector()` returns None on these, the
    framework falls back to the stub, and the operator sees a one-time
    `detector.onnxruntime_missing` event in the platform-admin diag surface.
    Same shape as the agent's existing paho/cryptography degrade pattern.
    """
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "PIL", "numpy"))


# ---------------------------------------------------------------------------
# Model file management
# ---------------------------------------------------------------------------

def _cached_model_path(sha256: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{sha256}.onnx")


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sweep_partials(cache_dir: str, max_age_sec: float = PARTIAL_CLEANUP_AGE_SEC) -> None:
    """Best-effort cleanup of `.partial.*` stragglers older than `max_age_sec`.
    Defends against a leaked partial filling the SD card across reboots
    after a crash during write."""
    now = time.time()
    for path in glob.glob(os.path.join(cache_dir, ".partial.*")):
        try:
            if now - os.path.getmtime(path) > max_age_sec:
                os.unlink(path)
                log.info("detector: swept stale partial %s", path)
        except OSError:
            pass


# Network errors we retry. http.client.IncompleteRead is the canonical
# transient (mid-chunk TCP reset); urllib.error.URLError + TimeoutError +
# ConnectionError cover DNS/connect/read-timeout/connection-reset.
_RETRYABLE_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
)


def download_model(
    url: str,
    expected_sha256: str,
    cache_dir: str = DEFAULT_MODEL_CACHE_DIR,
    timeout_sec: float = DEFAULT_DOWNLOAD_TIMEOUT_SEC,
    retries: int = DEFAULT_DOWNLOAD_RETRIES,
) -> str:
    """Download `url` into `cache_dir/<sha>.onnx`, verifying the sha256.

    Atomic: streams into a `.partial` next to the destination, fsyncs, then
    `os.replace` — so a crash mid-download never leaves a half-file at the
    real path. If the destination already exists and matches the sha, the
    download is skipped (idempotent boot).

    Hardened: a `try/finally` around every code path guarantees the partial
    file is unlinked on ANY exception (mid-write, mid-sha-read, sha mismatch,
    os.replace cross-device). Stale partials older than 1h are GC'd on entry.

    Raises:
      - `urllib.error.URLError` on persistent network failure (after retries)
      - `ValueError` on sha256 mismatch (post-download verify)
      - `OSError` on filesystem issues (mkdir, fsync, replace)
    """
    if not url or not expected_sha256:
        raise ValueError("download_model requires both url and expected_sha256")
    os.makedirs(cache_dir, exist_ok=True)
    _sweep_partials(cache_dir)

    dest = _cached_model_path(expected_sha256, cache_dir)
    if os.path.exists(dest):
        # Verify the cache still matches — a disk-corrupted file would
        # otherwise silently load + produce garbage outputs. Cheap (one
        # ~10 MB hash) compared to a single inference.
        try:
            if _sha256_of_file(dest) == expected_sha256:
                log.info("detector: model cache hit %s", dest)
                return dest
        except OSError:
            # If we can't even read the cache file, fall through to redownload.
            log.warning("detector: cache file unreadable; redownloading")
        log.warning("detector: cached model sha mismatch, re-downloading")
        try:
            os.remove(dest)
        except OSError:
            pass

    last_err: Optional[Exception] = None
    for attempt in range(1 + retries):
        tmp_path: Optional[str] = None
        success = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=cache_dir, prefix=".partial.", suffix=".onnx", delete=False
            ) as tmp:
                tmp_path = tmp.name
                req = urllib.request.Request(url, headers={"User-Agent": "makeros-hub-agent"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310 - pinned URL + sha verify
                    for chunk in iter(lambda: resp.read(1 << 20), b""):
                        tmp.write(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())

            got_sha = _sha256_of_file(tmp_path)
            if got_sha != expected_sha256:
                raise ValueError(
                    f"sha256 mismatch: expected {expected_sha256}, got {got_sha}"
                )
            os.replace(tmp_path, dest)
            success = True
            log.info("detector: model downloaded to %s", dest)
            return dest
        except _RETRYABLE_NETWORK_ERRORS as e:
            last_err = e
            log.warning("detector: download attempt %d failed: %s", attempt + 1, e)
            continue
        finally:
            # Unlink the partial on ANY non-success path — including
            # mid-sha-read EIO, sha mismatch, or os.replace cross-device.
            # Best-effort: a second-stage unlink raising must not mask the
            # original exception.
            if tmp_path and not success:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # If we got here, every attempt failed.
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# The Detector class
# ---------------------------------------------------------------------------

class OnnxYoloDetector:
    """Stateful Detector — loads the ONNX session once, runs inference on
    each call. Implements the `Detector` protocol from failure_watch.py
    (`__call__(jpeg: bytes) -> float`).

    Construct only from `build_detector()` / `build_detector_async()` so the
    soft-import + download + warm-up dance happens centrally.

    Exposes:
      - warmup() — idempotent; runs one zero-tensor inference to amortize
        cold-kernel-selection cost. Default behavior runs it INLINE during
        construction so __call__ never races warmup.
      - latency_summary() — (count, median_ms, max_ms) over the last N calls
      - model_sha — basename of the loaded model file (sha256), for diag
      - close() — releases the session (memory arena GC) and joins the
        warmup thread if one was spawned. Called by the agent on SIGTERM.
    """

    def __init__(
        self,
        model_path: str,
        class_weights: Tuple[float, float, float] = DEFAULT_CLASS_WEIGHTS,
        intra_op_threads: int = DEFAULT_INTRA_OP_NUM_THREADS,
        inter_op_threads: int = DEFAULT_INTER_OP_NUM_THREADS,
        warmup_inline: bool = True,
    ):
        import onnxruntime as ort  # noqa: F401 - import-time validation only
        from PIL import Image as _Image  # noqa: F401 - same

        self._model_path = model_path
        self._class_weights = class_weights
        self._session = self._make_session(model_path, intra_op_threads, inter_op_threads)
        self._input_name = self._session.get_inputs()[0].name or _INPUT_NAME_FALLBACK
        self._output_name = self._session.get_outputs()[0].name or _OUTPUT_NAME_FALLBACK
        self._warmed_up = threading.Event()
        self._warmup_lock = threading.Lock()
        self._warmup_thread: Optional[threading.Thread] = None
        self._latencies_ms: collections.deque[float] = collections.deque(maxlen=LATENCY_RING_SIZE)
        # One-shot guard so a model exported without sigmoid'd class scores
        # only logs once — not on every frame.
        self._out_of_range_logged = False
        if warmup_inline:
            # Inline warmup is the simplest correct shape — by the time
            # __call__ runs, the session is hot. Costs ~700ms once at boot,
            # but the WHOLE boot path already runs on a background thread
            # (build_detector_async) so the heartbeat loop isn't blocked.
            self.warmup()

    @property
    def model_sha(self) -> str:
        """The sha256 stem of the loaded model file (without `.onnx`).
        Used by the heartbeat diag so the cloud can see WHICH model this
        hub is running — handy when the operator rolls out a new pin."""
        return os.path.basename(self._model_path).rstrip(".onnx")

    @staticmethod
    def _make_session(model_path: str, intra_op: int, inter_op: int):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, intra_op)
        opts.inter_op_num_threads = max(1, inter_op)
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # Pin providers explicitly — silences the "default provider being
        # phased out" warning in ORT 1.17+ and future-proofs against the
        # default changing. CPU is the only one available on the Pi build.
        providers = ["CPUExecutionProvider"]
        return ort.InferenceSession(model_path, sess_options=opts, providers=providers)

    def warmup(self) -> None:
        """First inference is ~3-5x slower than steady state (kernel selection,
        memory arena allocation). Run one zero-tensor inference to amortize
        the cost at construction time so `__call__` is always hot.
        Idempotent — re-calling after warmup is a cheap no-op."""
        with self._warmup_lock:
            if self._warmed_up.is_set():
                return
            import numpy as np

            zeros = np.zeros((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
            try:
                self._session.run([self._output_name], {self._input_name: zeros})
            except Exception:  # noqa: BLE001 - warmup failure is non-fatal
                log.exception("detector: warmup inference failed; first real call will be slow")
            self._warmed_up.set()

    def latency_summary(self) -> Tuple[int, Optional[float], Optional[float]]:
        """(count, median_ms, max_ms) over the latency ring. Returns
        (0, None, None) when no samples have been recorded yet. Surfaced via
        the heartbeat diag so the operator sees a regression spike (e.g. a
        background process eating cores) without log-diving."""
        if not self._latencies_ms:
            return 0, None, None
        sorted_lat = sorted(self._latencies_ms)
        mid = len(sorted_lat) // 2
        median = (
            sorted_lat[mid]
            if len(sorted_lat) % 2 == 1
            else (sorted_lat[mid - 1] + sorted_lat[mid]) / 2
        )
        return len(sorted_lat), median, max(sorted_lat)

    def __call__(self, jpeg: bytes) -> float:
        """JPEG bytes → failure probability in [0, 1]. A decode/preprocess
        failure returns 0.0 (treated as 'no signal' by the cloud — never a
        notification trigger). An ORT inference RAISE is re-raised so the
        caller (`collect_failure_samples`) increments the dropped diagnostic
        counter."""
        from PIL import Image
        import numpy as np

        from .yolo_preprocess import letterbox_dims

        if not jpeg or not isinstance(jpeg, (bytes, bytearray)):
            return 0.0

        # Preprocess in ONE try/except — PIL.Image.open is lazy, so a
        # corrupt JPEG won't raise until the resize/convert/np.asarray
        # chain forces a decode. Wrap the whole chain so any of those
        # stages can fail uniformly to 0.0 + debug log.
        try:
            img = Image.open(BytesIO(jpeg)).convert("RGB")
            src_w, src_h = img.size
            new_w, new_h, pad_l, pad_t, _ = letterbox_dims(src_w, src_h)
            resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
            canvas = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (114, 114, 114))
            canvas.paste(resized, (pad_l, pad_t))
            arr = np.asarray(canvas, dtype=np.uint8)  # (H, W, 3)
            tensor = prepare_input_from_rgb_array(arr)
        except Exception:  # noqa: BLE001 - corrupt JPEG / preprocess error → soft no-signal
            log.debug("detector: preprocess failed; returning 0", exc_info=True)
            return 0.0

        t0 = time.monotonic()
        try:
            outputs = self._session.run([self._output_name], {self._input_name: tensor})
        except Exception:
            log.exception("detector: ORT inference failed")
            raise  # signals 'dropped' to the agent's diagnostic counter
        dt_ms = (time.monotonic() - t0) * 1000.0
        self._latencies_ms.append(dt_ms)

        try:
            p = decode_failure_probability(
                outputs[0],
                self._class_weights,
                on_out_of_range=self._note_out_of_range,
            )
        except Exception:
            log.exception("detector: output decode failed")
            return 0.0
        return p

    def _note_out_of_range(self, max_observed: float) -> None:
        """Called once when the decoder sees a class score outside the
        expected [0, 1] sigmoid range — indicates the operator exported
        the model WITHOUT sigmoid'd class scores (e.g. raw logits). The
        single warn surfaces the misconfiguration so the operator can
        re-export instead of letting the clamp silently degrade results."""
        if self._out_of_range_logged:
            return
        self._out_of_range_logged = True
        log.warning(
            "detector: YOLO class scores out of sigmoid range (max=%.3f); "
            "model may have been exported without sigmoid'd class heads "
            "(re-export with the Ultralytics CLI defaults)",
            max_observed,
        )

    def close(self) -> None:
        """Release the ORT session (memory arena reclaimed when refcount
        hits 0) and join the warmup thread if one is still alive.
        Called by the agent on SIGTERM so systemd can stop us cleanly."""
        try:
            if self._warmup_thread and self._warmup_thread.is_alive():
                self._warmup_thread.join(timeout=2.0)
        except Exception:  # noqa: BLE001 - cleanup must not raise
            pass
        # Drop the session reference — ORT's destructor frees the arena.
        self._session = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Async holder — lets the heavy boot run off the heartbeat path
# ---------------------------------------------------------------------------

class DetectorHolder:
    """Thread-safe one-slot mailbox for the detector callable.

    The heartbeat loop reads `.detector()` at the start of every beat.
    Before the background boot finishes, that returns None (caller falls
    back to the stub). Once boot completes (success or fail), the slot is
    set to either the OnnxYoloDetector or None (stub mode), and every
    subsequent beat sees the new value.

    Why this exists: `build_detector()` can block up to ~30s on a slow
    download. Doing that synchronously in agent.run() would push the
    first heartbeat past the cloud's offline threshold. Threading the
    boot keeps the agent looking online from second one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._detector: Optional[OnnxYoloDetector] = None
        self._ready = threading.Event()
        self._boot_outcome: Optional[str] = None  # diagnostic surface

    def detector(self) -> Optional[OnnxYoloDetector]:
        with self._lock:
            return self._detector

    def set(self, detector: Optional[OnnxYoloDetector], outcome: str) -> None:
        with self._lock:
            self._detector = detector
            self._boot_outcome = outcome
        self._ready.set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def boot_outcome(self) -> Optional[str]:
        return self._boot_outcome

    def close(self) -> None:
        with self._lock:
            d = self._detector
            self._detector = None
        if d is not None:
            d.close()


# ---------------------------------------------------------------------------
# Factory — synchronous, returns the detector directly. For inline use in
# tests / callers that want explicit control.
# ---------------------------------------------------------------------------

def build_detector(
    url: Optional[str] = None,
    sha256: Optional[str] = None,
    cache_dir: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    retries: Optional[int] = None,
    class_weights: Optional[Tuple[float, float, float]] = None,
) -> Optional[OnnxYoloDetector]:
    """Returns an `OnnxYoloDetector` ready to call, or None if disabled /
    unavailable. Env vars are read FRESH inside this function (not at module
    import) so a systemd drop-in change is picked up on every call.

    Skips silently when:
      - MODEL_URL or MODEL_SHA256 is empty (operator hasn't pinned the model)
      - onnxruntime / Pillow / numpy isn't installed in the venv
      - Download / sha verify fails (network down at boot)
    """
    eff_url = url if url is not None else _env_url()
    eff_sha = sha256 if sha256 is not None else _env_sha()
    eff_cache_dir = cache_dir if cache_dir is not None else _env_cache_dir()
    eff_timeout = (
        timeout_sec if timeout_sec is not None
        else _env_int("MAKEROS_HUB_DETECTOR_DOWNLOAD_TIMEOUT_SEC", DEFAULT_DOWNLOAD_TIMEOUT_SEC)
    )
    eff_retries = (
        retries if retries is not None
        else _env_int("MAKEROS_HUB_DETECTOR_DOWNLOAD_RETRIES", DEFAULT_DOWNLOAD_RETRIES)
    )
    eff_weights = class_weights if class_weights is not None else _env_class_weights()
    eff_intra = _env_int("MAKEROS_HUB_DETECTOR_INTRA_OP_THREADS", DEFAULT_INTRA_OP_NUM_THREADS)
    eff_inter = _env_int("MAKEROS_HUB_DETECTOR_INTER_OP_THREADS", DEFAULT_INTER_OP_NUM_THREADS)

    if not eff_url or not eff_sha:
        log.info("detector: MODEL_URL/MODEL_SHA256 unset; falling back to stub")
        return None
    if not runtime_available():
        log.warning(
            "detector: onnxruntime/Pillow/numpy not in venv; falling back to stub. "
            "Run install.sh to fetch them."
        )
        return None
    try:
        model_path = download_model(
            eff_url, eff_sha, cache_dir=eff_cache_dir, timeout_sec=eff_timeout, retries=eff_retries
        )
    except Exception:
        log.exception("detector: model download/verify failed; falling back to stub")
        return None
    try:
        det = OnnxYoloDetector(
            model_path,
            class_weights=eff_weights,
            intra_op_threads=eff_intra,
            inter_op_threads=eff_inter,
            warmup_inline=True,
        )
    except Exception:
        log.exception("detector: session init failed; falling back to stub")
        return None
    log.info("detector: ready (model=%s)", model_path)
    return det


# ---------------------------------------------------------------------------
# Factory — async. Returns a DetectorHolder; spawns a background thread
# that calls build_detector() and stuffs the result into the holder. The
# agent calls .detector() at the start of every heartbeat.
# ---------------------------------------------------------------------------

def build_detector_async(
    *,
    on_outcome: Optional[Callable[[str, Optional[str]], None]] = None,
) -> DetectorHolder:
    """Returns a DetectorHolder immediately; spawns a daemon thread that
    builds the detector in the background. The heartbeat loop reads
    `.detector()` at the start of every beat and falls back to the stub
    while the slot is None — so the first beat is sent FAST regardless of
    download latency.

    `on_outcome(outcome, model_sha)` is called once when boot completes
    (success or fail). Outcomes:
      - "active"         — detector ready, model_sha is the loaded model
      - "stub_no_url"    — MODEL_URL/MODEL_SHA256 unset (default config)
      - "stub_no_deps"   — onnxruntime/Pillow/numpy not in venv
      - "stub_download_failed" — network/sha mismatch
      - "stub_session_init_failed" — ORT session construction raised
    """
    holder = DetectorHolder()

    def _outcome_label() -> str:
        if not _env_url() or not _env_sha():
            return "stub_no_url"
        if not runtime_available():
            return "stub_no_deps"
        return ""  # let the build path classify

    def _boot() -> None:
        # Pre-classify the cheap fails so build_detector() doesn't need to.
        early = _outcome_label()
        if early:
            holder.set(None, early)
            if on_outcome:
                try:
                    on_outcome(early, None)
                except Exception:  # noqa: BLE001 - never let the callback sink boot
                    log.exception("detector: on_outcome callback raised")
            return
        det = build_detector()
        if det is None:
            outcome = "stub_download_failed"  # build_detector logs the precise reason
            holder.set(None, outcome)
        else:
            outcome = "active"
            holder.set(det, outcome)
        if on_outcome:
            try:
                on_outcome(outcome, det.model_sha if det else None)
            except Exception:  # noqa: BLE001
                log.exception("detector: on_outcome callback raised")

    t = threading.Thread(target=_boot, name="detector-boot", daemon=True)
    t.start()
    return holder
