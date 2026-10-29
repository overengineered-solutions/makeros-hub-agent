"""Optional device evidence from the members tailscaled; never an authorization gate."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urlencode

DEFAULT_SOCKET = "/run/tailscale-members.sock"
log = logging.getLogger("makeros-hub.ingest")


def lookup(peer, socket_path=DEFAULT_SOCKET):
    """Return (source, device); only the actual loopback peer is looked up, never a header."""
    connection = None
    try:
        address = ipaddress.ip_address(peer[0])
        loopback = address.is_loopback or (getattr(address, "ipv4_mapped", None) is not None
                                          and address.ipv4_mapped.is_loopback)
        if not loopback:
            return "lan", None
        addr = f"[{address}]:{peer[1]}" if address.version == 6 else f"{address}:{peer[1]}"
        connection = http.client.HTTPConnection("local-tailscaled.sock", timeout=1)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(1)
        connection.sock.connect(socket_path)
        connection.request("GET", "/localapi/v0/whois?" + urlencode({"addr": addr}))
        with connection.getresponse() as response:
            raw = response.read(64 * 1024 + 1)
            if response.status != 200 or len(raw) > 64 * 1024:
                raise ValueError("unavailable")
        body = json.loads(raw)
        node = body.get("Node") if isinstance(body, dict) else None
        if not isinstance(node, dict):
            raise ValueError("missing node")
        node_id = node.get("StableID") or node.get("ID")
        if type(node_id) is int and node_id > 0:
            node_id = str(node_id)
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", node_id):
            raise ValueError("invalid node id")
        name = node.get("ComputedName") or node.get("Name")
        if not isinstance(name, str) or not name.strip() or any(not c.isprintable() for c in name):
            raise ValueError("invalid node name")
        return "tailnet", {"nodeId": node_id, "name": name.strip()[:64]}
    except Exception:
        log.debug("Device evidence unavailable")  # No peer, name, response, or exception text.
        return "unknown", None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                log.debug("Device evidence connection cleanup failed")
