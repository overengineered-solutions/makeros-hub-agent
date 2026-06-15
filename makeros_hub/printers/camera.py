"""Vendor-flexible camera capture: pick the right source per printer.

Different printers expose their camera completely differently, so capture is a
small strategy dispatch rather than one hardcoded path:

  - Bambu A1 / A1 mini / P1 / P1S  -> proprietary TCP-TLS MJPEG on :6000
                                      (bambu_camera.capture_frame). The A1 mini
                                      is the PS pilot; this is the path SimplyPrint
                                      uses over LAN.
  - Bambu X1 / X1C / X1E            -> RTSP :322 (needs ffmpeg). NOT YET built ->
                                      returns None so it degrades gracefully.
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

_HTTP_TIMEOUT = 4.0
_MAX_FRAME_BYTES = 2 * 1024 * 1024


def http_snapshot(
    url: str,
    *,
    timeout: float = _HTTP_TIMEOUT,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> Optional[bytes]:
    """GET a single JPEG from a webcam snapshot URL (Moonraker/OctoPrint/etc.).
    Returns the bytes only if they look like a JPEG and are within the cap."""
    if not url:
        return None
    # Only fetch over http(s) — never let a config-supplied URL become file://,
    # ftp://, etc. (local-file / scheme-confusion exfil into a "camera frame").
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "makeros-hub-agent"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured LAN URL
            data = resp.read(max_bytes + 1)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if not data or len(data) > max_bytes or not data.startswith(b"\xff\xd8"):
        return None
    return data


def _is_bambu_x1(model: Any) -> bool:
    return "X1" in str(model or "").upper()


def camera_source_kind(printer: dict[str, Any]) -> Optional[str]:
    """Which capture strategy applies to this printer (for logging/diagnostics),
    or None when we have no camera path for it."""
    vendor = str(printer.get("vendor") or "").lower()
    if vendor == "bambu":
        if _is_bambu_x1(printer.get("model")):
            return "rtsp-x1"  # not yet implemented
        if printer.get("host") and printer.get("accessCode"):
            return "bambu-lan"
        return None
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
    if kind == "http-snapshot":
        return http_snapshot(_snapshot_url(printer) or "")
    # 'rtsp-x1' (TODO: ffmpeg) and None both degrade to no-frame.
    return None


class CameraScheduler:
    """Phase-adaptive capture cadence, per printer. Captures densely at a state
    change + during the first/last few % of a print (where things go wrong),
    sparsely mid-print, and rarely while idle — so the board feels live without
    hammering the printer or the heartbeat. Pure given an injected monotonic
    `now` (seconds). Stateful across beats (last-capture time + last state)."""

    FIRST_OR_LAST_LAYER_S = 20.0  # dense: first ~5% / last ~5% of a print
    MID_PRINT_S = 90.0  # sparse: steady-state printing
    IDLE_S = 600.0  # rare: idle/paused/offline keepalive

    def __init__(self) -> None:
        self._last_capture: dict[str, float] = {}
        self._last_state: dict[str, str] = {}

    def should_capture(
        self, printer_id: str, state: Optional[str], progress_pct: Any, now: float
    ) -> bool:
        state = state or ""
        prev_state = self._last_state.get(printer_id)
        last = self._last_capture.get(printer_id)
        self._last_state[printer_id] = state
        if last is None:  # first sighting
            return self._mark(printer_id, now)
        if state != prev_state:  # state change is high-signal — grab one now
            return self._mark(printer_id, now)
        if now - last >= self._interval(state, progress_pct):
            return self._mark(printer_id, now)
        return False

    def _interval(self, state: str, progress_pct: Any) -> float:
        if state == "printing":
            p = progress_pct if isinstance(progress_pct, (int, float)) else None
            if p is None or p < 5 or p > 95:
                return self.FIRST_OR_LAST_LAYER_S
            return self.MID_PRINT_S
        return self.IDLE_S

    def _mark(self, printer_id: str, now: float) -> bool:
        self._last_capture[printer_id] = now
        return True

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
    max_workers: int = 4,
    overall_timeout: float = 8.0,
) -> list[dict[str, str]]:
    """For every printer DUE for a frame (per the scheduler + its live state),
    capture in PARALLEL and return `[{printerId, jpegBase64}]`. Bounded by
    `overall_timeout` so a slow/unreachable camera never stalls the heartbeat —
    whatever finishes in time ships this beat, the rest are silently retried next
    beat. Never raises; capturedAt is intentionally omitted (the cloud stamps
    receipt time, avoiding agent/server clock skew)."""
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
        return []

    frames: list[dict[str, str]] = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(due)))
    try:
        fut_to_pid = {ex.submit(capture, t): t["printerId"] for t in due}
        try:
            for fut in concurrent.futures.as_completed(fut_to_pid, timeout=overall_timeout):
                try:
                    jpeg = fut.result()
                except Exception:  # noqa: BLE001 - a bad capture never sinks the beat
                    jpeg = None
                if jpeg:
                    frames.append(
                        {
                            "printerId": fut_to_pid[fut],
                            "jpegBase64": base64.b64encode(jpeg).decode("ascii"),
                        }
                    )
        except concurrent.futures.TimeoutError:
            pass  # keep whatever finished; the slow ones retry next beat
    finally:
        # Do NOT block the heartbeat waiting on stragglers — each capture is
        # self-bounded (socket/HTTP timeout + total-byte cap). cancel_futures
        # drops any not-yet-started captures (when due > max_workers); a running
        # one finishes on its own and its result is discarded. (A `with` block
        # would wait=True here and could add seconds to the beat.)
        ex.shutdown(wait=False, cancel_futures=True)
    return frames
