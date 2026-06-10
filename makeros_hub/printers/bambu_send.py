"""Bambu local send helpers: implicit FTPS upload + print-start payload build.

This module is deliberately stdlib-only so tests and non-printer paths do not
need paho installed.
"""

from __future__ import annotations

import ftplib
import os
import ssl
from pathlib import Path
from typing import BinaryIO

_UPLOAD_ERRORS = ftplib.all_errors + (ssl.SSLError,)


class BambuSendError(Exception):
    """A send/upload failure safe to surface in logs.

    The LAN access code is never included in this exception's message.
    """


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS variant for Bambu's implicit FTPS server on port 990."""

    @property
    def sock(self):
        return getattr(self, "_sock", None)

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size

    def storbinary(
        self,
        cmd: str,
        fp: BinaryIO,
        blocksize: int = 8192,
        callback=None,
        rest=None,
    ):
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except OSError:
                    pass
        finally:
            conn.close()
        return self.voidresp()


def upload_3mf(host: str, access_code: str, local_path: str | os.PathLike, remote_name: str) -> None:
    """Upload a sliced 3MF to the printer's FTPS root.

    Callers serialize per-printer sends. The remote name must be a root-level
    file name because Bambu's print-start URL points at ftp:///<file>.
    """
    if not remote_name or os.path.basename(remote_name) != remote_name:
        raise BambuSendError("remote_name must be a root-level file name")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = ImplicitFTP_TLS(context=ctx)
    try:
        ftp.connect(host, 990, timeout=30)
        ftp.login("bblp", access_code)
        ftp.prot_p()
        with Path(local_path).open("rb") as fp:
            ftp.storbinary(f"STOR {remote_name}", fp, blocksize=32768)
        try:
            ftp.quit()
        except _UPLOAD_ERRORS:
            ftp.close()
    except _UPLOAD_ERRORS as exc:
        ftp.close()
        raise BambuSendError("Bambu FTPS upload failed") from exc


def build_print_start_payload(
    file_name: str,
    *,
    plate: int = 1,
    use_ams: bool = False,
    ams_mapping=None,
    sequence_id,
    subtask_name: str | None = None,
    bed_type: str = "textured_plate",
) -> dict:
    """Build the exact MQTT project_file command for a root-uploaded 3MF."""
    task_name = subtask_name or os.path.splitext(os.path.basename(file_name))[0]
    return {
        "print": {
            "command": "project_file",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "param": f"Metadata/plate_{plate}.gcode",
            "file": file_name,
            "url": f"ftp:///{file_name}",
            "subtask_name": task_name,
            "bed_type": bed_type,
            "bed_leveling": True,
            "bed_levelling": True,
            "flow_cali": False,
            "vibration_cali": True,
            "layer_inspect": False,
            "timelapse": False,
            "md5": "",
            "use_ams": use_ams,
            "ams_mapping": _coerce_ams_mapping(ams_mapping),
            "sequence_id": str(sequence_id),
        }
    }


def _coerce_ams_mapping(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parts = [part.strip() for part in text.split(",")]
        if any(part == "" for part in parts):
            return []
        try:
            return [int(part) for part in parts]
        except ValueError:
            return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                return []
            out.append(item)
        return out
    return []
