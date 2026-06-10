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
import ssl
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from . import bambu_parse
from .jobs import JobTracker

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
        self._access_code = access_code  # secret — never logged
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
        self._jobs = JobTracker(printer_id, serial)

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
            self.printer_id, data, connection_state=conn_state, error_reason=reason
        )

    def pending_jobs(self) -> list[dict]:
        """Unacked terminal jobs (re-send-safe — the cloud dedupes on jobKey)."""
        with self._lock:
            return self._jobs.pending()

    def ack_jobs(self, job_keys: list[str]) -> None:
        """Drop jobs after a confirmed heartbeat 200."""
        with self._lock:
            self._jobs.ack(job_keys)
