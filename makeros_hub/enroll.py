"""One-time enrollment: exchange the admin-minted token for a durable per-hub
bearer credential, written to disk 0600. The token and credential are NEVER
printed (only the resulting hubId is)."""

from __future__ import annotations

import platform
import socket

from . import __version__
from .config import Config, read_credential, write_credential
from .http import post_json


def agent_meta() -> dict:
    return {
        "version": __version__,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
    }


def enroll(cfg: Config, token: str, *, force: bool = False) -> str:
    """Returns the hubId on success. Raises SystemExit with a directional
    message on failure (so the operator knows the next concrete step)."""
    if read_credential() and not force:
        raise SystemExit(
            "This hub already has a credential. It's already enrolled — just start "
            "the service (`sudo systemctl enable --now makeros-hub`). To re-enroll "
            "with a fresh token, pass --force (this overwrites the existing credential)."
        )

    resp = post_json(cfg.enroll_url, {"token": token, "agent": agent_meta()})

    if resp.status == 200:
        credential = resp.body.get("credential")
        hub_id = resp.body.get("hubId")
        if not isinstance(credential, str) or not isinstance(hub_id, str):
            raise SystemExit(
                "Enroll succeeded but the response was missing the credential/hubId — "
                "the cloud contract may have changed. Check /admin/observability."
            )
        write_credential(credential)
        return hub_id

    reason = resp.body.get("error", f"http_{resp.status}")
    hints = {
        "token_invalid": "That token isn't recognized. Mint a fresh one at "
        "/admin/3dprinting/hubs and copy it exactly.",
        "token_expired": "That token expired (15-min window). Mint a fresh one.",
        "token_consumed": "That token was already used. Mint a fresh one — each "
        "token enrolls exactly one hub.",
        "payload_shape_mismatch": "The cloud rejected the request shape — update "
        "the agent.",
    }
    raise SystemExit(f"Enrollment failed ({reason}). {hints.get(reason, '')}".strip())
