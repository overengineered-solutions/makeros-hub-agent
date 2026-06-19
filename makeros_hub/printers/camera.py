"""Vendor-flexible camera capture: pick the right source per printer.

Different printers expose their camera completely differently, so capture is a
small strategy dispatch rather than one hardcoded path:

  - Bambu A1 / A1 mini / P1 / P1S  -> proprietary TCP-TLS MJPEG on :6000
                                      (bambu_camera.capture_frame). The A1 mini
                                      is the PS pilot; this is the path SimplyPrint
                                      uses over LAN.
  - Bambu X1 / X1C / X1E / X2D /
    H2* / P2S                        -> RTSPS :322 H.264 via ffmpeg
                                      (rtsp_camera.capture_frame). Needs ffmpeg on
                                      the hub + "LAN Mode Liveview" ON; degrades to
                                      None (no frame) if either is missing.
  - Klipper / Moonraker, OctoPrint,
    or any "other" with a webcam     -> plain HTTP JPEG snapshot (e.g. Moonraker
                                      `/webcam/?action=snapshot`, crowsnest,
                                      mjpg-streamer). Just GET the URL.

Every source returns JPEG bytes or None (no camera / unreachable / not-a-JPEG) —
a printer without a working camera is always a graceful no-op, never an error.
Add a new vendor by adding a branch here + (if needed) a source module; the
heartbeat wiring and the cloud side don't change.
"""

from __future__ import annotations

import base64
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from .bambu_camera import capture_frame as _bambu_capture_frame
from .bambu_camera import capture_frame_with_reason as _bambu_capture_with_reason
from .rtsp_camera import CaptureResult as _RtspResult
from .rtsp_camera import capture_frame as _rtsp_capture_frame
from .rtsp_camera import capture_frame_with_reason as _rtsp_capture_with_reason

_HTTP_TIMEOUT = 4.0
_MAX_FRAME_BYTES = 2 * 1024 * 1024


def _http_snapshot_with_reason(
    url: str,
    *,
    timeout: float = _HTTP_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> tuple[Optional[bytes], Optional[str], str]:
    """GET a single JPEG from a webcam snapshot URL (Moonraker/OctoPrint/etc.),
    returning a categorized reason on failure so a Klipper/OctoPrint camera is as
    diagnosable as the Bambu paths. Reason vocabulary matches the cloud copy map."""
    if not url:
        return (None, "no-camera-source", "")
    # Only fetch over http(s) — never let a config-supplied URL become file://,
    # ftp://, etc. (local-file / scheme-confusion exfil into a "camera frame").
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return (None, "unknown", "non-http snapshot url")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "makeros-hub-agent"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured LAN URL
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        reason = "auth-fail" if exc.code in (401, 403) else "unreachable"
        return (None, reason, f"HTTP {exc.code}")
    except TimeoutError:
        return (None, "timeout", "")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return (None, "unreachable", str(exc)[:200])
    if not data or len(data) > max_bytes or not data.startswith(b"\xff\xd8"):
        return (None, "bad-jpeg", "")
    return (data, None, "")


def http_snapshot(
    url: str,
    *,
    timeout: float = _HTTP_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> Optional[bytes]:
    """GET a single JPEG from a webcam snapshot URL (Moonraker/OctoPrint/etc.).
    Returns the bytes only if they look like a JPEG and are within the cap.
    Back-compat shim around _http_snapshot_with_reason."""
    return _http_snapshot_with_reason(url, timeout=timeout, max_bytes=max_bytes)[0]


def _bambu_camera_transport(model: Any) -> Optional[str]:
    """Which LAN camera transport a Bambu model uses (verified vs ha-bambulab
    `models.py` CAMERA_RTSP/CAMERA_IMAGE + OpenBambuAPI `video.md`):
      'rtsp'  -> RTSPS :322 H.264   (X1, X1C, X1E, X2D, H2*, P2S)
      'image' -> raw-JPEG :6000     (A1, A1 mini, P1P, P1S)
    None for an unrecognized model."""
    m = str(model or "").upper().replace(" ", "").replace("-", "")
    if not m:
        return None
    if any(k in m for k in ("X1", "X2D", "H2", "P2S")):
        return "rtsp"
    if "A1" in m or "P1" in m:
        return "image"
    return None


def camera_source_kind(printer: dict[str, Any]) -> Optional[str]:
    """Which capture strategy applies to this printer (for logging/diagnostics),
    or None when we have no camera path for it."""
    vendor = str(printer.get("vendor") or "").lower()
    if vendor == "bambu":
        if not (printer.get("host") and printer.get("accessCode")):
            return None
        # RTSP-class (X1/H2/P2S) -> ffmpeg :322; everything else Bambu (A1/P1,
        # or an unrecognized model) -> the :6000 raw-JPEG path as before.
        if _bambu_camera_transport(printer.get("model")) == "rtsp":
            return "bambu-rtsp"
        return "bambu-lan"
    # Klipper / OctoPrint / other: an HTTP snapshot URL is the universal path.
    if _snapshot_url(printer):
        return "http-snapshot"
    return None


def _snapshot_url(printer: dict[str, Any]) -> Optional[str]:
    """Resolve a webcam snapshot URL for a non-Bambu printer. Prefer an explicit
    cameraSnapshotUrl from config; else derive the Moonraker/OctoPrint default
    from the moonraker/base URL we already have."""
    explicit = printer.get("cameraSnapshotUrl")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    base = printer.get("moonrakerUrl") or printer.get("host")
    if isinstance(base, str) and base.strip():
        b = base.strip().rstrip("/")
        if not b.startswith(("http://", "https://")):
            b = f"http://{b}"
        return f"{b}/webcam/?action=snapshot"
    return None


def capture_printer_frame(printer: dict[str, Any]) -> Optional[bytes]:
    """Capture one JPEG frame for `printer` using the source that fits its
    vendor, or None when there's no (working) camera path. Never raises."""
    kind = camera_source_kind(printer)
    if kind == "bambu-lan":
        return _bambu_capture_frame(str(printer.get("host")), str(printer.get("accessCode")))
    if kind == "bambu-rtsp":
        return _rtsp_capture_frame(str(printer.get("host")), str(printer.get("accessCode")))
    if kind == "http-snapshot":
        return http_snapshot(_snapshot_url(printer) or "")
    return None


def capture_printer_frame_with_reason(printer: dict[str, Any]) -> tuple[Optional[bytes], Optional[str], str]:
    """Capture one frame AND return a categorized failure reason on no-frame.

    Returns (jpeg_or_none, reason_or_none, stderr_tail). On success: (jpeg, None, "").
    On failure: (None, reason, stderr_tail) where reason is a stable lowercase
    string the cloud maps to operator-facing copy ('no-ffmpeg', 'liveview-off',
    'auth-fail', 'unreachable', 'timeout', 'bad-jpeg', 'unknown', or
    'no-camera-source' when the printer has no LAN-camera path at all).

    Today only the RTSP path (X1/H2/P2S) returns a categorized reason — that's
    where the silent-zero failure mode actually bites (per the 2026-06-17
    workflow diagnose: 100% of fleet camera failures were on RTSPS-class
    printers). The :6000 path (A1/P1) and HTTP-snapshot path still return a
    generic 'unknown' on no-frame; future work can extend bambu_camera /
    http_snapshot the same way without touching this signature."""
    kind = camera_source_kind(printer)
    if kind == "bambu-lan":
        r = _bambu_capture_with_reason(
            str(printer.get("host")), str(printer.get("accessCode"))
        )
        if r.jpeg:
            return (r.jpeg, None, "")
        return (None, r.reason or "unknown", r.detail)
    if kind == "bambu-rtsp":
        result: _RtspResult = _rtsp_capture_with_reason(
            str(printer.get("host")), str(printer.get("accessCode"))
        )
        if result.jpeg:
            return (result.jpeg, None, "")
        return (None, result.reason or "unknown", result.stderr_tail)
    if kind == "http-snapshot":
        return _http_snapshot_with_reason(_snapshot_url(printer) or "")
    return (None, "no-camera-source", "")


class CameraScheduler:
    """Phase-adaptive capture cadence, per printer. Captures densely at a state
    change + during the first/last few % of a print (where things go wrong),
    sparsely mid-print, and rarely while idle — so the board feels live without
    hammering the printer or the heartbeat. Pure given an injected monotonic
    `now` (seconds). Stateful across beats (last-capture time + last state).

    Mark-on-success contract (v0.41.0): `should_capture` ONLY reads/updates the
    state-tracking dict; `mark_captured` stamps `last_capture` and is called by
    the caller AFTER the capture actually returns a frame. A persistently-failing
    printer therefore stays "due" every beat (the heartbeat is the natural
    backoff: ~30-40s) instead of going dark for IDLE_S=600s. The agent's
    overall_timeout=8s + max_workers=4 already bound the worst-case CPU spend
    on a broken printer, so the more-aggressive retry is safe. Pre-v0.41.0 the
    scheduler stamped on every attempt, which produced 10-minute blackouts on
    Liveview-off printers (2026-06-17 PS Moya P2S regression)."""

    FIRST_OR_LAST_LAYER_S = 20.0  # dense: first ~5% / last ~5% of a print
    MID_PRINT_S = 90.0  # sparse: steady-state printing
    IDLE_S = 600.0  # rare: idle/paused/offline keepalive

    def __init__(self) -> None:
        self._last_capture: dict[str, float] = {}
        self._last_state: dict[str, str] = {}

    def should_capture(
        self, printer_id: str, state: Optional[str], progress_pct: Any, now: float
    ) -> bool:
        """Decide whether to attempt a capture this beat. Side-effects ONLY the
        last-state tracking — does NOT stamp last_capture. Callers must call
        `mark_captured` AFTER a successful frame is in hand."""
        state = state or ""
        prev_state = self._last_state.get(printer_id)
        last = self._last_capture.get(printer_id)
        self._last_state[printer_id] = state
        if last is None:  # first sighting — always due
            return True
        if state != prev_state:  # state change is high-signal — grab one now
            return True
        return now - last >= self._interval(state, progress_pct)

    def mark_captured(self, printer_id: str, now: float) -> None:
        """Stamp last_capture so the next beat respects the cadence interval.
        Call only after a SUCCESSFUL capture; failures stay due next beat.

        Threading note: this is called from capture WORKER threads while
        should_capture runs on the heartbeat thread. That is safe ONLY because
        collect_camera_frames fully drains its workers before the heartbeat
        loop computes the next beat's due-set — the scheduler is never read and
        written concurrently. A future "stream frames as they finish" refactor
        would need a lock around these two dicts."""
        self._last_capture[printer_id] = now

    def _interval(self, state: str, progress_pct: Any) -> float:
        if state == "printing":
            p = progress_pct if isinstance(progress_pct, (int, float)) else None
            # Inclusive boundaries: exactly 5% / 95% count as first/last-layer
            # (dense) — matches the "first ~5% / last ~5%" intent.
            if p is None or p <= 5 or p >= 95:
                return self.FIRST_OR_LAST_LAYER_S
            return self.MID_PRINT_S
        return self.IDLE_S

    def forget(self, keep_ids: set[str]) -> None:
        """Drop tracking for printers no longer present (called on reconcile)."""
        for d in (self._last_capture, self._last_state):
            for pid in [p for p in d if p not in keep_ids]:
                d.pop(pid, None)


def collect_camera_frames(
    targets: list[dict[str, Any]],
    status_by_id: dict[str, dict[str, Any]],
    scheduler: CameraScheduler,
    now: float,
    *,
    capture: Callable[[dict[str, Any]], Optional[bytes]] = capture_printer_frame,
    capture_with_reason: Callable[
        [dict[str, Any]], tuple[Optional[bytes], Optional[str], str]
    ] = capture_printer_frame_with_reason,
    max_workers: int = 4,
    overall_timeout: float = 8.0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """For every printer DUE for a frame (per the scheduler + its live state),
    capture in PARALLEL and return `([{printerId, jpegBase64}], failures)`.
    Bounded by `overall_timeout` so a slow/unreachable camera never stalls the
    heartbeat — whatever finishes in time ships this beat, the rest are retried
    next beat. `failures` is a list of per-printer dicts
    `{printerId, reason, stderrTail}` so the cloud can surface the silent-drop
    with a categorized cause ('liveview-off' / 'auth-fail' / 'unreachable' /
    'timeout' / 'no-ffmpeg' / 'bad-jpeg' / 'unknown') instead of just a list of
    printerIds vanishing (R4.5). Never raises; capturedAt is omitted (the cloud
    stamps receipt time, avoiding agent/server clock skew).

    Mark-on-success contract (v0.41.0): only successful captures stamp the
    scheduler's last_capture, so flaky printers retry every beat instead of
    going dark for IDLE_S=600s. The `capture` arg is preserved for tests that
    only need the boolean success path; production wiring uses
    `capture_with_reason` to surface categorized failures.

    BACK-COMPAT: callers receiving the legacy `list[str]` shape (printerIds
    only) can map(.get('printerId')) over the new failures list. The cloud
    heartbeat route v0.41.0+ parses the dict shape; pre-v0.41.0 cloud builds
    fall back gracefully if both shapes ever cross.
    """
    due = [
        t
        for t in targets
        if isinstance(t.get("printerId"), str)
        and scheduler.should_capture(
            t["printerId"],
            (status_by_id.get(t["printerId"]) or {}).get("state"),
            (status_by_id.get(t["printerId"]) or {}).get("progressPct"),
            now,
        )
    ]
    if not due:
        return [], []

    # Back-compat: if the caller passed only the legacy bytes-returning
    # `capture` lambda (no `capture_with_reason`), wrap it. Tests that
    # provide just `capture=` keep working unchanged; the heartbeat path
    # in agent.py uses the default (capture_printer_frame_with_reason).
    if (
        capture is not capture_printer_frame
        and capture_with_reason is capture_printer_frame_with_reason
    ):
        _legacy = capture

        def _wrap_legacy(t: dict[str, Any]) -> tuple[Optional[bytes], Optional[str], str]:
            jpeg = _legacy(t)
            return (jpeg, None if jpeg else "unknown", "")

        capture_with_reason = _wrap_legacy

    frames: list[dict[str, str]] = []
    captured: set[str] = set()
    failure_by_pid: dict[str, dict[str, str]] = {}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(due)))
    try:
        fut_to_pid = {ex.submit(capture_with_reason, t): t["printerId"] for t in due}
        try:
            for fut in concurrent.futures.as_completed(fut_to_pid, timeout=overall_timeout):
                pid = fut_to_pid[fut]
                try:
                    jpeg, reason, stderr_tail = fut.result()
                except Exception:  # noqa: BLE001 - a bad capture never sinks the beat
                    jpeg, reason, stderr_tail = (None, "unknown", "")
                if jpeg:
                    captured.add(pid)
                    scheduler.mark_captured(pid, now)
                    frames.append(
                        {
                            "printerId": pid,
                            "jpegBase64": base64.b64encode(jpeg).decode("ascii"),
                        }
                    )
                else:
                    failure_by_pid[pid] = {
                        "printerId": pid,
                        "reason": reason or "unknown",
                        "stderrTail": stderr_tail or "",
                    }
        except concurrent.futures.TimeoutError:
            pass  # keep whatever finished; the slow ones retry next beat
    finally:
        # Do NOT block the heartbeat waiting on stragglers — each capture is
        # self-bounded (socket/HTTP timeout + total-byte cap). cancel_futures
        # drops any not-yet-started captures (when due > max_workers); a running
        # one finishes on its own and its result is discarded. (A `with` block
        # would wait=True here and could add seconds to the beat.)
        ex.shutdown(wait=False, cancel_futures=True)
    # DUE printers that produced no frame this beat — include a categorized
    # reason 'timeout' for those still pending when overall_timeout hit, so
    # the cloud's no_frame event always names a cause per printer.
    failures: list[dict[str, str]] = []
    for t in due:
        pid = t["printerId"]
        if pid in captured:
            continue
        f = failure_by_pid.get(pid)
        if f is None:
            f = {"printerId": pid, "reason": "timeout", "stderrTail": ""}
        failures.append(f)
    return frames, failures
