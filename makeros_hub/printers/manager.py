"""PrinterManager — reconciles live printer adapters against the cloud's
config-down and gathers their normalized status for the heartbeat.

The cloud is the source of truth for WHICH printers exist + their connection
facts (operator adds them in /admin/3dprinting/hubs). The agent pulls that list
(GET /api/print/hub/config) whenever the heartbeat's `configVersion` changes,
then starts/stops/replaces adapters to match. A printer whose access code or IP
changed gets a fresh adapter (its fingerprint changed).

Imports the paho-backed BambuAdapter LAZILY so this module — and the heartbeat
loop — stay importable on a box where paho isn't installed yet.
"""

from __future__ import annotations

import logging
import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..diagnostics import get_default, redact
from .queue_progress import write_json_durably
from .threemf_objects import Unreadable3mf, check_plate_entry, gcode_plates, parse_plate_objects, plate_named

log = logging.getLogger("makeros-hub.printers")

MAX_DISPATCHED_QUEUE_JOBS = 1000
# A printer in any of these is mid-job: never send it another project_file (v0.53 — a hub restart mid-window used to
# re-dispatch a still-'assigned' job onto a printer that was already heating for it).
BUSY_GCODE_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE"})
BUSY_ACTIVITY_STATES = frozenset({"printing", "paused"})
# The dispatched-job guard persists across restarts (v0.53): the cloud re-sends an 'assigned' job every beat until the
# printer reports RUNNING, and bed heating (PREPARE, which maps to idle) can outlast an OTA restart.
DISPATCHED_STATE_PATH = Path(os.environ.get("MAKEROS_HUB_DISPATCHED_STATE", "/var/lib/makeros-hub/dispatched.json"))
DISPATCHED_TTL_SEC = 24 * 3600
MAX_DISPATCHED_COMMANDS = 1000
# v0.69: the command guard below persists too. pstation puts a 'sent' command back to pending after 10 minutes without a
# result (hub-commands.ts takePendingCommands) and fails it after an hour; a restart that lost both the result and an
# in-RAM guard re-ran a pause or a dryer cycle ten minutes later. Entries older than the cloud's hour are dropped.
DISPATCHED_COMMANDS_PATH = Path(os.environ.get("MAKEROS_HUB_DISPATCHED_COMMANDS", "/var/lib/makeros-hub/dispatched-commands.json"))
DISPATCHED_COMMANDS_TTL_SEC = 2 * 3600
# Fallback rebuild for adapters that remain in error. Paho normally retries
# network/authentication failures itself (observed in the shop on 2026-09-09),
# but an adapter whose start() failed needs the manager to try again. This
# bounds rebuilds; it cannot repair a wrong access code.
STUCK_ADAPTER_RETRY_S = 120.0
_SUBMISSION_UID_RE = re.compile(r"^[a-f0-9]{8,64}$")
# v0.72 automatic slicing (operator, 2026-10-18): a web upload may carry a member's checked mesh and a slice spec instead
# of a sliced plate. The hub slices it on the shop box through a root-owned runner (oes-pool-deploy
# scripts/install-slicer.py installs it; slicer/ in this repo is the container), then treats the plate like any upload.
SLICE_RUNNER = os.environ.get("MAKEROS_HUB_SLICE_RUNNER", "/opt/ps-slicer/run-slice")
SLICE_TIMEOUT_SEC = 660
_MESH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,118}\.(stl|obj|3mf)$", re.I)


def run_slice_job(uid: str) -> int:
    """The runner's exit code; it writes <spool>/<uid>/.slice/out/result.json either way."""
    import subprocess
    return subprocess.run(["sudo", "-n", SLICE_RUNNER, uid], capture_output=True, timeout=SLICE_TIMEOUT_SEC).returncode


def parse_slicer_caps(result) -> dict | None:
    """What a ps-slicer result.json says its image slices (Send to PS step 2), strictly: two finite numbers with
    0 < scaleMin <= scaleMax <= 1e6, and line widths in (0, 5] mm under at most 16 short model names. Anything else, or an
    image from before this one that says nothing, is unknown (None)."""
    import math
    if not isinstance(result, dict):
        return None
    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    lo, hi = result.get("scaleMin"), result.get("scaleMax")
    if not (num(lo) and num(hi) and 0 < lo <= hi <= 1e6):
        return None
    widths = result.get("lineWidth") if isinstance(result.get("lineWidth"), dict) else {}
    return {"scaleMin": float(lo), "scaleMax": float(hi),
            "lineWidth": {k: float(v) for k, v in list(widths.items())[:16] if isinstance(k, str) and 0 < len(k) <= 40 and num(v) and 0 < v <= 5}}


class SlicerCaps:
    """What the installed ps-slicer image says it slices, for the heartbeat (slicerCaps). Read from the image itself (a probe
    job, and every slice result), never from the agent's own version: install-slicer.py installs the image by its own box
    tap, so an agent and its image can differ, and an image can be rolled back under a running agent. Refreshed every
    REFRESH_SEC; None = unknown, which the site reads as the 0.05 to 20 every image since 0.72 takes."""
    REFRESH_SEC = 15 * 60

    def __init__(self) -> None:
        self.value: dict | None = None
        self.checked_at: float | None = None

    def seen(self, result) -> None:
        self.value = parse_slicer_caps(result)
        self.checked_at = time.monotonic()

    def stale(self) -> bool:
        return self.checked_at is None or time.monotonic() - self.checked_at > self.REFRESH_SEC


SLICER_CAPS = SlicerCaps()


def probe_slicer(spool_dir, runner=run_slice_job) -> dict | None:
    """Ask the installed image what it slices: a job of {"probe": true} through the runner a slice uses, in the spool like
    one. An image from before this one refuses it as a slice request, so its answer reads as unknown. Leaves nothing behind."""
    import shutil
    import uuid
    uid = uuid.uuid4().hex
    root = Path(spool_dir) / uid
    try:
        work = root / ".slice"
        work.mkdir(parents=True, mode=0o700)
        (work / "job.json").write_text(json.dumps({"probe": True}), encoding="utf-8")
        runner(uid)
        return json.loads((work / "out" / "result.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run says nothing; the slicer is then unknown
        log.info("slicer probe: no answer (%s)", exc)
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def slice_spec_of(spec: dict) -> dict:
    """Only the fields the slicer reads, so nothing else from the cloud reaches the job file (the wrapper validates them)."""
    fil = spec.get("filament") if isinstance(spec.get("filament"), dict) else {}
    fixes = spec.get("fixes") if isinstance(spec.get("fixes"), dict) else {}
    out: dict = {"model": spec.get("model"),
                 # material (v0.74.1): the spool's stated material, only when the site sends one
                 "filament": {**{k: fil.get(k) for k in ("type", "colorHex", "trayInfoIdx")},
                              **({"material": fil["material"]} if isinstance(fil.get("material"), str) and fil["material"].strip() else {})},
                 "fixes": {k: bool(fixes.get(k)) for k in ("orient", "brim", "supports") if k in fixes}}
    if isinstance(spec.get("scale"), (int, float)) and not isinstance(spec.get("scale"), bool):
        out["scale"] = float(spec["scale"])
    return out


def plate_of(value) -> int:
    """The plate a cloud message names: a positive int (a digit string is coerced), 1..64; anything else = plate 1 — the
    SAME rule for a fetch (what we parse) and an assignment (what we print), so the two can never disagree (codex v0.57)."""
    # v0.80.1: a canonical number only, never int() of an arbitrary digit string (4,300+ digits raise ValueError)
    return plate_named(value) or 1


def _fingerprint(p: dict) -> tuple:
    """What, if changed, means we must rebuild the adapter (new connection)."""
    return (p.get("vendor"), p.get("host"), p.get("serial"), p.get("accessCode"))


class PrinterManager:
    def __init__(self, diagnostics=None, laser=None) -> None:
        # v0.68: laser_* commands go here and never reach a printer adapter. The default knows no lasers, so every laser
        # command it sees fails closed (unknown_command / bad_params / expired / laser_unavailable).
        from ..laser import LaserHub
        self.laser = laser if laser is not None else LaserHub()
        self._adapters: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple] = {}
        # The cloud's connection facts per Bambu printer, kept so the heartbeat
        # can retry a failed build without waiting for a config edit. Holds the
        # accessCode — never log a row of this map.
        self._bambu_configs: dict[str, dict] = {}
        # Earliest monotonic time to rebuild a wedged adapter (see
        # STUCK_ADAPTER_RETRY_S). Set on every (re)build AND on every failed
        # build, so a printer with no adapter at all is retried on the same
        # backoff instead of every heartbeat.
        self._stuck_retry_at: dict[str, float] = {}
        self._diagnostics = diagnostics or get_default()
        # Static status for printers the agent can't drive yet (klipper, or a
        # Bambu missing its connection facts) — surfaced so the admin sees why.
        self._static: dict[str, dict] = {}
        # Unacked terminal jobs rescued from torn-down adapters (config change
        # rebuilds the adapter; its in-memory buffer must NOT die with it —
        # Codex review finding). Drained by pending_jobs / cleared by ack_jobs.
        self._orphan_jobs: dict[str, dict] = {}
        # Queue assignments are at-least-once from the cloud. Once this process
        # has successfully sent a start command for a queueJobId, a same-window
        # resend must not re-upload or re-start the printer. Terminal progress
        # reports prune this bounded guard.
        self._dispatched_queue_jobs: OrderedDict[str, float] = OrderedDict()
        self._dispatched_seq: dict[str, int] = {}     # queueJobId -> assignmentSeq of the guarded dispatch (v0.58 r5)
        self._dispatched_wall: dict[str, float] = {}   # queueJobId -> wall-clock stamp, the persisted twin of the above
        self._load_dispatched()
        # Same at-least-once guard for control commands. Cloud delivery is
        # one-shot (queued->delivered is atomic; delivered rows are never
        # re-selected), but a lost result report could in theory get the row
        # redelivered — a second ams_dry mode=1 would restart the heater cycle.
        # A successfully-dispatched requestId here makes a redelivery re-report
        # ok without republishing. Bounded like _dispatched_queue_jobs.
        self._dispatched_commands: OrderedDict[str, float] = OrderedDict()   # requestId -> wall stamp (v0.69: persisted)
        self._load_dispatched_commands()
        # Per-printer camera-routing facts (vendor/model/host/accessCode/urls),
        # rebuilt every reconcile. Lets the camera capturer pick the right source
        # (Bambu :6000 / HTTP snapshot / …) without re-plumbing config-down.
        self._camera_meta: dict[str, dict] = {}
        self.config_version: str | None = None

    def _record_failure(self, message, extra_secrets=None) -> None:
        if self._diagnostics is not None:
            self._diagnostics.record("printers", message, extra_secrets=extra_secrets)

    def _access_code_for(self, pid: str):
        fp = self._fingerprints.get(pid)
        if isinstance(fp, tuple) and len(fp) >= 4 and fp[3]:
            return fp[3]
        adapter = self._adapters.get(pid)
        return getattr(adapter, "_access_code", None) or self._bambu_configs.get(pid, {}).get("accessCode")

    def _redact_printer_exception(self, exc, code=None) -> str:
        return redact(str(exc), extra_secrets=[code] if code else None)

    def reconcile(self, printers: list[dict], version: str | None) -> None:
        self.config_version = version
        desired_ids = {p["id"] for p in printers if isinstance(p.get("id"), str)}
        self._bambu_configs = {p["id"]: dict(p) for p in printers if isinstance(p.get("id"), str) and p.get("vendor") == "bambu"}

        # Rebuild camera-routing meta from the full desired list (no stale rows).
        self._camera_meta = {
            p["id"]: {
                "printerId": p["id"],
                "vendor": p.get("vendor"),
                "model": p.get("model"),
                "host": p.get("host"),
                "serial": p.get("serial"),
                "accessCode": p.get("accessCode"),
                "moonrakerUrl": p.get("moonrakerUrl"),
                "cameraSnapshotUrl": p.get("cameraSnapshotUrl"),
                # Strict True only — a malformed/older payload sending a string
                # like "false" must NOT enable capture (preserve default-off).
                "cameraEnabled": p.get("cameraEnabled") is True,
                # cameraDense (0069): the cloud is asking a human to judge this printer from its picture right now —
                # capture on the dense cadence instead of the 600s idle tier. Same strict-True, default-off semantics.
                "cameraDense": p.get("cameraDense") is True,
                # V5 — AI failure-watch per-printer opt-in (cloud also gates on
                # the workspace feature). Same strict-True default-off semantics.
                "aiFailureWatchEnabled": p.get("aiFailureWatchEnabled") is True,
                "aiFailureSensitivity": (
                    str(p.get("aiFailureSensitivity") or "medium").lower()
                    if p.get("aiFailureSensitivity") in ("low", "medium", "high")
                    else "medium"
                ),
            }
            for p in printers
            if isinstance(p.get("id"), str)
        }

        # Drop adapters / static entries for printers no longer in config.
        for pid in list(self._adapters):
            if pid not in desired_ids:
                self._stop(pid)
        for pid in list(self._static):
            if pid not in desired_ids:
                self._static.pop(pid, None)
        # A printer whose build FAILED has no adapter, so the loop above never
        # reaches it — drop its retry deadline here or the map grows for the
        # life of the process.
        for pid in list(self._stuck_retry_at):
            if pid not in desired_ids:
                self._stuck_retry_at.pop(pid, None)

        for p in printers:
            pid = p.get("id")
            if not isinstance(pid, str):
                continue
            vendor = p.get("vendor")
            if vendor == "bambu":
                self._reconcile_bambu(pid, p)
            elif vendor == "klipper":
                self._reconcile_klipper(pid, p)
            else:
                # 'other' is the catch-all for non-Bambu non-Klipper printers
                # the operator may add (raw OctoPrint, Marlin host, etc.).
                # We don't drive these yet — surface clearly.
                self._static[pid] = {
                    "printerId": pid,
                    "connectionState": "error",
                    "errorReason": f"{vendor}_not_supported_yet",
                }

    def _reconcile_klipper(self, pid: str, p: dict) -> None:
        """Klipper printer: needs a Moonraker URL. Builds + starts a polling
        KlipperAdapter; no MQTT/paho dependency. Same fingerprint shape as
        Bambu so an admin edit (e.g. moved the printer to a new IP) triggers
        a clean restart."""
        moonraker_url = p.get("moonrakerUrl")
        if not moonraker_url:
            self._stop(pid)
            self._record_failure(f"klipper printer {pid} missing moonrakerUrl")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "incomplete_config",
            }
            return
        self._static.pop(pid, None)
        # Klipper fingerprint = (vendor, moonrakerUrl, model) — same shape contract
        # as Bambu's (vendor, host, serial, accessCode) so reconcile() can
        # treat them uniformly. Different from _fingerprint() so we compute
        # it inline here. The model is in it because start_print refuses a
        # G-code sliced for another model: an edited model must reach the adapter.
        fp = ("klipper", moonraker_url, p.get("model"))
        if self._fingerprints.get(pid) == fp and pid in self._adapters:
            return
        self._stop(pid)
        try:
            from .klipper import KlipperAdapter
        except ImportError as e:
            log.error("cannot import klipper adapter for %s: %s", pid, e)
            self._record_failure(f"cannot import klipper adapter for {pid}: {e}")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_klipper_module_missing",
            }
            return
        adapter = KlipperAdapter(pid, moonraker_url=moonraker_url, model=p.get("model"))
        try:
            adapter.start()
        except Exception as e:  # noqa: BLE001 - one bad printer can't sink config-down
            safe = redact(str(e))
            log.warning("cannot start klipper adapter for %s: %s", pid, safe)
            self._record_failure(f"cannot start klipper adapter for {pid}: {safe}")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_start_failed",
            }
            return
        self._adapters[pid] = adapter
        self._fingerprints[pid] = fp

    def _reconcile_bambu(self, pid: str, p: dict) -> None:
        host, serial, code = p.get("host"), p.get("serial"), p.get("accessCode")
        if not (host and serial and code):
            # Incomplete config — can't connect. Make it visible, don't crash.
            # The backoff is armed even though nothing was built: statuses()
            # walks every Bambu config each heartbeat, and an unfixable one must
            # not re-log/re-record a failure on every beat (hot loop).
            self._stop(pid)
            self._defer_retry(pid)
            self._record_failure(f"printer {pid} incomplete_config")
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "incomplete_config",
            }
            return
        self._static.pop(pid, None)
        fp = _fingerprint(p)
        if self._fingerprints.get(pid) == fp and pid in self._adapters:
            # Connection facts unchanged — normally keep the live connection. But
            # an adapter wedged in a hard 'error' state (CONNACK auth-fail /
            # never-connected, which paho won't auto-retry) is torn down + rebuilt
            # periodically so a recovered printer reconnects on its own. Transient
            # 'offline' drops are left to paho's own reconnect loop.
            if not self._stuck_needs_retry(pid):
                return
            log.info("bambu %s wedged in error state — rebuilding adapter to retry", pid)
        # New/changed connection facts, OR a stuck adapter being retried → rebuild.
        self._stop(pid)
        try:
            from .bambu import BambuAdapter  # lazy: needs paho
        except ImportError as e:  # paho not installed
            safe = self._redact_printer_exception(e, code)
            self._defer_retry(pid)
            log.error("cannot start Bambu adapter for %s — paho-mqtt missing: %s", pid, safe)
            self._record_failure(
                f"cannot start Bambu adapter for {pid}: paho-mqtt missing: {safe}",
                extra_secrets=[code],
            )
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_missing_paho",
            }
            return
        adapter = BambuAdapter(pid, host=host, serial=serial, access_code=code, model=p.get("model"))
        try:
            adapter.start()
        except Exception as e:  # noqa: BLE001 - one bad printer must not sink config-down
            safe = self._redact_printer_exception(e, code)
            self._defer_retry(pid)
            try:
                adapter.stop()  # start() may have created a client before failing.
            except Exception as stop_error:  # noqa: BLE001
                log.warning("partial Bambu teardown failed for %s: %s", pid, self._redact_printer_exception(stop_error, code))
            log.warning("cannot start Bambu adapter for %s: %s", pid, safe)
            self._record_failure(f"cannot start Bambu adapter for {pid}: {safe}", extra_secrets=[code])
            self._static[pid] = {
                "printerId": pid,
                "connectionState": "error",
                "errorReason": "agent_start_failed",
            }
            return
        self._adapters[pid] = adapter
        self._fingerprints[pid] = fp
        self._stuck_retry_at[pid] = time.monotonic() + STUCK_ADAPTER_RETRY_S

    def _defer_retry(self, pid: str) -> None:
        """Arm the rebuild backoff after a build that produced no adapter. Set
        AFTER _stop() (which clears the deadline along with the adapter)."""
        self._stuck_retry_at[pid] = time.monotonic() + STUCK_ADAPTER_RETRY_S

    def _stuck_needs_retry(self, pid: str) -> bool:
        """Retry a missing/errored adapter after its deadline; leave healthy and
        temporarily offline adapters to their own reconnect loop."""
        adapter = self._adapters.get(pid)
        if adapter is None:
            # No deadline recorded = never attempted → build it now.
            return time.monotonic() >= self._stuck_retry_at.get(pid, 0.0)
        try:
            state = adapter.status().get("connectionState")
        except Exception:  # noqa: BLE001 — a status read must never sink reconcile
            return False
        if state != "error":
            return False
        return time.monotonic() >= self._stuck_retry_at.get(pid, 0.0)

    def _stop(self, pid: str) -> None:
        code = self._access_code_for(pid)
        adapter = self._adapters.pop(pid, None)
        self._fingerprints.pop(pid, None)
        self._stuck_retry_at.pop(pid, None)
        if adapter is not None:
            # Stop FIRST, then rescue unacked terminal jobs (codex review of
            # release-0.68.0): a stopped adapter records nothing more, so the
            # rescue sees its final buffer. A config edit (e.g. rotated access
            # code) rebuilds the adapter and its buffer would otherwise vanish.
            try:
                adapter.stop()
            except Exception as e:  # noqa: BLE001 - a failed teardown must not lose the rescue
                log.warning("stopping %s failed: %s", pid, self._redact_printer_exception(e, code))
            try:
                rescued = adapter.pending_jobs()
                # Rebuilt adapters reload these same durable jobs. Keying them
                # avoids duplicate growth without discarding unacked evidence.
                for j in rescued:
                    if j.get("notDispatched"):
                        # v0.69: never unfenced. It stays in the printer's terminal file, and the rebuilt adapter reloads
                        # it behind the same wait for the old dispatch (codex review of 02dc878). ponytail: a printer
                        # removed for good keeps it on disk unsent, like any file nothing reads any more.
                        continue
                    self._orphan_jobs.setdefault(j["jobKey"], j)
                if rescued:
                    log.info("rescued jobs from %s: processed=%d, expected=%d", pid, len(rescued), len(rescued))
            except Exception as e:  # noqa: BLE001
                safe = self._redact_printer_exception(e, code)
                log.warning("could not rescue pending jobs from %s: %s", pid, safe)
                self._record_failure(f"could not rescue pending jobs from {pid}: {safe}", extra_secrets=[code])

    def statuses(self) -> list[dict]:
        # Heartbeats keep running when configVersion is unchanged. Retry failed connections here too,
        # using the same backoff/rebuild path; waiting for the next config edit can strand a printer forever.
        # Printers with NO adapter are included: a retry whose start() raised leaves none behind, and it is
        # exactly that printer (off, unplugged, LAN mode off) that must be retried again next window.
        for pid, config in list(self._bambu_configs.items()):
            if self._stuck_needs_retry(pid):
                self._reconcile_bambu(pid, config)
        out: list[dict] = []
        for pid, adapter in self._adapters.items():
            try:
                # Adapters that support it re-ask for get_version until the printer answers with its real module list
                # (only the A1 minis replied to the connect-time request; the other models' lists are what a VP of that
                # model must present to OrcaSlicer). Read-only, self-limiting, and never allowed to sink a beat.
                # The same goes for keeping AMS filament backup on (2026-10-23): a nudge, never allowed to sink a beat.
                for name in ("request_version_if_missing", "ensure_auto_refill"):
                    ask = getattr(adapter, name, None)
                    if callable(ask):
                        try:
                            ask()
                        except Exception:  # noqa: BLE001 — a probe or nudge is never worth a failed status read
                            pass
                out.append(adapter.status())
            except Exception as e:  # noqa: BLE001 — one bad adapter must not sink the heartbeat
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("status read failed for %s: %s", pid, safe)
                self._record_failure(f"status read failed for {pid}: {safe}", extra_secrets=[code])
                out.append({"printerId": pid, "connectionState": "error", "errorReason": "agent_status_error"})
        out.extend(self._static.values())
        return out

    def live_statuses(self) -> list[dict]:
        """v0.70: the telemetry push's read, every second on its own thread. Only each adapter's status(), which takes
        the adapter's own lock: no stuck-adapter retry, no get_version re-ask and no static error rows (those belong to
        the heartbeat beat, and none of them is ever connected). list() snapshots the adapter dict, so a reconcile on
        the heartbeat thread cannot change it under this iteration. Never raises: a bad adapter is skipped this second."""
        out: list[dict] = []
        for pid, adapter in list(self._adapters.items()):
            try:
                out.append(adapter.status())
            except Exception as e:  # noqa: BLE001 — the heartbeat's statuses() records it; here it is just skipped
                log.debug("telemetry status read failed for %s: %s", pid,
                          self._redact_printer_exception(e, self._access_code_for(pid)))
        return out

    def camera_targets(self) -> list[dict]:
        """Per-printer camera-routing facts (vendor/model/host/accessCode/urls)
        for the camera capturer. One entry per configured printer; the capturer
        decides which have a usable camera source and which don't."""
        return list(self._camera_meta.values())

    def pending_jobs(self) -> list[dict]:
        """Unacked evidence: every adapter's rows as they are, plus a rescued
        orphan only when no adapter holds its key (a rebuilt adapter reloads the
        same durable rows; codex review of release-0.68.0: deduping an adapter's
        own rows dropped a real same-key print). The cloud dedupes on jobKey."""
        rows: list[dict] = []
        waiting: set = set()
        for pid, adapter in self._adapters.items():
            try:
                mine = adapter.pending_jobs()
                marked = {j["jobKey"] for j in mine if j.get("notDispatched")}
                pend = getattr(adapter, "pending_queue_jobs", None)
                if marked and callable(pend) and pend():
                    # v0.69: a print that replaced a dropped saved print waits until that printer's older dispatch is
                    # resolved. The site's heartbeat matches an unbound terminal to the printer's 'printing' job by time,
                    # so it must arrive after the old job is held, never before (codex review of 26528c0).
                    waiting |= marked
                rows.extend(mine)
            except Exception as e:  # noqa: BLE001 — one adapter can't sink the loop
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("pending_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"pending_jobs failed for {pid}: {safe}", extra_secrets=[code])
        held = {j.get("jobKey") for j in rows}
        return [j for k, j in self._orphan_jobs.items() if k not in held] + [j for j in rows if j["jobKey"] not in waiting]

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Fan a confirmed-send ack out to every adapter + the orphan buffer."""
        if not job_keys:
            return
        keys = set(job_keys)
        self._orphan_jobs = {k: j for k, j in self._orphan_jobs.items() if k not in keys}
        for pid, adapter in self._adapters.items():
            try:
                adapter.ack_jobs(job_keys)
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("ack_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"ack_jobs failed for {pid}: {safe}", extra_secrets=[code])

    def _remember_dispatched_queue_job(self, queue_job_id: str, assignment_seq: int | None = None) -> bool:
        self._dispatched_queue_jobs[queue_job_id] = time.monotonic()
        self._dispatched_queue_jobs.move_to_end(queue_job_id)
        self._dispatched_wall[queue_job_id] = time.time()
        if isinstance(assignment_seq, int) and not isinstance(assignment_seq, bool):
            self._dispatched_seq[queue_job_id] = assignment_seq   # v0.58 r5: the guard is keyed by job AND assignment
        else:
            self._dispatched_seq.pop(queue_job_id, None)
        while len(self._dispatched_queue_jobs) > MAX_DISPATCHED_QUEUE_JOBS:
            evicted, _ = self._dispatched_queue_jobs.popitem(last=False)
            self._dispatched_wall.pop(evicted, None)
            self._dispatched_seq.pop(evicted, None)
        return self._save_dispatched()

    def _guard_matches(self, queue_job_id: str, assignment_seq) -> bool:
        """Is the guarded dispatch THIS assignment? Unknown on either side (pre-seq cloud / legacy state) counts as yes."""
        if queue_job_id not in self._dispatched_queue_jobs:
            return False
        known = self._dispatched_seq.get(queue_job_id)
        return known is None or not isinstance(assignment_seq, int) or isinstance(assignment_seq, bool) or known == assignment_seq

    def _forget_dispatched_queue_job(self, queue_job_id: str) -> None:
        self._dispatched_queue_jobs.pop(queue_job_id, None)
        self._dispatched_seq.pop(queue_job_id, None)
        if self._dispatched_wall.pop(queue_job_id, None) is not None:
            self._save_dispatched()

    def _load_dispatched(self) -> None:
        """Rehydrate the guard after a restart; entries older than DISPATCHED_TTL_SEC are dropped (the cloud will have
        moved such a job on long ago)."""
        try:
            raw = json.loads(DISPATCHED_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now_wall, now_mono = time.time(), time.monotonic()
        if not isinstance(raw, dict):
            return
        # v2 (v0.58 r5): {"v": 2, "jobs": {id: {"t": stamp, "seq": n}}}; legacy: {id: stamp}
        entries: dict = raw.get("jobs") if raw.get("v") == 2 and isinstance(raw.get("jobs"), dict) else raw
        for job_id, entry in entries.items():
            stamp = entry.get("t") if isinstance(entry, dict) else entry
            seq = entry.get("seq") if isinstance(entry, dict) else None
            if not (isinstance(job_id, str) and isinstance(stamp, (int, float))):
                continue
            if isinstance(seq, int) and not isinstance(seq, bool):
                self._dispatched_seq[job_id] = seq
            # A stamp from the "future" = the wall clock stepped BACKWARDS across the restart (a Pi before time sync,
            # codex v0.53): keep the entry — dropping it reopens the duplicate-dispatch window this guard exists for.
            age = max(0.0, now_wall - float(stamp))
            if age < DISPATCHED_TTL_SEC:
                self._dispatched_queue_jobs[job_id] = now_mono
                self._dispatched_wall[job_id] = float(stamp)

    def _save_dispatched(self) -> bool:
        """True only once durable, directory entry included (v0.69: a resolved dispatch is popped on the strength of it;
        a swallowed directory-fsync failure reported success, codex review of bc8aa63)."""
        encoded = json.dumps({"v": 2, "jobs": {j: {"t": t, **({"seq": self._dispatched_seq[j]} if j in self._dispatched_seq else {})}
                                                for j, t in self._dispatched_wall.items()}})
        if write_json_durably(DISPATCHED_STATE_PATH, encoded):
            return True
        log.warning("could not persist the dispatched-job guard")
        return False

    def _remember_command(self, request_id: str) -> None:
        """Guard a command that ran, durably before its result is even queued. ponytail: the publish and this write are
        not atomic — a crash in the milliseconds between them can still re-run a command the cloud redelivers."""
        self._dispatched_commands[request_id] = time.time()
        self._dispatched_commands.move_to_end(request_id)
        while len(self._dispatched_commands) > MAX_DISPATCHED_COMMANDS:
            self._dispatched_commands.popitem(last=False)
        if not write_json_durably(DISPATCHED_COMMANDS_PATH, json.dumps(dict(self._dispatched_commands))):
            log.warning("command guard not durable: a restart could re-run %s if the cloud redelivers it", request_id)

    def _load_dispatched_commands(self) -> None:
        try:
            raw = json.loads(DISPATCHED_COMMANDS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        # a stamp from the future (the clock stepped back across the restart) is kept, like the dispatch guard
        fresh = {rid: float(t) for rid, t in raw.items()
                 if isinstance(rid, str) and isinstance(t, (int, float)) and not isinstance(t, bool) and now - t < DISPATCHED_COMMANDS_TTL_SEC}
        for rid in sorted(fresh, key=fresh.get):   # oldest first, so the cap evicts the oldest
            self._dispatched_commands[rid] = fresh[rid]

    def busy_printers(self) -> list[str]:
        """Printers currently preparing/printing/paused per their own live status (the hub is the truth for physical
        state). Used to refuse a second dispatch and to defer an OTA restart."""
        out: list[str] = []
        for pid, adapter in self._adapters.items():
            if self._adapter_busy(adapter):
                out.append(pid)
        return out

    @staticmethod
    def _adapter_busy(adapter) -> bool:
        status = getattr(adapter, "status", None)
        if not callable(status):
            return False
        try:
            st = status()
        except Exception:  # noqa: BLE001 — a status read failure must not block dispatch or updates
            return False
        if not isinstance(st, dict):
            return False
        return str(st.get("gcodeState", "")).upper() in BUSY_GCODE_STATES or str(st.get("state", "")).lower() in BUSY_ACTIVITY_STATES

    def _assignment_path_ok(self, submission_uid: str, file_name: str) -> bool:
        return (
            bool(_SUBMISSION_UID_RE.fullmatch(submission_uid))
            and file_name == os.path.basename(file_name)
            and file_name not in (".", "..")
            and "/" not in file_name
            and "\\" not in file_name
        )

    def fetch_uploads(self, fetches, spool_dir, *, getter, reporter, max_per_beat: int = 1,
                      max_bytes: int = 64 * 1024 * 1024, slice_runner=run_slice_job) -> int:
        """Web uploads (v0.57, design B4): the cloud lists this hub's member uploads it has not fetched; download each
        into <spool>/<uid>/<file> (streaming, size-capped, wall-clock-bounded), verify the cloud's sha256, parse the sent
        plate's filaments the same way a VP capture is parsed, and report `fetched` so the job enters the scheduler. One
        per call keeps the loop responsive (the agent runs this on its own worker thread); the cloud lists the rest next
        beat. A failed fetch is logged and retried next beat (the file stays listed) and never leaves a temp file; a sha
        mismatch is reported so the member is told to re-upload. Returns the number of successful reports.
        `getter(path, dest) -> (sha256, size)`, `reporter(body) -> Response`."""
        from ..vprinter.capture import _vp_submit_filament, parse_printer_model_id, parse_required_filaments
        import hashlib
        import math
        import os as _os

        def stream_sha(path: Path) -> str | None:
            """sha256 of an existing spool file under the same cap — None when it is over the cap (never read into RAM)."""
            digest = hashlib.sha256()
            total = 0
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    digest.update(chunk)
            return digest.hexdigest()

        done = 0
        base = Path(spool_dir)
        taken = 0
        for f in (fetches if isinstance(fetches, list) else []):
            if taken >= max_per_beat:
                break
            if not isinstance(f, dict):
                continue
            job_id, uid, name, sha, path = (f.get("queueJobId"), f.get("submissionUid"), f.get("fileName"), f.get("sha256"), f.get("path"))
            if not all(isinstance(v, str) and v for v in (job_id, uid, name, sha, path)) or not self._assignment_path_ok(uid, name):
                log.warning("skipping malformed fetch: %s", {k: f.get(k) for k in ("queueJobId", "submissionUid", "fileName")})
                continue   # malformed rows never consume the per-beat slot (codex r2: a bad first row must not starve the rest)
            if not str(path).startswith("/api/print/hub/file/"):
                log.warning("skipping fetch with an unexpected path for %s", job_id)
                continue
            taken += 1
            sha = sha.lower()
            # the plate the cloud will print: a positive int (a digit string is coerced); anything else = 1, the same
            # default dispatch applies — so the parsed requirements always describe the plate that prints (codex r1)
            plate = plate_of(f.get("plate"))
            dest_dir = base / uid
            dest = dest_dir / name
            tmp = dest_dir / f".{name}.fetch.tmp"
            spec = f.get("slice")
            sliced: dict | None = None
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if isinstance(spec, dict):
                    sliced = self._slice_upload(f, spec, uid, dest_dir, dest, sha, path, getter=getter, reporter=reporter,
                                                stream_sha=stream_sha, runner=slice_runner)
                    if sliced is None:
                        continue   # reported: a sha mismatch or a slice the member is told about
                    got = sha
                else:
                    got = stream_sha(dest) if dest.exists() else None
                    if dest.exists() and got != sha:
                        # a stale same-name file (earlier upload, over the cap, or corrupt): never reused — replaced below
                        log.warning("web upload %s: spool file present but does not match the cloud's sha — re-fetching", job_id)
                        dest.unlink(missing_ok=True)
                        got = None
                    if got is None:
                        try:
                            got, _size = getter(path, tmp)
                        finally:
                            if got is None or got != sha:
                                tmp.unlink(missing_ok=True)   # any failure or mismatch: no temp file survives
                        if got != sha:
                            log.error("web upload %s: sha mismatch (cloud %s…, got %s…) — reporting", job_id, sha[:12], got[:12])
                            reporter({"queueJobId": job_id, "sha256": got})   # the cloud fails the job + tells the member
                            continue
                        _os.replace(tmp, dest)
                # Also on retry with an existing file: the prior rename may
                # have succeeded while its directory flush failed. No ACK yet.
                for directory in (dest_dir, base):
                    dfd = _os.open(str(directory), _os.O_RDONLY)
                    try:
                        _os.fsync(dfd)
                    finally:
                        _os.close(dfd)
                items = parse_required_filaments(dest, plate)
                body: dict = {"queueJobId": job_id, "sha256": got, "requiredFilaments": [_vp_submit_filament(i) for i in items]}
                model_id = parse_printer_model_id(dest)
                if model_id:
                    body["printerModelId"] = model_id   # the cloud refuses a file sliced for another printer (audit)
                if sliced is not None:
                    body["slice"] = {k: sliced.get(k) for k in ("grams", "seconds", "supportUsed", "filament", "process", "settings")}
                grams = [i.get("usedG") for i in items if isinstance(i.get("usedG"), (int, float)) and not isinstance(i.get("usedG"), bool)]
                if grams:
                    body["estGrams"] = int(math.ceil(sum(grams)))
                resp = reporter(body)
                ok = getattr(resp, "status", None) == 200 and isinstance(getattr(resp, "body", None), dict) and resp.body.get("ok") is True
                if ok:
                    done += 1
                    log.info("web upload %s fetched into the spool (plate %d, %d filament(s), estGrams=%s)", job_id, plate, len(items), body.get("estGrams"))
                else:
                    log.warning("web upload %s: cloud did not accept the fetched report: %s", job_id, getattr(resp, "body", resp))
            except Exception as exc:  # noqa: BLE001 — a failed fetch must never sink the loop; retried next beat
                tmp.unlink(missing_ok=True)
                log.warning("web upload %s: fetch failed: %s", job_id, exc)
        return done

    def _slice_upload(self, f: dict, spec: dict, uid: str, dest_dir: Path, dest: Path, sha: str, path: str, *, getter,
                      reporter, stream_sha, runner) -> dict | None:
        """v0.72: download the member's mesh into <spool>/<uid>/.slice, slice it there through the runner, and move the plate
        to the job's file name. Returns the slice summary, or None when it reported a sha mismatch or a slice error (the
        cloud fails the job with the slicer's sentence). A plate sliced on an earlier beat whose report did not land is
        reused, not sliced again."""
        import shutil
        job_id = f.get("queueJobId")
        mesh_name = f.get("meshName")
        if not isinstance(mesh_name, str) or not _MESH_NAME_RE.match(mesh_name):
            reporter({"queueJobId": job_id, "sha256": sha, "sliceError": "the file name is not one the slicer accepts"})
            return None
        work = dest_dir / ".slice"
        mesh = work / mesh_name
        result_path = work / "out" / "result.json"
        if dest.exists() and result_path.exists() and mesh.exists() and stream_sha(mesh) == sha:
            try:
                done = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(done, dict) and done.get("ok"):
                    return done
            except (OSError, ValueError):
                pass
        if work.is_symlink():
            work.unlink()
        elif work.exists():
            shutil.rmtree(work)
        dest.unlink(missing_ok=True)
        work.mkdir(mode=0o700)
        tmp = work / f".{mesh_name}.fetch.tmp"
        got = None
        try:
            got, _size = getter(path, tmp)
        finally:
            if got != sha:
                tmp.unlink(missing_ok=True)
        if got != sha:
            log.error("slice upload %s: sha mismatch (cloud %s…, got %s…) — reporting", job_id, sha[:12], str(got)[:12])
            reporter({"queueJobId": job_id, "sha256": got})
            return None
        os.replace(tmp, mesh)
        (work / "job.json").write_text(json.dumps({**slice_spec_of(spec), "mesh": mesh_name}), encoding="utf-8")
        started = time.monotonic()
        try:
            rc = runner(uid)
        except Exception as exc:  # noqa: BLE001 - a runner that cannot start is a slice error the member hears about
            log.warning("slice upload %s: runner failed to start: %s", job_id, exc)
            rc = -1
        result: dict = {}
        try:
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        except (OSError, ValueError):
            result = {}
        SLICER_CAPS.seen(result)   # every slice says what the image that ran it takes (step 2): a rolled-back image shows at once
        sliced_file = work / "out" / "sliced.3mf"
        if not (isinstance(result, dict) and result.get("ok") and sliced_file.exists()):
            reason = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else \
                ("the shop's slicer is not available right now" if rc == -1 else "the slicer stopped before it finished")
            log.warning("slice upload %s: not sliced (rc %s): %s", job_id, rc, reason)
            reporter({"queueJobId": job_id, "sha256": sha, "sliceError": reason[:300]})
            return None
        os.replace(sliced_file, dest)
        log.info("slice upload %s: sliced in %.1f s (%s g, %s s print)", job_id, time.monotonic() - started, result.get("grams"), result.get("seconds"))
        return result

    def dispatch_commands(self, pending_commands) -> list[dict]:
        """Execute cloud-delivered control commands (pause/resume/stop) against
        the target adapters and return result reports for the next heartbeat.
        Mirrors dispatch_assignments: look the adapter up by printerId, run the
        command, map ok/failure to a {requestId, command, status, detail} report
        the cloud's ingestPrinterCommandResults consumes (status 'ok' -> done,
        anything else -> failed with the detail)."""
        # R6.7 defense-in-depth: the cloud caps delivery at 5/heartbeat, so a
        # larger burst means a buggy or compromised control plane. Process the
        # first N and fail the rest as rate_limited so the cloud still closes
        # them out (rather than the agent blindly publishing an unbounded flood
        # of MQTT control messages).
        max_per_heartbeat = 16
        reports: list[dict] = []
        items = pending_commands if isinstance(pending_commands, list) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            request_id = item.get("requestId")
            printer_id = item.get("printerId")
            command = item.get("command")
            if not all(isinstance(v, str) and v for v in (request_id, printer_id, command)):
                # The cloud only ever delivers validated rows, so a malformed
                # item has no real command row to close — log it (not silent) and
                # drop, since we can't emit a valid-command terminal result for it.
                log.warning("skipping malformed command: %s", item)
                continue
            if request_id in self._dispatched_commands:
                # Already executed this requestId in a prior beat — do NOT
                # republish (a duplicate ams_dry mode=1 would restart the dryer).
                # Re-report ok so the cloud can close out a row whose first
                # result report was lost. Doesn't consume a rate-limit slot.
                reports.append({"requestId": request_id, "command": command, "status": "ok"})
                continue
            if command.startswith("laser_") and self.laser.seen(request_id):
                # A laser requestId is attempted once: a redelivery gets no second HA call or store and no second,
                # contradicting result (an accepted load's one result comes from its worker).
                log.warning("ignoring a redelivered laser command %s", request_id)
                continue
            if index >= max_per_heartbeat:
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "rate_limited",
                    }
                )
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else None
            if command.startswith("laser_"):
                # Lasers (v0.68): before the adapter lookup, so a laser row can never reach a printer adapter. A load
                # answers later (None now); its one result rides a later heartbeat via laser.take_command_results().
                report = self.laser.dispatch(request_id, printer_id, command, params)
                if report is not None:
                    if report.get("status") == "ok":
                        self._remember_command(request_id)
                    reports.append(report)
                continue
            adapter = self._adapters.get(printer_id)
            send = getattr(adapter, "send_command", None) if adapter is not None else None
            if not callable(send):
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "printer_unavailable",
                    }
                )
                continue
            try:
                result = send(command, params)
            except Exception as exc:  # noqa: BLE001
                log.warning("command %s for %s raised: %s", command, printer_id, exc)
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": "exception",
                    }
                )
                continue
            if isinstance(result, dict) and result.get("ok"):
                # Record only successful dispatches so a failed one (e.g. a
                # transient printer_unavailable) can still be retried if the cloud
                # ever redelivers; bound it like the queue-job guard.
                self._remember_command(request_id)
                reports.append({"requestId": request_id, "command": command, "status": "ok"})
            else:
                reason = result.get("reason") if isinstance(result, dict) else "unknown"
                reports.append(
                    {
                        "requestId": request_id,
                        "command": command,
                        "status": "failed",
                        "detail": str(reason),
                    }
                )
        return reports

    def dispatch_assignments(self, assignments, spool_dir, *, on_report=None) -> list[dict]:
        """Start cloud-assigned queue jobs and return queue-status reports.

        Reports are ordered for the cloud transition map: assigned -> uploading,
        or uploading -> held after a real send failure. The transition to
        printing is observed later from printer telemetry.
        """
        reports: list[dict] = []
        base = Path(spool_dir)
        reserved: set[str] = set()   # printers that took a start THIS pass (audit 2026-08-30): telemetry lags the start

        def emit(report: dict) -> bool:
            """Every report is handed to `on_report` the moment it exists (the agent writes it to the durable outbox
            there — v0.58 r2: a crash between a start and the end of this pass must not lose 'uploading') and returned.
            Returns False when the hook could not make it durable (the caller then keeps its recoverable state)."""
            reports.append(report)
            if callable(on_report):
                try:
                    on_report(report)
                except Exception as exc:  # noqa: BLE001
                    log.error("report not durable (%s): %s", report.get("state"), exc)
                    return False
            return True
        for assignment in assignments if isinstance(assignments, list) else []:
            if not isinstance(assignment, dict):
                continue
            queue_job_id = assignment.get("queueJobId")
            printer_id = assignment.get("printerId")
            submission_uid = assignment.get("submissionUid")
            file_name = assignment.get("fileName")
            if not all(isinstance(v, str) and v for v in (queue_job_id, printer_id, submission_uid, file_name)):
                log.warning("skipping malformed assignment: %s", assignment)
                continue
            def recovery_uploading(_a=assignment, _q=queue_job_id) -> dict:
                # v0.58 r2: a re-sent assignment for a job this hub ALREADY started answers 'uploading' again (the cloud's
                # sent_at stamp is idempotent) — a crash between the start and the outbox write must not leave the cloud blind
                out = {"queueJobId": _q, "state": "uploading"}
                seq = _a.get("assignmentSeq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    out["assignmentSeq"] = seq
                return out

            seq_asg = assignment.get("assignmentSeq")
            if queue_job_id in self._dispatched_queue_jobs:
                if self._guard_matches(queue_job_id, seq_asg):
                    emit(recovery_uploading())
                    continue
                # the SAME job under a NEW assignment (the cloud re-queued and re-assigned it): the old guard is stale
                log.info("assignment %s re-issued (seq %s → %s) — the earlier dispatch guard is superseded",
                         queue_job_id, self._dispatched_seq.get(queue_job_id), seq_asg)
                self._forget_dispatched_queue_job(queue_job_id)

            def held(reason: str, _a=assignment, _q=queue_job_id) -> dict:
                # v0.56: EVERY refusal names the assignment it answers (assignmentSeq) so the cloud binds it to that revision
                out = {"queueJobId": _q, "state": "held", "reason": reason}
                seq = _a.get("assignmentSeq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    out["assignmentSeq"] = seq
                return out

            if not self._assignment_path_ok(submission_uid, file_name):
                emit(held("bad_assignment"))
                continue

            adapter = self._adapters.get(printer_id)
            start_print = getattr(adapter, "start_print", None) if adapter is not None else None
            if not callable(start_print):
                emit(held("printer_unavailable"))
                continue
            if printer_id in reserved:
                emit(held("printer_busy"))
                continue

            pending_jobs = getattr(adapter, "pending_queue_jobs", None)
            if callable(pending_jobs):
                pending_list = pending_jobs() or []
            else:   # an adapter that only knows ids (seq unknown → treated as this assignment)
                pending_ids = getattr(adapter, "pending_queue_job_ids", None)
                pending_list = [{"queueJobId": j} for j in (pending_ids() or [])] if callable(pending_ids) else []
            mine = [p for p in pending_list if isinstance(p, dict) and p.get("queueJobId") == queue_job_id]
            if mine and (mine[0].get("assignmentSeq") is None or not isinstance(seq_asg, int) or isinstance(seq_asg, bool) or mine[0]["assignmentSeq"] == seq_asg):
                # the adapter's durable dispatch state already holds THIS assignment (started, then the agent died before the
                # manager guard / outbox were written): report it, never 'printer_busy' (codex v0.58 r2)
                self._remember_dispatched_queue_job(queue_job_id, seq_asg if isinstance(seq_asg, int) and not isinstance(seq_asg, bool) else None)
                emit(recovery_uploading())
                continue
            if self._adapter_busy(adapter):
                # v0.53: the printer is mid-job by its OWN report — never upload/start another file onto it, whatever the
                # cloud's view (an 'assigned' re-send after a restart, or a stale idle in the cloud mirror).
                emit(held("printer_busy"))
                continue

            local_path = base / submission_uid / file_name
            if not local_path.is_file():
                emit(held("file_not_found"))
                continue

            # v0.80.1: never tell a printer to start a plate the file does not hold (it takes the command and stops on its
            # screen, X1C 0500-4003, which the hub could only report as start_not_observed two minutes later), and never guess
            # one: an assignment that names no plate (an OrcaSlicer send that did not say) prints a one-plate file's own plate
            # and is refused for a file with several. Any trouble reading the file holds this assignment, never the pass.
            refusal = None
            try:
                named = plate_named(assignment.get("plate"))
                held_plates = gcode_plates(local_path)
                plate_int = named or 1   # not a zip (a Klipper .gcode): its adapter takes no plate
                if held_plates is not None:
                    holds = ", ".join(map(str, held_plates))
                    if not held_plates:
                        refusal = f"plate_missing: the file has no plate {named or 1}; it holds no sliced plate"
                    elif named is None and len(held_plates) > 1:
                        refusal = f"plate_unchosen: no plate was named; the file holds plate {holds}"
                    elif named is not None and named not in held_plates:
                        refusal = f"plate_missing: the file has no plate {named}; it holds plate {holds}"
                    else:
                        plate_int = named or held_plates[0]
                        check_plate_entry(local_path, plate_int)
            except Unreadable3mf as exc:
                refusal = f"plate_unreadable: {exc}"[:200]
            except Exception as exc:  # noqa: BLE001 — a file that breaks the check holds its own assignment only
                log.warning("assignment %s: plate check failed: %s", queue_job_id, exc)
                refusal = "plate_unreadable: the file could not be read"
            if refusal:
                log.warning("assignment %s: %s — not starting it", queue_job_id, refusal)
                emit(held(refusal))
                continue
            # Enumerate the plate's skippable objects from the staged 3MF so the
            # cloud can offer per-object cancel. Best-effort: a parse miss just
            # omits the list (skip simply isn't offered for that job).
            uploading: dict = {"queueJobId": queue_job_id, "state": "uploading"}
            seq0 = assignment.get("assignmentSeq")
            if isinstance(seq0, int) and not isinstance(seq0, bool):
                uploading["assignmentSeq"] = seq0   # v0.58: every assignment-derived report names its assignment
            objects = parse_plate_objects(local_path, plate_int)
            if objects:
                uploading["objects"] = objects
            # The member's own print command (cloud-stored from the capture, agent v0.51+): replayed verbatim by
            # the adapter so dual-nozzle/H2D fields survive. Absent for web uploads → the adapter rebuilds.
            raw_print = assignment.get("rawPrint")
            if not isinstance(raw_print, dict) or not raw_print:
                raw_print = None
            try:
                result = start_print(
                    local_path,
                    file_name,
                    plate=plate_int,
                    use_ams=bool(assignment.get("useAms", False)),
                    ams_mapping=assignment.get("amsMapping"),
                    queue_job_id=queue_job_id,
                    raw_print=raw_print,
                    # B3 (v0.56): the job's required filaments (cloud-normalised) drive virtual→physical tray translation
                    required_filaments=assignment.get("requiredFilaments") if isinstance(assignment.get("requiredFilaments"), list) else None,
                    assignment_seq=seq0 if isinstance(seq0, int) and not isinstance(seq0, bool) else None,   # v0.58: rides on every progress report
                )
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(printer_id)
                safe = self._redact_printer_exception(e, code)
                log.warning("assignment dispatch failed for %s on %s: %s", queue_job_id, printer_id, safe)
                self._record_failure(
                    f"assignment dispatch failed for {queue_job_id} on {printer_id}: {safe}",
                    extra_secrets=[code],
                )
                result = {"ok": False, "reason": "start_failed"}
            if not isinstance(result, dict):
                result = {"ok": False, "reason": "start_failed"}

            if result.get("ok"):
                # v0.56 (codex r2): 'uploading' is reported only AFTER start_print succeeded — a pre-upload refusal
                # (spool_mismatch / printer_stale) must never leave the cloud believing the file went up.
                # v0.58 r2: the report goes to the durable outbox BEFORE the manager guard is persisted (a crash in between
                # then re-dispatches idempotently rather than going silent), and ALWAYS — an idempotent re-send too.
                if emit(uploading):
                    self._remember_dispatched_queue_job(queue_job_id, seq0 if isinstance(seq0, int) and not isinstance(seq0, bool) else None)
                else:
                    # not durable: leave the manager guard UNSET — the adapter's pending_queue_job_ids() recovers the
                    # re-sent assignment with a fresh 'uploading' instead of a silent skip (codex v0.58 r4)
                    log.warning("uploading report for %s not durable — guard left unset for recovery", queue_job_id)
                reserved.add(printer_id)
            else:
                emit(held(result.get("reason", "start_failed")))
        return reports

    def collect_queue_progress(self, on_report=None) -> list[dict]:
        """Drain queue-status updates observed by printer adapters. `on_report` is handed down to adapters that support it
        (v0.58 r3: the durable outbox write happens before the adapter persists the popped dispatch)."""
        reports: list[dict] = []

        def durable(report: dict) -> None:
            # v0.69: the resolved assignment's guard is made durable here, inside the adapter's pre-pop hook, so it is on
            # disk BEFORE the adapter persists the popped dispatch: a second crash between the two can no longer leave
            # neither, and the site's unchanged re-send start the print again (codex review of 708dff1)
            if on_report is not None:
                on_report(report)
            if report.get("state") in ("completed", "held") and isinstance(report.get("queueJobId"), str):
                if not self._remember_dispatched_queue_job(report["queueJobId"], report.get("assignmentSeq")):
                    raise OSError("dispatch guard not durable")   # the adapter keeps its dispatch; replayed next beat

        for pid, adapter in self._adapters.items():
            collect_progress = getattr(adapter, "collect_queue_progress", None)
            if not callable(collect_progress):
                continue
            try:
                try:
                    produced = collect_progress(on_report=durable)
                except TypeError:
                    produced = collect_progress()   # an adapter without the hook (tests / other printer kinds)
                    if callable(on_report):
                        for report in produced:
                            on_report(report)
                for report in produced:
                    reports.append(report)
                    if report.get("state") in ("completed", "held") and isinstance(
                        report.get("queueJobId"), str
                    ):
                        # v0.69: REMEMBER the resolved assignment instead of forgetting it. After a crash before the
                        # 'uploading' report, the site still holds the job 'assigned' and this very beat's response re-sends
                        # it; with the dispatch just resolved and no guard, it was started a second time (codex review of
                        # a599650; present at v0.68). The guard is keyed by job AND assignmentSeq, so a re-queued job (a new
                        # seq) still dispatches; a stale re-send is answered 'uploading', which the site refuses as stale.
                        self._remember_dispatched_queue_job(report["queueJobId"], report.get("assignmentSeq"))
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("collect_queue_progress failed for %s: %s", pid, safe)
                self._record_failure(f"collect_queue_progress failed for {pid}: {safe}", extra_secrets=[code])
        return reports

    def stop_all(self) -> None:
        self._bambu_configs.clear()  # A later status read must never restart a stopped manager.
        for pid in list(self._adapters):
            self._stop(pid)
        self._stuck_retry_at.clear()
        self._camera_meta.clear()
        self._static.clear()
