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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONFIG_PATH = Path(os.environ.get("MAKEROS_HUB_CONFIG", "/etc/makeros-hub/config.toml"))
CREDENTIAL_PATH = Path(
    os.environ.get("MAKEROS_HUB_CREDENTIAL", "/var/lib/makeros-hub/credential")
)
SPOOL_DIR = Path(os.environ.get("MAKEROS_HUB_SPOOL_DIR", "/var/lib/makeros-hub/spool"))
DEFAULT_HEARTBEAT_SEC = 30
# Non-privileged port the OrcaSlicer ingest HTTP server binds on the LAN. The
# member points OrcaSlicer's Print Host at http://<hub>.local:<port>.
DEFAULT_INGEST_PORT = 8787
# Manual HTTP upload is buffered; 64 MiB leaves room for parsing on the shop box.
# FTPS uses its separate streaming limit.
DEFAULT_MAX_UPLOAD_MB = 64
VPRINTER_CODE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

log = logging.getLogger("makeros-hub.config")


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

    def __post_init__(self):
        self.cloud_url = validate_cloud_url(self.cloud_url)

    @property
    def enroll_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/enroll"

    @property
    def heartbeat_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/heartbeat"

    @property
    def config_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/config"

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
    def queue_status_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/queue-status"

    @property
    def vp_submit_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/vp-submit"


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
    return Config(
        cloud_url=cloud_url,
        heartbeat_sec=heartbeat,
        ingest_port=ingest_port,
        max_upload_mb=max_upload_mb,
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
