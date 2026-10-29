"""Orca's small Moonraker surface on the ingest door. No printer command transport."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zipfile
import xml.etree.ElementTree as ET

from . import __version__, job_files
from .multipart import MultipartError, UploadTooLarge, boundary_from_content_type, stream_multipart
from .printers.ingest_facts import POOLS, read_file_facts
from .printers.threemf_objects import Unreadable3mf
from .vprinter.capture import sha256_file
from .vprinter.live_pool import vp_pool_from_statuses_ranked
from .vprinter.outbox import validate_submission_uid

log = logging.getLogger("makeros-hub.moonraker")
CONTROLS = "Use the PS site's Your prints and printer controls. This door only stages files and queues prints."
PROTOCOL = "moonraker-v1"
STAGE_TTL = 24 * 60 * 60
MAX_RECORDS = 256
MAX_SPOOL_BYTES = 4 * 1024**3
MAX_MEMBER_STAGES = 16
LANES = 16  # Maximum displayed spools; Orca's tray bitmask must stay below 32.
REFUSAL_CODES = frozenset(str(c) for c in (400, 403, 404, 405, 408, 409, 410, 413, 502, 503, 507)) | {"other"}


class DoorHealth:
    def __init__(self):
        self.state = "off"
        self.refusals = {}
        self._lock = threading.Lock()

    def set_state(self, state):
        with self._lock:
            self.state = state

    def refuse(self, code):
        code = str(code) if str(code) in REFUSAL_CODES else "other"
        with self._lock:
            self.refusals[code] = min(2**31 - 1, self.refusals.get(code, 0) + 1)

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "refusals": dict(self.refusals)}


class DoorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def site_words(body):
    """The site's own plain sentence for a refusal, or None for its short codes (invalid_token, ineligible, ...)."""
    reason = body.get("reason") if isinstance(body, dict) else None
    return reason.strip() if isinstance(reason, str) and " " in reason.strip() and len(reason) <= 300 else None


def validate_access(body, pool):
    """An uncached site authorization, using its existing print-token resolver."""
    if not isinstance(body, dict):
        raise DoorError(503, "The site's Moonraker authorization response is invalid. Ask the shop.")
    if body.get("ok") is False:
        raise DoorError(403, "Print token or membership is no longer eligible. Check the PS site.")
    if (body.get("ok") is not True or body.get("submissionProtocol") != PROTOCOL
            or body.get("modelPool") != pool or body.get("printerModel") != POOLS[pool][0]
            or not isinstance(body.get("memberId"), str) or not body["memberId"].strip()
            or not isinstance(body.get("printerIds"), list) or len(body["printerIds"]) > 256
            or any(not isinstance(p, str) or not p for p in body["printerIds"])
            or len(set(body["printerIds"])) != len(body["printerIds"])):
        raise DoorError(503, "The site's Moonraker authorization/submit contract is unavailable. Ask the shop.")
    spools = body.get("loadedSpools")
    if not isinstance(spools, dict) or set(spools) - set(body["printerIds"]):
        raise DoorError(503, "The site's loaded-spool response is invalid. Ask the shop.")
    for spool in spools.values():
        if spool is None:
            continue
        if (not isinstance(spool, dict) or not isinstance(spool.get("material"), str)
                or not spool["material"].strip()
                or not isinstance(spool.get("colorHex"), str)
                or not re.fullmatch(r"#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", spool["colorHex"])):
            raise DoorError(503, "The site's loaded-spool response is invalid. Ask the shop.")
    if POOLS[pool][2] is None and set(spools) != set(body["printerIds"]):
        raise DoorError(503, "The site must report every Klipper's loaded spool, or explicit null for empty.")
    return body


class LivePools:
    """Heartbeat-owned snapshot of the same source and ranking the VPs use.

    An attempted refresh invalidates the old success first. Per-request member
    filtering happens before ranking; no member's derived view is cached.
    """
    def __init__(self, max_age=90):
        self.max_age = max_age
        self._snapshot = None
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._snapshot = None

    def update(self, statuses, ranking, configs=()):
        self.invalidate()
        if not isinstance(statuses, list) or any(not isinstance(s, dict) for s in statuses):
            raise ValueError("invalid live printer statuses")
        capacities = {c.model: min(LANES, c.units * c.trays) for c in configs}
        snapshot = (time.monotonic(), copy.deepcopy(statuses), copy.deepcopy(ranking), capacities)
        with self._lock:
            self._snapshot = snapshot

    def lanes(self, access):
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or time.monotonic() - snapshot[0] > self.max_age:
            raise DoorError(503, "Loaded spools could not be refreshed. Retry Sync after the shop restores the hub.")
        _, statuses, ranking, capacities = snapshot
        allowed = set(access["printerIds"])
        model = access["printerModel"]
        klipper = POOLS[access["modelPool"]][2] is None
        scoped = []
        for status in statuses:
            pid = status.get("printerId")
            if pid not in allowed:
                continue
            if status.get("connectionState") == "error":
                raise DoorError(503, "A printer's loaded-spool refresh failed. Retry Sync shortly.")
            if status.get("connectionState") != "connected":
                continue
            if not klipper and str(status.get("model", "")).casefold() != model.casefold():
                raise DoorError(503, "Live printer identity disagrees with the authorized model pool.")
            if klipper:
                scoped.append({"printerId": pid, "vtTray": access["loadedSpools"][pid]})
            else:
                scoped.append(status)
        capacity = capacities.get(model, LANES)
        trays = vp_pool_from_statuses_ranked(scoped, 1, capacity, ranking, time.time())
        lanes = {}
        for i, tray in enumerate(trays):
            # The AMS colour with its alpha (RRGGBBAA): Orca 2.4.2 and 2.5 both read 8 digits (normalize_color_value), so
            # clear PETG (00000000) stays Clear and translucent pink (F9C1BD80) stays translucent. Cutting it to 6 digits
            # synced clear as opaque black, and the site matches alpha exactly, so such a slice could never meet its spool.
            colour = tray.get("tray_color")
            colour = colour.upper() if isinstance(colour, str) and re.fullmatch(r"[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", colour) else "000000FF"
            # The VP's min/max temperatures can be catalog fallbacks, not a spool's
            # chosen printing temperature. Zero is Orca's explicit unknown sentinel.
            lanes[f"lane{i + 1}"] = {"lane": str(i), "material": tray.get("tray_type", ""),
                                    "color": "#" + colour, "bed_temp": 0, "nozzle_temp": 0}
            filament_id = tray.get("tray_info_idx")
            if isinstance(filament_id, str) and re.fullmatch(r"[A-Za-z0-9_+-]{1,32}", filament_id):
                lanes[f"lane{i + 1}"]["filament_id"] = filament_id
        return lanes


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StagedUploads:
    """SQLite receipts plus the existing <spool>/<submission UID>/<filename>.

    The process lock spans upload/start/cleanup; SQLite commits SUBMITTING before
    any network request. A killed process releases flock; the next start reuses
    the UID and relies on the site's atomic UID conflict check to recover a receipt.
    """
    def __init__(self, spool_dir, health=None):
        self.health = health or DoorHealth()
        self.spool = Path(spool_dir)
        self.spool.mkdir(parents=True, exist_ok=True)
        self.db_path = self.spool / ".moonraker.sqlite3"
        self.lock_path = self.spool / ".moonraker.lock"
        self._thread_lock = threading.Lock()
        self._last_clean = 0.0
        with self.locked() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS uploads (
                uid TEXT PRIMARY KEY, member TEXT NOT NULL, pool TEXT NOT NULL,
                name TEXT, sha TEXT, size INTEGER NOT NULL, created REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('receiving','staged','submitting','queued')),
                facts TEXT, job TEXT)""")
            if "released" not in {r["name"] for r in db.execute("PRAGMA table_info(uploads)")}:
                db.execute("ALTER TABLE uploads ADD COLUMN released INTEGER NOT NULL DEFAULT 0")
            os.chmod(self.db_path, 0o600)
            _sync_dir(self.spool)
            self._clean(db)

    @contextmanager
    def locked(self, blocking=True):
        if not self._thread_lock.acquire(blocking=blocking):
            raise DoorError(503, "Another file operation is in progress. Retry shortly.")
        try:
            with self.lock_path.open("a+b") as lock:
                os.chmod(self.lock_path, 0o600)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    raise DoorError(503, "Another file operation is in progress. Retry shortly.") from None
                db = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
                try:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA synchronous=FULL")
                    yield db
                finally:
                    db.close()
        finally:
            self._thread_lock.release()

    def _remove(self, db, uid):
        validate_submission_uid(uid)
        directory = self.spool / uid
        if directory.exists():
            shutil.rmtree(directory)
            _sync_dir(self.spool)
        db.execute("DELETE FROM uploads WHERE uid = ?", (uid,))

    def _clean(self, db):
        rows = db.execute("""SELECT uid FROM uploads WHERE state='receiving'
                          OR (state='queued' AND released=1) OR (state='staged' AND created < ?)""",
                          (time.time() - STAGE_TTL,)).fetchall()
        for row in rows:
            self._remove(db, row["uid"])
        self._last_clean = time.monotonic()
        self.health.set_state("ok")
        if rows:
            log.info("moonraker expiry processed=%d expected=%d", len(rows), len(rows))

    def release(self, job_ids):
        """Only a confirmed handover or explicit site release can reclaim a queued file.

        Mark first: interrupted deletion is retried on startup/maintenance. SUBMITTING
        rows remain untouched, including starts whose site receipt was lost.
        """
        with self.locked(blocking=False) as db:
            for job_id in job_ids:
                db.execute("UPDATE uploads SET released=1 WHERE state='queued' AND job=?", (job_id,))
            self._clean(db)

    def maintain(self):
        if time.monotonic() - self._last_clean < 60:
            return
        try:
            with self.locked(blocking=False) as db:
                self._clean(db)
        except DoorError:
            pass  # A live operation owns the files; try again on the next server tick.
        except (OSError, sqlite3.Error):
            self.health.set_state("store_unavailable")
            self._last_clean = time.monotonic()  # retry in a minute, not on every 0.1 s tick of every listener
            log.exception("Moonraker staged-file maintenance failed; files retained")

    def check_room(self, db, length, member):
        from .ingest import MIN_FREE_BYTES
        count, size = db.execute("SELECT count(*), coalesce(sum(size),0) FROM uploads").fetchone()
        own = db.execute("SELECT count(*) FROM uploads WHERE member=? AND state='staged'", (member,)).fetchone()[0]
        if count >= MAX_RECORDS or size + length > MAX_SPOOL_BYTES or own >= MAX_MEMBER_STAGES:
            raise DoorError(507, "The staged-file quota is full. Ask the shop to reconcile retained jobs.")
        if shutil.disk_usage(self.spool).free < length + MIN_FREE_BYTES:
            raise DoorError(507, "The shop must free spool disk space before another upload.")

    def upload(self, handler, access, length, boundary):
        from .ingest import _safe_filename
        with self.locked(blocking=False) as db:
            self._clean(db)
            self.check_room(db, length, access["memberId"])
            uid = validate_submission_uid(os.urandom(16).hex())
            db.execute("INSERT INTO uploads(uid,member,pool,size,created,state) VALUES(?,?,?,?,?,'receiving')",
                       (uid, access["memberId"], access["modelPool"], length, time.time()))
            directory = self.spool / uid
            try:
                directory.mkdir(mode=0o700)
                pending = directory / ".upload.tmp"
                with pending.open("wb") as fh:
                    name, fields, sha, size = stream_multipart(handler._read_chunks(length), boundary, fh, handler.max_bytes)
                    fh.flush()
                    os.fsync(fh.fileno())
                if not name or size == 0:
                    raise DoorError(400, "The upload must contain one nonempty file.")
                if fields.get("root", "gcodes") != "gcodes" or fields.get("path", ""):
                    raise DoorError(400, "Upload to root=gcodes without a directory path.")
                if set(fields) - {"root", "path", "print", "plateindex"}:
                    raise DoorError(400, "Unsupported upload fields.")
                # Even print=true is storage only; both Orca versions explicitly start next.
                name = _safe_filename(name)
                target = directory / name
                pending.replace(target)
                facts = read_file_facts(target, access["modelPool"], fields.get("plateindex"))
                _sync_dir(directory)
                _sync_dir(self.spool)
                db.execute("UPDATE uploads SET name=?,sha=?,size=?,facts=?,state='staged' WHERE uid=?",
                           (name, sha, size, json.dumps(facts, sort_keys=True), uid))
            except Exception:
                self._remove(db, uid)
                raise
            result = {"item": {"path": f"{uid}/{name}", "root": "gcodes"},
                      "print_started": False, "print_queued": False}
            # INTERIM — keep result until the operator retires Orca 2.4.2; 2.5 reads the top level.
            return {**result, "result": result}

    def start(self, stored_path, access, token, submit):
        if not isinstance(stored_path, str) or not re.fullmatch(r"[0-9a-f]{32}/[A-Za-z0-9-][A-Za-z0-9._-]{0,199}", stored_path):
            raise DoorError(404, "Staged file not found for this member and model.")
        uid, name = stored_path.split("/")
        with self.locked() as db:
            row = db.execute("SELECT * FROM uploads WHERE uid=? AND name=? AND member=? AND pool=?",
                             (uid, name, access["memberId"], access["modelPool"])).fetchone()
            if row is None or row["state"] == "receiving" or row["released"]:
                raise DoorError(404, "Staged file not found for this member and model.")
            if row["state"] == "queued":
                if not isinstance(row["job"], str) or not row["job"].strip():
                    raise DoorError(503, "The stored job receipt is damaged. Ask the shop to reconcile it.")
                return {"result": {"jobId": row["job"], "status": "queued"}}
            if row["state"] == "staged" and row["created"] < time.time() - STAGE_TTL:
                self._remove(db, uid)
                raise DoorError(410, "Staged upload expired; upload it again.")
            target = self.spool / uid / name
            if (not target.is_file() or target.is_symlink() or target.stat().st_size != row["size"]
                    or sha256_file(target) != row["sha"]):
                raise DoorError(409, "Staged file changed or is missing. Ask the shop to reconcile it.")
            facts = read_file_facts(target, access["modelPool"], json.loads(row["facts"])["plate"] if target.suffix.lower() == ".3mf" else None)
            if facts != json.loads(row["facts"]):
                raise DoorError(409, "Staged file facts changed. Ask the shop to reconcile it.")
            db.execute("UPDATE uploads SET state='submitting' WHERE uid=?", (uid,))
            try:
                result = submit(member_token=token, submission_uid=uid, file_name=name,
                                file_sha256=row["sha"], file_size=row["size"], print_now=True,
                                printer_model=facts["printerModel"], est_grams=facts["estGrams"],
                                required_filaments=facts["requiredFilaments"], file_facts=facts)
            except Exception:
                log.warning("Moonraker submit unconfirmed uid=%s; retained for retry", uid)
                result = None
            if not isinstance(result, dict):
                result = {}
            if result.get("status") in {"rejected", "bad_token"}:
                # Retain even a rejection: an earlier timed-out attempt might have committed.
                words = site_words(result)
                raise DoorError(403, (words + " " if words else "The site refused this start. ")
                                + "Check Your prints or ask the shop before retrying.")
            if result.get("status") != "queued" or not isinstance(result.get("jobId"), str) or not result["jobId"].strip():
                raise DoorError(502, "The shop may already have this print. Check Your prints on the website before sending again.")
            db.execute("UPDATE uploads SET state='queued',job=? WHERE uid=?", (result["jobId"], uid))
            try:
                job_files.offer(result["jobId"], target)
            except Exception:
                log.exception("Moonraker accepted job file copy could not be offered uid=%s", uid)
            return {"result": {"jobId": result["jobId"], "status": "queued"}}


class MoonrakerDoor:
    def __init__(self, spool_dir, authorize, pools, health=None):
        self.health = health or DoorHealth()
        self.store = StagedUploads(spool_dir, self.health)
        self.authorize = authorize
        self.pools = pools

    def handle(self, handler):
        url = urlsplit(handler.path)
        pool, endpoint = handler.model_pool, url.path
        if pool is None or not (endpoint == "/websocket" or endpoint.startswith(("/server/", "/printer/", "/machine/", "/access/"))):
            return False
        try:
            allowed = {("GET", "/server/info"), ("GET", "/server/database/item"),
                       ("GET", "/server/files/roots"), ("POST", "/server/files/upload"),
                       ("GET", "/server/files/directory"),
                       ("POST", "/printer/print/start")}
            if (handler.command, endpoint) not in allowed:
                raise DoorError(405, CONTROLS)
            expected_query = {"/server/database/item": {"namespace": ["lane_data"]},
                              "/server/files/directory": {"path": ["gcodes"]}}.get(endpoint, {})
            if (not expected_query and url.query) or parse_qs(url.query, keep_blank_values=True) != expected_query:
                raise DoorError(400, "Unsupported query. " + CONTROLS)
            token = handler.headers.get("X-Api-Key", "").strip()
            if not token or len(token) > 512:
                raise DoorError(403, "Set the member's PS print token as the API key.")
            if self.authorize is None:
                raise DoorError(503, "The site's Moonraker authorization contract is unavailable.")
            access = validate_access(self.authorize(token, pool), pool)
            if endpoint == "/server/info":
                with self.store.locked() as db:
                    self.store.check_room(db, 0, access["memberId"])
                result = {"result": {"machine_name": f"PS {access['printerModel']} queue", "model_pool": pool,
                                     "moonraker_version": f"makeros-hub-{__version__}", "klippy_state": "ready"}}
            elif endpoint == "/server/database/item":
                result = {"result": {"namespace": "lane_data", "value": self.pools.lanes(access)}}
            elif endpoint == "/server/files/roots":
                result = {"result": [{"name": "gcodes", "permissions": "rw"}]}
            elif endpoint == "/server/files/directory":
                from .ingest import MIN_FREE_BYTES
                with self.store.locked() as db:
                    self.store.check_room(db, 0, access["memberId"])
                    size = db.execute("SELECT coalesce(sum(size),0) FROM uploads").fetchone()[0]
                    free = max(0, min(MAX_SPOOL_BYTES - size, shutil.disk_usage(self.store.spool).free - MIN_FREE_BYTES))
                result = {"result": {"dirs": [], "files": [], "disk_usage": {"free": free}}}
            elif endpoint == "/server/files/upload":
                from .ingest import MAX_FORM_OVERHEAD
                length = self._length(handler, handler.max_bytes + MAX_FORM_OVERHEAD)
                boundary = boundary_from_content_type(handler.headers.get("Content-Type"))
                if not boundary:
                    raise DoorError(400, "Expected multipart/form-data.")
                if not handler.upload_slot.acquire(blocking=False):
                    raise DoorError(503, "Another upload is in progress. Retry shortly.")
                try:
                    result = self.store.upload(handler, access, length, boundary)
                finally:
                    handler.upload_slot.release()
            else:
                length = self._length(handler, 4096)
                if handler.headers.get_content_type() != "application/json":
                    raise DoorError(400, "Start requires JSON with the staged filename.")
                body = json.loads(b"".join(handler._read_chunks(length)))
                if not isinstance(body, dict) or set(body) != {"filename"}:
                    raise DoorError(400, "Start requires only the staged filename returned by upload.")
                result = self.store.start(body["filename"], access, token, handler.submit_with_device)
            handler._json(201 if endpoint == "/server/files/upload" else 200, result)
        except DoorError as exc:
            self._error(handler, exc.code, str(exc))
        except UploadTooLarge:
            self._error(handler, 413, "The file exceeds the upload limit.")
        except TimeoutError:
            self._error(handler, 408, "Upload timed out. Retry the operation.")
        except EOFError:
            self._error(handler, 400, "The request body was truncated.")
        except (ValueError, KeyError, MultipartError, Unreadable3mf, zipfile.BadZipFile, ET.ParseError):
            log.warning("Moonraker request/file validation failed", exc_info=True)
            self._error(handler, 400, "Invalid request or sliced-file facts. Check the model, selected plate, filament and nozzle settings.")
        except (OSError, sqlite3.Error):
            self.health.set_state("store_unavailable")
            log.exception("Moonraker spool operation failed; uncertain submissions retained")
            self._error(handler, 507, "The spool could not persist this operation. Check Your prints and ask the shop.")
        except Exception:
            log.exception("Moonraker request failed; no cached success returned")
            self._error(handler, 503, "The shop's queue or loaded-spool refresh is unavailable. Retry shortly.")
        return True

    @staticmethod
    def _length(handler, limit):
        if handler.headers.get("Transfer-Encoding") or len(handler.headers.get_all("Content-Length", [])) != 1:
            raise DoorError(400, "Supply one Content-Length and no Transfer-Encoding.")
        try:
            length = int(handler.headers["Content-Length"])
        except ValueError:
            raise DoorError(400, "Invalid Content-Length.") from None
        if length <= 0:
            raise DoorError(400, "Empty request body.")
        if length > limit:
            raise DoorError(413, "Request exceeds the size limit.")
        return length

    @staticmethod
    def _error(handler, code, message):
        handler.door_health.refuse(code)
        handler.close_connection = True
        handler._json(code, {"error": {"code": code, "message": message}})
