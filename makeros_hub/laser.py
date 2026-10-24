"""Lasers (v0.68.0): plug telemetry, cutting runs, the gate's status and file checks, and the two laser verbs.

What this module can do, and nothing else:
  - take the plug readings Home Assistant pushes to POST /ha/laser (its own listener; the hub holds no HA token);
  - derive each laser's state (off / on_idle / cutting / unknown) and its cutting runs, for the heartbeat;
  - poll the gate's GET /status, and have the gate POST /check member files the cloud offers;
  - run laser_power_on (POST to the HA webhook; HA checks the exhaust, the fans and the person sensor, then runs
    switch.turn_on) and laser_load (fetch a checked file from the cloud, POST it to the gate's /store).
There is no off verb here, and no path that sends bytes to the laser except the gate's /store.

Secrets are files the operator mints, each 0600 and owned by makeros-hub. A missing, empty or group/world-readable
file means "not configured": never a default, never a crash. Values are never logged; the webhook URL (its id is the
secret) is never logged either.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .http import _OPENER
from .printers.queue_progress import write_json_durably

log = logging.getLogger("makeros-hub.laser")

# Secret files (contract section 1). Read on every use, so a file the operator mints later takes effect without a restart.
SECRETS_DIR = Path(os.environ.get("MAKEROS_HUB_LASER_SECRETS_DIR", "/var/lib/makeros-hub"))
GATE_TOKEN_FILE = "laser-gate-token"
WEBHOOK_URL_FILE = "ha-laser-webhook-url"
PUSH_TOKEN_FILE = "ha-laser-push-token"
# Open and unacked closed runs per laser, so a restart loses none (owner rule: nothing that matters lives only in RAM).
RUNS_STATE_PATH = Path(os.environ.get("MAKEROS_HUB_LASER_RUNS", "/var/lib/makeros-hub/laser-runs.json"))

LASER_COMMANDS = ("laser_power_on", "laser_load")
HA_STALE_S = 75            # haLink goes down this long after the last push (HA pushes every 30 s)
MAX_LASERS = 8
MAX_RUNS = 32
MAX_FILE_CHECKS = 16
MAX_CHECK_OFFERS = 3
MAX_PUSH_BYTES = 4096
LASER_FILE_MAX_BYTES = 4 * 1024 * 1024
LASER_FILE_PATH = "/api/print/hub/laser-file/"
STORE_TIMEOUT_S = 120
CHECK_TIMEOUT_S = 60       # a worst-case 4 MiB file takes the gate ~10 s on a desktop, more on a Pi
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Z]{2}[0-9]{4}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?(\.\d+)?(Z|[+-]\d\d:?\d\d)$")
_SWITCH_STATES = ("on", "off", "unavailable", "unknown")
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


# --- config ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LaserConfig:
    ha_push_allowed_ip: str
    ha_push_listen: tuple = ("0.0.0.0", 8788)
    gate_url: str | None = None
    gate_laser: str | None = None   # the slug the gate serves; unset = the only laser in config-down
    cutting_amps: float = 2.0
    on_amps: float = 0.5
    run_gap_s: float = 60.0


def _ipv4(value, name) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise ValueError(f"[laser] {name} must be an IPv4 address") from None


def _number(raw, name, default) -> float:
    v = raw.get(name, default)
    try:
        v = float(v) if type(v) in (int, float) else math.nan
    except OverflowError:   # a 400-digit TOML integer
        v = math.nan
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"[laser] {name} must be a positive number")
    return v


def parse_laser_config(raw) -> LaserConfig | None:
    """The optional [laser] table -> LaserConfig, None when absent. Raises ValueError (the caller logs it and runs with
    lasers not configured) on an unknown key or a bad value, so a typo never half-configures a laser."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("[laser] must be a table")
    known = {"ha_push_listen", "ha_push_allowed_ip", "gate_url", "gate_laser", "cutting_amps", "on_amps", "run_gap_s"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[laser] unknown key(s): {', '.join(unknown)}")
    listen = raw.get("ha_push_listen", "0.0.0.0:8788")
    m = re.fullmatch(r"([0-9.]+):([0-9]{1,5})", listen) if isinstance(listen, str) else None
    if not m or not 0 < int(m[2]) < 65536:
        raise ValueError("[laser] ha_push_listen must be <IPv4>:<port>")
    gate_url = raw.get("gate_url")
    if gate_url is not None:
        parts = urlsplit(gate_url) if isinstance(gate_url, str) else None
        try:
            host_ok = parts is not None and any(ipaddress.IPv4Address(parts.hostname or "") in n for n in _PRIVATE_NETS)
            port_ok = parts is not None and parts.port is not None
        except ValueError:
            host_ok = port_ok = False
        if not (host_ok and port_ok and parts.scheme == "http" and parts.path in ("", "/") and not parts.query
                and not parts.fragment and not parts.username and not parts.password):
            raise ValueError("[laser] gate_url must be http://<loopback or private IPv4>:<port>")
        gate_url = gate_url.rstrip("/")
    gate_laser = raw.get("gate_laser")
    if gate_laser is not None and not (isinstance(gate_laser, str) and _SLUG_RE.fullmatch(gate_laser)):
        raise ValueError("[laser] gate_laser must be a laser slug")
    cfg = LaserConfig(
        ha_push_allowed_ip=_ipv4(raw.get("ha_push_allowed_ip"), "ha_push_allowed_ip"),
        ha_push_listen=(_ipv4(m[1], "ha_push_listen host"), int(m[2])),
        gate_url=gate_url,
        gate_laser=gate_laser,
        cutting_amps=_number(raw, "cutting_amps", 2.0),
        on_amps=_number(raw, "on_amps", 0.5),
        run_gap_s=_number(raw, "run_gap_s", 60),
    )
    if cfg.on_amps >= cfg.cutting_amps:
        raise ValueError("[laser] on_amps must be below cutting_amps")
    return cfg


def read_secret(path) -> str | None:
    """A 0600 one-line secret, or None when it is missing, empty, not a regular file, or readable by group/others."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO planted there can't block us
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            return None
        value = os.read(fd, 4096).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return value if value and not any(c.isspace() for c in value) else None


def _webhook_url(value: str | None) -> str | None:
    """http(s)://<host>[:port]/api/webhook/<id>, nothing more, or None."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query
            or parts.fragment or not re.fullmatch(r"/api/webhook/[A-Za-z0-9_-]+", parts.path)):
        return None
    return value


# --- pure rules ------------------------------------------------------------------------------------------------------

def iso_z(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t is not None else None


def parse_reading(value) -> float | None:
    """An HA state as pushed: a number or a numeric string. "unavailable", "unknown", "" and non-finite -> None."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        value = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError):   # not a number, or an integer too big for a float
        return None
    return value if math.isfinite(value) else None


def derive_state(*, configured: bool, push_age_s: float | None, switch: str | None, current_a: float | None,
                 cutting_amps: float = 2.0, on_amps: float = 0.5) -> tuple[str, str]:
    """-> (haLink, state). Contract section 3, STATE, in order."""
    if not configured:
        return "not_configured", "unknown"
    if push_age_s is None or push_age_s > HA_STALE_S:
        return "down", "unknown"
    if switch == "off":
        return "up", "off"
    if switch != "on" or current_a is None:
        return "up", "unknown"
    if current_a >= cutting_amps:
        return "up", "cutting"
    if current_a < on_amps:
        return "up", "off"
    return "up", "on_idle"


_END_REASONS = ("switch_off", "telemetry_lost", "idle")
_RUN_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def _finite(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def _saved_run(r, closed: bool) -> dict | None:
    keys = ("runKey", "startedAt", "lastCuttingAt") + (("endedAt", "endReason") if closed else ())
    if not (isinstance(r, dict) and isinstance(r.get("runKey"), str) and _RUN_KEY_RE.fullmatch(r["runKey"])
            and all(_finite(r.get(k)) for k in keys[1:4]) and (not closed or r.get("endReason") in _END_REASONS)):
        return None
    return {k: r[k] for k in keys}


class RunTracker:
    """Cutting runs for one laser. LaserHub saves to_state() whenever it changes (codex review of release-0.68.0: a
    closed run the cloud had not acked was lost to a restart), so every run is re-sent until acked; one that was open
    at the restart comes back closed (load()).
    sample() takes a pushed reading at its receipt time; tick() runs every heartbeat and only ever closes a run, so a
    stale "cutting" reading can never open a second, overlapping run."""

    def __init__(self, gap_s: float = 60.0):
        self.gap_s = gap_s
        self.open: dict | None = None
        self.closed: list[dict] = []

    def sample(self, state: str, t: float) -> None:
        if state == "cutting":
            if self.open is None:
                # the open run rides too, so keep 31 closed: report() never cuts the newest closed run, which the site
                # would close at its last cutting reading and then keep that way (codex re-review of 97f1dfd)
                del self.closed[:-(MAX_RUNS - 1)]
                self.open = {"runKey": uuid.uuid4().hex, "startedAt": t, "lastCuttingAt": t}
            else:
                self.open["lastCuttingAt"] = max(self.open["lastCuttingAt"], t)
            return
        self.tick(state, t)

    def tick(self, state: str, now: float) -> None:
        run = self.open
        if run is None:
            return
        if state == "off":
            reason = "switch_off"
        elif state == "unknown":
            reason = "telemetry_lost"
        elif now - run["lastCuttingAt"] >= self.gap_s:
            reason = "idle"
        else:
            return
        self.closed.append({**run, "endedAt": run["lastCuttingAt"], "endReason": reason})
        del self.closed[:-MAX_RUNS]
        self.open = None

    def report(self) -> list[dict]:
        runs = ([{**self.open, "endedAt": None, "endReason": None}] if self.open else []) + self.closed
        return [{"runKey": r["runKey"], "startedAt": iso_z(r["startedAt"]), "lastCuttingAt": iso_z(r["lastCuttingAt"]),
                 "endedAt": iso_z(r["endedAt"]), "endReason": r["endReason"]} for r in runs[:MAX_RUNS]]

    def ack(self, run_keys) -> None:
        keys = set(run_keys)
        self.closed = [r for r in self.closed if r["runKey"] not in keys]

    def to_state(self) -> dict:
        return {"open": self.open, "closed": self.closed}

    def load(self, state) -> None:
        """A saved to_state(); anything malformed is skipped. A run open at the restart closes at its last cutting
        reading: the hub saw nothing while it was down, and cutting after the restart may be another member's session
        (codex re-review of release-0.68.0)."""
        if not isinstance(state, dict):
            return
        closed = state.get("closed") if isinstance(state.get("closed"), list) else []
        self.closed = [r for r in (_saved_run(c, True) for c in closed) if r]
        run = _saved_run(state.get("open"), False)
        if run:
            self.closed.append({**run, "endedAt": run["lastCuttingAt"], "endReason": "telemetry_lost"})
        del self.closed[:-MAX_RUNS]
        self.open = None


# --- HTTP to the gate and to HA --------------------------------------------------------------------------------------

class Unreachable(Exception):
    pass


def request(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None,
            timeout: float = 5.0) -> tuple[int, dict]:
    """-> (status, JSON object body or {}). Raises Unreachable on a transport failure or when the whole exchange takes
    longer than `timeout` (urllib's timeout is per socket read, so a peer dripping bytes would otherwise hold the
    heartbeat). Never follows a redirect, and its errors never carry the URL (the webhook id is a secret)."""
    out: dict = {}

    def run():
        try:
            out["ok"] = _request(method, url, body, headers, timeout)
        except Unreachable as exc:
            out["err"] = exc

    # ponytail: a peer that drips forever keeps one daemon thread alive until its socket closes; the caller moves on.
    t = threading.Thread(target=run, name="laser-http", daemon=True)
    t.start()
    t.join(timeout)
    if "ok" in out:
        return out["ok"]
    raise out.get("err") or Unreachable("deadline")


def _request(method, url, body, headers, timeout) -> tuple[int, dict]:
    try:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _json_object(resp.read(65536))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(65536)
        except Exception:  # noqa: BLE001 - an unreadable error body is just no detail
            raw = b""
        return exc.code, _json_object(raw)
    except (urllib.error.URLError, OSError, HTTPException, ValueError) as exc:
        raise Unreachable(type(exc).__name__) from None


def _json_object(raw: bytes) -> dict:
    try:
        v = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return v if isinstance(v, dict) else {}


def _int_or_none(v):
    return v if type(v) is int and v >= 0 else None


def _gate_reading(body: dict) -> dict:
    seen = body.get("seenAt")
    moving = body.get("moving")
    return {"idle": body.get("idle") if type(body.get("idle")) is bool else None, "moving": moving if isinstance(moving, bool) else None, "statusWord": _int_or_none(body.get("statusWord")),
            "fileCount": _int_or_none(body.get("fileCount")),
            "seenAt": seen if isinstance(seen, str) and _ISO_RE.fullmatch(seen) else None}


class _Fail(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# --- the hub's laser state -------------------------------------------------------------------------------------------

class _Laser:
    def __init__(self, laser_id: str, slug: str, name: str, gap_s: float, now: float):
        self.id, self.slug, self.name = laser_id, slug, name
        self.switch: str | None = None
        self.current_a: float | None = None
        self.power_w: float | None = None
        self.seen_wall: float | None = None
        self.seen_mono: float | None = None
        self.state, self.state_since, self.ha_link = "unknown", now, "not_configured"
        self.runs = RunTracker(gap_s)


@dataclass
class LaserReport:
    lasers: list | None
    file_checks: list
    run_keys: dict          # laser id -> run keys this report carried
    check_items: list       # the check results this report carried


class LaserHub:
    def __init__(self, cfg: LaserConfig | None = None, *, fetcher=None, diagnostics=None, secrets_dir=None):
        self.cfg = cfg
        self._fetcher = fetcher   # (cloud path, dest) -> (sha256, size); the agent wires get_to_file
        self._diagnostics = diagnostics
        self._secrets_dir = secrets_dir
        self._lock = threading.Lock()
        self._lasers: dict[str, _Laser] = {}
        self._gate = ("not_configured", None, None)   # (gateLink, gateDetail, reading) from the last /status poll
        self._check_queue: list[dict] = []
        self._check_results: list[dict] = []
        self._checking = False
        self._load_running = False
        self._seen: dict[str, None] = {}   # laser requestIds already attempted (an HA POST sent, or a load accepted)
        self._command_results: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        # Saved runs per laser id, claimed when config-down lists that laser; until the first set_lasers they are
        # written back as they were, so a save before config arrives never drops them.
        self._unclaimed_runs = self._load_runs()
        self._runs_saved = json.dumps(self._unclaimed_runs, sort_keys=True)

    # secrets and the gate
    def _secret(self, name: str) -> str | None:
        return read_secret(Path(self._secrets_dir or SECRETS_DIR) / name)

    def _gate_auth(self) -> dict | None:
        token = self._secret(GATE_TOKEN_FILE) if self.cfg and self.cfg.gate_url else None
        return {"Authorization": f"Bearer {token}"} if token else None

    def _gate_laser_id(self) -> str | None:
        """The one laser the gate serves: the gate_laser slug, else the only laser in config-down."""
        if not self.cfg or not self.cfg.gate_url:
            return None
        if self.cfg.gate_laser:
            return next((lz.id for lz in self._lasers.values() if lz.slug == self.cfg.gate_laser), None)
        return next(iter(self._lasers)) if len(self._lasers) == 1 else None

    @staticmethod
    def _load_runs() -> dict:
        try:
            raw = json.loads(RUNS_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = raw.items() if isinstance(raw, dict) else ()
        return dict([(k, v) for k, v in items if isinstance(k, str) and _UUID_RE.fullmatch(k) and isinstance(v, dict)]
                    [:MAX_LASERS])

    def _save_runs(self) -> None:   # caller holds the lock
        # ponytail: one small write per change, so about one per HA push (30 s) while a laser cuts.
        state = {**self._unclaimed_runs, **{lid: lz.runs.to_state() for lid, lz in self._lasers.items()}}
        encoded = json.dumps(state, sort_keys=True)
        if encoded != self._runs_saved and write_json_durably(RUNS_STATE_PATH, encoded):
            self._runs_saved = encoded

    def _record(self, message: str) -> None:
        if self._diagnostics is not None:
            self._diagnostics.record("laser", message)

    # config-down
    def set_lasers(self, raw) -> None:
        """config-down `lasers`: [{id, slug, name}]. Invalid entries are skipped; a laser that stays keeps its state."""
        wanted: dict[str, tuple] = {}
        for item in raw if isinstance(raw, list) else []:
            if len(wanted) >= MAX_LASERS:
                break
            if not isinstance(item, dict):
                continue
            lid, slug, name = item.get("id"), item.get("slug"), item.get("name")
            if (isinstance(lid, str) and _UUID_RE.fullmatch(lid) and isinstance(slug, str) and _SLUG_RE.fullmatch(slug)
                    and isinstance(name, str) and lid not in wanted):
                wanted[lid] = (slug, name[:80])
        now = time.time()
        gap = self.cfg.run_gap_s if self.cfg else 60.0
        with self._lock:
            for lid in list(self._lasers):
                if lid not in wanted:
                    del self._lasers[lid]
            for lid, (slug, name) in wanted.items():
                if lid in self._lasers:
                    self._lasers[lid].slug, self._lasers[lid].name = slug, name
                else:
                    self._lasers[lid] = _Laser(lid, slug, name, gap, now)
                    self._lasers[lid].runs.load(self._unclaimed_runs.get(lid))
            self._unclaimed_runs = {}   # a laser config-down no longer lists takes its runs with it
            self._save_runs()

    # Home Assistant's pushes
    def handle_push(self, peer: str, method: str, path: str, headers, rfile) -> tuple[int, dict]:
        if not self.cfg or peer != self.cfg.ha_push_allowed_ip:
            return 403, {"error": "forbidden"}
        if path.split("?", 1)[0] != "/ha/laser":
            return 404, {"error": "not_found"}
        if method != "POST":
            return 405, {"error": "method_not_allowed"}
        token = self._secret(PUSH_TOKEN_FILE)
        if not token:
            return 503, {"error": "not_configured"}
        got = (headers.get("Authorization") or "").encode("utf-8", "replace")
        if not hmac.compare_digest(got, f"Bearer {token}".encode()):
            return 401, {"error": "unauthorized"}
        length = headers.get("Content-Length") or ""
        if not length.isdigit() or int(length) > MAX_PUSH_BYTES:
            return 400, {"error": "bad_request"}
        raw = rfile.read(int(length))
        body = _json_object(raw) if len(raw) == int(length) else None
        if not body or not isinstance(body.get("laser"), str):
            return 400, {"error": "bad_request"}
        if not self.push(body):
            return 404, {"error": "unknown_laser"}
        return 200, {"ok": True}

    def push(self, body: dict, *, now: float | None = None, mono: float | None = None) -> bool:
        """One plug reading. The sample time is the hub's receipt time (UTC)."""
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            laser = next((lz for lz in self._lasers.values() if lz.slug == body.get("laser")), None)
            if laser is None:
                return False
            switch = body.get("switch")
            laser.switch = switch if switch in _SWITCH_STATES else None
            current = parse_reading(body.get("currentA"))
            power = parse_reading(body.get("powerW"))
            laser.current_a = None if current is None else min(100.0, max(0.0, current))
            laser.power_w = None if power is None else min(10000.0, max(0.0, power))
            laser.seen_wall, laser.seen_mono = now, mono
            self._derive(laser, now, mono)
            laser.runs.sample(laser.state, now)
            self._save_runs()
        return True

    def _derive(self, laser: _Laser, now: float, mono: float) -> None:
        cfg = self.cfg
        laser.ha_link, state = derive_state(
            configured=cfg is not None and self._secret(PUSH_TOKEN_FILE) is not None,
            push_age_s=None if laser.seen_mono is None else mono - laser.seen_mono,
            switch=laser.switch, current_a=laser.current_a,
            cutting_amps=cfg.cutting_amps if cfg else 2.0, on_amps=cfg.on_amps if cfg else 0.5)
        if state != laser.state:
            laser.state, laser.state_since = state, now

    # the gate
    def poll_gate(self) -> tuple:
        """GET /status (3 s) -> (gateLink, gateDetail, reading or None)."""
        auth = self._gate_auth()
        if auth is None:
            return "not_configured", None, None
        try:
            status, body = request("GET", self.cfg.gate_url + "/status", headers=auth, timeout=3)
        except Unreachable:
            return "down", "gate_unreachable", None
        if status == 401:
            return "down", "gate_unauthorized", None
        if status != 200:
            return "down", "gate_error", None
        reading = _gate_reading(body)
        if body.get("controller") == "up":
            return "up", None, reading
        return "down", "controller_unreachable", reading

    # the heartbeat
    def report(self, *, now: float | None = None, mono: float | None = None, poll=True) -> LaserReport:
        """Everything the next heartbeat carries. Polls the gate (3 s at most) when it is configured."""
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        if poll:
            gate = self.poll_gate()
            with self._lock:
                self._gate = gate
        with self._lock:
            gate_id = self._gate_laser_id()
            entries, run_keys = [], {}
            for laser in list(self._lasers.values())[:MAX_LASERS]:
                self._derive(laser, now, mono)
                laser.runs.tick(laser.state, now)
                link, detail, reading = self._gate if laser.id == gate_id else ("not_configured", None, None)
                runs = laser.runs.report()
                run_keys[laser.id] = [r["runKey"] for r in runs if r["endedAt"] is not None]
                entries.append({
                    "machineId": laser.id, "state": laser.state, "stateSince": iso_z(laser.state_since),
                    "switch": laser.switch, "currentA": laser.current_a, "powerW": laser.power_w,
                    "haLink": laser.ha_link, "haSeenAt": iso_z(laser.seen_wall),
                    "gateLink": link, "gateDetail": detail, "gate": reading, "runs": runs,
                })
            checks = self._check_results[:MAX_FILE_CHECKS]
            self._save_runs()   # tick() may have closed a run
        return LaserReport(lasers=entries or None, file_checks=list(checks), run_keys=run_keys, check_items=checks)

    def ack(self, report: LaserReport, laser_ack) -> None:
        """The heartbeat that carried `report` got a 200 with `laser_ack` (its laserAck): drop only the closed runs and
        check results it carried that laserAck names (v0.68.1, codex release review: 0.68.0 dropped everything it
        carried, so a run or verdict the site failed to write was lost). No laserAck, from a site before it or malformed,
        drops nothing. Whatever is kept rides the next beat; the caps bound a site that never acks: 32 closed runs per
        laser (31 while one is open) and 32 check results, the oldest dropped first."""
        lists = [laser_ack.get(k) for k in ("runKeys", "fileIds")] if isinstance(laser_ack, dict) else []
        if len(lists) != 2 or not all(isinstance(v, list) for v in lists):
            return   # malformed in any part: the site always sends both lists (codex review of e4eb9e7)
        run_keys, file_ids = ({v for v in vs if isinstance(v, str)} for vs in lists)
        with self._lock:
            for lid, keys in report.run_keys.items():
                if lid in self._lasers:
                    # only runs this report carried closed: an open run's key the site stored must not drop it once closed
                    self._lasers[lid].runs.ack(run_keys.intersection(keys))
            self._save_runs()
            done = {id(c) for c in report.check_items if c.get("fileId") in file_ids}
            self._check_results = [c for c in self._check_results if id(c) not in done]

    # file checks (heartbeat response laserChecks), off the heartbeat thread
    def offer_checks(self, offers) -> int:
        taken = 0
        with self._lock:
            queued = {o["fileId"] for o in self._check_queue}
            for o in (offers if isinstance(offers, list) else [])[:MAX_CHECK_OFFERS]:
                o = _valid_offer(o, self._lasers)
                if o is None or o["fileId"] in queued or len(self._check_queue) >= MAX_FILE_CHECKS:
                    continue
                self._check_queue.append(o)
                queued.add(o["fileId"])
                taken += 1
            if self._check_queue and not self._checking:
                self._checking = True
                threading.Thread(target=self._check_worker, name="laser-checks", daemon=True).start()
        return taken

    def _check_worker(self) -> None:
        while True:
            with self._lock:
                if not self._check_queue:
                    self._checking = False
                    return
                offer = self._check_queue.pop(0)
            try:
                result = self.check_one(offer)
            except Exception as exc:  # noqa: BLE001 - one file never stops the worker
                log.warning("laser check %s crashed: %s", offer["fileId"], type(exc).__name__)
                result = {"fileId": offer["fileId"], "sha256": offer["sha256"], "verdict": "retry", "detail": "exception"}
            with self._lock:
                self._check_results.append(result)
                del self._check_results[:-(2 * MAX_FILE_CHECKS)]

    def check_one(self, offer: dict) -> dict:
        base = {"fileId": offer["fileId"], "sha256": offer["sha256"]}
        auth = self._gate_auth()
        with self._lock:
            served = offer["machineId"] == self._gate_laser_id()
        if auth is None or not served:
            return {**base, "verdict": "retry", "detail": "gate_not_configured"}
        try:
            data = self._fetch(offer["path"], offer["sha256"], offer["sizeBytes"])
        except _Fail as f:
            if f.detail == "sha_mismatch":
                return {**base, "verdict": "refused", "code": "sha_mismatch",
                        "reason": "the file didn't arrive at the shop intact; upload it again"}
            return {**base, "verdict": "retry", "detail": f.detail}
        query = {"maxPowerPct": offer["maxPowerPct"]}
        if "profile" in offer:
            query["profile"] = json.dumps(offer["profile"], separators=(",", ":"))
        try:
            status, body = request("POST", f"{self.cfg.gate_url}/check?{urlencode(query)}", body=data,
                                   headers={**auth, "Content-Type": "application/octet-stream"}, timeout=CHECK_TIMEOUT_S)
        except Unreachable:
            return {**base, "verdict": "retry", "detail": "gate_unreachable"}
        if status == 200 and body.get("verdict") == "checked":
            if "profile" in offer and (not _checked(body) or body.get("sha256") != offer["sha256"] or body.get("sizeBytes") != offer["sizeBytes"]):
                log.error("laser: gate returned an invalid material check")
                return {**base, "verdict": "retry", "detail": "bad_gate_response"}
            keys = ("checkVersion", "preparedSha256", "sizeBytes", "maxPowerPct", "widthMm", "heightMm", "layers")
            return {**base, "verdict": "checked", "result": {k: body[k] for k in keys if k in body}}
        if status == 200 and body.get("verdict") == "refused":
            out = {**base, "verdict": "refused", "reason": str(body.get("reason") or "the gate refused this file")[:300]}
            if isinstance(body.get("code"), str):
                out["code"] = body["code"][:64]
            return out
        return {**base, "verdict": "retry", "detail": _gate_detail(status, body)}

    def _fetch(self, path: str, sha: str, size: int) -> bytes:
        if self._fetcher is None:
            raise _Fail("fetch_failed")
        with tempfile.TemporaryDirectory(prefix="laser-") as d:
            dest = Path(d) / "file.rd"
            try:
                got_sha, got_size = self._fetcher(path, dest)
            except Exception as exc:  # noqa: BLE001 - any download failure is a retry, never a crash
                log.warning("laser file fetch failed: %s", type(exc).__name__)
                raise _Fail("fetch_failed") from None
            if got_sha != sha or got_size != size:
                raise _Fail("sha_mismatch")
            return dest.read_bytes()

    # commands
    def dispatch(self, request_id: str, laser_id: str, command: str, params) -> dict | None:
        """One laser_* row from pendingCommands -> its report now, or None for a load that was accepted (its result
        rides a later heartbeat through take_command_results)."""
        def fail(detail):
            return {"requestId": request_id, "command": command, "status": "failed", "detail": detail}

        if self.seen(request_id):
            return None   # attempted once already: never a second HA call or store, never a second result
        if command not in LASER_COMMANDS:
            return fail("unknown_command")
        p = params if isinstance(params, dict) else {}
        expires = parse_instant(p.get("expiresAt"))
        if expires is None:
            return fail("bad_params")
        if expires <= time.time():
            return fail("expired")
        with self._lock:
            laser = self._lasers.get(laser_id)
            slug = laser.slug if laser else None
        if slug is None:
            return fail("laser_unavailable")
        if command == "laser_power_on":
            detail = self._power_on(request_id, slug)
            log.info("laser_power_on %s for %s: %s", request_id, slug, detail or "ok")
            return {"requestId": request_id, "command": command, "status": "ok"} if detail is None else fail(detail)
        load = _valid_load(p)
        if load is None:
            return fail("bad_params")
        with self._lock:
            if self._gate_auth() is None or laser_id != self._gate_laser_id():
                return fail("gate_not_configured")
            if self._load_running:
                return fail("gate_busy")
            self._load_running = True
            self._remember(request_id)
        threading.Thread(target=self._load_worker, args=(request_id, slug, load, expires), name="laser-load",
                         daemon=True).start()
        return None

    def seen(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._seen

    def _remember(self, request_id: str) -> None:   # caller holds the lock
        self._seen[request_id] = None
        while len(self._seen) > 1000:
            self._seen.pop(next(iter(self._seen)))

    def _power_on(self, request_id: str, slug: str) -> str | None:
        url = _webhook_url(self._secret(WEBHOOK_URL_FILE)) if self.cfg is not None else None   # no [laser]: never
        if url is None:
            return "ha_not_configured"
        with self._lock:
            self._remember(request_id)   # before the POST: a lost answer must not become a second turn-on
        body = json.dumps({"requestId": request_id, "laser": slug}).encode()
        try:
            status, _ = request("POST", url, body=body, headers={"Content-Type": "application/json"}, timeout=5)
        except Unreachable:
            return "ha_unreachable"
        return None if 200 <= status < 300 else "ha_rejected"

    def _load_worker(self, request_id: str, slug: str, load: dict, expires: float) -> None:
        try:
            detail = self._store(load, expires)
        except Exception as exc:  # noqa: BLE001 - the member gets a failed result, never silence
            log.warning("laser_load %s crashed: %s", request_id, type(exc).__name__)
            detail = "exception"
        log.info("laser_load %s for %s code=%s sha=%s: %s", request_id, slug, load["code"], load["sha256"][:12],
                 detail or "ok")
        report = {"requestId": request_id, "command": "laser_load", "status": "ok" if detail is None else "failed"}
        if detail is not None:
            report["detail"] = detail
        with self._lock:
            self._command_results.append(report)
            self._load_running = False

    def _store(self, load: dict, expires: float) -> str | None:
        try:
            data = self._fetch(load["path"], load["sha256"], load["sizeBytes"])
        except _Fail as f:
            return f.detail
        if time.time() >= expires:
            return "expired"   # never store for a request the member has given up on
        auth = self._gate_auth()
        if auth is None:
            return "gate_not_configured"
        # Leave a second for the HTTP reply before the hub gives up waiting.
        expires = min(expires, time.time() + STORE_TIMEOUT_S - 1)
        query = {"name": load["code"], "maxPowerPct": load["maxPowerPct"], "sha256": load["sha256"],
                 "expiresAt": f"{expires:.6f}"}
        if "profile" in load:
            query.update(profile=json.dumps(load["profile"], separators=(",", ":")), preparedSha256=load["preparedSha256"])
        query = urlencode(query)
        try:
            status, body = request("POST", f"{self.cfg.gate_url}/store?{query}", body=data,
                                   headers={**auth, "Content-Type": "application/octet-stream"}, timeout=STORE_TIMEOUT_S)
        except Unreachable:
            return "gate_unreachable"
        if status == 200 and body.get("ok") is True:
            if "profile" in load and (body.get("preparedSha256") != load["preparedSha256"] or body.get("name") != load["code"]
                                     or type(body.get("fileCountBefore")) is not int or type(body.get("fileCountAfter")) is not int
                                     or body["fileCountAfter"] != body["fileCountBefore"] + 1):
                log.error("laser: gate returned an invalid store acknowledgement")
                return "no_result"
            return None
        if body.get("error") == "refused":
            return f"gate:refused: {body.get('reason') or ''}"[:200]
        return _gate_detail(status, body)

    def take_command_results(self) -> list[dict]:
        with self._lock:
            out, self._command_results = self._command_results, []
        return out

    # the HA push listener
    def start(self) -> None:
        if self.cfg is None or self._server is not None:
            return
        try:
            server = _PushServer(self.cfg.ha_push_listen, self)
        except OSError as exc:
            self._record(f"HA push listener failed to start on {self.cfg.ha_push_listen[1]}: {exc.strerror}")
            log.error("laser: HA push listener failed to start: %s", exc.strerror)
            return
        self._server = server
        threading.Thread(target=server.serve_forever, name="laser-ha-push", daemon=True).start()
        log.info("laser: HA push listener on %s:%d for %s", *self.cfg.ha_push_listen, self.cfg.ha_push_allowed_ip)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _gate_detail(status: int, body: dict) -> str:
    err = body.get("error")
    return (f"gate:{err}" if isinstance(err, str) and err else f"gate:http_{status}")[:200]


def parse_instant(value) -> float | None:
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else None


def _valid_profile(value):
    return isinstance(value, list) and 1 <= len(value) <= 3 and all(
        isinstance(o, dict) and set(o) == {"speed", "min", "max"} and all(_finite(v) for v in o.values())
        and 1 <= o["speed"] <= 1000 and 0 < o["min"] <= o["max"] <= 100 for o in value)


def _checked(body):
    layers = body.get("layers")
    return (type(body.get("checkVersion")) is int and body["checkVersion"] == 1 and isinstance(body.get("preparedSha256"), str)
            and _SHA_RE.fullmatch(body["preparedSha256"]) and _finite(body.get("widthMm"))
            and 0 <= body["widthMm"] <= 1000 and _finite(body.get("heightMm")) and 0 <= body["heightMm"] <= 600
            and isinstance(layers, list) and 1 <= len(layers) <= 64
            and all(isinstance(l, dict) and type(l.get("index")) is int and 0 <= l["index"] < 64
                    and all(_finite(l.get(k)) for k in ("minPowerPct", "maxPowerPct", "speedMmS"))
                    and 0 < l["minPowerPct"] <= l["maxPowerPct"] <= 100 and 1 <= l["speedMmS"] <= 1000 for l in layers))


def _valid_offer(o, lasers) -> dict | None:
    if not isinstance(o, dict):
        return None
    fid, mid, path, sha = o.get("fileId"), o.get("machineId"), o.get("path"), o.get("sha256")
    size, cap = o.get("sizeBytes"), o.get("maxPowerPct")
    if not (isinstance(fid, str) and _UUID_RE.fullmatch(fid) and isinstance(mid, str) and mid in lasers
            and path == LASER_FILE_PATH + fid
            and isinstance(sha, str) and _SHA_RE.fullmatch(sha) and type(size) is int
            and 1 <= size <= LASER_FILE_MAX_BYTES and type(cap) is int and 1 <= cap <= 100):
        log.warning("laser: skipping a malformed check offer")
        return None
    if "profile" in o and not _valid_profile(o["profile"]):
        log.warning("laser: skipping a malformed material profile")
        return None
    return {**({"profile": o["profile"]} if "profile" in o else {}), "fileId": fid, "machineId": mid, "path": path, "sha256": sha, "sizeBytes": size, "maxPowerPct": cap}


def _valid_load(p: dict) -> dict | None:
    fid, sha, size, code, cap = p.get("fileId"), p.get("sha256"), p.get("sizeBytes"), p.get("code"), p.get("maxPowerPct")
    if not (isinstance(fid, str) and _UUID_RE.fullmatch(fid) and p.get("path") == LASER_FILE_PATH + fid
            and isinstance(sha, str) and _SHA_RE.fullmatch(sha) and type(size) is int
            and 1 <= size <= LASER_FILE_MAX_BYTES and isinstance(code, str) and _CODE_RE.fullmatch(code)
            and type(cap) is int and 1 <= cap <= 100):
        return None
    if "profile" in p and (not _valid_profile(p["profile"]) or not isinstance(p.get("preparedSha256"), str)
                           or not _SHA_RE.fullmatch(p["preparedSha256"])):
        return None
    return {**({"profile": p["profile"], "preparedSha256": p["preparedSha256"]} if "profile" in p else {}), "fileId": fid, "path": p["path"], "sha256": sha, "sizeBytes": size, "code": code, "maxPowerPct": cap}


class _PushHandler(BaseHTTPRequestHandler):
    timeout = 10   # a stalled client cannot hold a worker thread

    def log_message(self, *_args):
        pass

    def _route(self):
        try:
            status, body = self.server.hub.handle_push(self.client_address[0], self.command, self.path, self.headers,
                                                       self.rfile)
        except Exception as exc:  # noqa: BLE001 - a bad push never takes the listener down
            log.warning("laser: HA push handler crashed: %s", type(exc).__name__)
            status, body = 500, {"error": "internal"}
        raw = json.dumps(body).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _route


class _PushServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, hub: LaserHub):
        self.hub = hub
        super().__init__(addr, _PushHandler)
