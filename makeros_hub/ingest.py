"""OctoPrint-compatible ingest server — the LAN HTTP endpoint OrcaSlicer's
"Send" targets. Runs on the hub alongside the heartbeat loop (own thread).

OrcaSlicer (PrusaSlicer's OctoPrint host) does exactly two things:
  GET  /api/version        — connection test; must return JSON whose `text`
                             starts with "OctoPrint" and that has an `api` key.
  POST /api/files/local    — multipart upload (fields: file, print, path) with
                             the member's print token in `X-Api-Key`.

On upload the hub: saves the sliced file to a local spool dir, then registers
the submission with the cloud control plane
(POST /api/print/hub/submit, authenticated by the hub's own bearer + the
member token). The cloud resolves the token → member, runs the eligibility
gate, and creates the queue job; the member watches it in their portal.

The cloud-submit call is injected (`submit_fn`) so the server is unit-testable
without a network or a real cloud.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import device_evidence, job_files
from .multipart import MultipartError, UploadTooLarge, boundary_from_content_type, stream_multipart
from .printers.gcode_meta import read_gcode_meta
from .printers.threemf_objects import plate_picture
from .moonraker import DoorError, DoorHealth, LivePools, MoonrakerDoor

log = logging.getLogger("makeros-hub.ingest")

# The `text` MUST start with "OctoPrint" or the slicer rejects the host.
VERSION_BODY = {
    "api": "0.1",
    "server": "1.10.0",
    "text": "OctoPrint 1.10.0 (MakerOS Hub)",
}

# A submit_fn returns one of these outcome dicts.
#   {"status": "queued",   "jobId": "..."}     -> 201
#   {"status": "rejected", "reason": "..."}    -> 403 (no queue row was created)
#   {"status": "bad_token"}                    -> 403 (OrcaSlicer "invalid API key")
#   {"status": "error",    "detail": "..."}    -> 502
SubmitFn = Callable[..., dict]
MAX_FORM_OVERHEAD = 1024 * 1024
MIN_FREE_BYTES = 128 * 1024 * 1024

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
    upload_slot: threading.BoundedSemaphore
    moonraker: MoonrakerDoor | None = None
    model_pool: str | None = None
    members_tailscale_socket = device_evidence.DEFAULT_SOCKET

    def submit_with_device(self, **kwargs):
        source, device = self.device_evidence
        log.info("ingest submit source=%s nodeId=%s", source, device["nodeId"] if device else "-")
        if device:
            kwargs["device"] = device
        return self.submit_fn(**kwargs)

    # Per-request socket timeout — bounds a slow/stalled connection (a partial
    # upload won't hold a thread forever). A normal LAN upload is seconds;
    # 120s is generous headroom.
    timeout = 120
    # Until the request line arrives the connection holds a slot for nothing: a client whose network vanished (a tailnet
    # login switch, 2026-10-29) left its connections open on the members daemon and four of them filled every slot for
    # minutes. A real client sends its request at once.
    REQUEST_LINE_TIMEOUT = 10

    def setup(self):
        super().setup()
        self.connection.settimeout(self.REQUEST_LINE_TIMEOUT)

    def handle(self):
        self.device_evidence = device_evidence.lookup(self.client_address, self.members_tailscale_socket)
        if self.server.admit_device(self.connection, self.client_address, *self.device_evidence):
            super().handle()

    def parse_request(self):
        self.connection.settimeout(self.timeout)
        return super().parse_request()

    # Quiet the default stderr logging; route through our logger at debug.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("ingest %s - " + fmt, self.address_string(), *args)

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.moonraker and self.moonraker.handle(self):
            return
        if self.path.rstrip("/") in ("/api/version", "/api/version".rstrip("/")):
            self._json(200, VERSION_BODY)
            return
        if self.path == "/" or self.path == "/healthz":
            self._json(200, {"ok": True, "service": "makeros-hub-ingest"})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        # Keep the connection through body receipt, spool persistence and the submit reply.
        if not self.server.set_uploading(self.connection, True):
            self.close_connection = True
            return
        try:
            self._post()
        finally:
            self.server.set_uploading(self.connection, False)

    def _post(self):
        if self.moonraker and self.moonraker.handle(self):
            return
        if not self.upload_slot.acquire(blocking=False):
            self._json(503, {"error": "upload_busy", "detail": "Another upload is in progress. Try again shortly."})
            return
        try:
            self._upload()
        finally:
            self.upload_slot.release()

    def _refuse_method(self):
        if self.moonraker and self.moonraker.handle(self):
            return
        MoonrakerDoor._error(self, 405, "Use the PS site's Your prints and printer controls.")

    do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _refuse_method

    def _upload(self) -> None:
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
        if length > self.max_bytes + MAX_FORM_OVERHEAD:
            self._json(413, {"error": "file_too_large", "maxBytes": self.max_bytes})
            return
        try:
            free = shutil.disk_usage(self.spool_dir).free
        except OSError:
            log.exception("cannot inspect upload spool")
            self._json(507, {"error": "spool_unavailable"})
            return
        if free < length + MIN_FREE_BYTES:
            self._json(507, {"error": "spool_full", "detail": "The shop needs to free disk space before another upload."})
            return

        submission_uid = os.urandom(16).hex()
        dest_dir = self.spool_dir / submission_uid
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            pending = dest_dir / ".upload.tmp"
            with pending.open("wb") as fh:
                filename, fields, sha, size = stream_multipart(self._read_chunks(length), boundary, fh, self.max_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            if filename is None:
                self._discard_spool(dest_dir)
                self._json(400, {"error": "no_file_field"})
                return
            file_name = _safe_filename(filename)
            print_now = fields.get("print", "false").lower() == "true"
            pending.replace(dest_dir / file_name)
            for directory in (dest_dir, self.spool_dir):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except UploadTooLarge:
            self._discard_spool(dest_dir)
            self._json(413, {"error": "file_too_large", "maxBytes": self.max_bytes})
            return
        except TimeoutError:
            self._discard_spool(dest_dir)
            self._json(408, {"error": "upload_timeout"})
            return
        except EOFError:
            self._discard_spool(dest_dir)
            self._json(400, {"error": "truncated_body"})
            return
        except (MultipartError, UnicodeError) as exc:
            self._discard_spool(dest_dir)
            log.warning("multipart parse failed: %s", exc)
            self._json(400, {"error": "bad_multipart"})
            return
        except OSError as exc:
            self._discard_spool(dest_dir)
            log.error("spool write failed: %s", exc)
            self._json(507, {"error": "spool_write_failed"})
            return

        # A G-code names the printer and filaments it was sliced for; the cloud routes on them (a .3mf → None, today's
        # path). A parse miss never fails the upload — the job just falls back to staff review.
        meta_kwargs: dict = {}
        try:
            meta = read_gcode_meta(dest_dir / file_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("gcode metadata unreadable for %s: %s", file_name, exc)
            meta = None
        if meta is not None:
            meta_kwargs = {
                "printer_model": meta["printerModel"],
                "est_grams": meta["estGrams"],
                "required_filaments": meta["requiredFilaments"],
            }
        if file_name.lower().endswith(".3mf"):
            picture = plate_picture(dest_dir / file_name)
            if picture is not None:
                meta_kwargs["plate_image"] = base64.b64encode(picture).decode("ascii")

        try:
            outcome = self.submit_with_device(
                member_token=member_token,
                submission_uid=submission_uid,
                file_name=file_name,
                file_sha256=sha,
                file_size=size,
                print_now=print_now,
                **meta_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("cloud submit raised: %s", exc)
            outcome = {"status": "error"}

        status = outcome.get("status") if isinstance(outcome, dict) else None
        if status == "bad_token":
            # Bad member — the spooled file will never print; don't leak it.
            self._discard_spool(dest_dir)
            # OrcaSlicer treats 403 as "invalid API key" — the right hint.
            self._json(403, {"error": "invalid_api_key"})
            return
        if status == "rejected" and isinstance(outcome.get("reason"), str) and outcome["reason"]:
            # The app creates no job on rejection; 201 would hide the failure.
            self._discard_spool(dest_dir)
            self._json(403, {"error": "print_rejected", "reason": outcome["reason"]})
            return
        if status != "queued" or not isinstance(outcome.get("jobId"), str) or not outcome["jobId"].strip():
            # A timed-out response can follow a committed job. Deleting its file
            # strands that job; retain it for reconciliation, bounded by free space.
            log.error("ingest outcome unconfirmed submission_uid=%s; file retained for staff review", submission_uid)
            self._json(502, {"error": "submission_unconfirmed", "detail": "Check Your prints before resending; ask the shop if the job is missing."})
            return

        # A durable file and an actual cloud job receipt are both required.
        log.info(
            "ingest accepted %s (%d bytes, print=%s) -> %s",
            file_name,
            size,
            print_now,
            status,
        )
        job_files.offer(outcome["jobId"], dest_dir / file_name)
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

    def _discard_spool(self, dest_dir: Path) -> None:
        """Remove a spooled submission whose job won't print (rejected / bad
        token / never-registered) so the spool disk stays bounded."""
        try:
            for child in dest_dir.iterdir():
                child.unlink(missing_ok=True)
            dest_dir.rmdir()
        except OSError as exc:
            log.warning("could not discard rejected spool directory %s: %s", dest_dir, exc)

    def _read_chunks(self, n: int):
        remaining = n
        deadline = time.monotonic() + self.timeout
        while remaining > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("upload deadline exceeded")
            self.connection.settimeout(left)
            chunk = self.rfile.read1(min(remaining, 256 * 1024))
            if not chunk:
                raise EOFError("truncated body")
            remaining -= len(chunk)
            yield chunk


def make_handler(submit_fn: SubmitFn, spool_dir: Path, max_bytes: int):
    """Bind the injected deps onto a Handler subclass (BaseHTTPRequestHandler is
    instantiated per-request, so deps live as class attributes)."""

    class Handler(_Handler):
        pass

    Handler.submit_fn = staticmethod(submit_fn)
    Handler.spool_dir = spool_dir
    Handler.max_bytes = max_bytes
    Handler.upload_slot = threading.BoundedSemaphore(1)
    Handler.door_health = DoorHealth()
    return Handler


class _BoundedServer(ThreadingHTTPServer):
    # Bound threads even before headers arrive; body limits alone miss slow clients.
    # 32, not 4: every member's slicer shares this port, and Orca 2.5 opens several at once (sync, status, uploads).
    # Uploads still go one at a time (upload_slot); this bounds threads, not work.
    MAX_CONNECTIONS = 32
    MAX_DEVICE_CONNECTIONS = 6

    def __init__(self, *args):
        self.connections = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        self._connection_lock = threading.Lock()
        self._devices = {}  # In accept order, including connections still waiting for WhoIs.
        self._uploading = set()
        self._last_full_log = 0.0
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            if time.monotonic() - self._last_full_log > 60:
                self._last_full_log = time.monotonic()
                log.warning("ingest connections full (%d); answering 503 until one finishes", self.MAX_CONNECTIONS)
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nRetry-After: 5\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            with self._connection_lock:
                self._devices[request] = None
            super().process_request(request, client_address)
        except Exception:
            with self._connection_lock:
                self._devices.pop(request, None)
            self.connections.release()
            raise

    def admit_device(self, request, peer, source, device):
        bucket = ("tailnet", device["nodeId"]) if device else (("lan", peer[0]) if source == "lan" else ("unknown",))
        with self._connection_lock:
            self._devices[request] = bucket
            own = [conn for conn, key in self._devices.items() if key == bucket and conn is not request]
            # Only an identified device is capped: if whois is down every tailnet client lands in "unknown", and a shared
            # cap of six would refuse members wholesale; the global slots still bound them.
            if bucket != ("unknown",) and len(own) >= self.MAX_DEVICE_CONNECTIONS:
                oldest = next((conn for conn in own if conn not in self._uploading), None)
                if oldest is None:
                    del self._devices[request]
                    return False
                del self._devices[oldest]
                # SHUT_RDWR wakes a handler blocked in a buffered read; close alone does not.
                try:
                    oldest.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.close_request(oldest)
                log.info("ingest closed oldest idle connection nodeId=%s", device["nodeId"] if device else "-")
            return True

    def set_uploading(self, request, uploading):
        with self._connection_lock:
            if request not in self._devices:
                return False
            if uploading:
                self._uploading.add(request)
            else:
                self._uploading.discard(request)
            return True

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connection_lock:
                self._devices.pop(request, None)
                self._uploading.discard(request)
            self.connections.release()

    def service_actions(self):
        door = self.RequestHandlerClass.moonraker
        if door:
            door.store.maintain()


class IngestServer:
    """The threaded HTTP server. start() spawns a daemon thread; stop() shuts it
    down cleanly. Bind host 0.0.0.0 so members on the LAN can reach it."""

    def __init__(self, submit_fn: SubmitFn, *, port: int, spool_dir: Path, max_bytes: int,
                 moonraker_authorize=None, moonraker_pools=None, moonraker_pool="p2s",
                 members_tailscale_socket=None, moonraker_hosts=None, moonraker_staged=None, moonraker_copy=None):
        self.port = port
        spool_dir.mkdir(parents=True, exist_ok=True)
        handler = make_handler(submit_fn, spool_dir, max_bytes)
        handler.members_tailscale_socket = members_tailscale_socket or device_evidence.DEFAULT_SOCKET
        self._door_health = handler.door_health
        if moonraker_pool and moonraker_authorize is not None:
            try:
                handler.moonraker = MoonrakerDoor(spool_dir, moonraker_authorize, moonraker_pools or LivePools(),
                                                self._door_health, hosts=moonraker_hosts, notify_staged=moonraker_staged,
                                                copy_staged=moonraker_copy)
                handler.model_pool = moonraker_pool
            except Exception:  # noqa: BLE001 — the staff trial's store must never take member ingest or the hub down
                self._door_health.set_state("store_unavailable")
                log.exception("Moonraker staged-file store unavailable; door off, member ingest stays available")
        self._server = _BoundedServer(("0.0.0.0", port), handler)

    def moonraker_health(self):
        return self._door_health.snapshot()

    def reconcile_moonraker(self, reports, released_jobs=(), released_uploads=()):
        door = self._server.RequestHandlerClass.moonraker
        if door is None:
            return True
        jobs = {r["queueJobId"] for r in reports
                if isinstance(r, dict) and r.get("state") in {"printing", "completed"}
                and isinstance(r.get("queueJobId"), str)}
        if isinstance(released_jobs, (list, tuple)) and len(released_jobs) <= 256 and all(
                isinstance(j, str) and 0 < len(j) <= 128 for j in released_jobs):
            jobs.update(released_jobs)
        uploads = []
        if isinstance(released_uploads, (list, tuple)) and len(released_uploads) <= 256 and all(
                isinstance(uid, str) and re.fullmatch(r"[0-9a-f]{32}", uid) for uid in released_uploads):
            uploads = released_uploads
        if jobs or uploads:
            try:
                door.store.release(jobs, uploads)
            except DoorError:
                return False  # An upload/start owns the lock; retain the report and retry next heartbeat.
            except Exception:
                self._door_health.set_state("store_unavailable")
                log.exception("Moonraker file release failed; retained for reconciliation")
                return False
        return True

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                         name="makeros-hub-ingest", daemon=True).start()
        log.info("OrcaSlicer ingest server listening on :%d", self._server.server_address[1])

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
