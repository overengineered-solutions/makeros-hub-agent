"""Copy OrcaSlicer sends to the site so a job can move to another box's free printer (v0.74, 2026-10-19).

Operator: "a sliced file looking for a printer should never be limited by our own architecture". The site's open queue
(pstation 0175) rebinds a waiting job to the box whose printer is free, and that box fetches the file from the site. A site
upload already lives there; an OrcaSlicer send lives only on the box that caught it, so this copies it up. The heartbeat
says `uploadJobFiles: true` only while the open queue is on, so one box costs no extra traffic. Best effort, off the
heartbeat thread: a failed copy only means that job cannot move, and it still prints here.
"""
from __future__ import annotations

import gzip
import logging
import os
import queue
import tempfile
import threading

from .http import MAX_RAW_FILE_BYTES, TransportError, post_bytes

log = logging.getLogger("makeros_hub.job_files")

MAX_BYTES = 64 * 1024 * 1024        # = pstation MAX_UPLOAD_BYTES, measured after gzip
ATTEMPTS = 3

_wanted = threading.Event()
_queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=50)


def want(on: bool) -> None:
    """The heartbeat's answer: copy sends up while the site's open queue is on."""
    (_wanted.set if on else _wanted.clear)()


def offer(job_id: object, path: object) -> bool:
    """A send the site accepted. Queued for copying only while wanted; a full queue drops the oldest concern, not the print."""
    if not _wanted.is_set() or not isinstance(job_id, str) or not job_id.strip():
        return False
    try:
        _queue.put_nowait((job_id.strip(), str(path)))
        return True
    except queue.Full:
        log.warning("job file copy queue full; %s will not move between boxes", job_id)
        return False


def upload_one(url: str, path: str, bearer: str, *, poster=post_bytes) -> bool:
    """True when done (stored, or the site will never take it); False to try again."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return True   # the file is gone (printed and cleaned up): nothing to copy
    if size <= 0 or size > MAX_RAW_FILE_BYTES:
        log.info("job file copy outside the raw size limit: %s", path)
        return True
    try:
        # Disk staging supplies Content-Length without buffering the file or requiring chunked POST support.
        with open(path, "rb") as fh, tempfile.TemporaryFile(dir=os.path.dirname(path)) as data:
            with gzip.GzipFile(filename="", fileobj=data, mode="wb", mtime=0) as zipped:
                raw_size = 0
                while chunk := fh.read(256 * 1024):
                    raw_size += len(chunk)
                    if raw_size > MAX_RAW_FILE_BYTES or data.tell() > MAX_BYTES:
                        log.info("job file copy exceeds the size limit: %s", path)
                        return True
                    zipped.write(chunk)
            compressed_size = data.tell()
            if compressed_size > MAX_BYTES:
                log.info("job file copy exceeds the compressed size limit: %s", path)
                return True
            data.seek(0)
            status = poster(url, data, bearer=bearer, timeout=180.0,
                            content_encoding="gzip", content_length=compressed_size)
    except (TransportError, OSError) as e:
        log.info("job file copy will retry: %s", e)
        return False
    if status in (200, 204):
        return True
    if status in (404, 409, 413):   # not this box's queued job any more, different bytes, or too large: never retry
        log.info("job file copy refused by the site (%s): %s", status, path)
        return True
    return False


def run(url_for, bearer: str, stop: threading.Event, *, poster=post_bytes, pause=None) -> None:
    pause = pause or stop.wait
    while not stop.is_set():
        try:
            job_id, path = _queue.get(timeout=5.0)
        except queue.Empty:
            continue
        for attempt in range(ATTEMPTS):
            if stop.is_set() or upload_one(url_for(job_id), path, bearer, poster=poster):
                break
            pause(10 * (attempt + 1))


def start(cfg, bearer: str, stop: threading.Event) -> threading.Thread:
    t = threading.Thread(target=run, args=(cfg.job_file_url, bearer, stop), name="job-file-copy", daemon=True)
    t.start()
    return t
