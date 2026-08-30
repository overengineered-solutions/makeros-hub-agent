from __future__ import annotations

import asyncio
import hashlib
import json
import math
import logging
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

log = logging.getLogger("makeros-hub.vprinter.capture")


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
    attempts: int = 0
    # The Virtual Printer that captured the job — its model is the job's target model (each VP is one
    # model group) and its serial lets the cloud dedupe a double Send per VP.
    vp_serial: str = ""
    vp_model: str = ""
    # OrcaSlicer's own `print` command as sent to the VP (bounded copy, v0.51). The hub REPLAYS it to the assigned
    # printer (bambu_send.build_print_start_payload raw_print=...) so every option the member chose — plate, bed
    # type, calibration flags, timelapse, and the H2D's dual-nozzle `ams_mapping2`/nozzle fields we cannot
    # enumerate — reaches the machine verbatim; only file/url/ids/sequence are forced. None = no captured
    # command (web upload) → the dispatcher rebuilds the command exactly as before.
    raw_print: dict[str, Any] | None = None

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
        vp_serial: str = "",
        vp_model: str = "",
        intent_dedupe_sec: float = 30.0,
    ) -> None:
        self.on_capture = on_capture
        self.log = log
        self.upload_wait_sec = upload_wait_sec
        self.vp_serial = vp_serial
        self.vp_model = vp_model
        self.intent_dedupe_sec = intent_dedupe_sec
        self._recent_intents: OrderedDict[tuple, float] = OrderedDict()
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
        if self._is_redelivery(intent, now):
            self.log(
                f"virtual printer capture ignored re-delivered project_file for {intent.filename!r} "
                f"member_id={intent.member_id!r} sequence_id={intent.raw.get('sequence_id')!r}"
            )
            return
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
        self._recent_intents.clear()

    def _is_redelivery(self, intent: ProjectFileIntent, now: float) -> bool:
        """A QoS1 re-delivered project_file (same member, filename, sequence_id inside the window) must
        not pair with the member's NEXT upload. OrcaSlicer numbers every command, so a genuine second
        Send carries a new sequence_id. ponytail: with no sequence_id the identity is the whole raw
        command — two byte-identical genuine Sends of one file inside the window would then collapse;
        add a per-upload nonce if a slicer ever sends unnumbered commands."""
        for ident, expires_at in list(self._recent_intents.items()):
            if expires_at <= now:
                del self._recent_intents[ident]
        seq = intent.raw.get("sequence_id")
        tag = str(seq) if seq not in (None, "") else json.dumps(intent.raw, sort_keys=True, default=str)
        ident = (intent.member_id, intent.filename, tag)
        if ident in self._recent_intents:
            return True
        self._recent_intents[ident] = now + self.intent_dedupe_sec
        while len(self._recent_intents) > self.max_pending:
            self._recent_intents.popitem(last=False)
        return False

    def _try_capture(self, key: "_CaptureKey") -> bool:
        """Pair pending uploads and project_file intents for one (member, filename) FIFO: OrcaSlicer's
        Send is upload-then-command, so the oldest of each belong together. A member sending the same
        filename twice inside the window therefore yields TWO jobs (RC8) instead of an "ambiguous" drop.
        The cloud dedupes a double Send per VP; a re-delivered command is filtered before it is queued
        (_is_redelivery)."""
        upload_queue = self._uploads.get(key)
        intent_queue = self._intents.get(key)
        captured = False
        while upload_queue and intent_queue:
            upload = upload_queue.popleft().record
            intent = intent_queue.popleft().intent
            try:
                job = assemble_captured_job(upload, intent, vp_serial=self.vp_serial, vp_model=self.vp_model)
            except Exception as exc:  # noqa: BLE001 - observe-only hook must not sink protocol ACKs
                self.log(f"virtual printer capture skipped for {upload.filename!r}: {exc}")
                continue
            try:
                self.on_capture(job)
            except Exception as exc:  # noqa: BLE001 - capture is observe-only in V1
                self.log(f"virtual printer capture callback failed for {upload.filename!r}: {exc}")
            captured = True
        if not upload_queue:
            self._uploads.pop(key, None)
        if not intent_queue:
            self._intents.pop(key, None)
        return captured

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


RAW_PRINT_MAX_BYTES = 16 * 1024


def replayable_print(raw: Any) -> dict[str, Any] | None:
    """A bounded, JSON-clean copy of the member's `print` command for replay; None when absent, unencodable or
    oversized (the dispatcher then falls back to the rebuilt command, exactly as before v0.51)."""
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        encoded = json.dumps(raw, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):   # RecursionError: a pathologically nested command (codex)
        return None
    if len(encoded) > RAW_PRINT_MAX_BYTES:
        log.warning(
            "vprinter.capture raw print command is %d bytes (> %d) — not replaying it",
            len(encoded),
            RAW_PRINT_MAX_BYTES,
        )
        return None
    return json.loads(encoded)


def assemble_captured_job(
    upload: UploadRecord,
    intent: ProjectFileIntent,
    *,
    submitted_at: datetime | None = None,
    vp_serial: str = "",
    vp_model: str = "",
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
    submission_uid = _deterministic_submission_uid(
        upload.member_id,
        upload.sha256,
        intent.plate,
        ams_mapping,
        upload.file_path.name,
    )
    return CapturedJob(
        member_id=upload.member_id,
        filename=upload.filename,
        file_path=upload.file_path,
        sha256=upload.sha256,
        size=upload.size,
        ams_mapping=ams_mapping,
        use_ams=intent.use_ams,
        required_filaments=parse_required_filaments(upload.file_path, intent.plate),
        submitted_at=submitted_at or datetime.now(timezone.utc),
        submission_uid=submission_uid,
        plate=intent.plate,
        vp_serial=vp_serial,
        vp_model=vp_model,
        raw_print=replayable_print(intent.raw),
    )


def build_vp_submit_body(job: CapturedJob, *, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "hubSubmissionUid": job.submission_uid,
        "memberId": job.member_id,
        "fileName": job.filename,
        "fileSha256": job.file_sha256,
        "fileSizeBytes": job.size,
        # The capturing VP's model is the job's target model; `model` (the hub's first live VP) is only
        # the fallback for outbox records written before vp_model existed.
        "printerModel": job.vp_model or model,
        "useAms": job.use_ams,
        # The cloud contract is amsMapping: number[]. When a print carries both
        # ams_mapping + ams_mapping2 the capture stores a dict; flatten to the
        # primary list so the body validates (ams_mapping2 / multi-AMS fidelity
        # is deferred to the V3 matcher contract).
        "amsMapping": _ams_mapping_list(job.ams_mapping),
        "requiredFilaments": [_vp_submit_filament(item) for item in job.required_filaments],
    }
    # estGrams (v0.52): the plate's total slicer weight, CEILed so the shop never undercharges (makeros rule); absent
    # when no filament carried used_g (old 3MFs) — the cloud then parks the completion for staff grams as before.
    grams = [item.get("usedG") for item in job.required_filaments if isinstance(item.get("usedG"), (int, float)) and not isinstance(item.get("usedG"), bool)]
    if grams:
        body["estGrams"] = int(math.ceil(sum(grams)))
    if job.plate is not None:
        body["plate"] = job.plate
    # Additive (cloud-side double-Send dedupe on member + VP + sha + plate + mapping): the mapping exactly
    # as OrcaSlicer sent it (list, or {ams_mapping, ams_mapping2} for dual-nozzle prints) + the VP serial.
    if job.ams_mapping is not None:
        body["amsMappingRaw"] = job.ams_mapping
    if job.vp_serial:
        body["vpSerial"] = job.vp_serial
    # Additive (agent v0.51): the member's own print command; the cloud stores it and hands it back in the
    # assignment so dispatch replays it (see CapturedJob.raw_print).
    if isinstance(job.raw_print, dict) and job.raw_print:
        body["rawPrint"] = job.raw_print
    return body


def _ams_mapping_list(ams_mapping: Any) -> list[Any]:
    if isinstance(ams_mapping, list):
        return ams_mapping
    if isinstance(ams_mapping, dict):
        primary = ams_mapping.get("ams_mapping")
        if isinstance(primary, list):
            return primary
    return []


def _deterministic_submission_uid(
    member_id: str,
    file_sha256: str,
    plate: int | None,
    ams_mapping: Any,
    upload_name: str,
) -> str:
    """Stable for one capture (an agent retry re-sends the same uid), distinct per UPLOAD: the spooled
    file name is unique per STOR, so a member's second Send of the same bytes is its own job (RC8)."""
    ams_mapping_json = json.dumps(
        _ams_mapping_list(ams_mapping),
        sort_keys=True,
        separators=(",", ":"),
    )
    plate_value = "" if plate is None else str(plate)
    payload = f"{member_id}\n{file_sha256}\n{plate_value}\n{ams_mapping_json}\n{upload_name}"
    return hashlib.sha256(payload.encode()).hexdigest()


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
    used_g = item.get("usedG")
    if isinstance(used_g, (int, float)) and not isinstance(used_g, bool) and used_g >= 0:
        filament["usedG"] = used_g
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


def parse_required_filaments(path: Path, plate: int | None = None) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(SLICE_INFO_PATH)
            if info.file_size > MAX_SLICE_INFO_BYTES:
                return []
            raw = archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile):
        return []
    return parse_slice_info_config(raw, plate)


def _plate_element(root: ElementTree.Element, plate: int | None) -> ElementTree.Element | None:
    """The <plate> whose <metadata key="index"> equals the sent plate, or None (→ whole file, the pre-v0.56 behaviour)."""
    if plate is None:
        return None
    for element in root.iter():
        if _strip_ns(element.tag).lower() != "plate":
            continue
        for meta in element.iter():
            if _strip_ns(meta.tag).lower() != "metadata":
                continue
            attrs = {_strip_ns(key).lower(): value for key, value in meta.attrib.items()}
            if (attrs.get("key") or attrs.get("name") or "").lower() == "index" and _optional_int(attrs.get("value")) == plate:
                return element
    return None


def parse_slice_info_config(raw: bytes | str, plate: int | None = None) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return _parse_slice_info_fallback(text)

    by_slot: dict[int, dict[str, Any]] = {}
    # v0.56: a multi-plate project lists EVERY plate's filaments; only the sent plate's are required (a spool used
    # solely on another plate must neither block dispatch nor inflate the grams estimate).
    plate_scope = _plate_element(root, plate)
    scope = plate_scope if plate_scope is not None else root
    for element in scope.iter():
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
        # v0.56: Bambu's filament id (tray_info_idx, e.g. GFL99) — the hub's tray translation needs it to tell two
        # same-colour spools apart; rides to the cloud as trayInfoIdx (its stored shape calls it idx).
        tray_info_idx = _first_str(attrs, ("tray_info_idx", "trayinfoidx"))
        if tray_info_idx:
            item["trayInfoIdx"] = tray_info_idx.upper()[:12]
        # v0.52 (design B1 / owner decision 5): the slicer's per-filament weight for the sent plate. The cloud bills
        # COMPLETED jobs from it (staff-confirmed), so no one types grams by hand. Bambu writes used_g on <filament>.
        used_g = _first_float(attrs, ("used_g", "weight", "used_grams"))
        if used_g is not None and used_g >= 0:
            item["usedG"] = round(used_g, 2)

    # The project-wide metadata arrays (0-based) are a FALLBACK for files without per-filament elements (1-based
    # Bambu ids) — mixing the two bases produced phantom slot 0 entries (codex v0.56 r1), so they only seed an empty result.
    if not by_slot:
        _merge_array_metadata(root, by_slot)

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


def _first_float(attrs: dict[str, str], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = attrs.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(str(value).strip())
        except ValueError:
            continue
        if parsed == parsed and parsed not in (float("inf"), float("-inf")):   # finite only
            return parsed
    return None


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
