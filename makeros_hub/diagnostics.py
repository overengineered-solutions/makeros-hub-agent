"""Passive diagnostics for the hub heartbeat.

The diagnostics payload is intentionally cheap and bounded: small sticky error
rings, boolean-only network probes, process-cached system facts, and an in-memory
WARNING/ERROR log ring. Everything emitted from this module is redacted.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import socket
import time

SUBSYSTEMS = (
    "tailscale",
    "printers",
    "heartbeat",
    "config",
    "update",
    "ingest",
    "vprinter",
    # V5 — registered so `_record_diagnostic(diagnostics, "camera"|"failure_watch"|"detector", ...)`
    # in agent.py + onnx_detector.py survives `snapshot()` instead of being silently
    # dropped on the unknown-subsystem floor.
    "camera",
    "failure_watch",
    "detector",
)
ERROR_MESSAGE_MAX = 280
LOG_MESSAGE_MAX = 180
LOG_RING_SIZE = 16
BINARY_CACHE_TTL_SEC = 5.0
NETWORK_CACHE_TTL_SEC = 60.0
NETWORK_DEFAULT = {"cloud": False, "tailscale_com": False, "pkgs_tailscale": False}
BINARIES = ("curl", "wget", "python3", "tailscale", "apt-get", "systemctl", "sudo", "git")

_SYSTEM_FACTS_CACHE = None
_BINARY_CACHE = None
_BINARY_CACHE_AT = 0.0
_NETWORK_CACHE = {}
_DEFAULT_DIAGNOSTICS = None
_COLLECT_FAILURE_LOGGED = False

log = logging.getLogger("makeros-hub.diagnostics")

_SECRET_KEYS = (
    "authkey",
    "auth_key",
    "auth-key",
    "auth key",
    "authorization",
    "bearer",
    "accesscode",
    "access_code",
    "access-code",
    "access code",
    "apikey",
    "api_key",
    "api-key",
    "api key",
    "x-api-key",
    "password",
    "passwd",
    "secret",
    "token",
)
_SPACE_VALUE_KEYS = (
    "authkey",
    "auth_key",
    "auth-key",
    "auth key",
    "authorization",
    "bearer",
    "accesscode",
    "access_code",
    "access-code",
    "access code",
    "apikey",
    "api_key",
    "api-key",
    "api key",
    "x-api-key",
    "password",
    "passwd",
    "secret",
)

_BAMBU_USERINFO_RE = re.compile(r"(?i)(\bbblp:)([^@\s/]+)(@)")
_ACCESS_CODE_VALUE_RE = re.compile(
    r"(?i)(\baccess[_ -]?code\b)(\s*(?:=|:|\bis\b)\s*)"
    r"(['\"]?)(\[redacted\]|[^'\"\s,.;\]\}>\)]+)(['\"]?)"
)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _is_value_delimiter(ch: str) -> bool:
    return ch.isspace() or ch in ",;]}>)"


def _redact_range(text: str, start: int, end: int) -> str:
    if start >= end:
        return text
    return text[:start] + "[redacted]" + text[end:]


def _already_redacted_at(text: str, start: int) -> bool:
    return text[start:].lower().startswith("[redacted]")


def _looks_secret_value(value: str, key: str) -> bool:
    stripped = value.strip().strip("'\"()[]{}<>,;")
    lower = stripped.lower()
    if not stripped:
        return False
    if key in ("bearer", "authorization"):
        return True
    if lower.startswith("tskey-") or lower.startswith("tskey_"):
        return True
    if len(stripped) >= 8:
        return True
    return False


def _redact_after_key(text: str, key: str, allow_space_value: bool) -> str:
    lower = text.lower()
    pos = 0
    key_l = key.lower()
    while True:
        idx = lower.find(key_l, pos)
        if idx < 0:
            return text
        before = lower[idx - 1] if idx > 0 else ""
        after_idx = idx + len(key_l)
        after = lower[after_idx] if after_idx < len(lower) else ""
        if (before and (before.isalnum() or before in "_-")) or (
            after and (after.isalnum() or after in "_-")
        ):
            pos = after_idx
            continue

        cursor = after_idx
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        had_separator = False
        if cursor < len(text) and text[cursor] in "=:":
            had_separator = True
            cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
        elif not allow_space_value:
            pos = after_idx
            continue

        if cursor < len(text) and text[cursor] in ("'", '"'):
            quote = text[cursor]
            value_start = cursor + 1
            value_end = value_start
            while value_end < len(text) and text[value_end] != quote:
                value_end += 1
            if value_start < value_end:
                if _already_redacted_at(text, value_start):
                    pos = value_start + len("[redacted]")
                    continue
                if not had_separator and not _looks_secret_value(text[value_start:value_end], key_l):
                    pos = after_idx
                    continue
                text = _redact_range(text, value_start, value_end)
                lower = text.lower()
                pos = value_start + len("[redacted]")
                continue
        else:
            value_start = cursor
            value_end = value_start
            if had_separator and lower[value_start:].startswith("bearer "):
                value_start += len("bearer ")
                value_end = value_start
            while value_end < len(text) and not _is_value_delimiter(text[value_end]):
                value_end += 1
            if value_start < value_end:
                if _already_redacted_at(text, value_start):
                    pos = value_start + len("[redacted]")
                    continue
                if not had_separator and not _looks_secret_value(text[value_start:value_end], key_l):
                    pos = after_idx
                    continue
                text = _redact_range(text, value_start, value_end)
                lower = text.lower()
                pos = value_start + len("[redacted]")
                continue
        pos = after_idx


def _redact_obvious_tokens(text: str) -> str:
    out = []
    cursor = 0
    while cursor < len(text):
        if text[cursor].isspace():
            out.append(text[cursor])
            cursor += 1
            continue
        start = cursor
        while cursor < len(text) and not text[cursor].isspace():
            cursor += 1
        token = text[start:cursor]
        stripped = token.strip("'\"()[]{}<>,;")
        lower = stripped.lower()
        if lower.startswith("tskey-") or lower.startswith("tskey_"):
            out.append(token.replace(stripped, "[redacted]"))
        else:
            out.append(token)
    return "".join(out)


def _redact_bambu_userinfo(text: str) -> str:
    def replace(match: re.Match) -> str:
        if match.group(2).lower() == "[redacted]":
            return match.group(0)
        return f"{match.group(1)}[redacted]{match.group(3)}"

    return _BAMBU_USERINFO_RE.sub(replace, text)


def _redact_access_code_values(text: str) -> str:
    def replace(match: re.Match) -> str:
        if match.group(4).lower() == "[redacted]":
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{match.group(3)}[redacted]{match.group(5)}"

    return _ACCESS_CODE_VALUE_RE.sub(replace, text)


def redact(text, extra_secrets=None) -> str:
    """Redact tokens and values that look like auth keys, bearers, or access codes."""
    safe = "" if text is None else str(text)
    safe = _redact_bambu_userinfo(safe)
    safe = _redact_access_code_values(safe)
    for secret in extra_secrets or ():
        if secret:
            safe = safe.replace(str(secret), "[redacted]")
    safe = _redact_obvious_tokens(safe)
    for key in _SECRET_KEYS:
        safe = _redact_after_key(safe, key, key in _SPACE_VALUE_KEYS)
    return safe


class SubsystemErrorRing:
    """Sticky last-error store keyed by subsystem.

    A subsystem's value is only replaced by a newer error for that same
    subsystem. Successful reads elsewhere never clear or overwrite it.
    """

    def __init__(self, max_per_subsystem: int = 1):
        self.max_per_subsystem = max(1, int(max_per_subsystem))
        self._errors = {name: [] for name in SUBSYSTEMS}

    def record(self, subsystem: str, message, extra_secrets=None) -> None:
        if subsystem not in self._errors:
            return
        clean = _truncate(
            redact(message, extra_secrets=extra_secrets).strip() or "unknown error",
            ERROR_MESSAGE_MAX,
        )
        entries = self._errors[subsystem]
        entries.append({"subsystem": subsystem, "message": clean, "ts": int(time.time())})
        del entries[: max(0, len(entries) - self.max_per_subsystem)]

    def snapshot(self) -> dict:
        out = {}
        for subsystem, entries in self._errors.items():
            if not entries:
                continue
            last = entries[-1]
            out[subsystem] = {"message": last["message"], "ts": last["ts"]}
        return out


class LogRingHandler(logging.Handler):
    """Root logger handler that keeps a small redacted WARNING/ERROR ring."""

    def __init__(self, capacity: int = LOG_RING_SIZE):
        super().__init__(level=logging.WARNING)
        self.capacity = max(1, int(capacity))
        self._entries = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging handlers must not raise
            message = "log formatting failed"
        entry = {
            "level": record.levelname,
            "name": record.name,
            "message": _truncate(redact(message).strip(), LOG_MESSAGE_MAX),
            "ts": int(time.time()),
        }
        self._entries.append(entry)
        del self._entries[: max(0, len(self._entries) - self.capacity)]

    def snapshot(self) -> list[dict]:
        return [dict(entry) for entry in self._entries]


def binary_presence() -> dict:
    global _BINARY_CACHE, _BINARY_CACHE_AT
    now = time.monotonic()
    if _BINARY_CACHE is not None and now - _BINARY_CACHE_AT < BINARY_CACHE_TTL_SEC:
        return dict(_BINARY_CACHE)
    _BINARY_CACHE = {name: shutil.which(name) is not None for name in BINARIES}
    _BINARY_CACHE_AT = now
    return dict(_BINARY_CACHE)


def _host_from_url(url) -> str | None:
    if not url:
        url = os.environ.get("MAKEROS_HUB_CLOUD_URL")
    if not url:
        return None
    value = str(url).strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("@")[-1]
    if value.startswith("[") and "]" in value:
        return value[1:].split("]", 1)[0] or None
    return value.split(":", 1)[0] or None


def _tcp_reachable(host: str | None, timeout: float) -> bool:
    if not host:
        return False
    conn = None
    try:
        conn = socket.create_connection((host, 443), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - diagnostics are best-effort booleans
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


def network_reachability(timeout=4, cloud_url=None) -> dict:
    now = time.monotonic()
    cloud_host = _host_from_url(cloud_url)
    cache_key = (cloud_host, float(timeout))
    cached = _NETWORK_CACHE.get(cache_key)
    if cached and now - cached["at"] < NETWORK_CACHE_TTL_SEC:
        return dict(cached["value"])
    probe_timeout = max(0.1, float(timeout))
    value = {
        "cloud": _tcp_reachable(cloud_host, probe_timeout),
        "tailscale_com": _tcp_reachable("tailscale.com", probe_timeout),
        "pkgs_tailscale": _tcp_reachable("pkgs.tailscale.com", probe_timeout),
    }
    _NETWORK_CACHE[cache_key] = {"at": now, "value": value}
    return dict(value)


def _disk_free_pct() -> float | None:
    try:
        usage = shutil.disk_usage("/")
        if usage.total <= 0:
            return None
        return round((usage.free / usage.total) * 100, 1)
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return None


def _uptime_sec() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except Exception:  # noqa: BLE001 - best-effort fact
        return None


def _default_agent_version() -> str:
    try:
        from . import __version__
    except Exception:  # noqa: BLE001 - diagnostics must stay import-safe
        return ""
    return __version__


def system_facts(agent_version: str | None = None) -> dict:
    global _SYSTEM_FACTS_CACHE
    version = agent_version or os.environ.get("MAKEROS_HUB_AGENT_VERSION") or _default_agent_version()
    if _SYSTEM_FACTS_CACHE is not None and _SYSTEM_FACTS_CACHE.get("agentVersion") == version:
        return dict(_SYSTEM_FACTS_CACHE)
    _SYSTEM_FACTS_CACHE = {
        "os": f"{platform.system()} {platform.release()}".strip(),
        "kernel": platform.version(),
        "python": platform.python_version(),
        "agentVersion": version,
        "diskFreePct": _disk_free_pct(),
        "uptimeSec": _uptime_sec(),
    }
    return dict(_SYSTEM_FACTS_CACHE)


class Diagnostics:
    def __init__(self, *, cloud_url: str | None = None, agent_version: str | None = None, enable_network: bool = True):
        self.cloud_url = cloud_url
        self.agent_version = agent_version
        self.enable_network = enable_network
        self.errors = SubsystemErrorRing()
        self.log_handler = LogRingHandler()
        self._system_facts = system_facts(agent_version)

    def record(self, subsystem: str, message, extra_secrets=None) -> None:
        self.errors.record(subsystem, message, extra_secrets=extra_secrets)

    def _minimal_diagnostics(self) -> dict:
        try:
            last_errors = self.errors.snapshot()
        except Exception:  # noqa: BLE001 - diagnostics fallback must not raise
            last_errors = {}
        try:
            recent_log = self.log_handler.snapshot()
        except Exception:  # noqa: BLE001 - diagnostics fallback must not raise
            recent_log = []
        return {
            "systemFacts": dict(getattr(self, "_system_facts", {}) or {}),
            "network": dict(NETWORK_DEFAULT),
            "lastErrors": last_errors,
            "recentLog": recent_log,
        }

    def collect_cheap_diagnostics(self) -> dict:
        try:
            network = (
                network_reachability(timeout=0.5, cloud_url=self.cloud_url)
                if self.enable_network
                else dict(NETWORK_DEFAULT)
            )
            return {
                "systemFacts": dict(self._system_facts),
                "binaries": binary_presence(),
                "network": network,
                "lastErrors": self.errors.snapshot(),
                "recentLog": self.log_handler.snapshot(),
            }
        except Exception as exc:  # noqa: BLE001 - diagnostics must not sink heartbeat
            _log_collect_failure_once(exc)
            return self._minimal_diagnostics()


def _log_collect_failure_once(exc: Exception) -> None:
    global _COLLECT_FAILURE_LOGGED
    if _COLLECT_FAILURE_LOGGED:
        return
    _COLLECT_FAILURE_LOGGED = True
    try:
        log.warning("diagnostics collection failed: %s", redact(str(exc)))
    except Exception:  # noqa: BLE001 - failure logging is best-effort
        pass


def get_default() -> Diagnostics:
    global _DEFAULT_DIAGNOSTICS
    if _DEFAULT_DIAGNOSTICS is None:
        _DEFAULT_DIAGNOSTICS = Diagnostics(enable_network=False)
    return _DEFAULT_DIAGNOSTICS


def set_default(diagnostics: Diagnostics) -> None:
    global _DEFAULT_DIAGNOSTICS
    _DEFAULT_DIAGNOSTICS = diagnostics


def record(subsystem: str, message, extra_secrets=None) -> None:
    get_default().record(subsystem, message, extra_secrets=extra_secrets)


def collect_cheap_diagnostics(diagnostics: Diagnostics | None = None) -> dict:
    try:
        return (diagnostics or get_default()).collect_cheap_diagnostics()
    except Exception as exc:  # noqa: BLE001 - heartbeat callers must not crash
        _log_collect_failure_once(exc)
        return {}


def install_log_handler(diagnostics: Diagnostics | None = None) -> LogRingHandler:
    diag = diagnostics or get_default()
    root = logging.getLogger()
    if diag.log_handler not in root.handlers:
        root.addHandler(diag.log_handler)
    return diag.log_handler


def reset_caches() -> None:
    global _SYSTEM_FACTS_CACHE, _BINARY_CACHE, _BINARY_CACHE_AT, _NETWORK_CACHE, _COLLECT_FAILURE_LOGGED
    _SYSTEM_FACTS_CACHE = None
    _BINARY_CACHE = None
    _BINARY_CACHE_AT = 0.0
    _NETWORK_CACHE = {}
    _COLLECT_FAILURE_LOGGED = False
