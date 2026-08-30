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
from .threemf_objects import parse_plate_objects

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
# A paho client that got a CONNACK auth-rejection (or never completed its first
# connect) does NOT reliably auto-retry — so an adapter built while a printer was
# unreachable / mis-coded stays dead even after the printer RECOVERS (operator
# resets LAN mode, fixes the access code, powers it back on). The manager tears
# down + rebuilds such a wedged adapter at most this often so it SELF-HEALS with
# no agent restart. Bounded to avoid thrashing a genuinely-dead printer.
STUCK_ADAPTER_RETRY_S = 120.0
_SUBMISSION_UID_RE = re.compile(r"^[a-f0-9]{8,64}$")


def plate_of(value) -> int:
    """The plate a cloud message names: a positive int (a digit string is coerced), 1..64; anything else = plate 1 — the
    SAME rule for a fetch (what we parse) and an assignment (what we print), so the two can never disagree (codex v0.57)."""
    if isinstance(value, bool):
        return 1
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and 1 <= value <= 64:
        return value
    return 1


def _fingerprint(p: dict) -> tuple:
    """What, if changed, means we must rebuild the adapter (new connection)."""
    return (p.get("vendor"), p.get("host"), p.get("serial"), p.get("accessCode"))


class PrinterManager:
    def __init__(self, diagnostics=None) -> None:
        self._adapters: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple] = {}
        # Earliest monotonic time to rebuild a wedged adapter (see
        # STUCK_ADAPTER_RETRY_S). Set on every (re)build; checked when the
        # fingerprint is unchanged so a recovered printer self-heals.
        self._stuck_retry_at: dict[str, float] = {}
        self._diagnostics = diagnostics or get_default()
        # Static status for printers the agent can't drive yet (klipper, or a
        # Bambu missing its connection facts) — surfaced so the admin sees why.
        self._static: dict[str, dict] = {}
        # Unacked terminal jobs rescued from torn-down adapters (config change
        # rebuilds the adapter; its in-memory buffer must NOT die with it —
        # Codex review finding). Drained by pending_jobs / cleared by ack_jobs.
        self._orphan_jobs: list[dict] = []
        # Queue assignments are at-least-once from the cloud. Once this process
        # has successfully sent a start command for a queueJobId, a same-window
        # resend must not re-upload or re-start the printer. Terminal progress
        # reports prune this bounded guard.
        self._dispatched_queue_jobs: OrderedDict[str, float] = OrderedDict()
        self._dispatched_wall: dict[str, float] = {}   # queueJobId -> wall-clock stamp, the persisted twin of the above
        self._load_dispatched()
        # Same at-least-once guard for control commands. Cloud delivery is
        # one-shot (queued->delivered is atomic; delivered rows are never
        # re-selected), but a lost result report could in theory get the row
        # redelivered — a second ams_dry mode=1 would restart the heater cycle.
        # A successfully-dispatched requestId here makes a redelivery re-report
        # ok without republishing. Bounded like _dispatched_queue_jobs.
        self._dispatched_commands: OrderedDict[str, float] = OrderedDict()
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
        return getattr(adapter, "_access_code", None)

    def _redact_printer_exception(self, exc, code=None) -> str:
        return redact(str(exc), extra_secrets=[code] if code else None)

    def reconcile(self, printers: list[dict], version: str | None) -> None:
        self.config_version = version
        desired_ids = {p["id"] for p in printers if isinstance(p.get("id"), str)}

        # Rebuild camera-routing meta from the full desired list (no stale rows).
        self._camera_meta = {
            p["id"]: {
                "printerId": p["id"],
                "vendor": p.get("vendor"),
                "model": p.get("model"),
                "host": p.get("host"),
                "accessCode": p.get("accessCode"),
                "moonrakerUrl": p.get("moonrakerUrl"),
                "cameraSnapshotUrl": p.get("cameraSnapshotUrl"),
                # Strict True only — a malformed/older payload sending a string
                # like "false" must NOT enable capture (preserve default-off).
                "cameraEnabled": p.get("cameraEnabled") is True,
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
        # Klipper fingerprint = (vendor, moonrakerUrl) — same shape contract
        # as Bambu's (vendor, host, serial, accessCode) so reconcile() can
        # treat them uniformly. Different from _fingerprint() so we compute
        # it inline here.
        fp = ("klipper", moonraker_url)
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
        adapter = KlipperAdapter(pid, moonraker_url=moonraker_url)
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
            self._stop(pid)
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

    def _stuck_needs_retry(self, pid: str) -> bool:
        """True when the existing adapter is wedged in a hard 'error' state paho
        won't recover from on its own (CONNACK auth-rejection or a connect that
        never completed) AND we're past the rebuild backoff. A healthy or merely
        'offline' (transient, paho-recoverable) adapter returns False; a missing
        adapter returns True (build it)."""
        adapter = self._adapters.get(pid)
        if adapter is None:
            return True
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
            # Rescue unacked terminal jobs BEFORE teardown — a config edit
            # (e.g. rotated access code) rebuilds the adapter and its buffer
            # would otherwise vanish with it.
            try:
                rescued = adapter.pending_jobs()
                if rescued:
                    self._orphan_jobs.extend(rescued)
                    log.info("rescued %d unacked job(s) from %s before teardown", len(rescued), pid)
            except Exception as e:  # noqa: BLE001
                safe = self._redact_printer_exception(e, code)
                log.warning("could not rescue pending jobs from %s: %s", pid, safe)
                self._record_failure(f"could not rescue pending jobs from {pid}: {safe}", extra_secrets=[code])
            adapter.stop()

    def statuses(self) -> list[dict]:
        out: list[dict] = []
        for pid, adapter in self._adapters.items():
            try:
                out.append(adapter.status())
            except Exception as e:  # noqa: BLE001 — one bad adapter must not sink the heartbeat
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("status read failed for %s: %s", pid, safe)
                self._record_failure(f"status read failed for {pid}: {safe}", extra_secrets=[code])
                out.append({"printerId": pid, "connectionState": "error", "errorReason": "agent_status_error"})
        out.extend(self._static.values())
        return out

    def camera_targets(self) -> list[dict]:
        """Per-printer camera-routing facts (vendor/model/host/accessCode/urls)
        for the camera capturer. One entry per configured printer; the capturer
        decides which have a usable camera source and which don't."""
        return list(self._camera_meta.values())

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs across all adapters + any rescued from
        torn-down adapters. Safe to send repeatedly — the cloud dedupes on
        jobKey."""
        out: list[dict] = list(self._orphan_jobs)
        for pid, adapter in self._adapters.items():
            try:
                out.extend(adapter.pending_jobs())
            except Exception as e:  # noqa: BLE001 — one adapter can't sink the loop
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("pending_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"pending_jobs failed for {pid}: {safe}", extra_secrets=[code])
        return out

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Fan a confirmed-send ack out to every adapter + the orphan buffer."""
        if not job_keys:
            return
        keys = set(job_keys)
        self._orphan_jobs = [j for j in self._orphan_jobs if j["jobKey"] not in keys]
        for pid, adapter in self._adapters.items():
            try:
                adapter.ack_jobs(job_keys)
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("ack_jobs failed for %s: %s", pid, safe)
                self._record_failure(f"ack_jobs failed for {pid}: {safe}", extra_secrets=[code])

    def _remember_dispatched_queue_job(self, queue_job_id: str) -> None:
        self._dispatched_queue_jobs[queue_job_id] = time.monotonic()
        self._dispatched_queue_jobs.move_to_end(queue_job_id)
        self._dispatched_wall[queue_job_id] = time.time()
        while len(self._dispatched_queue_jobs) > MAX_DISPATCHED_QUEUE_JOBS:
            evicted, _ = self._dispatched_queue_jobs.popitem(last=False)
            self._dispatched_wall.pop(evicted, None)
        self._save_dispatched()

    def _forget_dispatched_queue_job(self, queue_job_id: str) -> None:
        self._dispatched_queue_jobs.pop(queue_job_id, None)
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
        for job_id, stamp in raw.items():
            if not (isinstance(job_id, str) and isinstance(stamp, (int, float))):
                continue
            # A stamp from the "future" = the wall clock stepped BACKWARDS across the restart (a Pi before time sync,
            # codex v0.53): keep the entry — dropping it reopens the duplicate-dispatch window this guard exists for.
            age = max(0.0, now_wall - float(stamp))
            if age < DISPATCHED_TTL_SEC:
                self._dispatched_queue_jobs[job_id] = now_mono
                self._dispatched_wall[job_id] = float(stamp)

    def _save_dispatched(self) -> None:
        try:
            DISPATCHED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = DISPATCHED_STATE_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._dispatched_wall))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, DISPATCHED_STATE_PATH)
            try:
                fd = os.open(str(DISPATCHED_STATE_PATH.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        except OSError as e:
            log.warning("could not persist the dispatched-job guard: %s", e)

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
                      max_bytes: int = 64 * 1024 * 1024) -> int:
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
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
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
                    try:   # the rename itself must be on disk before we tell the cloud the file is ours to print (codex r2)
                        dfd = _os.open(str(dest_dir), _os.O_RDONLY)
                        try:
                            _os.fsync(dfd)
                        finally:
                            _os.close(dfd)
                    except OSError as exc:
                        log.warning("web upload %s: directory fsync failed (%s) — continuing", job_id, exc)
                items = parse_required_filaments(dest, plate)
                body: dict = {"queueJobId": job_id, "sha256": got, "requiredFilaments": [_vp_submit_filament(i) for i in items]}
                model_id = parse_printer_model_id(dest)
                if model_id:
                    body["printerModelId"] = model_id   # the cloud refuses a file sliced for another printer (audit)
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
                self._dispatched_commands[request_id] = time.monotonic()
                self._dispatched_commands.move_to_end(request_id)
                while len(self._dispatched_commands) > MAX_DISPATCHED_COMMANDS:
                    self._dispatched_commands.popitem(last=False)
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

            if queue_job_id in self._dispatched_queue_jobs:
                emit(recovery_uploading())
                continue

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

            pending_ids = getattr(adapter, "pending_queue_job_ids", None)
            if callable(pending_ids) and queue_job_id in (pending_ids() or []):
                # the adapter's durable dispatch state already holds THIS job (started, then the agent died before the
                # manager guard / outbox were written): report it, never 'printer_busy' (codex v0.58 r2)
                self._remember_dispatched_queue_job(queue_job_id)
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

            plate_int = plate_of(assignment.get("plate"))   # v0.57: the same coercion the fetch used
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
                    self._remember_dispatched_queue_job(queue_job_id)
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
        for pid, adapter in self._adapters.items():
            collect_progress = getattr(adapter, "collect_queue_progress", None)
            if not callable(collect_progress):
                continue
            try:
                try:
                    produced = collect_progress(on_report=on_report) if on_report is not None else collect_progress()
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
                        self._forget_dispatched_queue_job(report["queueJobId"])
            except Exception as e:  # noqa: BLE001
                code = self._access_code_for(pid)
                safe = self._redact_printer_exception(e, code)
                log.warning("collect_queue_progress failed for %s: %s", pid, safe)
                self._record_failure(f"collect_queue_progress failed for {pid}: {safe}", extra_secrets=[code])
        return reports

    def stop_all(self) -> None:
        for pid in list(self._adapters):
            self._stop(pid)
