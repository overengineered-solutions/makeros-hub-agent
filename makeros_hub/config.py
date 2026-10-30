"""Config + credential storage for the hub agent.

Layout on the Pi:
  /etc/makeros-hub/config.toml      cloud_url + heartbeat interval (non-secret)
  /var/lib/makeros-hub/credential   the per-hub bearer credential, mode 0600

Everything is overridable by env (MAKEROS_HUB_*) so the systemd unit or a quick
manual run can point at a different cloud without editing files.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .laser import parse_laser_config

CONFIG_PATH = Path(os.environ.get("MAKEROS_HUB_CONFIG", "/etc/makeros-hub/config.toml"))
CREDENTIAL_PATH = Path(
    os.environ.get("MAKEROS_HUB_CREDENTIAL", "/var/lib/makeros-hub/credential")
)
SPOOL_DIR = Path(os.environ.get("MAKEROS_HUB_SPOOL_DIR", "/var/lib/makeros-hub/spool"))
DEFAULT_HEARTBEAT_SEC = 30
# v0.70: how often the telemetry push sends coalesced status deltas (docs/telemetry-v1.md); 0 turns the push off and the
# 30 s heartbeat carries status alone, as before. The site can move a running hub between 2 and 60 s with `nextMs`.
DEFAULT_TELEMETRY_SEC = 5
# Non-privileged port the OrcaSlicer ingest HTTP server binds on the LAN. The
# member points OrcaSlicer's Print Host at http://<hub>.local:<port>.
DEFAULT_INGEST_PORT = 8787
# Raw manual HTTP upload ceiling; intake streams to disk. FTPS has its own limit.
DEFAULT_MAX_UPLOAD_MB = 1024
DEFAULT_MOONRAKER_HOSTS = {
    "p2s-printers.oes-mesh.internal": "p2s",
    "x1c-printers.oes-mesh.internal": "x1c",
    "a1mini-printers.oes-mesh.internal": "a1-mini",
    "sv08-printers.oes-mesh.internal": "sv08-max",
    "giga-printers.oes-mesh.internal": "giga",
}
VPRINTER_CODE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

log = logging.getLogger("makeros-hub.config")


def moonraker_hostname(value: str) -> str:
    """A bounded DNS name from Host; IPs and malformed values use the fallback pool."""
    if not isinstance(value, str) or len(value) > 260:
        return ""
    name, separator, port = value.partition(":")
    if separator and (not re.fullmatch(r"[0-9]{1,5}", port) or not 0 < int(port) <= 65535):
        return ""
    name = name.removesuffix(".")
    if len(name) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                              for label in name.split(".")):
        return ""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name.lower()
    return ""


def validate_cloud_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("cloud URL must be an HTTPS origin")
    parts = urlsplit(value)
    local_http = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parts.scheme != "https" and not local_http) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("cloud URL must be an HTTPS origin (HTTP only for local tests)")
    return value.rstrip("/")


@dataclass
class Config:
    cloud_url: str
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC
    ingest_port: int = DEFAULT_INGEST_PORT
    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB
    # v0.68: the optional [laser] table (makeros_hub.laser.LaserConfig); None = every laser reports not_configured.
    laser: Any = None
    # v0.70: telemetry push cadence in seconds, 0 = off (last so positional construction of older callers is unchanged).
    telemetry_sec: int = DEFAULT_TELEMETRY_SEC
    moonraker_pool: str = "p2s"
    members_tailscale_socket: str = "/run/tailscale-members.sock"
    moonraker_hosts: dict[str, str] = field(default_factory=lambda: DEFAULT_MOONRAKER_HOSTS.copy())

    def __post_init__(self):
        self.cloud_url = validate_cloud_url(self.cloud_url)
        # Imported here, not at the top: printers -> vprinter imports this module, and a top-level import made
        # `makeros-hub run` die at start with a circular import (0.89.0, 2026-10-29). test_entry_points guards it.
        from .printers.ingest_facts import POOLS
        if not isinstance(self.moonraker_pool, str) or (self.moonraker_pool and self.moonraker_pool not in POOLS):
            log.error("moonraker_pool must name a known pool or be empty; Moonraker door disabled, member ingest remains available")
            self.moonraker_pool = ""
        raw_hosts = self.moonraker_hosts
        self.moonraker_hosts = {}
        if not isinstance(raw_hosts, dict):
            log.error("moonraker_hosts must be a table; ignored, moonraker_pool and member ingest remain available")
            return
        for host, pool in raw_hosts.items():
            name = moonraker_hostname(host)
            if not name or not isinstance(pool, str) or pool not in POOLS:
                log.error("moonraker_hosts entry must name a DNS host and a known pool; entry ignored")
                continue
            self.moonraker_hosts[name] = pool

    @property
    def enroll_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/enroll"

    @property
    def heartbeat_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/heartbeat"

    @property
    def telemetry_url(self) -> str:
        # v0.70: coalesced status deltas, beside the heartbeat (docs/telemetry-v1.md).
        return self.cloud_url.rstrip("/") + "/api/print/hub/telemetry"

    @property
    def config_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/config"

    def job_file_url(self, job_id: str) -> str:
        # v0.74: copy an OrcaSlicer send's file to the site so the job can move between boxes (makeros_hub/job_files.py)
        return self.cloud_url.rstrip("/") + "/api/print/hub/job-file/" + job_id

    @property
    def wait_url(self) -> str:
        # v0.73: the long-poll that ends the heartbeat sleep when the site has new work for this box (makeros_hub/wake.py)
        return self.cloud_url.rstrip("/") + "/api/print/hub/wait"

    @property
    def paper_url(self) -> str:
        # v0.73: members' paper prints (makeros_hub/paper.py): measurement in, the site's yes or no out, then the outcome
        return self.cloud_url.rstrip("/") + "/api/print/hub/paper"

    @property
    def submit_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/submit"

    @property
    def fetched_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/fetched"

    def file_url(self, path: str) -> str:
        """A cloud-relative download path from the heartbeat's `fetches` (never an absolute URL — the hub only ever
        talks to its own cloud)."""
        return self.cloud_url.rstrip("/") + "/" + str(path).lstrip("/")

    @property
    def camera_url(self) -> str:
        # Camera frames POST here as raw JPEG bytes, out-of-band from the heartbeat (whose body is size-capped).
        return self.cloud_url.rstrip("/") + "/api/print/hub/camera"

    @property
    def camera_live_url(self) -> str:
        """POST one live-view frame (v0.75, raw JPEG): the site keeps only the newest, in memory."""
        return self.cloud_url.rstrip("/") + "/api/print/hub/camera/live"

    @property
    def queue_status_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/queue-status"

    @property
    def vp_submit_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/vp-submit"

    @property
    def vp_staged_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/vp-staged"


@dataclass(frozen=True)
class VirtualPrinterMember:
    access_code_sha256: str
    member_id: str


@dataclass(frozen=True)
class VirtualPrinterConfig:
    enabled: bool
    serial: str
    model: str
    name: str
    fw: str
    bind_ip: str
    units: int
    trays: int
    ams_type: str
    members: tuple[VirtualPrinterMember, ...]
    pool: tuple[dict[str, Any], ...]
    # Live-mirrored observed hardware; omitted on older config-downs.
    nozzles: tuple[dict[str, Any], ...] = ()


def parse_virtual_printer_config(raw: Any) -> VirtualPrinterConfig | None:
    """Parse the optional config-down virtualPrinter/virtual_printer block.

    Shape errors disable the VP for this heartbeat rather than crashing the
    agent. Access-code hashes are intentionally never included in log messages.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning("virtual_printer config ignored: expected object")
        return None
    if raw.get("enabled") is not True:
        return None

    required = {
        "serial": _required_str(raw, "serial"),
        "model": _required_str(raw, "model"),
        "name": _required_str(raw, "name"),
        "fw": _required_str(raw, "fw"),
        "bind_ip": _required_str(raw, "bind_ip", "bindIp"),
    }
    missing = [key for key, value in required.items() if value is None]
    if missing:
        log.warning("virtual_printer config ignored: missing/invalid %s", ",".join(missing))
        return None

    bind_ip = required["bind_ip"]
    if not _is_ipv4(bind_ip):
        log.warning("virtual_printer config ignored: bind_ip must be IPv4")
        return None

    # Real Bambu hardware tops out at 4 AMS units (X1C/P1S) = 16 slots, but a
    # per-model VP deduping across a large fleet (e.g. 16 P2S each with an AMS)
    # can need more than 16 unique filament slots. units max is raised to 8 (32
    # slots) to PROBE whether stock OrcaSlicer renders >4 AMS units; trays stays
    # 4 because a physical AMS unit is always 4 slots. Default stays 4, so this
    # is inert until a VP's `units` is explicitly set higher.
    units = _positive_int(_get_field(raw, "units", default=4), default=4, maximum=8)
    trays = _positive_int(_get_field(raw, "trays", default=4), default=4, maximum=4)
    if units is None or trays is None:
        log.warning("virtual_printer config ignored: units/trays must be positive integers")
        return None

    ams_type = _get_field(raw, "ams_type", "amsType", default="n3f")
    if not isinstance(ams_type, str) or ams_type not in {"n3f", "n3s", "ams"}:
        log.warning("virtual_printer config ignored: ams_type is invalid")
        return None

    members = _parse_vprinter_members(raw.get("members"))
    if not members:
        log.warning("virtual_printer config ignored: members must include at least one valid entry")
        return None

    pool = _parse_vprinter_pool(raw.get("pool", []))
    if pool is None:
        log.warning("virtual_printer config ignored: pool must be a list of tray objects")
        return None

    return VirtualPrinterConfig(
        enabled=True,
        serial=required["serial"] or "",
        model=required["model"] or "",
        name=required["name"] or "",
        fw=required["fw"] or "",
        bind_ip=bind_ip or "",
        units=units,
        trays=trays,
        ams_type=ams_type,
        members=tuple(members),
        pool=tuple(pool),
    )


def _required_str(raw: dict[str, Any], key: str, camel_key: str | None = None) -> str | None:
    value = _get_field(raw, key, camel_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _get_field(
    raw: dict[str, Any],
    key: str,
    camel_key: str | None = None,
    *,
    default: Any = None,
) -> Any:
    if key in raw:
        return raw[key]
    if camel_key is not None and camel_key in raw:
        return raw[camel_key]
    return default


def _positive_int(value: Any, *, default: int, maximum: int) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > maximum:
        return None
    return parsed


def _is_ipv4(value: str | None) -> bool:
    if not value:
        return False
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return True


def _parse_vprinter_members(raw: Any) -> list[VirtualPrinterMember]:
    if not isinstance(raw, list):
        return []
    members: list[VirtualPrinterMember] = []
    seen_hashes: set[str] = set()
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        raw_code_hash = _get_field(item, "access_code_sha256", "accessCodeSha256")
        raw_member_id = _get_field(item, "member_id", "memberId")
        if not (isinstance(raw_code_hash, str) and isinstance(raw_member_id, str)):
            dropped += 1
            continue
        access_code_sha256 = raw_code_hash.strip().lower()
        member_id = raw_member_id.strip()
        if (
            not access_code_sha256
            or not member_id
            or not VPRINTER_CODE_HASH_RE.fullmatch(access_code_sha256)
        ):
            dropped += 1
            continue
        if access_code_sha256 in seen_hashes:
            dropped += 1
            continue
        seen_hashes.add(access_code_sha256)
        members.append(
            VirtualPrinterMember(access_code_sha256=access_code_sha256, member_id=member_id)
        )
    if dropped:
        suffix = "y" if dropped == 1 else "ies"
        log.warning("virtual_printer config dropped %d invalid member entr%s", dropped, suffix)
    return members


def _parse_vprinter_pool(raw: Any) -> list[dict[str, Any]] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    pool: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tray = copy.deepcopy(item)
        material = tray.get("material") or tray.get("tray_type")
        if isinstance(material, str) and material.strip():
            tray["tray_type"] = material.strip()
        color = tray.get("color") or tray.get("tray_color")
        if isinstance(color, str) and color.strip():
            tray["tray_color"] = _normalize_vprinter_color(color)
            tray.setdefault("cols", [tray["tray_color"]])
        tray.setdefault("tray_info_idx", tray.get("filament_id") or tray.get("material_code") or "")
        tray.setdefault("tray_sub_brands", tray.get("productName") or tray.get("product_name") or "")
        tray.setdefault("nozzle_temp_min", str(tray.get("nozzle_temp_min", "190")))
        tray.setdefault("nozzle_temp_max", str(tray.get("nozzle_temp_max", "230")))
        tray.setdefault("remain", tray.get("remain", -1))
        tray.setdefault("tag_uid", tray.get("tag_uid", "0000000000000000"))
        tray.setdefault("tray_uuid", tray.get("tray_uuid", "00000000000000000000000000000000"))
        pool.append(tray)
    return pool


def _normalize_vprinter_color(value: str) -> str:
    cleaned = value.strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(ch in "0123456789ABCDEF" for ch in cleaned):
        return cleaned + "FF"
    if len(cleaned) == 8 and all(ch in "0123456789ABCDEF" for ch in cleaned):
        return cleaned
    return "FFFFFFFF"


def load_config(cloud_url_override: str | None = None) -> Config:
    """Resolve config from (precedence): explicit arg > env > config.toml."""
    data: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    cloud_url = (
        cloud_url_override
        or os.environ.get("MAKEROS_HUB_CLOUD_URL")
        or data.get("cloud_url")
    )
    if not cloud_url:
        raise SystemExit(
            "No cloud URL. Pass --cloud-url, set MAKEROS_HUB_CLOUD_URL, or add "
            f"cloud_url to {CONFIG_PATH}."
        )
    heartbeat = int(
        os.environ.get("MAKEROS_HUB_HEARTBEAT_SEC")
        or data.get("heartbeat_sec")
        or DEFAULT_HEARTBEAT_SEC
    )
    ingest_port = int(
        os.environ.get("MAKEROS_HUB_INGEST_PORT")
        or data.get("ingest_port")
        or DEFAULT_INGEST_PORT
    )
    max_upload_mb = int(
        os.environ.get("MAKEROS_HUB_MAX_UPLOAD_MB")
        or data.get("max_upload_mb")
        or DEFAULT_MAX_UPLOAD_MB
    )
    # v0.70: not the `or` chain above — "0" is the off switch and must survive the parse (env, then config.toml).
    raw_telemetry = os.environ.get("MAKEROS_HUB_TELEMETRY_SEC")
    if raw_telemetry is None or not raw_telemetry.strip():
        raw_telemetry = data.get("telemetry_sec", DEFAULT_TELEMETRY_SEC)
    telemetry_sec = max(0, int(raw_telemetry))
    try:
        laser = parse_laser_config(data.get("laser"))
    except ValueError as exc:
        # A bad [laser] table never stops the printers: lasers run not_configured and the reason is logged.
        log.error("%s; lasers stay not configured until it is fixed", exc)
        laser = None
    return Config(
        cloud_url=cloud_url,
        heartbeat_sec=heartbeat,
        ingest_port=ingest_port,
        max_upload_mb=max_upload_mb,
        laser=laser,
        telemetry_sec=telemetry_sec,
        moonraker_pool=data.get("moonraker_pool", "p2s"),
        members_tailscale_socket=data.get("members_tailscale_socket", "/run/tailscale-members.sock"),
        moonraker_hosts=data.get("moonraker_hosts", DEFAULT_MOONRAKER_HOSTS),
    )


def persist_cloud_url(cloud_url: str) -> None:
    """Write `cloud_url` into config.toml so the heartbeat loop targets the host
    the operator enrolled with — not the template placeholder. Called after a
    successful enroll.

    Stdlib-only: a minimal line rewrite (tomllib is read-only and we don't pull
    a TOML writer into a zero-dep agent). Replaces an existing top-level
    `cloud_url = …` line (commented lines don't match) or appends one; other
    lines (heartbeat_sec, comments) are preserved.
    """
    line = f"cloud_url = {json.dumps(validate_cloud_url(cloud_url))}\n"
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    out: list[str] = []
    replaced = False
    in_table = False
    for ln in raw.splitlines(keepends=True):
        if ln.lstrip().startswith("["):
            in_table = True
        if not in_table and re.match(r"\s*cloud_url\s*=", ln):
            if replaced:
                raise ValueError("duplicate cloud_url in config")
            out.append(line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.insert(0, line)
    content = "".join(out)
    tomllib.loads(content)  # refuse a broken configuration before replacing it
    fd, name = tempfile.mkstemp(prefix=".config-", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def read_credential() -> str | None:
    if os.environ.get("MAKEROS_HUB_CREDENTIAL_VALUE"):
        return os.environ["MAKEROS_HUB_CREDENTIAL_VALUE"]
    if CREDENTIAL_PATH.exists():
        return CREDENTIAL_PATH.read_text(encoding="utf-8").strip() or None
    return None


def write_credential(credential: str) -> None:
    """Persist the per-hub bearer 0600. Never logged."""
    if not isinstance(credential, str) or not credential.strip() or credential != credential.strip():
        raise ValueError("refusing an empty or padded hub credential")
    CREDENTIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp starts private; a failed write must leave the working credential.
    fd, name = tempfile.mkstemp(prefix=".credential-", dir=CREDENTIAL_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(credential)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, CREDENTIAL_PATH)
        directory_fd = os.open(CREDENTIAL_PATH.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(name).unlink(missing_ok=True)
