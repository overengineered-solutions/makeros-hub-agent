from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass


SSDP_GROUP = "239.255.255.250"
SSDP_BROADCAST = "255.255.255.255"
NOTIFY_INTERVAL_SEC = 30.0
BAMBU_RESPONSE_HEADERS = {
    "Cache-Control": "max-age=1800",
    "Ext": "",
    "Server": "VirtualBambu/0.1 UPnP/1.0",
}


@dataclass(frozen=True)
class SsdpConfig:
    ip: str
    serial: str
    model: str
    name: str
    fw: str


class SsdpRuntime:
    def __init__(self, transport: asyncio.DatagramTransport, notify_task: asyncio.Task) -> None:
        self.transport = transport
        self.notify_task = notify_task

    async def close(self) -> None:
        self.notify_task.cancel()
        await asyncio.gather(self.notify_task, return_exceptions=True)
        self.transport.close()


class BambuSsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, config: SsdpConfig, log: Callable[[str], None]) -> None:
        self.config = config
        self.log = log
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text = data.decode("utf-8", errors="replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        self.log(f"SSDP datagram from {addr}: {first}")
        if "M-SEARCH" not in text.upper() or self.transport is None:
            return
        response = build_ssdp_response(self.config)
        self.transport.sendto(response.encode("utf-8"), addr)
        self.log(f"SSDP response sent to {addr}")

    def error_received(self, exc: Exception) -> None:
        self.log(f"SSDP socket error: {exc}")


async def start_ssdp_responder(
    port: int,
    config: SsdpConfig,
    log: Callable[[str], None],
) -> SsdpRuntime:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", port))
    try:
        membership = socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    except OSError as exc:
        log(f"SSDP multicast join failed, continuing unicast-only: {exc}")
    transport, _ = await loop.create_datagram_endpoint(
        lambda: BambuSsdpProtocol(config, log),
        sock=sock,
    )
    notify_task = asyncio.create_task(_notify_loop(transport, port, config, log))
    return SsdpRuntime(transport, notify_task)


def build_ssdp_response(config: SsdpConfig) -> str:
    headers = _bambu_headers(config)
    lines = ["HTTP/1.1 200 OK"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.extend(["", ""])
    return "\r\n".join(lines)


def build_ssdp_notify(config: SsdpConfig) -> str:
    headers = {
        "Host": f"{SSDP_BROADCAST}:2021",
        "NTS": "ssdp:alive",
        "NT": "upnp:rootdevice",
        **_bambu_headers(config),
    }
    lines = ["NOTIFY * HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.extend(["", ""])
    return "\r\n".join(lines)


def _bambu_headers(config: SsdpConfig) -> dict[str, str]:
    return {
        **BAMBU_RESPONSE_HEADERS,
        "Location": config.ip,
        "USN": config.serial,
        "DevModel.bambu.com": config.model,
        "DevName.bambu.com": config.name,
        "DevSignal.bambu.com": "-44",
        "DevConnect.bambu.com": "lan",
        "DevBind.bambu.com": "free",
        "Devserial.bambu.com": config.serial,
        "DevIP.bambu.com": config.ip,
        "DevVersion.bambu.com": config.fw,
        "DevCap.bambu.com": "1",
    }


async def _notify_loop(
    transport: asyncio.DatagramTransport,
    port: int,
    config: SsdpConfig,
    log: Callable[[str], None],
) -> None:
    payload = build_ssdp_notify(config).encode("utf-8")
    try:
        while True:
            transport.sendto(payload, (SSDP_BROADCAST, port))
            log("SSDP NOTIFY broadcast sent")
            await asyncio.sleep(NOTIFY_INTERVAL_SEC)
    except asyncio.CancelledError:
        pass
