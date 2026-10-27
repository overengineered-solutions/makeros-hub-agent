"""Members' paper prints on the shop's Epson ET-16600 (v0.73, 2026-10-18).

Operator: paper printing at the shop moves from free to the membership allowance ("100 a month with up to 20 being
color", a dense page counts as 2). The site queues a member's file and hands it to this hub in the heartbeat answer
(`paper`: the Epson's address and the jobs). For each job, one at a time and off the heartbeat thread: fetch the file,
measure and render it in the sandboxed ps-paper container (paper/ps_paper.py through /opt/ps-paper/run-paper), report the
per-page ink coverage, and only when the site answers {"print": true} send the PWG raster to the Epson over IPPS and follow
it to completed, aborted or a 20-minute limit, then report the outcome. The member's file never touches the printer in
any other form, and nothing prints that the site did not approve against the allowance.
"""
from __future__ import annotations

import base64
import hashlib
import math
import json
import logging
import os
import re
import shutil
import ssl
import struct
import subprocess
import time
import errno
import http.client
import socket
from pathlib import Path
from . import report_outbox

log = logging.getLogger("makeros_hub.paper")

PAPER_RUNNER = os.environ.get("MAKEROS_HUB_PAPER_RUNNER", "/opt/ps-paper/run-paper")
MEDIA = {"letter": "na_letter_8.5x11in", "tabloid": "na_ledger_11x17in"}
FILES = {"pdf": "doc.pdf", "jpeg": "doc.jpg", "png": "doc.png", **{k: "doc." + k for k in
    "docx doc pptx ppt xlsx xls odt odp ods rtf txt heic heif".split()}}
BASE_FILES = {"pdf", "jpeg", "png"}
_formats_cache = (0.0, [])
ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HOST_RE = re.compile(r"^(192\.168|10\.\d{1,3})\.\d{1,3}\.\d{1,3}$")
FOLLOW_SECONDS = 20 * 60


def paper_ready() -> bool:
    return os.path.isfile(PAPER_RUNNER) and os.access(PAPER_RUNNER, os.X_OK)


def paper_formats() -> list[str]:
    """Ask the installed image, not the hub version; an old runner cannot advertise conversion."""
    global _formats_cache
    if time.monotonic() < _formats_cache[0]:
        return list(_formats_cache[1])
    formats = []
    try:
        r = subprocess.run(["sudo", "-n", PAPER_RUNNER, "formats"], capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout) if r.returncode == 0 else None
        if isinstance(data, list) and data and all(isinstance(k, str) and k in FILES for k in data):
            formats = sorted(set(data))
        else:
            log.warning("paper format probe unavailable; using the original paper formats")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("paper format probe failed: %s", type(e).__name__)
    _formats_cache = (time.monotonic() + 60, formats)
    return list(formats)


def measurement(result, work, extended):
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("Invalid renderer result")
    pages = result.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= (200 if extended else 30) or any(
        not isinstance(p, dict) or any(type(p.get(k)) not in (int, float) or not math.isfinite(p[k]) or not 0 <= p[k] <= 1
        for k in ("c", "m", "y", "k")) for p in pages):
        raise ValueError("Invalid renderer page measurements")
    pwg = Path(work) / "out" / "doc.pwg"
    if not 0 < pwg.stat().st_size <= 512 * 1024 * 1024:
        raise ValueError("Invalid raster size")
    data = {"pages": pages}
    if extended:
        preview, count, digest = result.get("preview"), result.get("sourcePages"), result.get("renderHash")
        if type(count) is not int or not 1 <= count <= 200 or not isinstance(preview, list) or len(preview) != min(20, len(pages)):
            raise ValueError("Missing paper preview")
        if any(not isinstance(p, str) or len(p) > 24 * 1024 for p in preview) or sum(map(len, preview)) > 512 * 1024:
            raise ValueError("Paper preview too large")
        for p in preview:
            raw = base64.b64decode(p, validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 24 or not all(0 < n <= 240 for n in struct.unpack(">II", raw[16:24])):
                raise ValueError("Invalid paper thumbnail")
        with pwg.open("rb") as fh:
            actual = hashlib.file_digest(fh, "sha256").hexdigest()
        if digest != actual:
            raise ValueError("Paper rendering changed")
        data.update(preview=preview, sourcePages=count, renderHash=digest)
    return data


# ── IPP, just what Print-Job and Get-Job-Attributes need (RFC 8010) ─────────────────────────────────────────────────
def _attr(tag: int, name: str, value) -> bytes:
    if isinstance(value, int) and tag in (0x21, 0x23):
        v = struct.pack(">i", value)
    else:
        v = value.encode() if isinstance(value, str) else value
    return bytes([tag]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(v)) + v


def print_job_request(host: str, job_name: str, media: str, colour: bool, copies: int, two_sided: bool) -> bytes:
    """The Print-Job header (operation and job attributes), before the PWG raster. Pure."""
    op = (_attr(0x47, "attributes-charset", "utf-8") + _attr(0x48, "attributes-natural-language", "en")
          + _attr(0x45, "printer-uri", f"ipps://{host}/ipp/print") + _attr(0x42, "requesting-user-name", "Procrastination Station")
          + _attr(0x42, "job-name", job_name[:100]) + _attr(0x49, "document-format", "image/pwg-raster"))
    job = (_attr(0x21, "copies", copies) + _attr(0x44, "sides", "two-sided-long-edge" if two_sided else "one-sided")
           + _attr(0x44, "media", MEDIA[media]) + _attr(0x44, "print-color-mode", "color" if colour else "monochrome")
           + _attr(0x23, "print-quality", 4))
    return struct.pack(">BBHI", 2, 0, 0x0002, 1) + b"\x01" + op + b"\x02" + job + b"\x03"


def job_attrs_request(host: str, job_id: int) -> bytes:
    op = (_attr(0x47, "attributes-charset", "utf-8") + _attr(0x48, "attributes-natural-language", "en")
          + _attr(0x45, "printer-uri", f"ipps://{host}/ipp/print") + _attr(0x21, "job-id", job_id)
          + _attr(0x44, "requested-attributes", "job-state") + _attr(0x44, "", "job-state-reasons"))
    return struct.pack(">BBHI", 2, 0, 0x0009, 2) + b"\x01" + op + b"\x03"


def parse_ipp(data: bytes) -> tuple[int, dict]:
    """(status code, {name: [values]}) of an IPP response. Pure."""
    if len(data) < 8:
        raise ValueError("short IPP response")
    status = struct.unpack(">H", data[2:4])[0]
    out: dict = {}
    pos, name = 8, None
    while pos < len(data):
        tag = data[pos]; pos += 1
        if tag < 0x10:
            if tag == 0x03:
                break
            continue
        nl = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        n = data[pos:pos + nl].decode(errors="replace"); pos += nl
        vl = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        v = data[pos:pos + vl]; pos += vl
        name = n or name
        val = struct.unpack(">i", v)[0] if tag in (0x21, 0x23) and len(v) == 4 else v.decode(errors="replace")
        out.setdefault(name, []).append(val)
    return status, out


def _ipp(host: str, body, length: int, timeout: float = 120.0) -> tuple[int, dict]:
    # ponytail: the Epson's certificate is self-signed and not pinned; LAN only. Pin its fingerprint if that ever matters.
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, 631, context=ctx, timeout=timeout)
    try:
        conn.request("POST", "/ipp/print", body=body, headers={"Content-Type": "application/ipp", "Content-Length": str(length)})
        r = conn.getresponse()
        data = r.read()
        if r.status != 200:
            raise OSError(f"IPP HTTP {r.status}")
        return parse_ipp(data)
    finally:
        conn.close()


class PrinterRefused(OSError):
    """The printer answered and said no: nothing will print."""


# Failures that prove no page could have come out: the printer refused the connection, or its address does not resolve or
# route, or it answered refusing the job. Anything else after approval (a timeout, a reset mid-send) stays "unknown".
def _never_reached(e: BaseException) -> bool:
    return isinstance(e, (PrinterRefused, ConnectionRefusedError, socket.gaierror)) or (
        isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH))


def send_to_printer(host: str, pwg_path: str, job_name: str, media: str, colour: bool, copies: int, two_sided: bool) -> int:
    head = print_job_request(host, job_name, media, colour, copies, two_sided)
    size = os.path.getsize(pwg_path)

    def body():
        yield head
        with open(pwg_path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                yield chunk
    status, attrs = _ipp(host, body(), len(head) + size, timeout=300.0)
    if status >= 0x0100 or not attrs.get("job-id"):
        raise PrinterRefused(f"the printer refused the job (IPP status 0x{status:04x})")
    return int(attrs["job-id"][0])


def follow(host: str, job_id: int, deadline: float, sleep=time.sleep, now=time.monotonic) -> tuple[bool | None, str]:
    """Poll the job until it ends: (printed?, a short reason). 9 completed, 7 canceled, 8 aborted."""
    reasons = ""
    while now() < deadline:
        q = job_attrs_request(host, job_id)
        try:
            status, attrs = _ipp(host, q, len(q), timeout=30.0)
            if status >= 0x0100:
                raise ValueError(f"IPP status 0x{status:04x}")
        except (OSError, ValueError) as e:
            reasons = f"lost the printer ({e})"
            sleep(10)
            continue
        state = (attrs.get("job-state") or [0])[0]
        reasons = ", ".join(str(x) for x in attrs.get("job-state-reasons", []))[:200]
        if state == 9:
            return True, "printed"
        if state in (7, 8):
            return False, f"the printer {'cancelled' if state == 7 else 'aborted'} it ({reasons})"
        sleep(10)
    return None, f"not finished after {FOLLOW_SECONDS // 60} minutes ({reasons or 'no answer'})"


def flush_reports(spool_dir: str, poster) -> bool:
    """Retry the receipt, never the physical print. Caller holds the paper worker lock."""
    path = Path(spool_dir) / "paper-outcome.json"
    try:
        try:
            rows = json.loads(path.read_text()) if path.exists() else []
            if not isinstance(rows, list) or any(not isinstance(r, dict) or not ID_RE.fullmatch(str(r.get("id", "")))
                    or not (isinstance(r.get("done"), bool) or r.get("uncertain") is True) for r in rows):
                raise ValueError("invalid paper outcome outbox")
        except ValueError as e:
            # A receipt nobody can read must not stop paper printing forever. Set it aside for a person to read; the site
            # turns the job it was about into a staff check after 45 minutes, because that job stays printing there.
            aside = path.with_name(f"paper-outcome.corrupt-{int(time.time())}.json")
            path.rename(aside)
            log.warning("paper outcome receipt unreadable (%s); set aside as %s, paper printing continues", e, aside.name)
            return True
        for row in rows:
            answer = poster(row)
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                raise ValueError("site did not acknowledge paper outcome")
        return not rows or report_outbox.save([], path)
    except Exception as e:  # noqa: BLE001 - keep the receipt and block new dispatch until it is acknowledged
        log.warning("paper outcome pending: %s", e)
        return False


# ── one job, end to end ──────────────────────────────────────────────────────────────────────────────────────────
def process(job: dict, host: str, spool_dir: str, *, getter, poster, runner=None, formats=None, sender=send_to_printer, follower=follow) -> str:
    """Fetch, measure, ask, print, report. Returns a short outcome for the log. `getter(path, dest)` downloads the file,
    `poster(body)` posts to /api/print/hub/paper and returns the site's JSON answer."""
    jid = job.get("id")
    if not isinstance(jid, str) or not ID_RE.match(jid) or job.get("fileType") not in FILES or job.get("media") not in MEDIA:
        return "skipped: bad job"
    if not HOST_RE.match(host or ""):
        return "skipped: no paper printer address"
    if not flush_reports(spool_dir, poster):
        return "outcome report pending"
    extended = job.get("preview") is True
    if job.get("fileType") not in BASE_FILES and not extended:
        poster({"id": jid, "measured": False, "reason": "This converted file needs a site version that shows its preview. Ask staff."})
        return "missing preview protocol"
    if extended or job.get("fileType") not in BASE_FILES or job.get("options") is not None:
        available = paper_formats() if formats is None else formats
        if job["fileType"] not in available:
            poster({"id": jid, "measured": False, "reason": "The paper converter is unavailable. Ask staff, or send a PDF with the original options."})
            return "missing paper capability"
    copies = job.get("copies") if isinstance(job.get("copies"), int) and 1 <= job["copies"] <= 10 else 1
    work = os.path.join(spool_dir, "paper-" + jid.replace("-", ""))
    receipt = Path(spool_dir) / "paper-outcome.json"
    try:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, mode=0o700)
        name = FILES[job["fileType"]]
        with open(os.path.join(work, "job.json"), "w", encoding="utf-8") as fh:
            config = {"file": name, "media": job["media"], "colour": job.get("colour") is True}
            if extended:
                config.update(options=job.get("options"), preview=True)
            json.dump(config, fh)
        try:
            getter(job.get("path"), os.path.join(work, name))
        except OSError as e:
            # a download hiccup is not the member's file being bad: leave the job queued, the next beat tries again
            log.warning("paper job %s: download failed, will retry: %s", jid, e)
            return "download failed; will retry"
        if not 0 < os.path.getsize(os.path.join(work, name)) <= 20 * 1024 * 1024:
            raise ValueError("Paper upload exceeds 20 MB")
        rc = (runner or (lambda uid: subprocess.run(["sudo", "-n", PAPER_RUNNER, uid], capture_output=True, timeout=660).returncode))(jid.replace("-", ""))
        try:
            result_file = Path(work) / "out" / "result.json"
            if result_file.stat().st_size > 768 * 1024:
                raise ValueError("Paper renderer result exceeds 768 KiB")
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"ok": False, "reason": f"the paper renderer did not answer (exit {rc})"}
        if not isinstance(result, dict):
            raise ValueError("Invalid paper renderer response")
        if not result.get("ok"):
            poster({"id": jid, "measured": False, "reason": str(result.get("reason") or "could not read the file")[:300]})
            return "refused by the renderer"
        answer = poster({"id": jid, **measurement(result, work, extended)})
        if not (isinstance(answer, dict) and answer.get("print") is True):
            return "not approved by the site"
        # A restart after sending cannot tell whether paper came out. Persist that uncertainty BEFORE sending.
        if not report_outbox.save([{"id": jid, "uncertain": True, "detail": "The hub restarted before confirming the printer outcome."}], receipt):
            # nothing was sent, so this is a plain failure and the member's pages come back; if this post is lost too, the
            # site's reaper makes it a staff check
            try:
                poster({"id": jid, "done": False, "detail": "not sent: the hub could not save its receipt"})
            except Exception:  # noqa: BLE001
                pass
            return "not sent: could not persist outcome guard"
        try:
            printer_job = sender(host, os.path.join(work, "out", "doc.pwg"), f"PS paper {jid[:8]}", job["media"], job.get("colour") is True,
                                 copies, job.get("twoSided") is True)
            ok, detail = follower(host, printer_job, time.monotonic() + FOLLOW_SECONDS)
        except Exception as e:  # noqa: BLE001 - bytes may have reached the printer before its reply was lost
            # a refused connection or a refused job printed nothing: a plain failure gives the member's pages back
            ok, detail = (False, f"not printed: {e}"[:300]) if _never_reached(e) else (None, f"printer outcome unknown: {type(e).__name__}")
        outcome = {"id": jid, "detail": detail[:300], **({"uncertain": True} if ok is None else {"done": ok})}
        if not report_outbox.save([outcome], receipt) or not flush_reports(spool_dir, poster):
            return "outcome report pending"
        return detail
    except Exception as e:  # noqa: BLE001 - the worker must report and keep the hub alive
        log.warning("paper job %s failed: %s", jid, e)
        try:
            poster({"id": jid, "measured": False, "reason": f"the hub hit an error: {type(e).__name__}"})
        except Exception:  # noqa: BLE001
            pass
        return f"error {type(e).__name__}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
