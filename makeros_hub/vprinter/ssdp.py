from __future__ import annotations

import asyncio
import socket
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


SSDP_GROUP = "239.255.255.250"
SSDP_BROADCAST = "255.255.255.255"
NOTIFY_INTERVAL_SEC = 30.0
SSDP_MIN_MSEARCH_BYTES = len(b"M-SEARCH * HTTP/1.1\r\n\r\n")
SSDP_RATE_LIMIT_PER_SEC = 3.0
SSDP_RATE_LIMIT_BURST = 6
SSDP_RATE_LIMIT_MAX_SOURCES = 1024
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
        self._rate_limiter = SsdpRateLimiter()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < SSDP_MIN_MSEARCH_BYTES:
            self.log(f"SSDP datagram from {addr} ignored: too small")
            return
        text = data.decode("utf-8", errors="replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        self.log(f"SSDP datagram from {addr}: {first}")
        if not _is_m_search(text) or self.transport is None:
            return
        if not self._rate_limiter.allow(addr[0]):
            self.log(f"SSDP response throttled for {addr}")
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


class SsdpRateLimiter:
    def __init__(
        self,
        *,
        rate_per_sec: float = SSDP_RATE_LIMIT_PER_SEC,
        burst: int = SSDP_RATE_LIMIT_BURST,
        max_sources: int = SSDP_RATE_LIMIT_MAX_SOURCES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.rate_per_sec = max(0.1, float(rate_per_sec))
        self.burst = max(1, int(burst))
        self.max_sources = max(1, int(max_sources))
        self.clock = clock or time.monotonic
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, source_ip: str) -> bool:
        now = self.clock()
        tokens, last_seen = self._buckets.get(source_ip, (float(self.burst), now))
        elapsed = max(0.0, now - last_seen)
        tokens = min(float(self.burst), tokens + elapsed * self.rate_per_sec)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[source_ip] = (tokens, now)
        self._buckets.move_to_end(source_ip)
        while len(self._buckets) > self.max_sources:
            self._buckets.popitem(last=False)
        return allowed


def _is_m_search(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    return lines[0].strip().upper().startswith("M-SEARCH ")


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
