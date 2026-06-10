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


_DISPOSITION_NAME = re.compile(rb'name="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(rb'filename="([^"]*)"', re.IGNORECASE)


def parse_multipart(body: bytes, boundary: str) -> ParsedForm:
    """Parse a multipart/form-data body into text fields + the single file part.

    Binary-safe: works entirely in bytes and never decodes the file payload.
    Raises MultipartError on a malformed body."""
    if not boundary:
        raise MultipartError("empty boundary")
    delim = b"--" + boundary.encode("latin-1")
    # Each part is preceded by the delimiter; the stream ends with delim + "--".
    # Split and drop the preamble (before the first delim) and the closing chunk.
    chunks = body.split(delim)
    fields: dict[str, str] = {}
    file_part: FilePart | None = None

    for chunk in chunks:
        # Skip the preamble (''), the closing '--\r\n', and stray empties.
        if chunk in (b"", b"--\r\n", b"--", b"\r\n"):
            continue
        if chunk.startswith(b"--"):  # closing delimiter tail
            continue
        # A part starts with CRLF after the delimiter, then headers, then CRLFCRLF,
        # then the body, then a trailing CRLF before the next delimiter.
        part = chunk
        if part.startswith(b"\r\n"):
            part = part[2:]
        header_blob, sep, rest = part.partition(b"\r\n\r\n")
        if not sep:
            continue  # not a well-formed part
        # Strip exactly one trailing CRLF that precedes the next delimiter.
        data = rest[:-2] if rest.endswith(b"\r\n") else rest

        name_m = _DISPOSITION_NAME.search(header_blob)
        if not name_m:
            continue
        name = name_m.group(1).decode("utf-8", "replace")
        filename_m = _DISPOSITION_FILENAME.search(header_blob)
        if filename_m:
            filename = filename_m.group(1).decode("utf-8", "replace")
            if filename:  # a file part
                file_part = FilePart(filename=filename, data=data)
                continue
        # A plain text field.
        fields[name] = data.decode("utf-8", "replace")

    return ParsedForm(fields=fields, file=file_part)
