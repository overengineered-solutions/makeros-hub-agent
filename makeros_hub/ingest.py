"""OctoPrint-compatible ingest server — the LAN HTTP endpoint OrcaSlicer's
"Send" targets. Runs on the hub alongside the heartbeat loop (own thread).

OrcaSlicer (PrusaSlicer's OctoPrint host) does exactly two things:
  GET  /api/version        — connection test; must return JSON whose `text`
                             starts with "OctoPrint" and that has an `api` key.
  POST /api/files/local    — multipart upload (fields: file, print, path) with
                             the member's print token in `X-Api-Key`.

On upload the hub: saves the sliced file to a local spool dir (the file NEVER
goes to the cloud), then registers the submission with the cloud control plane
(POST /api/print/hub/submit, authenticated by the hub's own bearer + the
member token). The cloud resolves the token → member, runs the eligibility
gate, and creates the queue job; the member watches it in their portal.

The cloud-submit call is injected (`submit_fn`) so the server is unit-testable
without a network or a real cloud.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .multipart import boundary_from_content_type, parse_multipart

log = logging.getLogger("makeros-hub.ingest")

# The `text` MUST start with "OctoPrint" or the slicer rejects the host.
VERSION_BODY = {
    "api": "0.1",
    "server": "1.10.0",
    "text": "OctoPrint 1.10.0 (MakerOS Hub)",
}

# A submit_fn returns one of these outcome dicts.
#   {"status": "queued",   "jobId": "..."}     -> 201
#   {"status": "rejected", "reason": "..."}    -> 201 (upload ok; portal shows why)
#   {"status": "bad_token"}                    -> 403 (OrcaSlicer "invalid API key")
#   {"status": "error",    "detail": "..."}    -> 502
SubmitFn = Callable[..., dict]

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "upload.bin")
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload.bin"
    return cleaned[:200]


class _Handler(BaseHTTPRequestHandler):
    # Injected by the server factory.
    submit_fn: SubmitFn
    spool_dir: Path
    max_bytes: int

    # Quiet the default stderr logging; route through our logger at debug.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("ingest %s - " + fmt, self.address_string(), *args)

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/api/version", "/api/version".rstrip("/")):
            self._json(200, VERSION_BODY)
            return
        if self.path == "/" or self.path == "/healthz":
            self._json(200, {"ok": True, "service": "makeros-hub-ingest"})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/api/files/local":
            self._json(404, {"error": "not_found"})
            return

        member_token = self.headers.get("X-Api-Key", "").strip()
        if not member_token:
            self._json(403, {"error": "missing_api_key"})
            return

        boundary = boundary_from_content_type(self.headers.get("Content-Type"))
        if not boundary:
            self._json(400, {"error": "expected_multipart"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "empty_body"})
            return
        if length > self.max_bytes:
            self._json(413, {"error": "file_too_large", "maxBytes": self.max_bytes})
            return

        body = self._read_exact(length)
        if body is None:
            self._json(400, {"error": "truncated_body"})
            return

        try:
            form = parse_multipart(body, boundary)
        except Exception as exc:  # noqa: BLE001 — surface a clean 400, log detail
            log.warning("multipart parse failed: %s", exc)
            self._json(400, {"error": "bad_multipart"})
            return
        if form.file is None:
            self._json(400, {"error": "no_file_field"})
            return

        file_name = _safe_filename(form.file.filename)
        print_now = (form.fields.get("print", "false").lower() == "true")
        submission_uid = os.urandom(16).hex()
        sha = hashlib.sha256(form.file.data).hexdigest()
        size = len(form.file.data)

        # Persist the sliced file hub-local before registering, so a job never
        # exists in the cloud without its file on disk.
        dest_dir = self.spool_dir / submission_uid
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / file_name).write_bytes(form.file.data)
        except OSError as exc:
            log.error("spool write failed: %s", exc)
            self._json(507, {"error": "spool_write_failed"})
            return

        try:
            outcome = self.submit_fn(
                member_token=member_token,
                submission_uid=submission_uid,
                file_name=file_name,
                file_sha256=sha,
                file_size=size,
                print_now=print_now,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("cloud submit raised: %s", exc)
            self._json(502, {"error": "cloud_unreachable"})
            return

        status = outcome.get("status")
        if status == "bad_token":
            # OrcaSlicer treats 403 as "invalid API key" — the right hint.
            self._json(403, {"error": "invalid_api_key"})
            return
        if status == "error":
            self._json(502, {"error": "cloud_error", "detail": outcome.get("detail")})
            return

        # queued OR rejected → the UPLOAD succeeded (OctoPrint's 201 contract is
        # "did the file land"); an eligibility rejection is surfaced in the
        # member portal, the reliable channel (OrcaSlicer's result UI is thin).
        log.info(
            "ingest accepted %s (%d bytes, print=%s) -> %s",
            file_name,
            size,
            print_now,
            status,
        )
        self._octoprint_created(file_name)

    def _octoprint_created(self, file_name: str) -> None:
        # The documented OctoPrint upload response shape.
        self._json(
            201,
            {
                "files": {
                    "local": {
                        "name": file_name,
                        "origin": "local",
                        "path": file_name,
                        "refs": {
                            "resource": f"http://{self.headers.get('Host', 'hub')}/api/files/local/{file_name}",
                            "download": f"http://{self.headers.get('Host', 'hub')}/downloads/files/local/{file_name}",
                        },
                    }
                },
                "done": True,
            },
        )

    def _read_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                return None
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)


def make_handler(submit_fn: SubmitFn, spool_dir: Path, max_bytes: int):
    """Bind the injected deps onto a Handler subclass (BaseHTTPRequestHandler is
    instantiated per-request, so deps live as class attributes)."""

    class Handler(_Handler):
        pass

    Handler.submit_fn = staticmethod(submit_fn)
    Handler.spool_dir = spool_dir
    Handler.max_bytes = max_bytes
    return Handler


class IngestServer:
    """The threaded HTTP server. start() spawns a daemon thread; stop() shuts it
    down cleanly. Bind host 0.0.0.0 so members on the LAN can reach it."""

    def __init__(self, submit_fn: SubmitFn, *, port: int, spool_dir: Path, max_bytes: int):
        self.port = port
        spool_dir.mkdir(parents=True, exist_ok=True)
        handler = make_handler(submit_fn, spool_dir, max_bytes)
        self._server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="makeros-hub-ingest", daemon=True
        )
        self._thread.start()
        log.info("OrcaSlicer ingest server listening on :%d", self.port)

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
