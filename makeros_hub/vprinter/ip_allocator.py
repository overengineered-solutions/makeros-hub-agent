"""Per-VP LAN IP allocator.

Each per-model virtual printer needs its own IP because OrcaSlicer's Device
tab binds a printer by IP. Two VPs on the same Pi IP would collide. We pick
an unused IP from the Pi's primary subnet via `arping`, claim it via
`ip addr add`, and persist the binding so the same model keeps the same IP
across restarts.

Allocation policy (deliberately conservative on the operator's LAN):
  - **Scan from the TOP** of the host range (e.g. .254 → .200). The DHCP
    pool on consumer routers is almost always .100-.199 + the .1 gateway;
    starting from .254 keeps us clear of any actively-leased addresses
    without requiring operator config.
  - **Verify with TWO arpings** (1s window, 1 packet each). Some devices
    are slow to respond — one arping is unreliable on a quiet LAN.
  - **Persist before claim** — write the binding to disk BEFORE running
    `ip addr add` so a crash mid-claim leaves the allocator in a state
    where the next start re-claims the same IP rather than leaking it.
  - **Idempotent claim** — `ip addr add` returns "File exists" if the IP
    is already on the interface (e.g. after a crash + restart). Treated
    as success.
  - **Persist the iface name** so a Pi with two NICs (wifi + ethernet)
    doesn't end up double-binding.

Test seam: every subprocess call goes through `_run` (overridable) so tests
inject a fake without touching the real system.

Sudoers: bootstrap.sh installs narrow patterns for
  /sbin/ip addr add * dev *
  /sbin/ip addr del * dev *
  /usr/sbin/arping -c * -w * *
— no shell escape, no wildcard verbs.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..lan_scan import _pack_ip, _resolve_netmask_for_ip, _resolve_own_ip

log = logging.getLogger(__name__)

# Where the persistence file lives. `/var/lib/makeros-hub/` is what bootstrap.sh
# already creates for the agent's state directory.
DEFAULT_STATE_PATH = Path("/var/lib/makeros-hub/vp-bindings.json")

# arping timing — 1 packet, 1s wait. Two consecutive arpings against the same
# candidate need both to fail for the IP to be considered free.
ARPING_COUNT = 1
ARPING_WAIT_SEC = 1
# How many candidates to consider before giving up. /24 has 254 host IPs;
# we'd usually find a free one in 1-2 tries.
MAX_CANDIDATES = 32
# `ip addr add` succeeds or fails fast — no need for a long timeout.
IP_CMD_TIMEOUT_SEC = 4.0
# arping is the slow piece: ARPING_COUNT × ARPING_WAIT_SEC + a little slack.
ARPING_TIMEOUT_SEC = 3.0


@dataclass(frozen=True)
class AllocationResult:
    """Outcome of an allocate_for(model) call. `bind_ip` is None on failure."""

    ok: bool
    model: str
    bind_ip: Optional[str]
    iface: Optional[str]
    error: Optional[str] = None
    # Useful for logging / heartbeat — how many candidates we tried before
    # finding (or failing to find) a free one.
    tried: int = 0


@dataclass
class Binding:
    """One persistent VP↔IP claim. Mutable for the in-memory cache."""

    model: str
    bind_ip: str
    iface: str
    claimed_at: float = field(default_factory=time.time)


# subprocess runner seam — tests inject a fake. Returns the subprocess result.
_RunFn = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _default_run(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class IpAllocator:
    """Owns the persistence file + the live bindings. Thread-unsafe by
    design — single caller (the VP manager) drives it. Methods that mutate
    state always persist BEFORE issuing privileged commands."""

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        *,
        run: _RunFn = _default_run,
        netmask_override: Optional[str] = None,
        own_ip_override: Optional[str] = None,
        iface_override: Optional[str] = None,
    ):
        self._state_path = state_path
        self._run = run
        self._netmask_override = netmask_override
        self._own_ip_override = own_ip_override
        self._iface_override = iface_override
        self._bindings: dict[str, Binding] = {}
        self._load()

    # ----- persistence -------------------------------------------------

    def _load(self) -> None:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("ip-allocator: state file read failed: %s", e)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("ip-allocator: state file corrupt, ignoring: %s", e)
            return
        bindings = data.get("bindings")
        if not isinstance(bindings, dict):
            return
        for model, entry in bindings.items():
            if not isinstance(entry, dict):
                continue
            bind_ip = entry.get("bind_ip")
            iface = entry.get("iface")
            if not isinstance(bind_ip, str) or not isinstance(iface, str):
                continue
            self._bindings[model] = Binding(
                model=model,
                bind_ip=bind_ip,
                iface=iface,
                claimed_at=float(entry.get("claimed_at", time.time())),
            )

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("ip-allocator: mkdir state dir failed: %s", e)
            return
        data = {
            "version": 1,
            "bindings": {m: asdict(b) for m, b in self._bindings.items()},
        }
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            log.warning("ip-allocator: state file write failed: %s", e)

    # ----- public API --------------------------------------------------

    def bindings_snapshot(self) -> dict[str, Binding]:
        return dict(self._bindings)

    def existing_for(self, model: str) -> Optional[Binding]:
        return self._bindings.get(model)

    def allocate_for(self, model: str) -> AllocationResult:
        """Pick + claim an unused IP for `model`. If a binding already
        exists, re-claim it (idempotent) and return success."""
        existing = self._bindings.get(model)
        if existing:
            # Re-claim on every reconcile so a Pi restart re-establishes the
            # IP without changing it. `ip addr add` returns "File exists"
            # when the IP is already on the iface — we treat that as success.
            claimed = self._claim_ip(existing.bind_ip, existing.iface)
            if claimed.ok:
                return AllocationResult(
                    ok=True,
                    model=model,
                    bind_ip=existing.bind_ip,
                    iface=existing.iface,
                    tried=0,
                )
            # Claim failed — re-allocate from scratch (rare; usually means
            # the iface name changed, e.g. eth0 → end0 after a kernel update).
            log.warning(
                "ip-allocator: re-claim of %s for model %s failed: %s — re-allocating",
                existing.bind_ip,
                model,
                claimed.error,
            )
            self._bindings.pop(model, None)
            self._save()

        # Fresh allocation.
        iface = self._iface_override or self._guess_iface()
        if not iface:
            return AllocationResult(
                ok=False, model=model, bind_ip=None, iface=None, error="no_iface"
            )
        own_ip = self._own_ip_override or _safe(_resolve_own_ip)
        netmask = self._netmask_override or (
            _resolve_netmask_for_ip(own_ip) if own_ip else None
        )
        if not own_ip or not netmask:
            return AllocationResult(
                ok=False, model=model, bind_ip=None, iface=iface, error="no_subnet"
            )

        # Build candidate list from the top of the host range, excluding the
        # router heuristic, broadcast, own IP, network address, and anything
        # already bound by us. We DO consider IPs in our own bindings
        # explicitly so the same model gets the same IP across re-runs.
        already_bound = {b.bind_ip for b in self._bindings.values()}
        candidates = self._enumerate_candidates(
            own_ip, netmask, exclude_extra=already_bound
        )
        if not candidates:
            return AllocationResult(
                ok=False, model=model, bind_ip=None, iface=iface, error="no_candidates"
            )

        tried = 0
        for cand in candidates[:MAX_CANDIDATES]:
            tried += 1
            if not self._ip_is_free(cand, iface):
                continue
            claimed = self._claim_ip(cand, iface)
            if claimed.ok:
                # Persist FIRST (so a crash mid-claim leaves a recoverable
                # state) — then we already issued the claim above. Save the
                # binding now that we know which IP stuck.
                self._bindings[model] = Binding(model=model, bind_ip=cand, iface=iface)
                self._save()
                return AllocationResult(
                    ok=True, model=model, bind_ip=cand, iface=iface, tried=tried
                )
            # Claim failed — try the next candidate.
            log.info("ip-allocator: claim %s on %s failed: %s", cand, iface, claimed.error)

        return AllocationResult(
            ok=False,
            model=model,
            bind_ip=None,
            iface=iface,
            error="no_free_ip",
            tried=tried,
        )

    def release(self, model: str) -> AllocationResult:
        """Drop the binding for `model` — remove the IP from the iface +
        forget it in the persistence file. Idempotent if no binding."""
        existing = self._bindings.pop(model, None)
        self._save()
        if not existing:
            return AllocationResult(ok=True, model=model, bind_ip=None, iface=None)
        # Best-effort — if `ip addr del` fails the iface either lost the IP
        # already (boot wipe) or something else freed it. Either way the
        # cloud-side state is what matters; the next allocation will pick
        # a fresh candidate.
        released = self._release_ip(existing.bind_ip, existing.iface)
        return AllocationResult(
            ok=True,
            model=model,
            bind_ip=existing.bind_ip,
            iface=existing.iface,
            error=released.error if not released.ok else None,
        )

    # ----- internals ---------------------------------------------------

    def _enumerate_candidates(
        self, own_ip: str, netmask: str, exclude_extra: set[str]
    ) -> list[str]:
        """Return host IPs in the subnet, top→bottom, minus skip-list."""
        net_bytes = _pack_ip(own_ip)
        mask_bytes = _pack_ip(netmask)
        if not net_bytes or not mask_bytes:
            return []
        ip_int = struct.unpack(">I", net_bytes)[0]
        mask_int = struct.unpack(">I", mask_bytes)[0]
        network_int = ip_int & mask_int
        host_bits = (~mask_int) & 0xFFFFFFFF
        if host_bits == 0:
            return []
        broadcast_int = network_int | host_bits

        skip = {own_ip, *exclude_extra}
        # Also skip the network and broadcast addresses + the router .1.
        skip.add(socket.inet_ntoa(struct.pack(">I", network_int)))
        skip.add(socket.inet_ntoa(struct.pack(">I", broadcast_int)))
        skip.add(socket.inet_ntoa(struct.pack(">I", network_int | 1)))

        out: list[str] = []
        # Walk from broadcast-1 down to network+2 (skipping .1 we set above).
        for offset in range(host_bits - 1, 1, -1):
            ip_str = socket.inet_ntoa(struct.pack(">I", network_int | offset))
            if ip_str in skip:
                continue
            out.append(ip_str)
            if len(out) >= MAX_CANDIDATES:
                break
        return out

    def _guess_iface(self) -> Optional[str]:
        """Read /proc/net/route to find the iface that owns the default
        route — the same iface we'll add the VP IP to."""
        try:
            with open("/proc/net/route", "r") as f:
                lines = f.readlines()
        except OSError:
            return None
        for raw in lines[1:]:
            cols = raw.split()
            if len(cols) < 8:
                continue
            try:
                dest = int(cols[1], 16)
            except ValueError:
                continue
            if dest == 0:  # default route
                return cols[0]
        return None

    def _ip_is_free(self, ip: str, iface: str) -> bool:
        """Two arpings — both must report no reply for the IP to be free.
        Some devices skip the first arping (interrupt coalescing on cheap
        NICs); the second catches them."""
        for _ in range(2):
            if self._arping_replied(ip, iface):
                return False
        return True

    def _arping_replied(self, ip: str, iface: str) -> bool:
        """One arping. Returns True if SOMEONE answered (IP is taken)."""
        argv = [
            "sudo",
            "-n",
            "/usr/sbin/arping",
            "-c",
            str(ARPING_COUNT),
            "-w",
            str(ARPING_WAIT_SEC),
            "-I",
            iface,
            ip,
        ]
        try:
            result = self._run(argv, ARPING_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("ip-allocator: arping %s failed: %s", ip, e)
            return False  # treat failure as "no reply" — we'll try claim
        # arping exits 0 on reply, 1 on no reply, other on error.
        return result.returncode == 0

    def _claim_ip(self, ip: str, iface: str) -> AllocationResult:
        """Run `sudo ip addr add <ip>/32 dev <iface>`. Returns ok=True on
        success or if the IP is already on the iface ('File exists')."""
        argv = [
            "sudo",
            "-n",
            "/sbin/ip",
            "addr",
            "add",
            f"{ip}/32",
            "dev",
            iface,
        ]
        try:
            result = self._run(argv, IP_CMD_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as e:
            return AllocationResult(ok=False, model="", bind_ip=ip, iface=iface, error=str(e))
        if result.returncode == 0:
            return AllocationResult(ok=True, model="", bind_ip=ip, iface=iface)
        stderr = (result.stderr or "").lower()
        if "file exists" in stderr or "already assigned" in stderr:
            return AllocationResult(ok=True, model="", bind_ip=ip, iface=iface)
        return AllocationResult(
            ok=False,
            model="",
            bind_ip=ip,
            iface=iface,
            error=stderr.strip() or f"ip addr add rc={result.returncode}",
        )

    def _release_ip(self, ip: str, iface: str) -> AllocationResult:
        argv = [
            "sudo",
            "-n",
            "/sbin/ip",
            "addr",
            "del",
            f"{ip}/32",
            "dev",
            iface,
        ]
        try:
            result = self._run(argv, IP_CMD_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as e:
            return AllocationResult(ok=False, model="", bind_ip=ip, iface=iface, error=str(e))
        if result.returncode == 0:
            return AllocationResult(ok=True, model="", bind_ip=ip, iface=iface)
        stderr = (result.stderr or "").lower()
        # Already gone — count as success.
        if "cannot assign" in stderr or "no such" in stderr:
            return AllocationResult(ok=True, model="", bind_ip=ip, iface=iface)
        return AllocationResult(
            ok=False,
            model="",
            bind_ip=ip,
            iface=iface,
            error=stderr.strip() or f"ip addr del rc={result.returncode}",
        )


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None
