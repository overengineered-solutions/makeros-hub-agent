from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .auth import MemberAuthSet
from .capture import ProjectFileIntent, parse_project_file_command


MAX_REMAINING_LENGTH = 268_435_455
MAX_MQTT_PACKET_BYTES = 4 * 1024 * 1024
MQTT_CONNECT_TIMEOUT_SEC = 45.0
MQTT_DRAIN_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class ConnectPacket:
    protocol_name: str
    protocol_level: int
    flags: int
    keep_alive: int
    client_id: str
    username: str | None
    password: str | None
    clean_session: bool


@dataclass(frozen=True)
class SubscribePacket:
    packet_id: int
    topics: list[tuple[str, int]]


@dataclass(frozen=True)
class PublishPacket:
    topic: str
    payload: bytes
    qos: int
    packet_id: int | None
    retain: bool
    dup: bool


@dataclass
class MqttSession:
    writer: asyncio.StreamWriter
    peer: object
    member_id: str
    report_topic: str
    request_topic: str
    subscribed: bool = False


def encode_remaining_length(length: int) -> bytes:
    if length < 0 or length > MAX_REMAINING_LENGTH:
        raise ValueError("MQTT remaining length out of range")
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        encoded.append(digit)
        if length == 0:
            return bytes(encoded)


def decode_remaining_length_from_bytes(data: bytes) -> tuple[int, int]:
    multiplier = 1
    value = 0
    for idx, byte in enumerate(data[:4]):
        value += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            return value, idx + 1
        multiplier *= 128
    raise ValueError("malformed or incomplete MQTT remaining length")


def parse_connect(payload: bytes) -> ConnectPacket:
    offset = 0
    protocol_name, offset = _read_utf8(payload, offset)
    if offset + 4 > len(payload):
        raise ValueError("CONNECT variable header is truncated")
    protocol_level = payload[offset]
    flags = payload[offset + 1]
    keep_alive = int.from_bytes(payload[offset + 2 : offset + 4], "big")
    offset += 4

    client_id, offset = _read_utf8(payload, offset)
    will_flag = bool(flags & 0x04)
    if will_flag:
        _, offset = _read_utf8(payload, offset)
        _, offset = _read_binary(payload, offset)

    username = None
    password = None
    if flags & 0x80:
        username, offset = _read_utf8(payload, offset)
    if flags & 0x40:
        password_bytes, offset = _read_binary(payload, offset)
        password = password_bytes.decode("utf-8", errors="replace")
    if offset != len(payload):
        raise ValueError("CONNECT payload has trailing bytes")

    return ConnectPacket(
        protocol_name=protocol_name,
        protocol_level=protocol_level,
        flags=flags,
        keep_alive=keep_alive,
        client_id=client_id,
        username=username,
        password=password,
        clean_session=bool(flags & 0x02),
    )


def build_connack(return_code: int) -> bytes:
    return b"\x20\x02\x00" + bytes([return_code])


def parse_subscribe(payload: bytes) -> SubscribePacket:
    if len(payload) < 2:
        raise ValueError("SUBSCRIBE packet is truncated")
    packet_id = int.from_bytes(payload[:2], "big")
    offset = 2
    topics: list[tuple[str, int]] = []
    while offset < len(payload):
        topic, offset = _read_utf8(payload, offset)
        if offset >= len(payload):
            raise ValueError("SUBSCRIBE topic is missing requested QoS")
        qos = payload[offset] & 0x03
        offset += 1
        topics.append((topic, qos))
    return SubscribePacket(packet_id=packet_id, topics=topics)


def build_suback(packet_id: int, granted_qos: list[int]) -> bytes:
    payload = packet_id.to_bytes(2, "big") + bytes(granted_qos)
    return b"\x90" + encode_remaining_length(len(payload)) + payload


def parse_publish(first_byte: int, payload: bytes) -> PublishPacket:
    qos = (first_byte >> 1) & 0x03
    retain = bool(first_byte & 0x01)
    dup = bool(first_byte & 0x08)
    topic, offset = _read_utf8(payload, 0)
    packet_id = None
    if qos:
        if offset + 2 > len(payload):
            raise ValueError("PUBLISH packet id is truncated")
        packet_id = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
    return PublishPacket(
        topic=topic,
        payload=payload[offset:],
        qos=qos,
        packet_id=packet_id,
        retain=retain,
        dup=dup,
    )


def build_publish(topic: str, payload: bytes, qos: int = 0, packet_id: int | None = None) -> bytes:
    if qos not in (0, 1):
        raise ValueError("only QoS 0 and QoS 1 are supported")
    variable = _write_utf8(topic)
    first_byte = 0x30 | (qos << 1)
    if qos:
        if packet_id is None:
            raise ValueError("QoS 1 publish requires packet_id")
        variable += packet_id.to_bytes(2, "big")
    body = variable + payload
    return bytes([first_byte]) + encode_remaining_length(len(body)) + body


def build_puback(packet_id: int) -> bytes:
    return b"\x40\x02" + packet_id.to_bytes(2, "big")


class MqttBroker:
    def __init__(
        self,
        *,
        serial: str,
        auth: MemberAuthSet,
        report_builder: Callable[[int, str, str, str], dict[str, Any]],
        version_builder: Callable[[str], dict[str, Any]],
        ack_builder: Callable[[str, str], dict[str, Any]],
        on_project_file: Callable[[ProjectFileIntent], None] | None,
        log: Callable[[str], None],
        report_interval: float = 1.0,
    ) -> None:
        self.serial = serial
        self.auth = auth
        self.report_builder = report_builder
        self.version_builder = version_builder
        self.ack_builder = ack_builder
        self.on_project_file = on_project_file
        self.log = log
        self.report_interval = report_interval
        self.default_report_topic = f"device/{serial}/report"
        self.default_request_topic = f"device/{serial}/request"
        self._sequence = 1
        self._active_writer: asyncio.StreamWriter | None = None
        self._client_tasks: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()
        self._gcode_state = "IDLE"
        self._gcode_file = ""
        self._prepare_percent = "0"
        self._finish_task: asyncio.Task | None = None

    async def start(self, host: str, port: int, ssl_context) -> asyncio.AbstractServer:
        return await asyncio.start_server(self._handle_client, host=host, port=port, ssl=ssl_context)

    async def close(self) -> None:
        if self._finish_task is not None and not self._finish_task.done():
            self._finish_task.cancel()
            await asyncio.gather(self._finish_task, return_exceptions=True)
        if self._active_writer is not None:
            self._active_writer.close()
        tasks = [task for task in self._client_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_writer = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        peer = writer.get_extra_info("peername")
        peer_ip = _peer_ip(peer)
        if self._active_writer is not None and not self._active_writer.is_closing():
            self.log(f"MQTT replacing existing client with new client from {peer}")
            self._active_writer.close()
        self._active_writer = writer
        periodic_task: asyncio.Task | None = None
        session: MqttSession | None = None
        self.log(f"MQTT TLS connection from {peer}")
        try:
            first_byte, payload = await asyncio.wait_for(
                _read_packet(reader),
                timeout=MQTT_CONNECT_TIMEOUT_SEC,
            )
            if first_byte != 0x10:
                self.log(f"MQTT expected CONNECT from {peer}, got 0x{first_byte:02X}")
                self.auth.record_failure(None, peer_ip)
                return
            connect = parse_connect(payload)
            self.log(
                "MQTT CONNECT "
                f"peer={peer} client_id={connect.client_id!r} protocol={connect.protocol_name}/"
                f"{connect.protocol_level} clean={connect.clean_session} keep_alive={connect.keep_alive} "
                f"username={connect.username!r}"
            )
            return_code, member_id = self._auth_connect(connect, peer_ip)
            writer.write(build_connack(return_code))
            await _drain(writer)
            self.log(f"MQTT CONNACK to {peer}: return_code=0x{return_code:02X}")
            if return_code != 0 or member_id is None:
                return

            session = MqttSession(
                writer=writer,
                peer=peer,
                member_id=member_id,
                report_topic=self.default_report_topic,
                request_topic=self.default_request_topic,
            )
            await self._send_report(session, "connack")
            periodic_task = asyncio.create_task(self._periodic_reports(session))
            while True:
                first_byte, payload = await _read_packet(reader)
                packet_type = first_byte & 0xF0
                if packet_type == 0x80:
                    subscribe = parse_subscribe(payload)
                    self.log(
                        f"MQTT SUBSCRIBE peer={peer} packet_id={subscribe.packet_id} "
                        f"topics={subscribe.topics!r}"
                    )
                    writer.write(build_suback(subscribe.packet_id, [0 for _ in subscribe.topics]))
                    await _drain(writer)
                    self._adapt_session_topics(session, subscribe)
                    await self._send_report(session, "subscribe")
                elif packet_type == 0x30:
                    publish = parse_publish(first_byte, payload)
                    await self._handle_publish(session, publish)
                elif packet_type == 0xA0:
                    pkt_id = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 0
                    self.log(f"MQTT UNSUBSCRIBE peer={peer} packet_id={pkt_id}")
                    writer.write(b"\xB0\x02" + pkt_id.to_bytes(2, "big"))
                    await _drain(writer)
                elif first_byte == 0xC0:
                    writer.write(b"\xD0\x00")
                    await _drain(writer)
                elif first_byte == 0xE0:
                    self.log(f"MQTT DISCONNECT peer={peer}")
                    return
                else:
                    self.log(f"MQTT unsupported packet from {peer}: first_byte=0x{first_byte:02X}")
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError:
            self.log(f"MQTT client {peer} closed the connection")
        except Exception as exc:
            self.log(f"MQTT error from {peer}: {exc}")
        finally:
            if periodic_task is not None:
                periodic_task.cancel()
                await asyncio.gather(periodic_task, return_exceptions=True)
            if self._active_writer is writer:
                self._active_writer = None
            writer.close()
            await _wait_closed(writer)
            if task is not None:
                self._client_tasks.discard(task)
            self.log(f"MQTT connection closed for {peer}")

    def _auth_connect(self, connect: ConnectPacket, peer_ip: str | None) -> tuple[int, str | None]:
        if connect.protocol_name != "MQTT" or connect.protocol_level != 4:
            self.auth.record_failure(connect.password, peer_ip)
            return 0x01, None
        if connect.username != "bblp":
            self.auth.record_failure(connect.password, peer_ip)
            return 0x04, None
        auth = self.auth.authenticate(connect.password, peer_ip)
        if auth.ok:
            return 0x00, auth.member_id
        if auth.rate_limited:
            self.log(f"MQTT auth rate-limited for peer_ip={peer_ip}")
        else:
            self.log(f"MQTT auth failed for peer_ip={peer_ip}")
        return 0x05, None

    def _adapt_session_topics(self, session: MqttSession, subscribe: SubscribePacket) -> None:
        for topic, _qos in subscribe.topics:
            serial = _serial_from_report_topic(topic)
            if serial:
                session.report_topic = topic
                session.request_topic = f"device/{serial}/request"
                session.subscribed = True
                return
        session.subscribed = any(topic == session.report_topic for topic, _qos in subscribe.topics)

    async def _handle_publish(self, session: MqttSession, publish: PublishPacket) -> None:
        text = publish.payload.decode("utf-8", errors="replace").rstrip("\x00 \r\n\t")
        parsed = _json_or_none(text)
        command = _command_name(parsed)
        self.log(
            "MQTT PUBLISH client->broker "
            f"peer={session.peer} topic={publish.topic!r} qos={publish.qos} "
            f"packet_id={publish.packet_id} command={command!r}"
        )
        if publish.qos == 1 and publish.packet_id is not None:
            session.writer.write(build_puback(publish.packet_id))
            await _drain(session.writer)
            self.log(f"MQTT PUBACK peer={session.peer} packet_id={publish.packet_id}")
        if not _is_request_topic(publish.topic):
            return
        if _is_pushall(parsed):
            await self._send_report(session, "pushall")
        elif _is_get_version(parsed):
            seq = ""
            if isinstance(parsed, dict):
                seq = str(parsed.get("info", {}).get("sequence_id", ""))
            await self._send_version(session, seq)
        elif _is_project_file(parsed):
            intent = parse_project_file_command(parsed, session.member_id)
            gfile = intent.filename if intent is not None else self._gcode_file or "job.3mf"
            seq = ""
            if isinstance(parsed, dict):
                seq = str(parsed.get("print", {}).get("sequence_id", ""))
            await self._send_print_ack(session, seq, gfile)
            if intent is not None and self.on_project_file is not None:
                try:
                    self.on_project_file(intent)
                except Exception as exc:  # noqa: BLE001 - observe-only capture
                    self.log(f"MQTT project_file capture hook failed for {gfile!r}: {exc}")

    async def _periodic_reports(self, session: MqttSession) -> None:
        try:
            while True:
                await asyncio.sleep(self.report_interval)
                if session.writer.is_closing() or not session.subscribed:
                    continue
                await self._send_report(session, "periodic")
        except asyncio.CancelledError:
            pass

    async def _send_report(self, session: MqttSession, reason: str) -> None:
        report = self.report_builder(
            self._sequence,
            self._gcode_state,
            self._gcode_file,
            self._prepare_percent,
        )
        self._sequence += 1
        payload = json.dumps(report, indent=4).encode("utf-8")
        packet = build_publish(session.report_topic, payload, qos=0)
        async with self._write_lock:
            session.writer.write(packet)
            await _drain(session.writer)
        self.log(
            f"MQTT PUBLISH broker->client reason={reason} topic={session.report_topic!r} "
            f"bytes={len(payload)}"
        )

    async def _send_version(self, session: MqttSession, sequence_id: str) -> None:
        version = self.version_builder(sequence_id)
        payload = json.dumps(version, indent=4).encode("utf-8")
        packet = build_publish(session.report_topic, payload, qos=0)
        async with self._write_lock:
            session.writer.write(packet)
            await _drain(session.writer)
        self.log(
            f"MQTT PUBLISH broker->client reason=get_version topic={session.report_topic!r} "
            f"bytes={len(payload)} modules={len(version.get('info', {}).get('module', []))}"
        )

    async def _send_print_ack(self, session: MqttSession, sequence_id: str, gcode_file: str) -> None:
        self.set_print_state("PREPARE", gcode_file=gcode_file, prepare_percent="0")
        ack = self.ack_builder(sequence_id, gcode_file)
        payload = json.dumps(ack, indent=4).encode("utf-8")
        packet = build_publish(session.report_topic, payload, qos=0)
        async with self._write_lock:
            session.writer.write(packet)
            await _drain(session.writer)
        self.log(f"MQTT PUBLISH broker->client reason=project_file_ack file={gcode_file!r}")
        self._schedule_finish(gcode_file)

    def set_print_state(
        self,
        gcode_state: str,
        *,
        gcode_file: str | None = None,
        prepare_percent: str | None = None,
    ) -> None:
        self._gcode_state = gcode_state
        if gcode_file is not None:
            self._gcode_file = gcode_file
        if prepare_percent is not None:
            self._prepare_percent = prepare_percent
        self.log(
            f"virtual printer state -> {gcode_state} "
            f"(file={self._gcode_file!r} prep={self._prepare_percent})"
        )

    def _schedule_finish(self, gcode_file: str, delay: float = 1.5) -> None:
        if self._finish_task is not None and not self._finish_task.done():
            self._finish_task.cancel()

        async def _finish() -> None:
            try:
                await asyncio.sleep(delay)
                self.set_print_state("FINISH", gcode_file=gcode_file, prepare_percent="100")
            except asyncio.CancelledError:
                pass

        self._finish_task = asyncio.create_task(_finish())


async def _read_packet(
    reader: asyncio.StreamReader,
    *,
    max_remaining_length: int = MAX_MQTT_PACKET_BYTES,
) -> tuple[int, bytes]:
    first = await reader.readexactly(1)
    remaining_length = 0
    multiplier = 1
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        remaining_length += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            if remaining_length > max_remaining_length:
                raise ValueError("MQTT packet exceeds maximum size")
            payload = await reader.readexactly(remaining_length)
            return first[0], payload
        multiplier *= 128
    raise ValueError("malformed MQTT remaining length")


def _is_pushall(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    pushing = parsed.get("pushing")
    return isinstance(pushing, dict) and pushing.get("command") == "pushall"


def _is_get_version(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    info = parsed.get("info")
    return isinstance(info, dict) and info.get("command") == "get_version"


def _is_project_file(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    pr = parsed.get("print")
    return isinstance(pr, dict) and pr.get("command") in ("project_file", "gcode_file")


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _command_name(parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return None
    for key in ("print", "info", "pushing"):
        value = parsed.get(key)
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            return value["command"]
    return None


def _is_request_topic(topic: str) -> bool:
    parts = topic.split("/")
    return len(parts) == 3 and parts[0] == "device" and parts[2] == "request"


def _serial_from_report_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "device" and parts[2] == "report" and parts[1]:
        return parts[1]
    return None


def _read_utf8(data: bytes, offset: int) -> tuple[str, int]:
    raw, offset = _read_binary(data, offset)
    return raw.decode("utf-8", errors="replace"), offset


def _read_binary(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise ValueError("MQTT string length is truncated")
    length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    if offset + length > len(data):
        raise ValueError("MQTT string payload is truncated")
    return data[offset : offset + length], offset + length


def _write_utf8(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("MQTT UTF-8 string is too long")
    return len(raw).to_bytes(2, "big") + raw


def _peer_ip(peer: object) -> str | None:
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return None


async def _drain(writer: asyncio.StreamWriter) -> None:
    await asyncio.wait_for(writer.drain(), timeout=MQTT_DRAIN_TIMEOUT_SEC)


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except Exception:
        pass
