"""Passive SSDP listener for Bambu LAN-MQTT printers.

Bambu printers broadcast SSDP NOTIFY messages on UDP **2021** announcing
themselves to the LAN. Bambu Studio + Bambu Handy use this for discovery.
Each NOTIFY carries the printer's serial, model (`Devel.Type`), firmware
version, and IP — exactly what our "Detected on your LAN" dropdown wants.

Our listener is intentionally PASSIVE:
  - Open a UDP socket bound to 2021 (or, if the OS denies — common when
    Bambu Studio is running on the same Pi — fall back to ephemeral and
    only consume what arrives via broadcast routing).
  - Listen for `listen_seconds` (default 3s — Bambu repeats NOTIFY every
    ~1s on the wire, so 3s catches every printer twice). Bigger budgets
    rarely help.
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
import socket
import time
from typing import Optional

from .lan_scan import DiscoveryHit

log = logging.getLogger(__name__)

BAMBU_SSDP_PORT = 2021
LISTEN_SECONDS = 3.0
# Bound the inbound UDP queue — a noisy LAN with a misbehaving device could
# spam unlimited traffic; we cap how much we'll read per listen window.
MAX_NOTIFY_BYTES = 16 * 1024
MAX_NOTIFY_PACKETS = 128

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


def listen_for_bambu_announcements(
    listen_seconds: float = LISTEN_SECONDS,
    port: int = BAMBU_SSDP_PORT,
) -> list[DiscoveryHit]:
    """Bind a UDP socket on port 2021 and absorb NOTIFY frames for the
    listen window. Dedupes by serial (or by IP when no serial). Best-effort
    — a port collision or permission denial returns an empty list rather
    than raising."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        log.info("bambu-ssdp: bind failed on %d (%s) — skipping listen", port, e)
        return []
    sock.settimeout(min(0.5, listen_seconds))
    deadline = time.monotonic() + listen_seconds
    hits: dict[str, DiscoveryHit] = {}
    packets = 0
    bytes_read = 0
    try:
        while time.monotonic() < deadline and packets < MAX_NOTIFY_PACKETS:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            packets += 1
            bytes_read += len(data)
            if bytes_read > MAX_NOTIFY_BYTES:
                break
            hit = parse_bambu_notify(data, addr[0])
            if hit:
                key = hit.display_info.get("serial") or hit.ip
                hits.setdefault(key, hit)
    finally:
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
