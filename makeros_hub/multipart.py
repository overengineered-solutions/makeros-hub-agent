"""Streaming multipart/form-data parser — stdlib only, binary-safe, bounded memory.

OrcaSlicer (PrusaSlicer's OctoPrint host) POSTs `multipart/form-data` to
`/api/files/local` with fields `file` (the sliced 3MF/gcode, carrying a
filename), `print` ("true"/"false"), and `path`. We need only those.

Python's `cgi.FieldStorage` is removed in 3.13 and `email`-based parsing copies
the (large) binary file through str-ish layers; a bytes-level boundary scanner
keeps the file out of memory.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import re
from dataclasses import dataclass


@dataclass
class FilePart:
    filename: str
    data: bytes


@dataclass
class ParsedForm:
    fields: dict[str, str]
    file: FilePart | None


class MultipartError(Exception):
    pass


def boundary_from_content_type(content_type: str | None) -> str | None:
    """Extract the boundary token from a `multipart/form-data; boundary=...`
    Content-Type header. Returns None if not multipart."""
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.IGNORECASE)
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip()


_DISPOSITION_NAME = re.compile(rb'(?:^|;\s*)name="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(rb'filename="([^"]*)"', re.IGNORECASE)


class UploadTooLarge(MultipartError):
    pass


def stream_multipart(chunks, boundary: str, out, max_bytes: int) -> tuple[str | None, dict, str, int]:
    """Write the one file part to out; keep only a chunk, headers and small text fields in memory."""
    if not boundary or len(boundary) > 70 or "\r" in boundary or "\n" in boundary:
        raise MultipartError("invalid boundary")
    delim = b"--" + boundary.encode("latin-1")
    buf, state = b"", "boundary"
    filename, fields = None, {}
    name, is_file, field = None, False, b""
    digest, size, parts = hashlib.sha256(), 0, 0

    for chunk in itertools.chain(chunks, [b""]):
        eof = not chunk
        buf += chunk
        while True:
            if state == "boundary":
                if len(buf) < len(delim) + 2:
                    break
                if not buf.startswith(delim):
                    raise MultipartError("invalid multipart framing")
                suffix = buf[len(delim):len(delim) + 2]
                buf = buf[len(delim) + 2:]
                if suffix == b"--":
                    state = "end"
                elif suffix == b"\r\n":
                    parts += 1
                    if parts > 64:
                        raise MultipartError("too many parts")
                    state = "headers"
                else:
                    raise MultipartError("invalid multipart delimiter")
            elif state == "headers":
                end = buf.find(b"\r\n\r\n")
                if end > 8192 or (end < 0 and len(buf) > 8195):
                    raise MultipartError("invalid multipart headers")
                if end < 0:
                    break
                headers, buf = buf[:end], buf[end + 4:]
                name_m = _DISPOSITION_NAME.search(headers)
                filename_m = _DISPOSITION_FILENAME.search(headers)
                name = name_m.group(1).decode("utf-8", "replace") if name_m else None
                is_file = bool(name is not None and filename_m and filename_m.group(1))
                if is_file:
                    if filename is not None or name != "file":
                        raise MultipartError("expected one file field")
                    filename = filename_m.group(1).decode("utf-8", "replace")
                field, state = b"", "data"
            elif state == "data":
                ending = rb"(?=\r\n|--(?:\r\n|$))" if eof else rb"(?=\r\n|--\r\n)"
                match = re.search(rb"\r\n" + re.escape(delim) + ending, buf)
                # Retain a delimiter plus its suffix even when split at any byte between reads.
                end = match.start() if match else max(0, len(buf) - len(delim) - 6)
                data, buf = buf[:end], buf[end:]
                if is_file:
                    size += len(data)
                    if size > max_bytes:
                        raise UploadTooLarge("file too large")
                    out.write(data)
                    digest.update(data)
                else:
                    field += data
                    if len(field) > 1024:
                        raise MultipartError("multipart field too large")
                if not match:
                    break
                if not is_file and name is not None:
                    fields[name] = field.decode("utf-8", "replace")
                buf, state = buf[2:], "boundary"
            else:
                if len(buf) > 2 or not b"\r\n".startswith(buf):
                    raise MultipartError("invalid multipart ending")
                break
        if eof and (state != "end" or buf not in (b"", b"\r\n")):
            raise MultipartError("truncated multipart body")
    return filename, fields, digest.hexdigest(), size


def parse_multipart(body: bytes, boundary: str) -> ParsedForm:
    """In-memory convenience for small bodies; ingest uses stream_multipart directly."""
    out = io.BytesIO()
    chunks = (body[i:i + 256 * 1024] for i in range(0, len(body), 256 * 1024))
    filename, fields, _sha, _size = stream_multipart(chunks, boundary, out, len(body))
    return ParsedForm(fields, FilePart(filename, out.getvalue()) if filename is not None else None)
