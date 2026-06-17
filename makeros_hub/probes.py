"""On-demand read-only diagnostic probes.

The cloud may only name a probe from PROBES. Each subprocess-backed probe maps
to a fixed argv declared below, and run_probe always executes with shell=False.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from .diagnostics import binary_presence, redact


@dataclass(frozen=True)
class ProbeSpec:
    argv: tuple[str, ...]
    timeout: float
    max_output_bytes: int
    internal: str | None = None


DEFAULT_TIMEOUT_SEC = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
_EFFECTIVE_CONFIG = None

PROBES: dict[str, ProbeSpec] = {
    "journalctl-tail": ProbeSpec(
        ("journalctl", "-u", "makeros-hub", "-n", "200", "--no-pager"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "tailscale-status": ProbeSpec(
        ("tailscale", "status"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "tailscale-status-json": ProbeSpec(
        ("tailscale", "status", "--json"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "which-binaries": ProbeSpec(
        (),
        timeout=1.0,
        max_output_bytes=16 * 1024,
        internal="which-binaries",
    ),
    "disk-free": ProbeSpec(
        ("df", "-h"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "ip-addr": ProbeSpec(
        ("ip", "addr"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "systemctl-status": ProbeSpec(
        ("systemctl", "status", "makeros-hub", "--no-pager", "-n", "50"),
        timeout=DEFAULT_TIMEOUT_SEC,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    ),
    "agent-config": ProbeSpec(
        (),
        timeout=1.0,
        max_output_bytes=16 * 1024,
        internal="agent-config",
    ),
    # "Scan now" — the admin-triggered LAN discovery sweep. Internal probe
    # (no subprocess) — it calls into makeros_hub.discovery.run_immediate_scan
    # and returns the discovered hits (Moonraker + Bambu) as a JSON-encoded
    # list in `output`. Bumped timeout: a sweep takes ~8s Moonraker + ~3s
    # SSDP wall-clock; the budget here is the OUTER probe deadline before
    # the dispatcher gives up on the result. 20s leaves headroom for jitter.
    # max_output_bytes sized to fit ~100 hits with full displayInfo blobs.
    "lan-scan": ProbeSpec(
        (),
        timeout=20.0,
        max_output_bytes=128 * 1024,
        internal="lan-scan",
    ),
}


def set_effective_config(cfg) -> None:
    global _EFFECTIVE_CONFIG
    _EFFECTIVE_CONFIG = cfg


def _duration_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _coerce_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _truncate_output(value: str, max_bytes: int) -> tuple[str, bool]:
    data = value.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return value, False
    if max_bytes <= 0:
        return "", True
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


def _safe_output(value, max_bytes: int) -> tuple[str, bool]:
    return _truncate_output(redact(_coerce_text(value)), max_bytes)


def _agent_config_snapshot() -> dict:
    from .config import CONFIG_PATH, CREDENTIAL_PATH, load_config, read_credential

    snapshot = {
        "configPath": str(CONFIG_PATH),
        "credentialPath": str(CREDENTIAL_PATH),
    }
    try:
        snapshot["credentialPresent"] = read_credential() is not None
    except Exception as exc:  # noqa: BLE001 - still return the non-secret config
        snapshot["credentialError"] = str(exc)
    try:
        cfg = _EFFECTIVE_CONFIG or load_config()
    except SystemExit as exc:
        snapshot["configError"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - config probe must not crash
        snapshot["configError"] = str(exc)
    else:
        snapshot.update(
            {
                "cloudUrl": cfg.cloud_url,
                "heartbeatSec": cfg.heartbeat_sec,
                "ingestPort": cfg.ingest_port,
                "maxUploadMb": cfg.max_upload_mb,
            }
        )
    return snapshot


def _run_internal_probe(name: str, spec: ProbeSpec, start: float) -> dict:
    try:
        if spec.internal == "which-binaries":
            raw_output = json.dumps(binary_presence(), sort_keys=True)
        elif spec.internal == "agent-config":
            raw_output = json.dumps(_agent_config_snapshot(), sort_keys=True)
        elif spec.internal == "lan-scan":
            # Forced fresh sweep, bypasses the periodic rate-limit. The same
            # hits are also CACHED so the next heartbeat ships them under
            # `discoveryHits` — no second sweep, the cloud sees the same set
            # twice (once via the probe rawOutput, once via the heartbeat
            # field). De-dup happens cloud-side.
            from . import discovery  # local import — keeps probes.py import-cheap

            raw_output = discovery.hits_to_json_for_probe(
                discovery.run_immediate_scan()
            )
        else:
            raise RuntimeError("unknown internal probe")
        output, truncated = _safe_output(raw_output, spec.max_output_bytes)
        return {
            "name": name,
            "status": "ok",
            "exitCode": 0,
            "output": output,
            "durationMs": _duration_ms(start),
            "truncated": truncated,
        }
    except Exception as exc:  # noqa: BLE001 - probes must never crash heartbeat
        return {
            "name": name,
            "status": "error",
            "exitCode": None,
            "error": redact(str(exc)),
            "output": "",
            "durationMs": _duration_ms(start),
            "truncated": False,
        }


def run_probe(name: str) -> dict:
    spec = PROBES.get(name)
    if spec is None:
        return {"name": name, "status": "rejected", "error": "unknown probe"}

    start = time.monotonic()
    if spec.internal:
        return _run_internal_probe(name, spec, start)

    try:
        completed = subprocess.run(
            list(spec.argv),
            shell=False,
            capture_output=True,
            timeout=spec.timeout,
            text=True,
        )
        output, truncated = _safe_output(
            _coerce_text(completed.stdout) + _coerce_text(completed.stderr),
            spec.max_output_bytes,
        )
        return {
            "name": name,
            "status": "ok" if completed.returncode == 0 else "error",
            "exitCode": completed.returncode,
            "output": output,
            "durationMs": _duration_ms(start),
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired as exc:
        output, truncated = _safe_output(
            _coerce_text(getattr(exc, "stdout", None)) + _coerce_text(getattr(exc, "stderr", None)),
            spec.max_output_bytes,
        )
        return {
            "name": name,
            "status": "timeout",
            "exitCode": None,
            "error": "probe timed out",
            "output": output,
            "durationMs": _duration_ms(start),
            "truncated": truncated,
        }
    except Exception as exc:  # noqa: BLE001 - probes must never crash heartbeat
        return {
            "name": name,
            "status": "error",
            "exitCode": None,
            "error": redact(str(exc)),
            "output": "",
            "durationMs": _duration_ms(start),
            "truncated": False,
        }
