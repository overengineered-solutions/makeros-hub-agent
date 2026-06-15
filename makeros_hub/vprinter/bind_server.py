from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


START_MAGIC = 0xA5A5
END_MAGIC = 0xA7A7
BIND_READ_TIMEOUT_SEC = 30.0
MAX_CONCURRENT_BIND_CONNECTIONS = 8
# Time-box socket close so an orphaned bind handler can't hang forever in
# wait_closed() after a config-change restart (mirrors MQTT/FTP). Teardown
# itself is already bounded by VP_TEARDOWN_TIMEOUT_SEC in the runtime.
BIND_CLOSE_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class BindReplyConfig:
    serial: str
    model: str
    name: str
    fw: str


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    total_len = 4 + len(body) + 2
    return struct.pack("<HH", START_MAGIC, total_len) + body + struct.pack("<H", END_MAGIC)


def decode_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 6:
        raise ValueError("bind frame too short")
    start_magic, total_len = struct.unpack("<HH", frame[:4])
    if start_magic != START_MAGIC:
        raise ValueError("invalid bind start magic")
    if total_len != len(frame):
        raise ValueError(f"bind length mismatch: header={total_len} actual={len(frame)}")
    (end_magic,) = struct.unpack("<H", frame[-2:])
    if end_magic != END_MAGIC:
        raise ValueError("invalid bind end magic")
    parsed = json.loads(frame[4:-2].decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("bind frame body must be a JSON object")
    return parsed


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(4)
    start_magic, total_len = struct.unpack("<HH", header)
    if start_magic != START_MAGIC:
        raise ValueError("invalid bind start magic")
    if total_len < 6:
        raise ValueError("invalid bind total length")
    rest = await reader.readexactly(total_len - 4)
    return decode_frame(header + rest)


async def start_bind_server(
    host: str,
    port: int,
    config: BindReplyConfig,
    log: Callable[[str], None],
    ssl_context=None,
    max_connections: int = MAX_CONCURRENT_BIND_CONNECTIONS,
) -> asyncio.AbstractServer:
    active_connections = 0
    connection_limit = max(1, int(max_connections))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active_connections
        peer = writer.get_extra_info("peername")
        label = "TLS bind" if ssl_context else "plain bind"
        if active_connections >= connection_limit:
            log(f"{label} connection refused at connection cap for {peer}")
            writer.close()
            await _wait_closed(writer)
            return
        active_connections += 1
        log(f"{label} connection from {peer}")
        try:
            request = await asyncio.wait_for(read_frame(reader), timeout=BIND_READ_TIMEOUT_SEC)
            log(f"{label} request from {peer}: {json.dumps(request, separators=(',', ':'))}")
            response = build_detect_reply(config)
            writer.write(encode_frame(response))
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            log(f"{label} response to {peer}: {json.dumps(response, separators=(',', ':'))}")
        except asyncio.IncompleteReadError:
            log(f"{label} connection from {peer} closed before a full frame")
        except TimeoutError:
            log(f"{label} connection from {peer} timed out")
        except Exception as exc:
            log(f"{label} error from {peer}: {exc}")
        finally:
            active_connections -= 1
            writer.close()
            await _wait_closed(writer)

    return await asyncio.start_server(
        handle,
        host=host,
        port=port,
        ssl=ssl_context,
        reuse_address=True,
    )


def build_detect_reply(config: BindReplyConfig) -> dict[str, Any]:
    return {
        "login": {
            "bind": "free",
            "command": "detect",
            "connect": "lan",
            "dev_cap": 1,
            "id": config.serial,
            "model": config.model,
            "name": config.name,
            "sequence_id": 3021,
            "version": config.fw,
        }
    }


async def close_server(server: asyncio.AbstractServer) -> None:
    server.close()
    await server.wait_closed()


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    # Bounded (mirrors mqtt_broker/ftp_server): a TLS wait_closed() can block on
    # the peer's close_notify; time-box it so an orphaned handler self-cleans.
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=BIND_CLOSE_TIMEOUT_SEC)
    except Exception:
        pass
