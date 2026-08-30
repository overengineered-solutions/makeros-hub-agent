"""Bambu LAN/Developer-mode MQTT adapter — the thin paho I/O around the pure
parser in bambu_parse.

Connection (verified against ha-bambulab/pybambu + bambulabs_api):
  host       = printer LAN IP        port = 8883 (TLS, self-signed — DO NOT verify)
  username   = "bblp" (literal, every Bambu)   password = the 8-char LAN access code
  protocol   = MQTT v3.1.1            subscribe device/<serial>/report  (QoS 0)
  on connect: publish {"pushing":{"command":"pushall"}} to device/<serial>/request
              to force a full snapshot, then live off the printer's deltas.

ONE long-lived connection per printer (A1 Mini/P1 only reliably support a single
local MQTT client — a second subscriber knocks us offline). paho's loop_start +
reconnect_delay_set gives us the self-healing reconnect; we re-subscribe and
re-pushall in on_connect every time.

Secrets: the access code is only ever the MQTT password held in process memory.
It is NEVER logged and NEVER put on the heartbeat wire (the status DTO carries
telemetry only).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import os
import ssl
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from . import bambu_parse, bambu_send, tray_translate
from .jobs import JobTracker
from .queue_progress import QueueProgressTracker

QUEUE_PROGRESS_DIR = Path(os.environ.get("MAKEROS_HUB_QUEUE_PROGRESS_DIR", "/var/lib/makeros-hub/queue-progress"))
TERMINAL_JOBS_DIR = Path(os.environ.get("MAKEROS_HUB_TERMINAL_JOBS_DIR", "/var/lib/makeros-hub/terminal-jobs"))


def _fsync_dir(path: Path) -> bool:
    """fsync the directory so an os.replace survives a power cut. Returns False when it could NOT be made durable —
    the pre-publish dispatch save treats that as "not persisted" (codex v0.54 round 3)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _safe_file_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))[:80] or "printer"

log = logging.getLogger("makeros-hub.bambu")

# If we never even reach the broker within this window, call it unreachable.
CONNECT_TIMEOUT_SEC = 20
# If we were connected but reports stop for this long, the printer went away.
STALE_SEC = 150
PUSHALL = json.dumps({"pushing": {"command": "pushall"}})
GET_VERSION = json.dumps({"info": {"command": "get_version"}})


def _classify_connect_failure(reason_code: Any) -> str:
    s = str(reason_code).lower()
    if "not authorized" in s or "bad user" in s or "password" in s or "credential" in s:
        return "mqtt_auth_failed"
    return "connect_refused"


class BambuAdapter:
    """Owns one printer's MQTT connection + merged state. Thread-safe reads via
    `status()`; the paho network loop runs in its own thread."""

    def __init__(self, printer_id: str, host: str, serial: str, access_code: str, model: str | None = None):
        self.printer_id = printer_id
        self.host = host
        self.serial = serial
        # STRIP whitespace: a trailing space/newline (paste artifact when the
        # operator re-enters the code) makes MQTT auth fail with the SAME visible
        # code that Bambu Studio's clean entry accepts — the Antoni Gaudi
        # 2026-06-20 mqtt_auth_failed that survived reboots + a fresh restart.
        self._access_code = (access_code or "").strip()  # secret — never logged
        self.model = model
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._connack: str | None = None  # None | 'ok' | 'fail'
        self._error_reason: str | None = None
        self._last_report_at: float | None = None
        self._started = 0.0
        self._shape_logged = False
        self._client: mqtt.Client | None = None
        # Terminal-job detection over the merged state (pure; fed under _lock).
        self._jobs = JobTracker(printer_id, serial, state_path=TERMINAL_JOBS_DIR / f"{_safe_file_component(printer_id)}.json")
        # Queue assignment state is driven by OBSERVED telemetry, not by
        # MQTT-publish success. The tracker reports "printing" only after
        # RUNNING/PAUSE appears and links completion to the JobTracker's real
        # terminal printer job key.
        self._queue_progress = QueueProgressTracker()
        # v0.54 durability: the queue↔print correlation survives an update/power cut/restart (owner rule: not RAM-only)
        self._queue_progress_path = QUEUE_PROGRESS_DIR / f"{_safe_file_component(printer_id)}.json"
        self._queue_progress_saved = ""
        self._load_queue_progress()

    @property
    def _report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def _request_topic(self) -> str:
        return f"device/{self.serial}/request"

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        # Self-signed cert on a trusted LAN — unauthenticated-server TLS.
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.username_pw_set("bblp", self._access_code)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._client = client
        self._started = time.monotonic()
        # connect_async + loop_start: non-blocking, auto-reconnecting.
        client.connect_async(self.host, 8883, keepalive=60)
        client.loop_start()
        log.info("bambu adapter %s connecting to %s", self.printer_id, self.host)

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._client = None

    # --- paho callbacks (run on the network thread) -----------------------
    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        if getattr(reason_code, "is_failure", False) or (
            isinstance(reason_code, int) and reason_code != 0
        ):
            with self._lock:
                self._connack = "fail"
                self._error_reason = _classify_connect_failure(reason_code)
            log.warning("bambu %s connect failed: %s", self.printer_id, self._error_reason)
            return
        with self._lock:
            self._connack = "ok"
            self._error_reason = None
        client.subscribe(self._report_topic, qos=0)
        client.publish(self._request_topic, PUSHALL)
        client.publish(self._request_topic, GET_VERSION)
        log.info("bambu %s connected; subscribed + pushall sent", self.printer_id)

    def _on_message(self, _client, _userdata, msg):
        try:
            doc = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("bambu %s: non-JSON report frame dropped", self.printer_id)
            return
        if not isinstance(doc, dict):
            return
        with self._lock:
            bambu_parse.merge_report(self._data, doc)
            self._last_report_at = time.monotonic()
            # Job lifecycle detection (wall-clock — startedAt/endedAt are real
            # timestamps on the wire, unlike the monotonic staleness clock).
            self._jobs.observe(self._data, time.time())
            if not self._shape_logged:
                self._shape_logged = True
                # First-parse shape observability (redacted) — see CLAUDE doctrine.
                log.info(
                    "bambu.shape_observed %s %s",
                    self.printer_id,
                    json.dumps(bambu_parse.summarize_shape(self._data)),
                )

    def _on_disconnect(self, *_args, **_kwargs):
        # *args: paho's on_disconnect arity shifted across 2.x (disconnect_flags
        # was added) — stay signature-agnostic since we only log here. paho
        # auto-reconnects; status() degrades to offline if reports go stale.
        log.info("bambu %s disconnected — will reconnect", self.printer_id)

    # --- status read (any thread) -----------------------------------------
    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            connack = self._connack
            err = self._error_reason
            last = self._last_report_at
            started = self._started
            data = self._data

        if connack == "fail":
            conn_state, reason = "error", err
        elif last is not None:
            conn_state = "connected" if (now - last) <= STALE_SEC else "offline"
            reason = None
        elif connack == "ok":
            conn_state, reason = "connecting", None
        elif now - started > CONNECT_TIMEOUT_SEC:
            conn_state, reason = "error", "unreachable"
        else:
            conn_state, reason = "connecting", None

        return bambu_parse.normalize_status(
            self.printer_id, data, connection_state=conn_state, error_reason=reason, model=self.model
        )

    def pending_queue_job_ids(self) -> list[str]:
        """Queue jobs dispatched here whose outcome is not yet known (v0.58: the manager asks before its busy check)."""
        with self._lock:
            return self._queue_progress.pending_queue_job_ids()

    def pending_queue_jobs(self) -> list[dict]:
        with self._lock:
            return self._queue_progress.pending_queue_jobs()

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs (re-send-safe — the cloud dedupes on jobKey)."""
        with self._lock:
            return self._jobs.pending()

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Drop jobs after a confirmed heartbeat 200."""
        with self._lock:
            self._jobs.ack(job_keys)

    def start_print(
        self,
        local_path,
        file_name: str,
        *,
        plate: int = 1,
        use_ams: bool = False,
        ams_mapping=None,
        queue_job_id: str | None = None,
        raw_print: dict | None = None,
        required_filaments: list | None = None,
        assignment_seq: int | None = None,
    ) -> dict:
        client = self._client
        connected = False
        if client is not None:
            is_connected = getattr(client, "is_connected", None)
            try:
                connected = bool(is_connected()) if callable(is_connected) else self._connack == "ok"
            except Exception:  # noqa: BLE001
                connected = self._connack == "ok"
        if client is None or not connected:
            return {"ok": False, "reason": "not_connected"}
        if queue_job_id:
            with self._lock:
                pending = self._queue_progress.pending_queue_jobs()
            mine = [p for p in pending if p["queueJobId"] == queue_job_id]
            if mine:
                old_seq = mine[0].get("assignmentSeq")
                if old_seq is None or assignment_seq is None or old_seq == assignment_seq:
                    # the durable dispatch state already holds THIS assignment (a re-send after a restart / a lost report):
                    # idempotent success — nothing is uploaded or published twice (audit 2026-08-30)
                    log.info("bambu %s: %s already dispatched here — not starting it again", self.printer_id, queue_job_id)
                    return {"ok": True, "already_dispatched": True}
                # a NEW assignment of the same job (the cloud re-queued it and assigned again): the stale dispatch record is
                # superseded (codex v0.58 r5); the live busy guard above/around still protects a print that is running
                log.warning("bambu %s: %s re-assigned (seq %s → %s) — superseding the stale dispatch record",
                            self.printer_id, queue_job_id, old_seq, assignment_seq)
                with self._lock:
                    self._queue_progress.discard_dispatch(queue_job_id)
                    self._save_queue_progress()
                pending = [p for p in pending if p["queueJobId"] != queue_job_id]
            if pending:
                # another job's outcome on this printer is still unknown: never stack a second start on it
                return {"ok": False, "reason": "printer_busy"}

        sequence_id = os.urandom(4).hex()
        payload = bambu_send.build_print_start_payload(
            file_name,
            plate=plate,
            use_ams=use_ams,
            ams_mapping=ams_mapping,
            sequence_id=sequence_id,
            raw_print=raw_print,
        )
        # B3 (v0.56): the member's ams_mapping names VIRTUAL pool positions; rewrite it to THIS printer's physical
        # trays from its live AMS state, or refuse before anything is uploaded (a wrong tray = the wrong material).
        # Only when the cloud sent the job's requirements (older clouds / web uploads without them keep the replay).
        if payload["print"].get("use_ams") is True or isinstance(required_filaments, list):
            st = self.status()
            if st.get("connectionState") != "connected":
                # the AMS mirror rides only on a FRESH report (bambu_parse.normalize_status); stale trays are no basis.
                # Its own reason: the cloud re-queues WITHOUT counting it as a spool refusal (codex v0.56 r1).
                log.warning("bambu %s: refusing %s — no fresh printer report to translate trays from", self.printer_id, queue_job_id)
                return {"ok": False, "reason": "printer_stale: printer has not reported its trays recently"}
            try:
                before = (payload["print"].get("ams_mapping"), payload["print"].get("ams_mapping2"))
                payload["print"] = tray_translate.translate_print_trays(
                    payload["print"], required_filaments if isinstance(required_filaments, list) else None, st.get("ams"), st.get("vtTray")
                )
            except tray_translate.TrayTranslationError as exc:
                log.warning("bambu %s: refusing %s — %s", self.printer_id, queue_job_id, exc)
                return {"ok": False, "reason": f"{exc.reason}: {exc.detail}"[:200]}
            log.info("bambu %s: trays for %s — virtual %s → physical %s / %s", self.printer_id, queue_job_id,
                     before, payload["print"].get("ams_mapping"), payload["print"].get("ams_mapping2"))

        try:
            bambu_send.upload_3mf(self.host, self._access_code, local_path, file_name)
        except bambu_send.BambuSendError as exc:
            log.warning("bambu %s upload failed: %s", self.printer_id, exc)
            return {"ok": False, "reason": "upload_failed"}
        # v0.54: record + persist the dispatch BEFORE the command leaves the box (codex #4) — a crash in between can only
        # leave a dispatch that never started (it times out as start_not_observed), never a running print the cloud
        # can't correlate. A failed publish discards it again.
        task_name = payload["print"].get("subtask_name")
        if queue_job_id:
            with self._lock:
                self._queue_progress.record_dispatch(
                    queue_job_id, self._jobs.pending(), task_name=task_name, active_key=self._jobs.active_key(),
                    assignment_seq=assignment_seq,
                )
                if not self._save_queue_progress():
                    # Durable state is the invariant (codex v0.54 r2): a print we cannot track across a restart is a
                    # print we do not start. The cloud re-sends the assignment; a human sees 'state_persist_failed'.
                    self._queue_progress.discard_dispatch(queue_job_id)
                    log.error("bambu %s: refusing to start %s — dispatch state could not be persisted", self.printer_id, queue_job_id)
                    return {"ok": False, "reason": "state_persist_failed"}
        try:
            info = client.publish(self._request_topic, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.warning("bambu %s print-start publish failed: %s", self.printer_id, exc)
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        if getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) != mqtt.MQTT_ERR_SUCCESS:
            log.warning(
                "bambu %s print-start publish returned rc=%s",
                self.printer_id,
                getattr(info, "rc", "unknown"),
            )
            self._discard_dispatch(queue_job_id)
            return {"ok": False, "reason": "start_command_failed"}
        return {"ok": True}

    def _discard_dispatch(self, queue_job_id: str | None) -> None:
        if not queue_job_id:
            return
        with self._lock:
            self._queue_progress.discard_dispatch(queue_job_id)
            self._save_queue_progress()

    def send_command(self, command: str, params: dict | None = None) -> dict:
        """Publish a LAN control command to device/<serial>/request — the same
        channel as start_print. pause/resume/stop (universal `print`-class
        commands; pybambu's proven shape) + ams_dry (`ams_filament_drying`;
        params {amsId, temp, durationHours}) + skip_objects (cancel specific
        objects mid-print; params {objList: [identify_id ints]}). The cloud only
        delivers the allowlisted set + validates params, but we re-check here as
        defense-in-depth. Returns {"ok": bool, "reason": str}."""
        if command not in {"pause", "resume", "stop", "ams_dry", "skip_objects"}:
            return {"ok": False, "reason": "unsupported_command"}
        client = self._client
        connected = False
        if client is not None:
            is_connected = getattr(client, "is_connected", None)
            try:
                connected = bool(is_connected()) if callable(is_connected) else self._connack == "ok"
            except Exception:  # noqa: BLE001
                connected = self._connack == "ok"
        if client is None or not connected:
            return {"ok": False, "reason": "not_connected"}

        sequence_id = os.urandom(4).hex()
        if command == "ams_dry":
            p = params or {}
            ams_id, temp, duration = p.get("amsId"), p.get("temp"), p.get("durationHours")
            # The cloud (AmsDryParamsDTO) is the range SSOT; here we only assert
            # basic shape as defense-in-depth. `bool` is a subclass of `int`, so
            # exclude it explicitly (amsId=True would otherwise become 1), and
            # require sane positives without re-encoding the cloud's tight bounds
            # (avoids the two ends drifting apart).
            def _num(x: object) -> bool:
                return isinstance(x, (int, float)) and not isinstance(x, bool)

            if not (
                isinstance(ams_id, int)
                and not isinstance(ams_id, bool)
                and ams_id >= 0
                and _num(temp)
                and temp > 0
                and _num(duration)
                and duration > 0
            ):
                return {"ok": False, "reason": "invalid_dry_params"}
            # ams_filament_drying — field set verified verbatim against the
            # BambuStudio client (DevFilaSystemCtrl.cpp, the printer maker's own
            # code) plus ha-bambulab #1448 and ~10 community implementations.
            # mode 1 = OnTime (timed dry); duration in HOURS; temp in °C with a
            # HARD >=45 floor (below is silently dropped — the cloud's
            # AmsDryParamsDTO enforces 45-65). cooling_temp is the POST-dry
            # cool-down target, NOT a floor: the source-of-truth client sends 0,
            # so we mirror that (the "cooling_temp must be >=45" lore conflated it
            # with temp). humidity matters only for mode 2; rotate_tray / filament
            # / close_power_conflict are the real optional fields the official
            # client always includes (filament "" = let the printer infer).
            payload = {
                "print": {
                    "sequence_id": sequence_id,
                    "command": "ams_filament_drying",
                    "ams_id": int(ams_id),
                    "mode": 1,
                    "temp": int(temp),
                    "cooling_temp": 0,
                    "duration": int(duration),
                    "humidity": 0,
                    "rotate_tray": False,
                    "filament": "",
                    "close_power_conflict": False,
                }
            }
        elif command == "skip_objects":
            obj_list = (params or {}).get("objList")
            # obj_list = the slicer's identify_id ints (verified vs BambuStudio
            # command_task_partskip: `obj_list` is a plain int array). Exclude
            # bools (bool ⊂ int). Bambu disables skip past 64 objects; the cloud
            # validates the tighter bounds, this is the defense-in-depth floor.
            if (
                not isinstance(obj_list, list)
                or not obj_list
                or len(obj_list) > 64
                or any(not isinstance(o, int) or isinstance(o, bool) or o < 0 for o in obj_list)
            ):
                return {"ok": False, "reason": "invalid_skip_params"}
            payload = {
                "print": {
                    "sequence_id": sequence_id,
                    "command": "skip_objects",
                    # Dedup, preserve order — the printer treats s_obj as a set,
                    # so a duplicate id is a harmless no-op we don't need to send.
                    "obj_list": list(dict.fromkeys(int(o) for o in obj_list)),
                }
            }
        else:
            payload = {"print": {"sequence_id": sequence_id, "command": command, "param": ""}}
        try:
            info = client.publish(self._request_topic, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.warning("bambu %s %s publish failed: %s", self.printer_id, command, exc)
            return {"ok": False, "reason": "command_failed"}
        if getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) != mqtt.MQTT_ERR_SUCCESS:
            log.warning(
                "bambu %s %s publish returned rc=%s",
                self.printer_id,
                command,
                getattr(info, "rc", "unknown"),
            )
            return {"ok": False, "reason": "command_failed"}
        return {"ok": True}

    def collect_queue_progress(self, on_report=None) -> list[dict]:
        """Drain queue-status reports inferred from observed printer telemetry. `on_report` (v0.58 r3) receives each report
        BEFORE the popped dispatch state is persisted: the agent writes it to the durable outbox there, so a crash between
        the two can only replay a report (the cloud is idempotent), never lose 'completed' while the dispatch is gone."""
        with self._lock:
            print_obj = self._data.get("print") if isinstance(self._data.get("print"), dict) else {}
            # None until the first report after (re)start: silence is not idle (codex v0.54 #1)
            observed = self._last_report_at is not None
            before = self._queue_progress.to_state()
            reports = self._queue_progress.collect(
                self._jobs.pending(),
                print_obj.get("gcode_state") if observed else None,
            )
            if callable(on_report):
                for i, report in enumerate(reports):
                    try:
                        on_report(report)
                    except Exception as exc:  # noqa: BLE001
                        # the report could NOT be made durable (codex v0.58 r4): put the tracker back the way it was, keep the
                        # on-disk state untouched, and hand out nothing — the same reports are regenerated next beat
                        log.error("progress report not durable (%s) — keeping the dispatch state, retrying next beat", exc)
                        self._queue_progress.load_state(before, now=time.monotonic(), now_wall=time.time())
                        return []
            self._save_queue_progress()
            return reports

    def _load_queue_progress(self) -> None:
        try:
            self._queue_progress.load_state(json.loads(self._queue_progress_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return

    def _save_queue_progress(self) -> bool:
        """Write only when the state changed (SD-card friendly); atomic + fsync. Returns False when the state could NOT be
        made durable — callers on the dispatch path refuse to proceed; the collect path logs and carries on."""
        try:
            encoded = json.dumps(self._queue_progress.to_state(), sort_keys=True)
            if encoded == self._queue_progress_saved:
                return True
            self._queue_progress_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_progress_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())          # power-cut safe (codex v0.54 #4)
            os.replace(tmp, self._queue_progress_path)
            if not _fsync_dir(self._queue_progress_path.parent):
                log.error("bambu %s: queue progress written but the directory could not be fsynced — not durable", self.printer_id)
                return False
            self._queue_progress_saved = encoded
            return True
        except OSError as exc:
            log.error("bambu %s: could not persist queue progress: %s", self.printer_id, exc)
            return False
