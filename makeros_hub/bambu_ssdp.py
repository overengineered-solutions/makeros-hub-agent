"""Passive SSDP listener for Bambu LAN-MQTT printers.

Bambu printers broadcast SSDP NOTIFY messages on UDP **2021** announcing
themselves to the LAN. Bambu Studio + Bambu Handy use this for discovery.
Each NOTIFY carries the printer's serial, model (`Devel.Type`), firmware
version, and IP — exactly what our "Detected on your LAN" dropdown wants.

Our listener is intentionally PASSIVE:
  - Open a UDP socket bound to 2021 (or, if the OS denies — common when
    Bambu Studio is running on the same Pi — fall back to ephemeral and
    only consume what arrives via broadcast routing).
  - Listen for `listen_seconds`. v0.71: 8 s (was 3) on BOTH the broadcast
    port 2021 and the multicast group 239.255.255.250:1990, joined on every
    interface the box has. A 3 s window on 2021 alone heard a few of the
    fifteen printers per sweep (shop log 2026-10-17: 5 of 15); the heal
    accumulated them over many sweeps, "Found on the network" lagged. The box
    now also has a second network leg (Equipment, VLAN 20, 2026-10-18), and a
    multicast join on INADDR_ANY covers only the default interface.
  - Parse each NOTIFY into a hit; dedup by serial.

Why not actively M-SEARCH? Bambu printers DO respond to M-SEARCH, but the
fields they hand back are the same ones the passive NOTIFY carries. The
passive approach makes no noise on the operator's LAN and won't get the hub
banned from a wary router's IDS.

Stdlib only — `socket`. No 3rd-party SSDP libraries.
"""

from __future__ import annotations

import logging
import re
import select
import socket
import struct
import time
from typing import Optional

from .lan_scan import DiscoveryHit

log = logging.getLogger(__name__)

BAMBU_SSDP_PORT = 2021
BAMBU_SSDP_MULTICAST = ("239.255.255.250", 1990)
LISTEN_SECONDS = 8.0
# Bound the inbound UDP queue — a noisy LAN with a misbehaving device could
# spam unlimited traffic; we cap how much we'll read per listen window.
# v0.71: fifteen printers on two ports over 8 s is ~100 packets of ~500 bytes.
MAX_NOTIFY_BYTES = 128 * 1024
MAX_NOTIFY_PACKETS = 512

# Match SSDP/NOTIFY-style header lines like `DevModel.bambu-net: X1C`.
_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*:\s*(.+?)\s*$", re.MULTILINE)


def parse_bambu_notify(packet: bytes, peer_ip: str) -> Optional[DiscoveryHit]:
    """Parse a single NOTIFY packet. Bambu's payload follows the SSDP-derived
    header layout — first line `NOTIFY * HTTP/1.1`, then `Key: Value` pairs.

    Returns a DiscoveryHit on success, None on shape mismatch. We're lenient
    on field names (Bambu's firmware variants differ): we accept any of
    `DevName`/`Devel.Name`, `DevModel`/`Devel.Type`, `DevSerial`/`Devel.Sn`.
    """
    try:
        text = packet.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — UDP frames are untrusted
        return None
    # Must start with NOTIFY (case insensitive). M-SEARCH responses use
    # `HTTP/1.1 200 OK` start lines — accept those too.
    first_line = text.split("\r\n", 1)[0].strip().upper()
    if not (first_line.startswith("NOTIFY") or first_line.startswith("HTTP/1.1 200")):
        return None
    headers: dict[str, str] = {}
    for m in _HEADER_RE.finditer(text):
        key = m.group(1).lower()
        headers[key] = m.group(2)
        # Bambu firmware variants append a `.bambu-net` / `.cam` namespace to
        # header names (e.g. `DevName.bambu-net`). The alias lookup below
        # treats namespaces as cosmetic — register the bare prefix too so
        # `first("devname")` matches `devname.bambu-net`. First-occurrence
        # wins (setdefault) so explicit `DevName:` headers override namespaced
        # ones.
        if "." in key:
            bare = key.split(".", 1)[0]
            headers.setdefault(bare, m.group(2))

    # Field aliases — different firmware variants emit different names.
    def first(*keys: str) -> Optional[str]:
        for k in keys:
            v = headers.get(k.lower())
            if v:
                return v
        return None

    serial = first("devsn", "devel.sn", "usn", "devid")
    model = first("devmodel", "devel.type", "type", "devtype")
    name = first("devname", "devel.name", "name", "friendlyname")
    fw = first("devel.softver", "devel.version", "softver", "version")
    # Bambu NOTIFY usually advertises a `Location:` URL pointing back at the
    # printer's HTTP service; fall back to the peer IP from recvfrom.
    location = first("location")
    ip = peer_ip
    if location:
        m = re.search(r"//([\d.]+)", location)
        if m:
            ip = m.group(1)

    # We need AT LEAST a serial OR a model to call this a hit — otherwise
    # it's some other UDP chatter on port 2021. (Synology / Apple / Samsung
    # SSDP all use different port/ST values, so collisions are rare.)
    if not (serial or model):
        return None

    display: dict[str, str] = {}
    if model:
        display["model"] = model
    if serial:
        display["serial"] = serial
    if fw:
        display["firmware"] = fw

    return DiscoveryHit(
        kind="bambu",
        ip=ip,
        hostname=name,
        display_info=display,
        observed_at=time.monotonic(),
    )


def _join_everywhere(sock: socket.socket, group: str) -> int:
    """Join the multicast group on every interface (struct ip_mreqn by ifindex, Linux). Returns how many joined; an
    interface without IPv4, or a platform without ip_mreqn, is skipped."""
    joined = 0
    try:
        names = socket.if_nameindex()
    except OSError:
        names = []
    for index, name in names:
        if name == "lo":
            continue
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            struct.pack("4s4si", socket.inet_aton(group), socket.inet_aton("0.0.0.0"), index))
            joined += 1
        except OSError:
            continue
    if not joined:
        try:   # the portable form: the default interface only
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0")))
            joined = 1
        except OSError:
            pass
    return joined


def _open(port: int, group: str | None = None) -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        log.info("bambu-ssdp: bind failed on %d (%s) — skipping that port", port, e)
        return None
    if group and not _join_everywhere(sock, group):
        log.info("bambu-ssdp: could not join %s on any interface — %d hears broadcasts only", group, port)
    sock.setblocking(False)
    return sock


def listen_for_bambu_announcements(
    listen_seconds: float = LISTEN_SECONDS,
    port: int = BAMBU_SSDP_PORT,
    multicast: tuple[str, int] | None = BAMBU_SSDP_MULTICAST,
) -> list[DiscoveryHit]:
    """Absorb NOTIFY frames on the broadcast port and the multicast group for the listen window. Dedupes by serial (or
    by IP when no serial). Best-effort — a port collision or permission denial on one port leaves the other; both
    failing returns an empty list rather than raising."""
    socks = [s for s in (_open(port), _open(multicast[1], multicast[0]) if multicast else None) if s is not None]
    if not socks:
        return []
    deadline = time.monotonic() + listen_seconds
    hits: dict[str, DiscoveryHit] = {}
    packets = 0
    bytes_read = 0
    try:
        while packets < MAX_NOTIFY_PACKETS and bytes_read <= MAX_NOTIFY_BYTES:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                ready, _w, _x = select.select(socks, [], [], min(0.5, left))
            except OSError:
                break
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    continue
                packets += 1
                bytes_read += len(data)
                hit = parse_bambu_notify(data, addr[0])
                if hit:
                    key = hit.display_info.get("serial") or hit.ip
                    hits.setdefault(key, hit)
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
    log.info(
        "bambu-ssdp: %d packets / %d bytes parsed, %d unique printers",
        packets,
        bytes_read,
        len(hits),
    )
    return list(hits.values())
