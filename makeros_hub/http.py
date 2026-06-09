"""Tiny stdlib HTTP-JSON transport with retries — the one place the agent talks
to the cloud. Abstracted so the PR-5 printer slice can swap in httpx without
touching the enroll/heartbeat logic. Responses are explicitly shape-checked
(Zod/Pydantic-parity on the device) rather than blindly trusted.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Response:
    status: int
    body: dict


class TransportError(Exception):
    pass


def post_json(
    url: str,
    payload: dict,
    *,
    bearer: str | None = None,
    timeout: float = 15.0,
    retries: int = 0,
    backoff_base: float = 1.0,
) -> Response:
    """POST JSON, parse a JSON object back. `retries` re-attempts on network /
    5xx with jittered exponential backoff (used by the heartbeat loop, not enroll).
    Raises TransportError on an unrecoverable failure."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _parse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            # 4xx are deterministic (bad token, revoked cred) — surface, don't retry.
            body = _safe_read(exc)
            if exc.code < 500 or attempt >= retries:
                return Response(status=exc.code, body=body)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= retries:
                raise TransportError(f"network error talking to {url}: {exc}") from exc
        attempt += 1
        # Jitter without importing random's global state concerns — coarse is fine.
        sleep_s = backoff_base * (2 ** (attempt - 1))
        sleep_s += (time.monotonic() % 1.0) * backoff_base  # cheap jitter
        time.sleep(min(sleep_s, 30.0))


def get_json(url: str, *, bearer: str | None = None, timeout: float = 15.0) -> Response:
    """GET JSON, parse a JSON object back. Used for config-down (the printer
    list + access codes). No retries — the heartbeat loop re-pulls on the next
    configVersion change anyway. Raises TransportError on a network failure."""
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _parse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return Response(status=exc.code, body=_safe_read(exc))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransportError(f"network error talking to {url}: {exc}") from exc


def _parse(status: int, raw: bytes) -> Response:
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TransportError(f"non-JSON response (status {status})") from exc
    if not isinstance(body, dict):
        raise TransportError(f"expected a JSON object, got {type(body).__name__}")
    return Response(status=status, body=body)


def _safe_read(exc: urllib.error.HTTPError) -> dict:
    try:
        raw = exc.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 — best-effort error-body parse
        return {}
