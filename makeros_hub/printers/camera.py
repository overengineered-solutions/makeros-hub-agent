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
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .bambu_camera import capture_frame as _bambu_capture_frame
from .bambu_camera import capture_frame_with_reason as _bambu_capture_with_reason
from .rtsp_camera import CaptureResult as _RtspResult
from .rtsp_camera import capture_frame as _rtsp_capture_frame
from .rtsp_camera import capture_frame_with_reason as _rtsp_capture_with_reason
from .rtsp_camera import _supported_timeout_flag as _rtsp_supported_timeout_flag
from .rtsp_camera import _DEFAULT_TIMEOUT as _RTSP_DEADLINE_S


def prime_camera_capabilities() -> None:
    """Warm the ffmpeg socket-timeout capability probe at agent startup so the
    first real capture beat doesn't pay the (cached) probe cost inside its
    deadline budget (Codex MEDIUM). Best-effort — never raises."""
    try:
        _rtsp_supported_timeout_flag()
    except Exception:  # noqa: BLE001 - warming is best-effort
        pass

_HTTP_TIMEOUT = 4.0
_MAX_FRAME_BYTES = 2 * 1024 * 1024


def camera_source_id(printer: dict[str, Any]) -> str:
    """Shared camera-v1 wire contract with pstation/src/lib/camera-source.ts."""
    keys = ("vendor", "model", "host", "serial", "moonrakerUrl", "cameraSnapshotUrl")
    facts = ["camera-v1"] + [str(printer.get(key) or "").strip() for key in keys]
    return hashlib.sha256(json.dumps(facts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


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
    # The whole body transfer: a camera trickling bytes no longer resets it. ponytail: the deadline is checked between
    # reads, so a peer trickling its HEADERS or chunk framing faster than the socket timeout can still hold this worker
    # longer; CameraScheduler.running keeps that to one thread per camera, and the camera reads as failing. A watchdog
    # that shuts the socket is the upgrade if a camera is ever seen doing it.
    deadline = time.monotonic() + timeout
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "makeros-hub-agent"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured LAN URL
            # uStreamer can return a valid placeholder JPEG when its USB camera is disconnected.
            # Its own online header distinguishes that placeholder from a real camera frame.
            online = resp.headers.get("X-UStreamer-Online")
            if online is not None and online.strip().lower() != "true":
                return (None, "unreachable", "camera source reports offline")
            data = bytearray()
            while len(data) <= max_bytes:
                if time.monotonic() > deadline:
                    return (None, "timeout", "transfer outlived the deadline")
                chunk = resp.read1(65536)
                if not chunk:
                    break
                data += chunk
            data = bytes(data)
    except urllib.error.HTTPError as exc:
        reason = "auth-fail" if exc.code in (401, 403) else "unreachable"
        return (None, reason, f"HTTP {exc.code}")
    except TimeoutError:
        return (None, "timeout", "")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # urlopen wraps a socket timeout as URLError(reason=TimeoutError) — keep
        # it as 'timeout' rather than the generic 'unreachable' (Codex MEDIUM).
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return (None, "timeout", "")
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
    # An operator-selected external camera belongs to the same printer record,
    # including Bambu printers whose built-in camera is unavailable.
    explicit = printer.get("cameraSnapshotUrl")
    if isinstance(explicit, str) and explicit.strip():
        return "http-snapshot"
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
    """Resolve a webcam snapshot URL for any printer. Prefer an explicit
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


# v0.69 (camera pipeline plan 2026-10-15). Shop box CPU is the budget: ffmpeg is the only heavy capture, so it keeps
# today's 4 slots; socket cameras (A1 :6000, HTTP snapshots) block in recv and each get a thread.
SITE_MAX_FRAME_BYTES = 1_500_000   # = pstation camera/route.ts MAX_BYTES; bigger is "too-large", never pushed
BEAT_BUDGET_S = 8.0                # = the v0.68 overall_timeout
FF_SLOTS = 4                       # ponytail: = v0.68's whole pool, so no new ffmpeg peak; lower it if cameraStats.childCpuMs/ffMs or loadAvg1m say the box chokes during builds
IO_MAX = 16                        # concurrent socket captures; more due cameras queue inside the beat (most overdue first)
BACKOFF_BASE_S, BACKOFF_CAP_S, BACKOFF_CAP_PRINTING_S = 60.0, 600.0, 120.0


@dataclass
class _Cam:
    source: str
    state: str
    link: Any                       # status["mqttConnectedAtMs"]: moves on every MQTT (re)connect = a power-cycle
    last_ok: Optional[float] = None  # the SITE answered 200
    last_try: float = -1.0          # not started: it stays overdue, and an old last_try wins a tie next beat
    streak: int = 0                 # real failures in a row
    retry_at: float = 0.0           # breaker
    sha: Optional[bytes] = None     # digest of the last accepted frame (frozen check)
    change: bool = False            # a state change not yet answered by an accepted frame or a real failure
    reset_at: float = float("-inf")  # last reconnect: due from then on, so a camera that reconnects every beat never jumps the queue
    reconnect: bool = False         # a failing camera reconnected: due until answered, ranked from reset_at


class CameraScheduler:
    """Per-printer capture cadence + a breaker for dead cameras (v0.69). Pure given an injected monotonic `now`.

    Cadence tiers (unchanged since v0.41): dense on a state change and in the first/last 5% of a print, sparse
    mid-print, rare while idle; a printer the site lists in `cameraWant` (or, from an older site, flagged cameraDense)
    gets the dense tier. A frame counts as captured only when the SITE accepted it (`accepted`, after the push), so a
    starved or refused frame stays due. A real failure (`failed`) is retried next beat once; from the second in a row
    the camera backs off 60 -> 120 -> 240 -> 480 -> 600 s (120 s cap while printing). A state change, a new MQTT
    connection (`mqttConnectedAtMs`: how a power-cycle shows up to the hub), a source change or a disable/enable
    (`forget`) resets it. Only the heartbeat thread touches this object, except `running`: the printers whose capture
    worker is still going (a straggler from an earlier beat), which a printer leaves only when its worker returns."""

    FIRST_OR_LAST_LAYER_S, MID_PRINT_S, IDLE_S = 20.0, 90.0, 600.0

    def __init__(self) -> None:
        self._cams: dict[str, _Cam] = {}
        # ponytail: set add/discard are atomic under the GIL; workers only add/discard their own printer id
        self.running: set[str] = set()
        self.stats: dict[str, Any] = {"beats": 0, "attempts": 0, "ok": 0, "notStarted": 0, "stillRunning": 0, "fail": {},
                                      "push": {}, "ioMs": [0, 0, 0], "ffMs": [0, 0, 0]}   # [count, sum, max]

    def plan(self, targets, status_by_id, now: float, want: Optional[set] = None) -> list[dict]:
        """The targets due this beat, most overdue first, then oldest last attempt (start order inside a lane)."""
        due = []
        for t in targets:
            pid = t.get("printerId")
            if not isinstance(pid, str) or camera_source_kind(t) is None:
                continue   # ponytail: no camera path (e.g. a Bambu without its access code) is a config gap, not a camera failure
            st = status_by_id.get(pid) or {}
            src, state, link = camera_source_id(t), st.get("state") or "", st.get("mqttConnectedAtMs")
            c = self._cams.get(pid)
            if c is None or c.source != src:                    # new printer or new source: fresh and due
                c, changed = _Cam(src, state, link), False
                self._cams[pid] = c
            else:
                changed = state != c.state or link != c.link     # a cycle under 150 s is idle->idle; only `link` moves
                if changed and state == c.state:
                    # a reconnect (power-cycle): a failing camera is retried at once, ranked from now (codex reviews of
                    # 02dc878 and 213d644: the streak reset below must not take its retry away)
                    c.reset_at, c.reconnect = now, c.reconnect or c.streak > 0   # a retry already armed stays armed
                elif changed:
                    c.change = True    # a state change: kept until answered, so one lost race does not hide it
                c.state, c.link = state, link
            if changed:
                c.streak, c.retry_at = 0, 0.0
            if now < c.retry_at:
                continue                                         # an open breaker wins over want
            wanted = pid in want if want is not None else bool(t.get("cameraDense"))   # older site: config-down flag
            interval = self.FIRST_OR_LAST_LAYER_S if wanted else self._interval(state, st.get("progressPct"))
            # a failing camera stays due (its breaker paces it): the one free miss is retried next beat even when its
            # tier is the 600 s idle one (codex review of 1e116ed)
            if c.change or c.reconnect or c.streak or c.last_ok is None or now - c.last_ok >= interval:
                # Earliest deadline first, after unanswered state changes: a new camera is due since forever, a working
                # one since last_ok + its tier. A camera that loses the race only grows more overdue, so it reaches the
                # front: wanted / first-layer cameras over the lane's capacity can no longer starve the mid-print ones. A
                # failing camera is due only from its failed try (one miss) or its breaker closing, never from its last
                # photo, so a dozen dead cameras cannot sit at the front of the lane forever.
                due_since = max(c.last_ok + interval if c.last_ok is not None else float("-inf"),
                                (c.retry_at or c.last_try) if c.streak else float("-inf"), c.reset_at)
                due.append(((not c.change, due_since, c.last_try), t))
        due.sort(key=lambda d: d[0])
        return [t for _, t in due]

    def attempted(self, pid: str, now: float) -> None:
        if pid in self._cams:
            self._cams[pid].last_try = now

    def accepted(self, pid: str, now: float, sha: Optional[bytes]) -> None:
        c = self._cams.get(pid)
        if c is None:
            return
        c.last_ok, c.streak, c.retry_at, c.sha, c.change, c.reconnect = now, 0, 0.0, sha, False, False
        self.stats["ok"] += 1

    def failed(self, pid: str, now: float) -> None:
        c = self._cams.get(pid)
        if c is None:
            return
        c.streak += 1
        c.change = c.reconnect = False   # the change / reconnect got its try; from here the breaker ranks it
        if c.streak >= 2:   # one miss retries next beat (a camera waking after a reboot); two opens the breaker
            cap = BACKOFF_CAP_PRINTING_S if c.state == "printing" else BACKOFF_CAP_S
            c.retry_at = now + min(BACKOFF_BASE_S * 2 ** min(c.streak - 2, 10), cap)   # exponent capped: no float overflow

    def is_repeat(self, pid: str, sha: Optional[bytes]) -> bool:
        c = self._cams.get(pid)
        return c is not None and c.sha is not None and c.sha == sha

    def breaker_open(self, now: float) -> int:
        return sum(now < c.retry_at for c in self._cams.values())

    def _interval(self, state: str, progress_pct: Any) -> float:
        if state == "printing":
            p = progress_pct if isinstance(progress_pct, (int, float)) else None
            # Inclusive boundaries: exactly 5% / 95% count as first/last-layer (dense).
            if p is None or p <= 5 or p >= 95:
                return self.FIRST_OR_LAST_LAYER_S
            return self.MID_PRINT_S
        return self.IDLE_S

    def forget(self, keep_ids: set[str]) -> None:
        """Drop tracking for printers no longer eligible (removed or camera disabled): a re-enable captures at once."""
        for pid in [p for p in self._cams if p not in keep_ids]:
            self._cams.pop(pid, None)


_NOT_STARTED = object()


def _lane(t: dict[str, Any]) -> str:
    return "ff" if camera_source_kind(t) == "bambu-rtsp" else "io"


def _add_ms(stat: list, dur_s: float) -> None:
    ms = int(dur_s * 1000)
    stat[0] += 1
    stat[1] += ms
    stat[2] = max(stat[2], ms)


def collect_camera_frames(
    targets: list[dict[str, Any]],
    status_by_id: dict[str, dict[str, Any]],
    scheduler: CameraScheduler,
    now: float,
    *,
    want: Optional[set] = None,
    capture: Callable[[dict[str, Any]], Optional[bytes]] = capture_printer_frame,
    capture_with_reason: Callable[
        [dict[str, Any]], tuple[Optional[bytes], Optional[str], str]
    ] = capture_printer_frame_with_reason,
    budget: float = BEAT_BUDGET_S,
    ff_slots: int = FF_SLOTS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Capture every DUE camera in two lanes and return `(frames, failures)` within `budget` seconds.

    io lane: one thread per due socket camera (A1 :6000, Pi webcams, Moonraker), up to IO_MAX at once, so a dead A1
    can no longer hold a slot a healthy camera needs. ff lane: `ff_slots` ffmpeg (RTSPS) captures, and one starts only
    when the beat still has its full 5.5 s deadline left. A capture that never started is NOT a failure: it is counted
    in stats.notStarted, never posted, and stays overdue, so it moves up next beat. A started capture that outlives the beat is
    'timeout'. A frame over the site's 1.5 MB cap is 'too-large'; the same bytes as the last accepted frame are
    'frozen' (a replayed picture must never read as current). Frames carry {printerId, jpegBase64, sourceId, sha256};
    failures {printerId, reason, stderrTail, sourceId}. The caller marks a frame accepted after the site's 200.
    Never raises; never blocks the beat on a straggler."""
    scheduler.stats["beats"] += 1
    due = scheduler.plan(targets, status_by_id, now, want)
    busy = [t for t in due if t["printerId"] in scheduler.running]
    if busy:
        # its capture from an earlier beat is still going: never a second one beside it (bounds threads at one per camera)
        scheduler.stats["stillRunning"] += len(busy)
        due = [t for t in due if t["printerId"] not in scheduler.running]
    if not due:
        return [], []
    # Back-compat: a caller passing only the legacy bytes-returning `capture` gets it wrapped.
    if capture is not capture_printer_frame and capture_with_reason is capture_printer_frame_with_reason:
        _legacy = capture

        def _wrap_legacy(t: dict[str, Any]) -> tuple[Optional[bytes], Optional[str], str]:
            jpeg = _legacy(t)
            return (jpeg, None if jpeg else "unknown", "")

        capture_with_reason = _wrap_legacy

    end = clock() + budget
    n_io = sum(_lane(t) == "io" for t in due)
    pools = {"io": concurrent.futures.ThreadPoolExecutor(max(1, min(IO_MAX, n_io))),
             "ff": concurrent.futures.ThreadPoolExecutor(max(1, ff_slots))}
    started: set[str] = set()   # set by a worker only once it passed its start gate

    def run(t: dict[str, Any], lane: str):
        left = end - clock()
        if left <= 0 or (lane == "ff" and left < _RTSP_DEADLINE_S):
            return _NOT_STARTED   # a start that cannot finish inside the beat is not a start
        started.add(t["printerId"])
        scheduler.running.add(t["printerId"])
        t0 = clock()
        try:
            jpeg, reason, tail = capture_with_reason(t)
        finally:
            scheduler.running.discard(t["printerId"])
        return jpeg, reason, tail, clock() - t0

    futs = {pools[_lane(t)].submit(run, t, _lane(t)): t for t in due}   # plan order = start order inside a lane
    try:
        for _ in concurrent.futures.as_completed(futs, timeout=max(0.0, end - clock())):
            pass
    except concurrent.futures.TimeoutError:
        pass   # keep whatever finished; stragglers are judged below
    finally:
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)   # never block the beat on a straggler

    frames: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for fut, t in futs.items():
        pid, lane = t["printerId"], _lane(t)
        if pid not in started:
            scheduler.stats["notStarted"] += 1
            continue   # silent: still due, and its old last_try sorts it first next beat
        scheduler.attempted(pid, now)
        scheduler.stats["attempts"] += 1
        if not fut.done():
            jpeg, reason, tail, dur = None, "timeout", "outlived the beat", budget
        elif fut.exception() is not None:
            jpeg, reason, tail, dur = None, "unknown", "", 0.0
        else:
            jpeg, reason, tail, dur = fut.result()
        _add_ms(scheduler.stats[lane + "Ms"], dur)
        sha = hashlib.sha256(jpeg).digest() if jpeg else None
        if jpeg and len(jpeg) > SITE_MAX_FRAME_BYTES:
            jpeg, reason, tail = None, "too-large", f"{len(jpeg)} bytes"
        elif jpeg and scheduler.is_repeat(pid, sha):
            jpeg, reason, tail = None, "frozen", "same bytes as the last accepted frame"
        src = camera_source_id(t)
        if jpeg:
            frames.append({"printerId": pid, "jpegBase64": base64.b64encode(jpeg).decode("ascii"),
                           "sourceId": src, "sha256": sha.hex()})
        else:
            reason = reason or "unknown"
            scheduler.failed(pid, now)
            scheduler.stats["fail"][reason] = scheduler.stats["fail"].get(reason, 0) + 1
            failures.append({"printerId": pid, "reason": reason, "stderrTail": tail or "", "sourceId": src})
    return frames, failures
