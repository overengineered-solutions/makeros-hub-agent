"""LAN discovery facade — combines the Moonraker HTTP sweep + Bambu SSDP
listener into one entry point the agent's heartbeat loop calls.

Periodic mode: the agent main loop invokes `maybe_run_periodic_scan()` once
per heartbeat. The function self-rate-limits to one full sweep per
`PERIODIC_INTERVAL_SEC`, so the actual scan only fires every ~5 minutes
regardless of how often the heartbeat ticks. Result hits are CACHED on this
module so the next heartbeat's `discoveryHits` field is just a list-of-dicts
read — no work on the request path.

On-demand mode: the cloud queues a `lan-scan` probe (see `probes.py`), the
agent's probe dispatcher calls `run_immediate_scan()`, which forces a sweep
RIGHT NOW (bypassing the rate limit) and returns the hits as a JSON string
for the probe result payload. The cached hits also refresh, so the next
heartbeat carries the fresh data even if the cloud reads the probe result
asynchronously.

Why one facade rather than two call sites? Both modes want the same merge
behavior (Moonraker + Bambu, dedup, hit cap, optional ordering). Centralizing
also keeps the periodic interval + cache lifetime in one place.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import bambu_ssdp, lan_scan
from .lan_scan import DiscoveryHit

log = logging.getLogger(__name__)

# How often the periodic worker actually runs a full sweep. 5 min keeps the
# dropdown fresh for a non-trivial admin add session without churning the LAN.
# The agent's heartbeat is 30s, so we'll skip 9 of every 10 heartbeats.
PERIODIC_INTERVAL_SEC = 5 * 60.0
# Cap on hits shipped to the cloud per heartbeat. A /24 with one Moonraker
# per printer rarely exceeds 20; 100 is plenty headroom + bounds the payload
# size so a misbehaving sweep can't bloat the heartbeat.
MAX_HITS_PER_BATCH = 100
# How long a cached hit stays "fresh" for cloud read after its sweep. Past
# this, the run_scan callsite re-sweeps even if PERIODIC_INTERVAL_SEC hasn't
# elapsed.
HIT_CACHE_TTL_SEC = 10 * 60.0


_state_lock = threading.Lock()
_last_scan_at: float = 0.0
_cached_hits: list[DiscoveryHit] = []


def _do_scan_locked_unsafe() -> list[DiscoveryHit]:
    """Run BOTH the HTTP sweep and SSDP listener; merge + dedup. Lock-naive —
    callers hold the module lock. Order: SSDP first (cheap blocking 3s),
    then HTTP sweep (~8s budget) — total ~11s worst case."""
    moonraker_hits = lan_scan.run_scan()
    bambu_hits = bambu_ssdp.listen_for_bambu_announcements()
    return _merge_hits(moonraker_hits, bambu_hits)


def _merge_hits(
    moonraker: list[DiscoveryHit], bambu: list[DiscoveryHit]
) -> list[DiscoveryHit]:
    """Dedup by (kind, ip) and cap at MAX_HITS_PER_BATCH. Bambu hits first
    (more identifying info per row), then Moonraker."""
    seen: dict[tuple[str, str], DiscoveryHit] = {}
    for h in bambu + moonraker:
        seen.setdefault((h.kind, h.ip), h)
        if len(seen) >= MAX_HITS_PER_BATCH:
            break
    return list(seen.values())


def maybe_run_periodic_scan(now: float | None = None) -> bool:
    """Called from the heartbeat loop. Decides whether enough wall time has
    elapsed since the last sweep, runs it inline if so, and updates the
    cache. Returns True iff a sweep actually ran this call (mostly for
    test/log clarity).

    Inline + blocking: the heartbeat loop runs single-threaded, so we're
    sharing the loop's compute. The total sweep budget (~11s) sits inside
    the 30s heartbeat — the cloud absorbs a slow tick on sweep ticks without
    drama. We DON'T spin up a background thread because then we'd have two
    callers racing on _cached_hits + need a more elaborate fence.
    """
    monotonic = now if now is not None else time.monotonic()
    with _state_lock:
        elapsed = monotonic - _last_scan_at
        if elapsed < PERIODIC_INTERVAL_SEC:
            return False
        hits = _do_scan_locked_unsafe()
        _set_cache_unsafe(hits, monotonic)
    return True


def run_immediate_scan() -> list[DiscoveryHit]:
    """Force a fresh sweep right now — used by the on-demand probe path.
    Returns the new hits AND updates the cache so the next heartbeat ships
    the same data. Bypasses the periodic interval."""
    with _state_lock:
        hits = _do_scan_locked_unsafe()
        _set_cache_unsafe(hits, time.monotonic())
        return list(hits)


def get_cached_hits(now: float | None = None) -> list[DiscoveryHit]:
    """Return the latest cached hits if they're inside the TTL, else empty.
    The heartbeat loop calls this to populate `payload['discoveryHits']`."""
    monotonic = now if now is not None else time.monotonic()
    with _state_lock:
        if monotonic - _last_scan_at > HIT_CACHE_TTL_SEC:
            return []
        return list(_cached_hits)


def _set_cache_unsafe(hits: list[DiscoveryHit], now: float) -> None:
    """Mutate the module cache. Lock-naive."""
    global _last_scan_at, _cached_hits
    _cached_hits = list(hits)
    _last_scan_at = now


def hits_to_payload(hits: list[DiscoveryHit]) -> list[dict[str, Any]]:
    """Serialize hits to the heartbeat-wire shape. Pure — no IO."""
    return [h.to_dict() for h in hits]


def hits_to_json_for_probe(hits: list[DiscoveryHit]) -> str:
    """Same as hits_to_payload but JSON-encoded — for the probe `rawOutput`
    field. Bounded length is enforced by the probe registry's max_output_bytes."""
    return json.dumps(hits_to_payload(hits), separators=(",", ":"))


# Test seam — pytest fixtures call this to clear module state between cases
# instead of monkey-patching the globals directly.
def reset_for_tests() -> None:
    with _state_lock:
        global _last_scan_at, _cached_hits
        _last_scan_at = 0.0
        _cached_hits = []
