"""Config + credential storage for the hub agent.

Layout on the Pi:
  /etc/makeros-hub/config.toml      cloud_url + heartbeat interval (non-secret)
  /var/lib/makeros-hub/credential   the per-hub bearer credential, mode 0600

Everything is overridable by env (MAKEROS_HUB_*) so the systemd unit or a quick
manual run can point at a different cloud without editing files.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("MAKEROS_HUB_CONFIG", "/etc/makeros-hub/config.toml"))
CREDENTIAL_PATH = Path(
    os.environ.get("MAKEROS_HUB_CREDENTIAL", "/var/lib/makeros-hub/credential")
)
SPOOL_DIR = Path(os.environ.get("MAKEROS_HUB_SPOOL_DIR", "/var/lib/makeros-hub/spool"))
DEFAULT_HEARTBEAT_SEC = 30
# Non-privileged port the OrcaSlicer ingest HTTP server binds on the LAN. The
# member points OrcaSlicer's Print Host at http://<hub>.local:<port>.
DEFAULT_INGEST_PORT = 8787
# Cap a single sliced-file upload (sliced 3MFs run a few MB; 256MB is a generous
# guard against a runaway/abusive upload OOMing the Pi).
DEFAULT_MAX_UPLOAD_MB = 256


@dataclass
class Config:
    cloud_url: str
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC
    ingest_port: int = DEFAULT_INGEST_PORT
    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB

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
    def queue_status_url(self) -> str:
        return self.cloud_url.rstrip("/") + "/api/print/hub/queue-status"


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
    line = f'cloud_url = "{cloud_url}"\n'
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(line, encoding="utf-8")
        return
    out: list[str] = []
    replaced = False
    for ln in CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
        if not replaced and re.match(r"\s*cloud_url\s*=", ln):
            out.append(line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(line)
    CONFIG_PATH.write_text("".join(out), encoding="utf-8")


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
