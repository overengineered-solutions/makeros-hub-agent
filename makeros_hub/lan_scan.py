"""LAN discovery — find Moonraker + Bambu LAN-MQTT printers on the hub's subnet.

The cloud's "Add a printer" form needs an IP to talk to. Operators who hate
Mainsail (or never touched a printer's network settings) get a "Detected on
your LAN" dropdown — they click an entry instead of typing an IP.

This module runs in two modes:
  - Periodic sweep (driven by the agent's main loop, ~every 5 min). Quiet,
    background. Results piggyback on the next heartbeat as `discoveryHits`.
  - On-demand "Scan now" (driven by a cloud-queued `lan-scan` probe). Same
    code path; results return in the probe's `rawOutput`.

The scan is stdlib-only and intentionally cheap:
  - Pull the Pi's primary interface + netmask (`socket` + `/proc/net/route`),
    derive the /24 or /16 subnet.
  - Fan out 32 parallel HTTP GETs to `http://<ip>:7125/server/info` with a
    250ms timeout each. Moonraker's response is a JSON object with a
    distinctive `result.klippy_state` field — false-positive-free.
  - For each Moonraker hit, follow up with `/printer/info` and
    `/machine/system_info` to grab the hostname + klippy version + host
    hardware. This gives the dropdown labels like
    `"Voron 2.4 — Klipper v0.12 on Pi 4 — 192.168.1.47"`.
  - Bambu printers broadcast SSDP on UDP 2021; we ALSO listen for those for
    ~3s and merge the unique IPs into the hit set with `kind='bambu'`. See
    `bambu_ssdp.py`.

Hit dedup key: `(kind, ip)`. The cloud caches the hits with a `last_seen_at`
and stales them after 30 min, so a re-arranged LAN doesn't leave stale picks
in the dropdown.

What the cloud expects per hit:
  {
    "kind": "moonraker" | "bambu",
    "ip": "192.168.1.47",
    "hostname": "voron24.local" | None,
    "displayInfo": {  # vendor-specific extra labels
      "klippyVersion": "v0.12.0",
      "moonrakerVersion": "v0.9.3",
      "hostHardware": "BCM2711 ARMv8 Processor rev 3",
      "klippyState": "ready" | "shutdown" | "error" | ...,
    } | { "model": "X1C", "serial": "01P00A..." },  # bambu
  }

Hard-coded sweep policy (deliberate to keep the operator's LAN quiet):
  - Skip the Pi's OWN IP (we already know we're there).
  - Skip .0 (network), .255 (broadcast), .1 (almost always the router).
  - 250ms per-IP HTTP timeout; 8s overall budget per subnet. A /24 with 32
    workers completes well inside that.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import socket
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

# Per-IP probe timeout. Moonraker behind a healthy LAN responds in <30 ms; we
# allow generous headroom for misbehaving Pis on a slow Wi-Fi. Bigger than this
# and a 254-host /24 sweep gets boring to wait for.
PROBE_TIMEOUT_SEC = 0.25
# Sweep parallelism. 32 keeps a /24 under 8s real-time + doesn't stress the
# Pi's CPU. Bambu users sometimes have a /16 (rare): cap at 8s overall
# regardless of subnet size by bounding the work to a sliced batch.
PROBE_PARALLELISM = 32
# Overall sweep budget. The heartbeat interval is 30s; we leave ample room
# for the rest of the loop. A periodic sweep is fine to skip when over budget.
SWEEP_OVERALL_BUDGET_SEC = 8.0
# Maximum subnet hosts we'll scan in a single sweep. A /24 = 254; a /16 would
# blow the budget so we cap at 510 (effectively two /24s). Operator's LAN is
# almost always /24.
MAX_HOSTS_PER_SWEEP = 510

# Moonraker's GET /server/info returns this shape. Distinct enough that
# nothing else on the LAN responds identically.
_MOONRAKER_PORT = 7125
_MOONRAKER_PROBE_PATH = "/server/info"
_MOONRAKER_FOLLOWUP_PATHS = ("/printer/info", "/machine/system_info")


@dataclass(frozen=True)
class DiscoveryHit:
    """One hit the LAN sweep + Bambu SSDP listener combine into."""

    kind: str  # "moonraker" | "bambu"
    ip: str
    hostname: Optional[str] = None
    display_info: dict[str, Any] = field(default_factory=dict)
    # Monotonic clock seconds when the hit was observed. The agent prefers
    # passing this to the cloud as ISO when serializing; tests compare on the
    # underlying float for determinism.
    observed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ip": self.ip,
            "hostname": self.hostname,
            "displayInfo": dict(self.display_info),
        }


# ---------------------------------------------------------------------------
# Subnet derivation
# ---------------------------------------------------------------------------


def discover_primary_subnet() -> Optional[tuple[str, str, str]]:
    """Find the Pi's primary IPv4 + netmask + own IP. Returns (network_str,
    netmask_str, own_ip_str) or None if nothing usable. Pure stdlib.

    Strategy:
      1. UDP-connect to a public address (no packet sent) to coax the kernel
         into picking the routing-table default interface. Read its local IP.
      2. Read /proc/net/route to find that interface's netmask (Linux only —
         we're on a Pi, which is always Linux).

    Returns None on macOS/Windows or any error path — the sweep skips silently
    rather than crashing.
    """
    try:
        own_ip = _resolve_own_ip()
    except OSError:
        log.debug("lan-scan: couldn't resolve own IP")
        return None
    netmask = _resolve_netmask_for_ip(own_ip)
    if not netmask:
        # Default to /24 when /proc/net/route is unreadable (e.g. macOS dev
        # box). Almost every home/shop LAN is /24 — the heuristic is right
        # often enough to be useful, wrong cheaply enough to skip.
        netmask = "255.255.255.0"
    network = _network_address(own_ip, netmask)
    if not network:
        return None
    return (network, netmask, own_ip)


def _resolve_own_ip() -> str:
    """UDP-connect trick to pick the default-route interface IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 192.0.2.0/24 is RFC 5737 documentation prefix — never routable, so
        # no packet leaves the box, but the kernel still picks the right
        # interface for routing.
        s.connect(("192.0.2.1", 53))
        return s.getsockname()[0]
    finally:
        s.close()


def _resolve_netmask_for_ip(target_ip: str) -> Optional[str]:
    """Parse /proc/net/route to find the netmask of the interface that owns
    `target_ip`. Linux-only. Returns dotted-quad string or None."""
    try:
        with open("/proc/net/route", "r") as f:
            lines = f.readlines()
    except OSError:
        return None
    # /proc/net/route columns are tab-separated; first line is the header.
    # We want the entry whose Destination = the network containing target_ip,
    # but the simplest robust heuristic is: take the FIRST non-default entry
    # whose interface matches default-gateway iface. We do something simpler:
    # find the iface that owns target_ip by inspecting all iface routes.
    target_packed = _pack_ip(target_ip)
    if not target_packed:
        return None
    for raw in lines[1:]:
        cols = raw.split()
        if len(cols) < 8:
            continue
        try:
            dest = int(cols[1], 16)
            mask = int(cols[7], 16)
        except ValueError:
            continue
        # /proc/net/route stores values in little-endian network order
        dest_le = struct.unpack("<I", struct.pack(">I", dest))[0]
        mask_le = struct.unpack("<I", struct.pack(">I", mask))[0]
        target_int = struct.unpack(">I", target_packed)[0]
        if mask_le != 0 and (target_int & mask_le) == dest_le:
            return socket.inet_ntoa(struct.pack(">I", mask_le))
    return None


def _pack_ip(ip: str) -> Optional[bytes]:
    try:
        return socket.inet_aton(ip)
    except OSError:
        return None


def _network_address(ip: str, netmask: str) -> Optional[str]:
    ip_bytes = _pack_ip(ip)
    mask_bytes = _pack_ip(netmask)
    if not ip_bytes or not mask_bytes:
        return None
    masked = bytes(a & b for a, b in zip(ip_bytes, mask_bytes))
    return socket.inet_ntoa(masked)


def enumerate_sweep_targets(
    network: str, netmask: str, own_ip: str
) -> list[str]:
    """List the IPs to probe within (network, netmask), excluding broadcast,
    .0, .1 (router), and own_ip. Capped at MAX_HOSTS_PER_SWEEP."""
    net_bytes = _pack_ip(network)
    mask_bytes = _pack_ip(netmask)
    if not net_bytes or not mask_bytes:
        return []
    net_int = struct.unpack(">I", net_bytes)[0]
    mask_int = struct.unpack(">I", mask_bytes)[0]
    # ~mask & 0xFFFFFFFF = host portion
    host_bits = (~mask_int) & 0xFFFFFFFF
    host_count = host_bits  # number of usable host IPs incl. broadcast
    if host_count == 0:
        return []
    # Cap defends against accidentally walking a /8 (16M hosts).
    if host_count > MAX_HOSTS_PER_SWEEP:
        host_count = MAX_HOSTS_PER_SWEEP
    targets: list[str] = []
    skip = {own_ip}
    for offset in range(1, host_count):  # 0=network, host_count=broadcast
        ip_int = net_int | offset
        ip_str = socket.inet_ntoa(struct.pack(">I", ip_int))
        if ip_str in skip:
            continue
        # Skip the router heuristic — .1 is almost always the gateway and
        # rarely runs Moonraker. Saves one probe per sweep.
        if ip_str.endswith(".1"):
            continue
        targets.append(ip_str)
    return targets


# ---------------------------------------------------------------------------
# Per-IP probes (Moonraker)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float) -> Optional[dict[str, Any]]:
    """Bare GET + json.loads with full error swallowing. Returns None on any
    transport / shape error — callers branch on truthiness."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read(64 * 1024)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(parsed, dict):
            return parsed
        return None
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def is_moonraker_response(body: dict[str, Any]) -> bool:
    """Identify a Moonraker /server/info response by its shape — the `result`
    envelope plus the distinctive `klippy_state` field. No other LAN service
    we've seen responds with this combo."""
    if not isinstance(body, dict):
        return False
    result = body.get("result")
    if not isinstance(result, dict):
        return False
    if "klippy_state" not in result:
        return False
    # klippy_state is one of the Moonraker-documented enums — bias toward
    # accepting anything string here so a future Moonraker version that adds
    # a new state value still gets picked up.
    return isinstance(result.get("klippy_state"), str)


def probe_moonraker(ip: str, timeout: float = PROBE_TIMEOUT_SEC) -> Optional[DiscoveryHit]:
    """One Moonraker probe + (on hit) two cheap follow-up info reads. Returns
    a DiscoveryHit or None. Total budget per IP on a hit ~= timeout * 3."""
    base = f"http://{ip}:{_MOONRAKER_PORT}"
    info = _http_get_json(f"{base}{_MOONRAKER_PROBE_PATH}", timeout)
    if not info or not is_moonraker_response(info):
        return None

    server_result = info["result"] if isinstance(info.get("result"), dict) else {}
    display_info: dict[str, Any] = {
        "klippyState": server_result.get("klippy_state"),
        "moonrakerVersion": server_result.get("moonraker_version"),
    }
    hostname: Optional[str] = None

    # Follow-ups are best-effort — a Moonraker hit alone is enough to land in
    # the dropdown. Each followup gets the same per-call timeout.
    printer_info = _http_get_json(f"{base}/printer/info", timeout)
    if isinstance(printer_info, dict):
        pr = printer_info.get("result")
        if isinstance(pr, dict):
            if isinstance(pr.get("hostname"), str):
                hostname = pr["hostname"]
            if isinstance(pr.get("software_version"), str):
                display_info["klippyVersion"] = pr["software_version"]

    system_info = _http_get_json(f"{base}/machine/system_info", timeout)
    if isinstance(system_info, dict):
        sys_result = system_info.get("result")
        if isinstance(sys_result, dict):
            sys_inner = sys_result.get("system_info")
            if isinstance(sys_inner, dict):
                cpu = sys_inner.get("cpu_info")
                if isinstance(cpu, dict) and isinstance(cpu.get("hardware_description"), str):
                    display_info["hostHardware"] = cpu["hardware_description"]

    # Trim noise — drop None values for compactness.
    display_info = {k: v for k, v in display_info.items() if v is not None}

    return DiscoveryHit(
        kind="moonraker",
        ip=ip,
        hostname=hostname,
        display_info=display_info,
        observed_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Subnet sweep
# ---------------------------------------------------------------------------


def sweep_subnet(
    network: str,
    netmask: str,
    own_ip: str,
    probe_fn=probe_moonraker,
    parallelism: int = PROBE_PARALLELISM,
    overall_budget_sec: float = SWEEP_OVERALL_BUDGET_SEC,
) -> list[DiscoveryHit]:
    """Fan out probes across the subnet; collect Moonraker hits. Caller
    decides what to do with them (merge with Bambu SSDP, persist, ship in
    heartbeat). The `probe_fn` seam exists for tests — pass a stub that
    returns canned hits to verify the orchestration without touching network.
    """
    targets = enumerate_sweep_targets(network, netmask, own_ip)
    if not targets:
        return []
    hits: list[DiscoveryHit] = []
    deadline = time.monotonic() + overall_budget_sec

    workers = min(parallelism, len(targets))
    if workers < 1:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(probe_fn, ip): ip for ip in targets}
        try:
            for fut in concurrent.futures.as_completed(
                futures, timeout=max(0.5, deadline - time.monotonic())
            ):
                # A per-IP probe that raises must NOT sink the whole sweep —
                # a noisy LAN with one misbehaving device should still surface
                # every other Moonraker hit. Log + drop.
                try:
                    hit = fut.result()
                except Exception as exc:  # noqa: BLE001 — probe exceptions are LAN noise
                    log.debug("lan-scan: probe for %s raised: %s", futures[fut], exc)
                    continue
                if isinstance(hit, DiscoveryHit):
                    hits.append(hit)
        except concurrent.futures.TimeoutError:
            log.info(
                "lan-scan: sweep budget %.1fs exhausted; %d hits before cutoff",
                overall_budget_sec,
                len(hits),
            )
            # Cancel remaining work so we don't leak threads past the budget.
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    return hits


# ---------------------------------------------------------------------------
# Top-level entry — periodic + on-demand
# ---------------------------------------------------------------------------


def run_scan() -> list[DiscoveryHit]:
    """One-shot sweep over the Pi's primary subnet. Returns the discovered
    hits — possibly empty. The periodic worker AND the `lan-scan` probe both
    funnel here, so behavior stays identical between sweep modes."""
    subnet = discover_primary_subnet()
    if not subnet:
        log.debug("lan-scan: skipped — no primary subnet")
        return []
    network, netmask, own_ip = subnet
    log.info("lan-scan: sweeping %s/%s (own=%s)", network, netmask, own_ip)
    hits = sweep_subnet(network, netmask, own_ip)
    log.info("lan-scan: %d hits", len(hits))
    return hits
