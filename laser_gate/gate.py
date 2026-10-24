"""The gate: config, the Ruida UDP link, store(), status, and the one HTTP listener.

Only Controller.send() writes to the controller, and it runs guard() first: a datagram that holds anything other than
an ALLOWED job command, the gate's own store envelope, or a DA 00 read of an allowlisted address raises before the
socket is touched. There is no code here that builds a start, stop, pause, frame, jog, home, panel-key, delete or
settings-write datagram.
"""
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .checker import (ALLOWED, NAME_RE, START, check, enc14, op_str, plan_packets, swizzle, tokenize, u14, u35,
                      unswizzle, valid_profile)

log = logging.getLogger("laser_gate")

CONTROLLER_PORT, REPLY_PORT = 50200, 40200  # job/settings port on the controller; the controller answers to 40200
ACK = swizzle(b"\xCC")  # 0xC6 on the wire. UNVERIFIED on this firmware (T0)
STATUS_ADDR = 0x0200    # machine status word; bit 15 = an axis is moving (verified 2026-10-12)
MOVING_BIT = 15
ENVELOPE_RE = re.compile(rb"\xE8\x02\xE7\x01[A-Z]{2}[0-9]{4}\x00")
SHA_RE = re.compile(r"[0-9a-f]{64}")


class ConfigError(Exception):
    pass


class GuardError(Exception):
    pass


class StoreExpired(Exception):
    pass


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise StoreExpired()
    return left


# --- config ----------------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Config:
    listen: str = "127.0.0.1:7650"
    allowed_peer: str = "127.0.0.1"
    token_file: str = "/etc/laser-gate/token"
    controller_ip: str = "10.0.3.3"
    bind_ip: str = "10.0.3.1"
    ack_timeout_s: float = 0.5
    store_enabled: bool = False
    file_count_addr: str = ""
    idle_status_mask: int = 0
    idle_status_value: int = 0
    names_verified: bool = False
    max_stored_files: int = 0
    min_power_pct: float = 0.0
    max_power_pct: float = 100.0
    bed_mm: tuple = (1000, 600)
    speed_mm_s: tuple = (1, 1000)
    max_file_bytes: int = 4194304
    packet_max_bytes: int = 1000

    @property
    def file_count(self):
        return int(self.file_count_addr, 16) if self.file_count_addr else None

    @property
    def listen_addr(self):
        host, port = self.listen.rsplit(":", 1)
        return host, int(port)


def _ipv4(name, v):
    try:
        return str(ipaddress.IPv4Address(v))
    except ValueError:
        raise ConfigError(f"{name} must be an IPv4 address, got {v!r}") from None


def validate(raw):
    """dict from config.toml -> Config. Unknown keys, wrong types and out-of-range values refuse to start."""
    fields = {f.name: f for f in dataclasses.fields(Config)}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    out = {}
    for k, v in raw.items():
        want = type(fields[k].default)
        if want is float and type(v) is int:
            v = float(v)
        elif want is tuple and isinstance(v, list):
            if len(v) != 2 or not all(type(x) in (int, float) and x > 0 for x in v):
                raise ConfigError(f"{k} must be two positive numbers")
            v = tuple(v)
        if type(v) is not want:  # bool is not int here: type() is exact
            raise ConfigError(f"{k} must be {want.__name__}, got {type(v).__name__}")
        out[k] = v
    cfg = Config(**out)
    for k in ("allowed_peer", "controller_ip", "bind_ip"):
        _ipv4(k, getattr(cfg, k))
    m = re.fullmatch(r"([0-9.]+):([0-9]{1,5})", cfg.listen)
    if not m or not 0 < int(m[2]) < 65536:
        raise ConfigError(f"listen must be host:port, got {cfg.listen!r}")
    host = _ipv4("listen host", m[1])
    if host in ("0.0.0.0", cfg.bind_ip):
        raise ConfigError("listen must not be 0.0.0.0 or the laser-side bind_ip")
    if cfg.file_count_addr and not (re.fullmatch(r"(0x)?[0-9A-Fa-f]{1,4}", cfg.file_count_addr)
                                    and cfg.file_count <= 0x3FFF):
        raise ConfigError("file_count_addr must be empty or a hex address 0000-3FFF")
    if not (1 <= cfg.max_power_pct <= 100 and 0 <= cfg.min_power_pct <= cfg.max_power_pct):
        raise ConfigError("need 0 <= min_power_pct <= max_power_pct <= 100 and max_power_pct >= 1")
    if not 0 <= cfg.idle_status_mask < 2**35 or not 0 <= cfg.idle_status_value < 2**35 or cfg.idle_status_value & ~cfg.idle_status_mask:
        raise ConfigError("idle status mask/value must be 35-bit words, with value inside mask")
    if cfg.idle_status_mask and (not cfg.idle_status_mask & (1 << MOVING_BIT) or cfg.idle_status_mask == 1 << MOVING_BIT
                                or cfg.idle_status_value & (1 << MOVING_BIT)):
        raise ConfigError("idle status must prove more than a stationary head (T4)")
    if not 0 <= cfg.max_stored_files <= 512:
        raise ConfigError("max_stored_files must be 0 (unverified) or 1..512")
    if not 0 < cfg.ack_timeout_s <= 10:
        raise ConfigError("ack_timeout_s must be in (0, 10]")
    if not cfg.speed_mm_s[0] < cfg.speed_mm_s[1]:
        raise ConfigError("speed_mm_s must be [low, high]")
    if not 0 < cfg.max_file_bytes <= 16 * 1024 * 1024:
        raise ConfigError("max_file_bytes must be 1..16 MiB")
    if not 64 <= cfg.packet_max_bytes <= 1400:
        raise ConfigError("packet_max_bytes must be 64..1400")
    return cfg


def read_token(path):
    try:
        st = os.stat(path)
        with open(path, encoding="ascii") as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"token file {path}: {e.__class__.__name__}") from None
    if st.st_mode & 0o077:
        raise ConfigError(f"token file {path} is readable by group or others; chmod 600 it")
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ConfigError(f"token file {path} must hold one token of at least 32 characters")
    return token


def load_config(path, need_token=True):
    """-> (Config, token or None)."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"config {path}: {e}") from None
    cfg = validate(raw)
    return cfg, (read_token(cfg.token_file) if need_token else None)


# --- the controller link ---------------------------------------------------------------------------------------------

def guard(payload, reads):
    """The never-send guard. Raises unless every command in this (unswizzled) datagram is an ALLOWED job command with
    its exact length, a DA 00 read of an address in `reads`, or the gate's own envelope at the very start. The header
    marker D8 00 may only travel in a datagram that starts with the envelope (store's first packet)."""
    # Standalone, bounded read-by-index only; never accepted in an uploaded job or a store packet.
    if len(payload) == 4 and payload[:2] == b"\xe8\x01" and all(b < 128 for b in payload[2:]) and 1 <= u14(payload[2:]) <= 512:
        return
    m = ENVELOPE_RE.match(payload)
    body = payload[m.end():] if m else payload
    if not body:
        raise GuardError("never-send guard: empty datagram")
    for t in tokenize(body):
        if t.key == (0xDA, 0x00) and t.n == 2 and u14(body[t.ps:t.end]) in reads:
            continue
        if t.key == START and not m:
            raise GuardError("never-send guard: D8 00 outside a store's first packet")
        if ALLOWED.get(t.key) != t.n:
            raise GuardError(f"never-send guard: refusing to send {op_str(t.key)}")


def frame(swizzled):
    """UDP framing: a 2-byte big-endian sum of the swizzled bytes, then the bytes (pstation ruida-readonly UdpPort)."""
    return (sum(swizzled) & 0xFFFF).to_bytes(2, "big") + swizzled


class Controller:
    """UDP to (ip, 50200) from a socket bound to (bind_ip, 40200). One socket per operation, so a late datagram from
    an earlier operation can never be read as this one's ACK. Datagrams from any other source IP are dropped."""

    def __init__(self, ip, bind_ip, ack_timeout_s, reads, send_port=CONTROLLER_PORT, recv_port=REPLY_PORT):
        self.ip, self.bind_ip, self.ack_timeout_s, self.reads = ip, bind_ip, ack_timeout_s, frozenset(reads)
        self.send_port, self.recv_port = send_port, recv_port  # other ports: tests only

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((self.bind_ip, self.recv_port))
        except OSError:
            s.close()
            raise
        return s

    def send(self, sock, payload, deadline=None):
        """The ONLY function that writes to the controller."""
        guard(payload, self.reads)
        data = frame(swizzle(payload))
        if deadline is not None:
            sock.settimeout(remaining(deadline))
        sock.sendto(data, (self.ip, self.send_port))

    def recv(self, sock, deadline=None):
        until = time.monotonic() + self.ack_timeout_s
        if deadline is not None:
            remaining(deadline)
            until = min(until, deadline)
        while (left := until - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, (src, _port) = sock.recvfrom(2048)
            except OSError:  # timeout, or ICMP port-unreachable surfacing as ECONNREFUSED
                if deadline is not None:
                    remaining(deadline)
                return None
            if deadline is not None:
                remaining(deadline)
            if src == self.ip:
                return data
        if deadline is not None:
            remaining(deadline)
        return None

    def acked(self, sock, deadline=None):
        return self.recv(sock, deadline) == ACK

    def name(self, sock, index, deadline=None):
        query = b"\xe8\x01" + enc14(index)
        self.send(sock, query, deadline)
        if not self.acked(sock, deadline):
            return None
        reply = unswizzle(self.recv(sock, deadline) or b"")
        if not 6 <= len(reply) <= 13 or reply[:4] != query or reply[-1:] != b"\x00" or any(b < 32 or b > 126 for b in reply[4:-1]):
            return None
        return reply[4:-1].decode("ascii").upper()

    def names(self, sock, count, deadline=None):
        names = []
        for i in range(1, count + 1):
            try:
                name = self.name(sock, i, deadline)
            except StoreExpired:
                log.warning("panel names processed=%d, expected=%d: expired", len(names), count)
                raise
            if name is None or name in names:
                log.warning("panel names processed=%d, expected=%d", len(names), count)
                return None
            names.append(name)
        log.info("panel names processed=%d, expected=%d", len(names), count)
        return names

    def read(self, sock, addr, deadline=None):
        """DA 00 read -> 35-bit value, or None on any miss."""
        self.send(sock, b"\xDA\x00" + enc14(addr), deadline)
        if not self.acked(sock, deadline):
            return None
        reply = self.recv(sock, deadline)
        d = unswizzle(reply or b"")
        if len(d) < 9 or d[:4] != b"\xDA\x01" + enc14(addr):
            return None
        return u35(d[4:9])


def _now_iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


class Gate:
    def __init__(self, cfg, token, send_port=CONTROLLER_PORT, recv_port=REPLY_PORT):  # other ports: tests only
        self.cfg = cfg
        self.auth = f"Bearer {token}".encode()
        self.reads = frozenset({STATUS_ADDR} | ({cfg.file_count} if cfg.file_count is not None else set()))
        self.ctl = Controller(cfg.controller_ip, cfg.bind_ip, cfg.ack_timeout_s, self.reads, send_port, recv_port)
        self._store_lock = threading.Lock()  # one store at a time; held means /status reports busy
        self._io_lock = threading.Lock()     # all controller I/O
        self._last = {"controller": "down", "statusWord": None, "moving": None, "fileCount": None, "seenAt": None}

    def calibration_gap(self, for_store):
        if for_store and self.cfg.file_count is None:
            return "file_count_addr is not set (T0)"
        if for_store and (not self.cfg.names_verified or not self.cfg.max_stored_files):
            return "panel name reads and capacity are not verified (T0)"
        if for_store and not self.cfg.idle_status_mask:
            return "idle/running/paused status is not verified (T4)"
        if self.cfg.min_power_pct <= 0:
            return "min_power_pct is not set (T5)"
        return None

    def idle(self, word):
        return None if word is None or not self.cfg.idle_status_mask else word & self.cfg.idle_status_mask == self.cfg.idle_status_value

    # /status
    def status(self):
        if self._io_lock.acquire(blocking=False):
            try:
                self._poll()
            finally:
                self._io_lock.release()
        return {"gateVersion": __version__, **self._last, "idle": self.idle(self._last["statusWord"]), "storeEnabled": self.cfg.store_enabled,
                "calibrated": {"minPowerPct": self.cfg.min_power_pct > 0, "fileCountAddr": self.cfg.file_count is not None},
                "busy": self._store_lock.locked()}

    def _poll(self):
        sw = count = None
        try:
            with self.ctl.open() as s:
                sw = self.ctl.read(s, STATUS_ADDR)
                if sw is not None and self.cfg.file_count is not None:
                    count = self.ctl.read(s, self.cfg.file_count)
        except OSError:
            pass
        if sw is None:
            self._last = {**self._last, "controller": "down", "statusWord": None, "moving": None, "fileCount": None}
        else:
            self._last = {"controller": "up", "statusWord": sw, "moving": bool(sw >> MOVING_BIT & 1),
                          "fileCount": count, "seenAt": _now_iso(time.time())}

    # /store steps 4-10. The HTTP layer runs steps 1-3 (enabled, calibrated, params) before it reads the body; 1 and 2
    # are repeated here so no other caller can skip them.
    def store(self, body, name, max_power_pct, sha256, profile=None, prepared_sha=None, *, expires=None):
        if not self.cfg.store_enabled:
            return 503, {"error": "store_disabled"}
        if self.calibration_gap(for_store=True):
            return 503, {"error": "not_calibrated", "detail": self.calibration_gap(for_store=True)}
        if type(expires) not in (int, float) or not math.isfinite(expires):
            return 400, {"error": "bad_request", "detail": "expiresAt"}
        deadline = time.monotonic() + min(120, expires - time.time())
        if deadline <= time.monotonic():
            return 409, {"error": "expired"}
        if hashlib.sha256(body).hexdigest() != sha256:
            return 400, {"error": "sha_mismatch"}
        v = check(body, max_power_pct, self.cfg, profile)
        if v["verdict"] != "checked":
            return 422, {"error": "refused", "code": v["code"], "reason": v["reason"], "offset": v["offset"]}
        if prepared_sha is not None and v["preparedSha256"] != prepared_sha:
            return 409, {"error": "prepared_mismatch"}
        packets = plan_packets(body, name, self.cfg.packet_max_bytes)
        for p in packets:  # every packet passes the guard before the first one is sent
            guard(p, self.reads)
        if not self._store_lock.acquire(blocking=False):
            return 409, {"error": "busy"}
        try:
            if not self._io_lock.acquire(timeout=remaining(deadline)):
                raise StoreExpired()
            try:
                code, result = self._transfer(packets, name, deadline)
                if code == 200:
                    result["preparedSha256"] = v["preparedSha256"]
                return code, result
            finally:
                self._io_lock.release()
        except StoreExpired:
            return 409, {"error": "expired"}
        finally:
            self._store_lock.release()

    def _transfer(self, packets, name, deadline):
        fc, n = self.cfg.file_count, len(packets)
        sent = 0
        try:
            with self.ctl.open() as s:
                sw = self.ctl.read(s, STATUS_ADDR, deadline)
                if sw is None:
                    return 502, {"error": "controller_unreachable"}
                if sw >> MOVING_BIT & 1:
                    return 409, {"error": "head_moving"}
                if self.idle(sw) is not True:
                    return 409, {"error": "not_idle"}
                before = self.ctl.read(s, fc, deadline)
                if before is None:
                    return 502, {"error": "controller_unreachable"}
                if before >= self.cfg.max_stored_files:
                    return 409, {"error": "memory_full"}
                names = self.ctl.names(s, before, deadline)
                if names is None:
                    return 502, {"error": "name_unverified"}
                if name in names:
                    return 409, {"error": "name_taken"}
                # A laptop or panel action while names were read makes this observation stale.
                if self.ctl.read(s, fc, deadline) != before or self.idle(self.ctl.read(s, STATUS_ADDR, deadline)) is not True:
                    return 409, {"error": "not_idle"}
                for k, p in enumerate(packets, 1):
                    self.ctl.send(s, p, deadline)
                    sent = k
                    if not self.ctl.acked(s, deadline):
                        # Nothing more is sent: there is no cleanup command, and a transfer is never resumed.
                        return 502, {"error": "transfer_aborted", "packet": k, "packets": n}
                confirm_until, after = min(deadline, time.monotonic() + 3.0), None
                while True:
                    after = self.ctl.read(s, fc, deadline)
                    if after == before + 1:
                        stored = self.ctl.names(s, after, deadline)
                        if stored is None or sorted(stored) != sorted([*names, name]):
                            return 502, {"error": "name_unverified"}
                        return 200, {"ok": True, "name": name, "fileCountBefore": before, "fileCountAfter": after,
                                     "packets": n, "bytes": sum(map(len, packets))}
                    if (after is not None and after != before) or time.monotonic() >= confirm_until:
                        return 502, {"error": "count_mismatch", "before": before, "after": after}
                    time.sleep(min(0.2, remaining(deadline)))
        except StoreExpired:
            return (502, {"error": "transfer_aborted", "packet": sent, "packets": n}) if sent else (409, {"error": "expired"})
        except OSError:
            return 502, {"error": "controller_unreachable"}


# --- HTTP ------------------------------------------------------------------------------------------------------------

ROUTES = {"/check": "POST", "/store": "POST", "/status": "GET"}


def _params(query, names):
    q = parse_qs(query, keep_blank_values=True)
    if set(q) - set(names) or any(len(v) != 1 for v in q.values()):
        return None
    return {k: v[0] for k, v in q.items()}


def _profile(params):
    raw = (params or {}).get("profile")
    if not isinstance(raw, str) or len(raw) > 1024:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if valid_profile(value) else None


def _power(v):
    return int(v) if v and re.fullmatch(r"[0-9]{1,3}", v) and 1 <= int(v) <= 100 else None


class Handler(BaseHTTPRequestHandler):
    def version_string(self):
        return "laser-gate"
    timeout = 30  # a stalled client cannot hold a worker thread forever

    def log_message(self, *_args):  # the gate logs its own lines; the default would log every request line
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        gate = self.server.gate
        peer = self.client_address[0]
        if peer != gate.cfg.allowed_peer:  # before any body byte is read
            log.info("refused %s %s from %s: forbidden", self.command, self.path.split("?")[0], peer)
            return self._reply(403, {"error": "forbidden"})
        got = self.headers.get("Authorization", "").encode("utf-8", "replace")
        if not hmac.compare_digest(got, gate.auth):
            log.info("refused %s %s: unauthorized", self.command, self.path.split("?")[0])
            return self._reply(401, {"error": "unauthorized"})
        url = urlsplit(self.path)
        method = ROUTES.get(url.path)
        if method is None:
            return self._reply(404, {"error": "not_found"})
        if self.command != method:
            return self._reply(405, {"error": "method_not_allowed"})
        if url.path == "/status":
            return self._reply(200, gate.status())
        if url.path == "/check":
            return self._check(gate, url.query)
        return self._store(gate, url.query)

    def _length(self, gate):
        """-> (n, None) or (None, error already sent). Never reads the body."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None, self._reply(411, {"error": "length_required"})
        if not re.fullmatch(r"[0-9]+", raw):  # ASCII digits only (str.isdigit takes "²")
            return None, self._reply(400, {"error": "bad_request", "detail": "Content-Length"})
        n = int(raw) if len(raw) <= 12 else gate.cfg.max_file_bytes + 1  # longer is never under the limit
        if n > gate.cfg.max_file_bytes:
            return None, self._reply(413, {"error": "too_large", "limit": gate.cfg.max_file_bytes})
        return n, None

    def _body(self, n):
        body = self.rfile.read(n)
        return body if len(body) == n else None

    def _check(self, gate, query):
        n, _ = self._length(gate)
        if n is None:
            return
        p = _params(query, ["maxPowerPct", "profile"])
        cap = _power(p.get("maxPowerPct")) if p is not None else None
        if cap is None:
            return self._reply(400, {"error": "bad_request", "detail": "maxPowerPct"})
        profile = _profile(p)
        if p is not None and "profile" in p and profile is None:
            return self._reply(400, {"error": "bad_request", "detail": "profile"})
        if gate.cfg.min_power_pct <= 0:
            return self._reply(503, {"error": "not_calibrated", "detail": "min_power_pct is not set (T5)"})
        body = self._body(n)
        if body is None:
            return self._reply(400, {"error": "bad_request", "detail": "body shorter than Content-Length"})
        v = check(body, cap, gate.cfg, profile)
        if v["verdict"] != "checked":
            log.info("check refused sha=%s code=%s offset=%s", v["sha256"][:12], v["code"], v["offset"])
        return self._reply(200, v)

    def _store(self, gate, query):
        """One log line per /store: name, sha256[:12], bytes, packets, result."""
        code, out = self._store_result(gate, query)
        p = _params(query, ["name", "maxPowerPct", "sha256", "profile", "preparedSha256", "expiresAt"]) or {}
        name = p.get("name") if NAME_RE.fullmatch(p.get("name", "")) else "-"  # only validated values reach the log
        sha = p.get("sha256", "")[:12] if SHA_RE.fullmatch(p.get("sha256", "")) else "-"
        size = self.headers.get("Content-Length", "")
        result = out.get("error", "ok") + (f":{out['code']}" if "code" in out else "")
        log.info("store name=%s sha=%s bytes=%s packets=%s result=%s", name, sha, size if size.isdigit() else "-",
                 out.get("packets", "-"), result)
        if code:
            self._reply(code, out)

    def _store_result(self, gate, query):
        """-> (status, json). status None means _length already answered."""
        if not gate.cfg.store_enabled:
            return 503, {"error": "store_disabled"}
        gap = gate.calibration_gap(for_store=True)
        if gap:
            return 503, {"error": "not_calibrated", "detail": gap}
        p = _params(query, ["name", "maxPowerPct", "sha256", "profile", "preparedSha256", "expiresAt"])
        if p is None:
            return 400, {"error": "bad_request", "detail": "query"}
        for k, ok in (("name", NAME_RE.fullmatch(p.get("name", ""))), ("maxPowerPct", _power(p.get("maxPowerPct"))),
                      ("sha256", SHA_RE.fullmatch(p.get("sha256", "")))):
            if not ok:
                return 400, {"error": "bad_request", "detail": k}
        profile = _profile(p)
        prepared = p.get("preparedSha256")
        if profile is None or not SHA_RE.fullmatch(prepared or ""):
            return 400, {"error": "bad_request", "detail": "profile/preparedSha256"}
        expiry = p.get("expiresAt", "")
        if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,6})?", expiry):
            return 400, {"error": "bad_request", "detail": "expiresAt"}
        expires = float(expiry)
        if expires <= time.time():
            return 409, {"error": "expired"}
        n, _ = self._length(gate)
        if n is None:
            return None, {"error": "length"}
        body = self._body(n)
        if body is None:
            return 400, {"error": "bad_request", "detail": "body shorter than Content-Length"}
        return gate.store(body, p["name"], _power(p["maxPowerPct"]), p["sha256"], profile, prepared, expires=expires)

    def __getattr__(self, name):
        # Every method (TRACE, CONNECT, made-up ones too) takes the same path: peer, token, route, method. Without this,
        # the base class answers an unlisted method with 501 before the peer check.
        if name.startswith("do_"):
            return self._route
        raise AttributeError(name)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gate, addr=None):
        self.gate = gate
        super().__init__(addr or gate.cfg.listen_addr, Handler)


def serve(config_path):
    cfg, token = load_config(config_path)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    srv = Server(Gate(cfg, token))
    log.info("laser-gate %s listening on %s for %s; controller %s via %s; store %s", __version__, cfg.listen,
             cfg.allowed_peer, cfg.controller_ip, cfg.bind_ip, "enabled" if cfg.store_enabled else "disabled")
    srv.serve_forever()
