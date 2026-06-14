from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import SPOOL_DIR
from .capture import CapturedJob

log = logging.getLogger("makeros-hub.vprinter.outbox")

SUBMISSION_UID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_submission_uid(submission_uid: str) -> str:
    value = str(submission_uid)
    if not SUBMISSION_UID_RE.fullmatch(value):
        raise ValueError(f"invalid virtual printer submission uid: {value!r}")
    return value


def to_record(job: CapturedJob) -> dict[str, Any]:
    return {
        "record_version": 1,
        "member_id": job.member_id,
        "filename": job.filename,
        "file_path": str(job.file_path),
        "sha256": job.sha256,
        "size": job.size,
        "ams_mapping": job.ams_mapping,
        "use_ams": job.use_ams,
        "required_filaments": job.required_filaments,
        "submitted_at": job.submitted_at.isoformat(),
        "submission_uid": validate_submission_uid(job.submission_uid),
        "plate": job.plate,
        "attempts": job.attempts,
    }


def from_record(record: dict[str, Any]) -> CapturedJob:
    if not isinstance(record, dict):
        raise ValueError("virtual printer outbox record must be a JSON object")

    submission_uid = _str_field(
        record,
        "submission_uid",
        "submissionUid",
        "hubSubmissionUid",
        default=None,
    )
    kwargs: dict[str, Any] = {
        "member_id": _str_field(record, "member_id", "memberId", default="") or "",
        "filename": _str_field(record, "filename", "fileName", default="") or "",
        "file_path": Path(
            _str_field(record, "file_path", "filePath", "filename", "fileName", default="")
            or ""
        ),
        "sha256": _str_field(record, "sha256", "file_sha256", "fileSha256", default="") or "",
        "size": _int_field(record, "size", "file_size", "fileSizeBytes", default=0),
        "ams_mapping": _field(record, "ams_mapping", "amsMapping", default=[]),
        "use_ams": _bool_field(record, "use_ams", "useAms", default=False),
        "required_filaments": _list_field(
            record,
            "required_filaments",
            "requiredFilaments",
            default=[],
        ),
        "submitted_at": _datetime_field(record, "submitted_at", "submittedAt"),
        "plate": _optional_int_field(record, "plate", default=None),
        "attempts": max(0, _int_field(record, "attempts", default=0)),
    }
    if submission_uid is not None:
        kwargs["submission_uid"] = validate_submission_uid(submission_uid)
    return CapturedJob(**kwargs)


class VPrinterOutbox:
    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else SPOOL_DIR / "vp-outbox"
        self._lock = threading.Lock()

    def persist(self, job: CapturedJob) -> None:
        record = to_record(job)
        uid = validate_submission_uid(job.submission_uid)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            final_path = self._path_for(uid)
            tmp_path = self.directory / f".{uid}.json.tmp"
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, final_path)
            _fsync_dir(self.directory)

    def remove(self, submission_uid: str) -> None:
        uid = validate_submission_uid(submission_uid)
        with self._lock:
            try:
                self._path_for(uid).unlink()
            except FileNotFoundError:
                return
            _fsync_dir(self.directory)

    def load_all(self) -> list[CapturedJob]:
        jobs: list[CapturedJob] = []
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.glob("*.json")):
                try:
                    uid = validate_submission_uid(path.stem)
                    with path.open("r", encoding="utf-8") as fh:
                        record = json.load(fh)
                    if isinstance(record, dict) and "submission_uid" not in record:
                        record = {**record, "submission_uid": uid}
                    job = from_record(record)
                    if job.submission_uid != uid:
                        raise ValueError("submission uid does not match outbox filename")
                except Exception as exc:  # noqa: BLE001 - one bad record must not break boot
                    log.warning("vprinter.outbox.corrupt path=%s error=%s", path, exc)
                    self._quarantine(path)
                    continue
                jobs.append(job)
        return jobs

    def _path_for(self, submission_uid: str) -> Path:
        uid = validate_submission_uid(submission_uid)
        return self.directory / f"{uid}.json"

    def _quarantine(self, path: Path) -> None:
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
            _fsync_dir(self.directory)
        except OSError as exc:
            log.warning("vprinter.outbox.quarantine_failed path=%s error=%s", path, exc)


def _field(record: dict[str, Any], *names: str, default: Any) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return default


def _str_field(record: dict[str, Any], *names: str, default: str | None) -> str | None:
    value = _field(record, *names, default=default)
    if value is None:
        return None
    return str(value)


def _int_field(record: dict[str, Any], *names: str, default: int) -> int:
    value = _field(record, *names, default=default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int_field(record: dict[str, Any], *names: str, default: int | None) -> int | None:
    value = _field(record, *names, default=default)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_field(record: dict[str, Any], *names: str, default: bool) -> bool:
    value = _field(record, *names, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _list_field(record: dict[str, Any], *names: str, default: list[Any]) -> list[Any]:
    value = _field(record, *names, default=default)
    return value if isinstance(value, list) else list(default)


def _datetime_field(record: dict[str, Any], *names: str) -> datetime:
    value = _field(record, *names, default=None)
    if isinstance(value, str):
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(timezone.utc)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
