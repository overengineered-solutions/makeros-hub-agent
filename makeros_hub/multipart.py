"""Pure multipart/form-data parser — stdlib only, binary-safe, no I/O, so the
OrcaSlicer upload parse is fully unit-testable without a socket.

OrcaSlicer (PrusaSlicer's OctoPrint host) POSTs `multipart/form-data` to
`/api/files/local` with fields `file` (the sliced 3MF/gcode, carrying a
filename), `print` ("true"/"false"), and `path`. We need only those.

Python's `cgi.FieldStorage` is removed in 3.13 and `email`-based parsing copies
the (large) binary file through str-ish layers; a tight bytes-level boundary
split is simpler and correct for our one shape.
"""

from __future__ import annotations

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


def parse_multipart(body: bytes, boundary: str) -> ParsedForm:
    """Parse a multipart/form-data body into text fields + the single file part.

    Binary-safe: works entirely in bytes and never decodes the file payload.
    Raises MultipartError on a malformed body."""
    if not boundary or len(boundary) > 70 or "\r" in boundary or "\n" in boundary:
        raise MultipartError("empty boundary")
    delim = b"--" + boundary.encode("latin-1")
    # Each part is preceded by the delimiter; the stream ends with delim + "--".
    # Split and drop the preamble (before the first delim) and the closing chunk.
    # A boundary is a complete delimiter line. A same-looking byte sequence
    # inside the binary file must survive unchanged. Bound part count as well.
    chunks = re.split(rb"\r\n" + re.escape(delim) + rb"(?=\r\n|--(?:\r\n|$))", b"\r\n" + body, maxsplit=65)
    if chunks[0] != b"" or chunks[-1] not in (b"--", b"--\r\n") or len(chunks) > 65:
        raise MultipartError("invalid multipart framing or too many parts")
    fields: dict[str, str] = {}
    file_part: FilePart | None = None

    for chunk in chunks[1:-1]:
        # A part starts with CRLF after the delimiter, then headers, then CRLFCRLF,
        # then the body, then a trailing CRLF before the next delimiter.
        part = chunk
        if part.startswith(b"\r\n"):
            part = part[2:]
        header_blob, sep, rest = part.partition(b"\r\n\r\n")
        if not sep or len(header_blob) > 8192:
            raise MultipartError("invalid multipart headers")
        # The delimiter split consumed its preceding CRLF, not the file's bytes.
        data = rest

        name_m = _DISPOSITION_NAME.search(header_blob)
        if not name_m:
            continue
        name = name_m.group(1).decode("utf-8", "replace")
        filename_m = _DISPOSITION_FILENAME.search(header_blob)
        if filename_m:
            filename = filename_m.group(1).decode("utf-8", "replace")
            if filename:  # a file part
                if file_part is not None or name != "file":
                    raise MultipartError("expected one file field")
                file_part = FilePart(filename=filename, data=data)
                continue
        # A plain text field.
        if len(data) > 1024:
            raise MultipartError("multipart field too large")
        fields[name] = data.decode("utf-8", "replace")

    return ParsedForm(fields=fields, file=file_part)
