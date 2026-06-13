from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
import zipfile
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


SLICE_INFO_PATH = "Metadata/slice_info.config"
MAX_SLICE_INFO_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class UploadRecord:
    member_id: str
    filename: str
    file_path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ProjectFileIntent:
    member_id: str
    filename: str
    ams_mapping: Any
    ams_mapping2: Any
    use_ams: bool
    md5: str | None
    raw: dict[str, Any]
    plate: int | None = None


@dataclass(frozen=True)
class CapturedJob:
    member_id: str
    filename: str
    file_path: Path
    sha256: str
    size: int
    ams_mapping: Any
    use_ams: bool
    required_filaments: list[dict[str, Any]]
    submitted_at: datetime
    submission_uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    plate: int | None = None

    @property
    def file_sha256(self) -> str:
        return self.sha256


_CaptureKey = tuple[str, str]


@dataclass(frozen=True)
class _PendingUpload:
    record: UploadRecord
    expires_at: float

    @property
    def member_id(self) -> str:
        return self.record.member_id

    @property
    def filename(self) -> str:
        return self.record.filename


@dataclass(frozen=True)
class _PendingIntent:
    intent: ProjectFileIntent
    expires_at: float

    @property
    def member_id(self) -> str:
        return self.intent.member_id

    @property
    def filename(self) -> str:
        return self.intent.filename


_PendingItem = _PendingUpload | _PendingIntent


class CaptureCoordinator:
    def __init__(
        self,
        on_capture: Callable[[CapturedJob], None],
        log: Callable[[str], None],
        upload_wait_sec: float = 2.0,
        max_pending: int = 256,
        max_pending_per_key: int = 2,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.on_capture = on_capture
        self.log = log
        self.upload_wait_sec = upload_wait_sec
        self.max_pending = max(1, int(max_pending))
        self.max_pending_per_key = max(1, int(max_pending_per_key))
        self.clock = clock or time.monotonic
        self._uploads: OrderedDict[_CaptureKey, deque[_PendingUpload]] = OrderedDict()
        self._intents: OrderedDict[_CaptureKey, deque[_PendingIntent]] = OrderedDict()
        self._expiry_handle: asyncio.TimerHandle | None = None

    def record_upload(self, upload: UploadRecord) -> None:
        now = self.clock()
        self._prune_expired(now)
        key = _capture_key(upload.member_id, upload.filename)
        queue = self._uploads.setdefault(key, deque())
        queue.append(_PendingUpload(upload, now + self.upload_wait_sec))
        self._uploads.move_to_end(key)
        self._enforce_per_key_limit(self._uploads, key, "upload")
        self._enforce_total_limit(self._uploads, self.max_pending, "upload")
        self._try_capture(key)
        self._schedule_expiry()

    def record_project_file(self, intent: ProjectFileIntent) -> None:
        now = self.clock()
        self._prune_expired(now)
        key = _capture_key(intent.member_id, intent.filename)
        queue = self._intents.setdefault(key, deque())
        queue.append(_PendingIntent(intent, now + self.upload_wait_sec))
        self._intents.move_to_end(key)
        self._enforce_per_key_limit(self._intents, key, "project_file")
        self._enforce_total_limit(self._intents, self.max_pending, "project_file")
        self._try_capture(key)
        self._schedule_expiry()

    def clear(self) -> None:
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        self._uploads.clear()
        self._intents.clear()

    def _try_capture(self, key: "_CaptureKey") -> bool:
        upload_queue = self._uploads.get(key)
        intent_queue = self._intents.get(key)
        if not upload_queue or not intent_queue:
            return False
        if len(upload_queue) != 1 or len(intent_queue) != 1:
            member_id, filename = key
            self.log(
                "virtual printer capture skipped for "
                f"{filename!r} member_id={member_id!r}: ambiguous pending match "
                f"uploads={len(upload_queue)} project_files={len(intent_queue)}"
            )
            self._uploads.pop(key, None)
            self._intents.pop(key, None)
            return False
        upload = upload_queue[0].record
        intent = intent_queue[0].intent
        try:
            job = assemble_captured_job(upload, intent)
        except Exception as exc:  # noqa: BLE001 - observe-only hook must not sink protocol ACKs
            self.log(f"virtual printer capture skipped for {upload.filename!r}: {exc}")
            self._uploads.pop(key, None)
            self._intents.pop(key, None)
            return False
        upload_queue.popleft()
        intent_queue.popleft()
        if not upload_queue:
            self._uploads.pop(key, None)
        if not intent_queue:
            self._intents.pop(key, None)
        try:
            self.on_capture(job)
        except Exception as exc:  # noqa: BLE001 - capture is observe-only in V1
            self.log(f"virtual printer capture callback failed for {upload.filename!r}: {exc}")
        return True

    def _schedule_expiry(self) -> None:
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        next_expiry = self._next_expiry()
        if next_expiry is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, next_expiry - self.clock())
        self._expiry_handle = loop.call_later(delay, self._expire_pending)

    def _expire_pending(self) -> None:
        self._expiry_handle = None
        self._prune_expired(self.clock())
        self._schedule_expiry()

    def _prune_expired(self, now: float) -> None:
        self._prune_bucket(self._uploads, now, "upload")
        self._prune_bucket(self._intents, now, "project_file")

    def _prune_bucket(
        self,
        bucket: OrderedDict["_CaptureKey", deque["_PendingItem"]],
        now: float,
        label: str,
    ) -> None:
        for key in list(bucket.keys()):
            queue = bucket[key]
            while queue and queue[0].expires_at <= now:
                expired = queue.popleft()
                self.log(
                    "virtual printer capture timed out waiting for counterpart "
                    f"for {expired.filename!r} member_id={expired.member_id!r} ({label})"
                )
            if not queue:
                bucket.pop(key, None)

    def _enforce_per_key_limit(
        self,
        bucket: OrderedDict["_CaptureKey", deque["_PendingItem"]],
        key: "_CaptureKey",
        label: str,
    ) -> None:
        queue = bucket.get(key)
        if queue is None:
            return
        while len(queue) > self.max_pending_per_key:
            evicted = queue.popleft()
            self.log(
                "virtual printer capture evicted oldest pending "
                f"{label} for {evicted.filename!r} member_id={evicted.member_id!r}"
            )

    def _enforce_total_limit(
        self,
        bucket: OrderedDict["_CaptureKey", deque["_PendingItem"]],
        maximum: int,
        label: str,
    ) -> None:
        while _pending_count(bucket) > maximum:
            oldest_key = _oldest_key(bucket)
            if oldest_key is None:
                return
            queue = bucket[oldest_key]
            evicted = queue.popleft()
            self.log(
                "virtual printer capture evicted pending "
                f"{label} for {evicted.filename!r} member_id={evicted.member_id!r}: "
                "pending limit reached"
            )
            if not queue:
                bucket.pop(oldest_key, None)

    def _next_expiry(self) -> float | None:
        expiries = [
            queue[0].expires_at
            for bucket in (self._uploads, self._intents)
            for queue in bucket.values()
            if queue
        ]
        return min(expiries) if expiries else None


def _capture_key(member_id: str, filename: str) -> _CaptureKey:
    return member_id, filename


def _pending_count(bucket: OrderedDict[_CaptureKey, deque[_PendingItem]]) -> int:
    return sum(len(queue) for queue in bucket.values())


def _oldest_key(bucket: OrderedDict[_CaptureKey, deque[_PendingItem]]) -> _CaptureKey | None:
    candidates = [(queue[0].expires_at, key) for key, queue in bucket.items() if queue]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def assemble_captured_job(
    upload: UploadRecord,
    intent: ProjectFileIntent,
    *,
    submitted_at: datetime | None = None,
) -> CapturedJob:
    if upload.filename != intent.filename:
        raise ValueError("upload and project_file filenames do not match")
    if upload.member_id != intent.member_id:
        raise ValueError("upload and project_file member ids do not match")
    if intent.md5 is not None:
        actual_md5 = md5_file(upload.file_path)
        if actual_md5.lower() != intent.md5.lower():
            raise ValueError("project_file md5 does not match uploaded file")
    ams_mapping = intent.ams_mapping
    if intent.ams_mapping2 is not None:
        ams_mapping = {"ams_mapping": intent.ams_mapping, "ams_mapping2": intent.ams_mapping2}
    return CapturedJob(
        member_id=upload.member_id,
        filename=upload.filename,
        file_path=upload.file_path,
        sha256=upload.sha256,
        size=upload.size,
        ams_mapping=ams_mapping,
        use_ams=intent.use_ams,
        required_filaments=parse_required_filaments(upload.file_path),
        submitted_at=submitted_at or datetime.now(timezone.utc),
        plate=intent.plate,
    )


def build_vp_submit_body(job: CapturedJob, *, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "hubSubmissionUid": job.submission_uid,
        "memberId": job.member_id,
        "fileName": job.filename,
        "fileSha256": job.file_sha256,
        "fileSizeBytes": job.size,
        "printerModel": model,
        "useAms": job.use_ams,
        # The cloud contract is amsMapping: number[]. When a print carries both
        # ams_mapping + ams_mapping2 the capture stores a dict; flatten to the
        # primary list so the body validates (ams_mapping2 / multi-AMS fidelity
        # is deferred to the V3 matcher contract).
        "amsMapping": _ams_mapping_list(job.ams_mapping),
        "requiredFilaments": [_vp_submit_filament(item) for item in job.required_filaments],
    }
    if job.plate is not None:
        body["plate"] = job.plate
    return body


def _ams_mapping_list(ams_mapping: Any) -> list[Any]:
    if isinstance(ams_mapping, list):
        return ams_mapping
    if isinstance(ams_mapping, dict):
        primary = ams_mapping.get("ams_mapping")
        if isinstance(primary, list):
            return primary
    return []


def _vp_submit_filament(item: dict[str, Any]) -> dict[str, Any]:
    filament: dict[str, Any] = {}
    if "slot" in item:
        filament["slot"] = item["slot"]
    filament_type = item.get("type") or item.get("material") or item.get("tray_type")
    if filament_type is not None:
        filament["type"] = filament_type
    color = item.get("color") or item.get("tray_color")
    if color is not None:
        filament["color"] = color
    tray_info_idx = item.get("trayInfoIdx") or item.get("tray_info_idx")
    if tray_info_idx is not None:
        filament["trayInfoIdx"] = tray_info_idx
    return filament


def parse_project_file_command(parsed: Any, member_id: str) -> ProjectFileIntent | None:
    if not isinstance(parsed, dict):
        return None
    print_obj = parsed.get("print")
    if not isinstance(print_obj, dict) or print_obj.get("command") not in ("project_file", "gcode_file"):
        return None
    filename = filename_from_project_file(print_obj)
    md5 = print_obj.get("md5")
    md5 = md5.strip() if isinstance(md5, str) and md5.strip() else None
    return ProjectFileIntent(
        member_id=member_id,
        filename=filename,
        ams_mapping=print_obj.get("ams_mapping"),
        ams_mapping2=print_obj.get("ams_mapping2"),
        use_ams=_boolish(print_obj.get("use_ams")),
        md5=md5,
        raw=dict(print_obj),
        plate=_resolve_plate(print_obj),
    )


def _resolve_plate(print_obj: dict[str, Any]) -> int | None:
    plate = _optional_int(print_obj.get("plate"))
    if plate is not None:
        return plate
    # Bambu often encodes the plate only in `param`/`url`, e.g.
    # "Metadata/plate_1.gcode" -> plate 1.
    for key in ("param", "url"):
        value = print_obj.get(key)
        if isinstance(value, str):
            match = re.search(r"plate_(\d+)", value)
            if match:
                return _optional_int(match.group(1))
    return None


def filename_from_project_file(print_obj: dict[str, Any]) -> str:
    for key in ("file", "subtask_name", "gcode_file"):
        value = print_obj.get(key)
        if isinstance(value, str) and value.strip():
            name = Path(value.strip()).name
            return name if name.endswith(".3mf") else f"{name}.3mf"
    return "job.3mf"


def parse_required_filaments(path: Path) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(SLICE_INFO_PATH)
            if info.file_size > MAX_SLICE_INFO_BYTES:
                return []
            raw = archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile):
        return []
    return parse_slice_info_config(raw)


def parse_slice_info_config(raw: bytes | str) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return _parse_slice_info_fallback(text)

    by_slot: dict[int, dict[str, Any]] = {}
    _merge_array_metadata(root, by_slot)

    for element in root.iter():
        attrs = {_strip_ns(key).lower(): value for key, value in element.attrib.items()}
        tag = _strip_ns(element.tag).lower()
        if not any(token in tag for token in ("filament", "slot", "tray")):
            continue
        slot = _first_int(attrs, ("slot", "id", "index", "idx", "extruder", "filament_id"))
        material = _first_str(attrs, ("material", "type", "tray_type", "filament_type"))
        color = _first_str(
            attrs,
            ("color", "colour", "tray_color", "filament_color", "filament_colour"),
        )
        if slot is None or (material is None and color is None):
            continue
        item = by_slot.setdefault(slot, {"slot": slot})
        if material:
            item["material"] = material
        normalized = normalize_color(color)
        if normalized:
            item["color"] = normalized

    return [by_slot[slot] for slot in sorted(by_slot)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - protocol metadata verification requires MD5.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"').lstrip("#").upper()
    if len(cleaned) == 6 and _is_hex(cleaned):
        return cleaned + "FF"
    if len(cleaned) == 8 and _is_hex(cleaned):
        return cleaned
    return None


def _merge_array_metadata(root: ElementTree.Element, by_slot: dict[int, dict[str, Any]]) -> None:
    metadata: dict[str, list[str]] = {}
    for element in root.iter():
        tag = _strip_ns(element.tag).lower()
        if tag != "metadata":
            continue
        attrs = {_strip_ns(key).lower(): value for key, value in element.attrib.items()}
        key = attrs.get("key") or attrs.get("name")
        value = attrs.get("value")
        if value is None and element.text:
            value = element.text
        if key and value is not None:
            metadata[key.lower()] = _split_values(value)

    type_values = (
        metadata.get("filament_type")
        or metadata.get("filament_types")
        or metadata.get("filament_material")
        or []
    )
    color_values = (
        metadata.get("filament_colour")
        or metadata.get("filament_color")
        or metadata.get("filament_colours")
        or metadata.get("filament_colors")
        or []
    )
    for slot in range(max(len(type_values), len(color_values))):
        material = type_values[slot].strip() if slot < len(type_values) else ""
        color = normalize_color(color_values[slot]) if slot < len(color_values) else None
        if not material and not color:
            continue
        item = by_slot.setdefault(slot, {"slot": slot})
        if material:
            item["material"] = material
        if color:
            item["color"] = color


def _parse_slice_info_fallback(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pattern = re.compile(
        r"filament[_\s-]*(?P<slot>\d+).*?"
        r"(?P<material>PLA(?:-CF)?|PETG|ABS|ASA|TPU|PA|PC|PVA|HIPS)?"
        r".*?(?P<color>#[0-9a-fA-F]{6,8}|[0-9a-fA-F]{8})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        item: dict[str, Any] = {"slot": int(match.group("slot"))}
        material = match.group("material")
        if material:
            item["material"] = material.upper()
        color = normalize_color(match.group("color"))
        if color:
            item["color"] = color
        entries.append(item)
    return entries


def _split_values(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [part.strip().strip('"') for part in re.split(r"[;,]", raw) if part.strip()]


def _first_int(attrs: dict[str, str], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = attrs.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _first_str(attrs: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _strip_ns(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _is_hex(value: str) -> bool:
    return all(ch in "0123456789ABCDEF" for ch in value)
