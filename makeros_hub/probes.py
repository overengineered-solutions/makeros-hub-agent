"""On-demand read-only diagnostic probes.

The cloud may only name a probe from PROBES. Each subprocess-backed probe maps
to a fixed argv declared below, and run_probe always executes with shell=False.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from typing import Callable

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
# Callback the agent registers at startup so the camera-test probe can
# enumerate eligible camera-bearing printers without a tight module-import
# cycle (probes → manager would pull paho-mqtt into probes.py). The agent
# calls `set_camera_targets_provider(callable)`; the callable returns a
# fresh list[dict] each invocation — same shape as PrinterManager.camera_targets()
# (printerId, vendor, model, host, accessCode, …, cameraEnabled, displayName).
_CAMERA_TARGETS_PROVIDER: "Callable[[], list[dict]] | None" = None

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
    # "Test cameras now" — admin-triggered one-shot capture across every
    # eligible camera-bearing printer on this hub. Returns per-printer
    # {printerId, displayName, model, transport, ok, reason, stderrTail,
    # durationMs} as JSON in `output`. The cloud renders the table inline
    # in the admin hub card so the operator can see exactly which printers
    # are silently dropping frames and why ("Toggle Liveview ON", "Check
    # access code", "Network unreachable"). Caller-side capture is
    # parallel-bounded to overall_timeout=12s; this is the OUTER dispatcher
    # budget, set higher to leave headroom for the worker spin-up.
    "camera-test": ProbeSpec(
        (),
        timeout=20.0,
        max_output_bytes=64 * 1024,
        internal="camera-test",
    ),
}


def set_effective_config(cfg) -> None:
    global _EFFECTIVE_CONFIG
    _EFFECTIVE_CONFIG = cfg


def set_camera_targets_provider(provider: "Callable[[], list[dict]] | None") -> None:
    """Register the agent's PrinterManager.camera_targets-style callable so
    the camera-test probe can run capture attempts against the same printer
    list the heartbeat tick uses. Passing None unregisters (test cleanup)."""
    global _CAMERA_TARGETS_PROVIDER
    _CAMERA_TARGETS_PROVIDER = provider


def _run_camera_test_probe() -> str:
    """One-shot capture against every eligible camera-bearing printer this
    hub manages. Returns a JSON-encoded list of per-printer rows
    `{printerId, displayName, model, transport, ok, reason, stderrTail,
    durationMs}` so the cloud admin can render a table inline. Reasons mirror
    the heartbeat's no-frame contract ('liveview-off' / 'auth-fail' /
    'unreachable' / 'timeout' / 'no-ffmpeg' / 'bad-jpeg' / 'unknown' /
    'no-camera-source'). Capture runs in parallel with overall_timeout=12s
    so the OUTER probe budget (20s) covers worker spin-up + serialization.
    """
    # Local import — keeps probes.py import-cheap and avoids the
    # paho-mqtt drag at probes-module load time.
    from .printers.camera import capture_printer_frame_with_reason

    provider = _CAMERA_TARGETS_PROVIDER
    if provider is None:
        return json.dumps({"error": "camera-targets provider not registered", "rows": []})
    try:
        targets = list(provider() or [])
    except Exception as exc:  # noqa: BLE001 - probe must never crash
        return json.dumps({"error": redact(str(exc)), "rows": []})

    rows: list[dict] = []
    # Per-row capture: do them sequentially here for simplicity. The probe
    # already has a 20s outer budget; even 8 printers × 1.5s each fits.
    # Parallelizing would just steal ThreadPoolExecutor budget from the
    # main heartbeat tick if it overlapped — the operator clicks this
    # button infrequently.
    for t in targets:
        pid = t.get("printerId")
        if not isinstance(pid, str):
            continue
        start = time.monotonic()
        try:
            jpeg, reason, stderr_tail = capture_printer_frame_with_reason(t)
        except Exception as exc:  # noqa: BLE001 - per-row failure isolation
            jpeg = None
            reason = "unknown"
            stderr_tail = redact(str(exc))
        rows.append(
            {
                "printerId": pid,
                "displayName": t.get("displayName") or pid,
                "model": t.get("model") or "",
                "vendor": t.get("vendor") or "",
                "ok": bool(jpeg),
                "jpegBytes": len(jpeg) if jpeg else 0,
                "reason": reason if not jpeg else None,
                "stderrTail": stderr_tail if not jpeg else "",
                "durationMs": _duration_ms(start),
            }
        )
    return json.dumps({"rows": rows})


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
        elif spec.internal == "camera-test":
            raw_output = _run_camera_test_probe()
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
