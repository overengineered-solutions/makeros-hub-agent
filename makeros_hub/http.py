"""Tiny stdlib HTTP-JSON transport with retries — the one place the agent talks
to the cloud. Abstracted so the PR-5 printer slice can swap in httpx without
touching the enroll/heartbeat logic. Responses are explicitly shape-checked
(Zod/Pydantic-parity on the device) rather than blindly trusted.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# Cap every parsed response body (config-down included) so a bad/hostile cloud or proxy can't force the Pi to
# buffer an unbounded body into memory. 8 MiB is far above any real config-down / heartbeat response.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# CSPRNG for backoff jitter — a shared PRNG seed across agents started together (e.g. after a cloud deploy) would
# leave them synchronized and thundering-herd the endpoint. (dual-review 2026-07-20.)
_RNG = random.SystemRandom()


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
                return _parse(resp.status, _read_capped(resp))
        except urllib.error.HTTPError as exc:
            # 4xx are deterministic (bad token, revoked cred) — surface, don't retry.
            body = _safe_read(exc)
            if exc.code < 500 or attempt >= retries:
                return Response(status=exc.code, body=body)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= retries:
                raise TransportError(f"network error talking to {url}: {exc}") from exc
        attempt += 1
        # Exponential backoff + FULL CSPRNG jitter so simultaneously-restarted agents de-synchronize.
        sleep_s = backoff_base * (2 ** (attempt - 1))
        sleep_s += _RNG.uniform(0.0, backoff_base)
        time.sleep(min(sleep_s, 30.0))


def post_bytes(
    url: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    bearer: str | None = None,
    timeout: float = 15.0,
) -> int:
    """POST raw bytes (a camera JPEG frame) and return the HTTP status. No JSON body/parse — the frame rides its OWN
    request, out-of-band from the heartbeat (whose body is size-capped on the cloud). Raises TransportError only on a
    network failure; callers are best-effort (a failed frame push must never sink the heartbeat)."""
    headers = {"Content-Type": content_type}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _read_capped(resp)  # drain (bounded) so the socket can be reused
            return resp.status
    except urllib.error.HTTPError as exc:
        _safe_read(exc)
        return exc.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransportError(f"network error posting bytes to {url}: {exc}") from exc


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
            return _parse(resp.status, _read_capped(resp))
    except urllib.error.HTTPError as exc:
        return Response(status=exc.code, body=_safe_read(exc))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransportError(f"network error talking to {url}: {exc}") from exc


def _read_capped(resp) -> bytes:
    """Read at most MAX_RESPONSE_BYTES; refuse a larger body rather than buffering it into memory."""
    raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TransportError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
    return raw


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
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 — best-effort error-body parse
        return {}


def get_to_file(url: str, dest, *, bearer: str | None = None, timeout: float = 60.0, max_bytes: int = 64 * 1024 * 1024,
                max_seconds: float = 300.0) -> tuple[str, int]:
    """GET a (web-upload) file into `dest`, streaming with a hard size cap AND a wall-clock budget for the whole transfer
    (`timeout` is only per-read inactivity — a dripping 200 would otherwise never end; codex v0.57 r1); returns
    (sha256, size). Raises TransportError on a network failure, a non-200, the cap or the deadline; the caller verifies the
    sha against the cloud's before trusting the bytes and removes `dest` on any failure."""
    import hashlib
    from pathlib import Path

    headers = {"Accept": "application/octet-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, Path(dest).open("wb") as out:
            if resp.status != 200:
                raise TransportError(f"GET {url}: HTTP {resp.status}")
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise TransportError(f"GET {url}: body exceeds {max_bytes} bytes")
                if time.monotonic() - started > max_seconds:
                    raise TransportError(f"GET {url}: transfer exceeded {max_seconds:.0f}s")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            import os as _os
            _os.fsync(out.fileno())   # crash-durable before the caller renames + reports (codex v0.57 r2)
    except urllib.error.HTTPError as exc:
        raise TransportError(f"GET {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransportError(f"network error fetching {url}: {exc}") from exc
    return digest.hexdigest(), size
