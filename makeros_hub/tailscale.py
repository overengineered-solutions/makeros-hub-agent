"""Tailscale reconciliation for remote hub access.

The auth key is intentionally accepted only from config-down and passed to the
root setup helper on stdin. It is never included in argv, logs, status dicts, or
exceptions raised by this module.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Sequence

TAILSCALE_BIN = os.environ.get("MAKEROS_HUB_TAILSCALE_BIN", "tailscale")
TAILSCALE_SETUP_SCRIPT = os.environ.get(
    "MAKEROS_HUB_TAILSCALE_SETUP_SCRIPT",
    "/opt/makeros-hub/tailscale-setup.sh",
)

Runner = Callable[..., subprocess.CompletedProcess]


def subprocess_runner(argv: Sequence[str], *, input: bytes | None = None, timeout: int = 30):
    return subprocess.run(
        list(argv),
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def tailscale_binary_exists() -> bool:
    if os.path.isabs(TAILSCALE_BIN):
        return os.path.isfile(TAILSCALE_BIN) and os.access(TAILSCALE_BIN, os.X_OK)
    return shutil.which(TAILSCALE_BIN) is not None


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _sanitize(value: str, secret: str | None = None) -> str:
    safe = value.strip()
    if secret:
        safe = safe.replace(secret, "[redacted]")
    return safe


def _status(
    state: str,
    *,
    ip: str | None = None,
    hostname: str | None = None,
    reason: str | None = None,
) -> dict:
    return {
        "tailscaleIp": ip,
        "tailscaleHostname": hostname,
        "tailscaleStatus": state,
        "tailscaleStatusReason": reason,
    }


def _sanitize_status(status: dict, secret: str | None = None) -> dict:
    if not secret:
        return status
    clean = dict(status)
    if clean.get("tailscaleStatusReason"):
        clean["tailscaleStatusReason"] = _sanitize(str(clean["tailscaleStatusReason"]), secret)
    return clean


def _first_detail(prefix: str, result, secret: str | None = None) -> str:
    detail = (_decode(getattr(result, "stderr", None)) or _decode(getattr(result, "stdout", None))).strip()
    if not detail:
        return prefix
    first_line = detail.splitlines()[0].strip()
    return _sanitize(f"{prefix}: {first_line}", secret)


def _parse_ipv4s(output: str) -> list[str]:
    ips: list[str] = []
    for token in output.split():
        try:
            ip = ipaddress.ip_address(token)
        except ValueError:
            continue
        if ip.version == 4:
            ips.append(str(ip))
    return ips


def _parse_status_hostname(status_output: str, tailnet_ip: str | None = None) -> str | None:
    first_hostname: str | None = None
    for raw in status_output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ip = ipaddress.ip_address(parts[0])
        except ValueError:
            continue
        if ip.version != 4:
            continue
        if first_hostname is None:
            first_hostname = parts[1]
        if tailnet_ip and str(ip) == tailnet_ip:
            return parts[1]
    return first_hostname


def _host_matches(current: str | None, desired: str | None) -> bool:
    if not current or not desired:
        return False
    current = current.strip().rstrip(".")
    desired = desired.strip().rstrip(".")
    return current == desired or current.split(".", 1)[0] == desired.split(".", 1)[0]


def _desired_hostname(cfg: dict | None) -> str:
    hostname = cfg.get("hostname") if isinstance(cfg, dict) else None
    if isinstance(hostname, str) and hostname.strip():
        return hostname.strip()
    return socket.gethostname()


def _truthy_posture_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none")
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(value)


def _has_non_host_route(routes) -> bool:
    if not isinstance(routes, (list, tuple, set)):
        return False
    for route in routes:
        if not isinstance(route, str) or not route.strip():
            continue
        try:
            network = ipaddress.ip_network(route.strip(), strict=False)
        except ValueError:
            continue
        if network.version == 4 and network.prefixlen < 32:
            return True
        if network.version == 6 and network.prefixlen < 128:
            return True
    return False


def _status_json_has_posture_drift(data) -> bool:
    if not isinstance(data, dict):
        return False
    candidates = [data]
    for key in ("Self", "Prefs", "CurrentPrefs", "UserPrefs"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    route_keys = ("AdvertiseRoutes", "AdvertisedRoutes", "PrimaryRoutes")
    exit_node_keys = ("AdvertiseExitNode", "ExitNode", "ExitNodeID", "ExitNodeIP", "ExitNodeOption")
    ssh_keys = ("RunSSH", "SSHEnabled", "TailscaleSSH", "WantRunningSSH")
    for candidate in candidates:
        for key in route_keys:
            if _truthy_posture_value(candidate.get(key)):
                return True
        if _has_non_host_route(candidate.get("AllowedIPs")):
            return True
        for key in exit_node_keys:
            if _truthy_posture_value(candidate.get(key)):
                return True
        for key in ssh_keys:
            if _truthy_posture_value(candidate.get(key)):
                return True
    return False


def _safe_posture_drifted(runner: Runner = subprocess_runner) -> bool:
    try:
        result = runner([TAILSCALE_BIN, "status", "--json"], timeout=10)
    except Exception:  # noqa: BLE001 - posture check is best-effort
        return False
    if getattr(result, "returncode", 1) != 0:
        return False
    try:
        data = json.loads(_decode(getattr(result, "stdout", None)))
    except (TypeError, ValueError):
        return False
    return _status_json_has_posture_drift(data)


def current_tailscale_status(
    runner: Runner = subprocess_runner,
    *,
    enabled: bool = True,
    installed: bool | None = None,
) -> dict:
    """Read current Tailscale state without mutating the system."""
    if not enabled:
        if installed is None:
            installed = tailscale_binary_exists()
        if not installed:
            return {}
    try:
        ip_result = runner([TAILSCALE_BIN, "ip", "-4"], timeout=10)
        status_result = runner([TAILSCALE_BIN, "status"], timeout=10)
    except FileNotFoundError:
        return _status("disabled", reason="tailscale not installed")
    except Exception as exc:  # noqa: BLE001 - status is best-effort telemetry
        return _status("error", reason=_sanitize(str(exc)) or "tailscale status failed")

    ip_output = _decode(getattr(ip_result, "stdout", None)) if getattr(ip_result, "returncode", 1) == 0 else ""
    ips = _parse_ipv4s(ip_output)
    tailnet_ip = ips[0] if ips else None
    status_output = (
        _decode(getattr(status_result, "stdout", None))
        if getattr(status_result, "returncode", 1) == 0
        else ""
    )
    hostname = _parse_status_hostname(status_output, tailnet_ip)

    if tailnet_ip:
        reason = None
        if getattr(status_result, "returncode", 1) != 0:
            reason = _first_detail("tailscale status unavailable", status_result)
        return _status("connected", ip=tailnet_ip, hostname=hostname, reason=reason)

    if getattr(ip_result, "returncode", 1) != 0:
        return _status("disabled", reason=_first_detail("tailscale not connected", ip_result))

    return _status("disabled", hostname=hostname, reason="tailscale has no IPv4 address")


def reconcile_tailscale(cfg: dict | None, runner: Runner = subprocess_runner) -> dict:
    """Reconcile local Tailscale state to the config-down `tailscale` block."""
    enabled = bool(cfg.get("enabled")) if isinstance(cfg, dict) else False
    auth_key = cfg.get("authKey") if isinstance(cfg, dict) else None
    auth_key = auth_key if isinstance(auth_key, str) else None
    desired_hostname = _desired_hostname(cfg if isinstance(cfg, dict) else None)

    current = _sanitize_status(current_tailscale_status(runner), auth_key)
    if current.get("tailscaleStatus") == "error":
        return current

    if not enabled:
        if current.get("tailscaleStatus") == "connected":
            try:
                result = runner(
                    ["sudo", TAILSCALE_SETUP_SCRIPT, "down"],
                    timeout=60,
                )
            except Exception as exc:  # noqa: BLE001 - reconciliation must not sink heartbeat
                return _status("error", reason=_sanitize(str(exc), auth_key) or "tailscale down failed")
            if getattr(result, "returncode", 1) != 0:
                return _status("error", reason=_first_detail("tailscale down failed", result, auth_key))
        return _status("disabled")

    if current.get("tailscaleStatus") == "connected" and _host_matches(
        current.get("tailscaleHostname"),
        desired_hostname,
    ):
        if not _safe_posture_drifted(runner):
            return current

    if not auth_key:
        return _status("error", reason="tailscale authKey missing")

    try:
        result = runner(
            ["sudo", TAILSCALE_SETUP_SCRIPT, "up", "--hostname", desired_hostname],
            input=auth_key.encode("utf-8"),
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001 - reconciliation must not sink heartbeat
        return _status("error", reason=_sanitize(str(exc), auth_key) or "tailscale up failed")

    if getattr(result, "returncode", 1) != 0:
        return _status("error", reason=_first_detail("tailscale up failed", result, auth_key))

    after = _sanitize_status(current_tailscale_status(runner), auth_key)
    if after.get("tailscaleStatus") == "connected":
        return after
    if after.get("tailscaleStatus") == "error":
        return after
    return _status(
        "joining",
        ip=after.get("tailscaleIp"),
        hostname=after.get("tailscaleHostname") or desired_hostname,
        reason=after.get("tailscaleStatusReason"),
    )
