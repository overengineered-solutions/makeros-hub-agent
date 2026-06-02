"""Config + credential storage for the hub agent.

Layout on the Pi:
  /etc/makeros-hub/config.toml      cloud_url + heartbeat interval (non-secret)
  /var/lib/makeros-hub/credential   the per-hub bearer credential, mode 0600

Everything is overridable by env (MAKEROS_HUB_*) so the systemd unit or a quick
manual run can point at a different cloud without editing files.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("MAKEROS_HUB_CONFIG", "/etc/makeros-hub/config.toml"))
CREDENTIAL_PATH = Path(
    os.environ.get("MAKEROS_HUB_CREDENTIAL", "/var/lib/makeros-hub/credential")
)
DEFAULT_HEARTBEAT_SEC = 30


@dataclass
class Config:
    cloud_url: str
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC

    @property
    def enroll_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/enroll"

    @property
    def heartbeat_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/heartbeat"


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
    return Config(cloud_url=cloud_url, heartbeat_sec=heartbeat)


def read_credential() -> str | None:
    if os.environ.get("MAKEROS_HUB_CREDENTIAL_VALUE"):
        return os.environ["MAKEROS_HUB_CREDENTIAL_VALUE"]
    if CREDENTIAL_PATH.exists():
        return CREDENTIAL_PATH.read_text(encoding="utf-8").strip() or None
    return None


def write_credential(credential: str) -> None:
    """Persist the per-hub bearer 0600. Never logged."""
    CREDENTIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start (don't briefly expose it world-readable).
    fd = os.open(str(CREDENTIAL_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(credential)
    os.chmod(CREDENTIAL_PATH, 0o600)
