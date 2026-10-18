from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from ..config import VirtualPrinterMember


AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SEC = 60.0
AUTH_MAX_KEYS = 1024


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    member_id: str | None = None
    rate_limited: bool = False


class AuthRateLimiter:
    def __init__(
        self,
        *,
        limit: int = AUTH_FAILURE_LIMIT,
        window_sec: float = AUTH_FAILURE_WINDOW_SEC,
        max_keys: int = AUTH_MAX_KEYS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self.max_keys = max(1, int(max_keys))
        self.clock = clock or time.monotonic
        self._by_ip: OrderedDict[str, deque[float]] = OrderedDict()
        self._by_code: OrderedDict[str, deque[float]] = OrderedDict()

    def is_limited(self, ip: str | None, code: str | None) -> bool:
        now = self.clock()
        self._prune_bucket(self._by_ip, now)
        self._prune_bucket(self._by_code, now)
        return (
            self._count_for_key(self._by_ip, _ip_key(ip), now) >= self.limit
            or self._count_for_key(self._by_code, _code_key(code), now) >= self.limit
        )

    def record_failure(self, ip: str | None, code: str | None) -> None:
        now = self.clock()
        self._prune_bucket(self._by_ip, now)
        self._prune_bucket(self._by_code, now)
        self._append(self._by_ip, _ip_key(ip), now)
        self._append(self._by_code, _code_key(code), now)

    def _count_for_key(
        self,
        bucket: OrderedDict[str, deque[float]],
        key: str,
        now: float,
    ) -> int:
        events = bucket.get(key)
        if events is None:
            return 0
        count = self._prune(events, now)
        if count:
            bucket.move_to_end(key)
        else:
            bucket.pop(key, None)
        return count

    def _append(self, bucket: OrderedDict[str, deque[float]], key: str, now: float) -> None:
        events = bucket.get(key)
        if events is None:
            events = deque()
            bucket[key] = events
        else:
            self._prune(events, now)
        events.append(now)
        bucket.move_to_end(key)
        while len(bucket) > self.max_keys:
            bucket.popitem(last=False)

    def _prune_bucket(self, bucket: OrderedDict[str, deque[float]], now: float) -> None:
        for key in list(bucket.keys()):
            if self._prune(bucket[key], now) == 0:
                bucket.pop(key, None)

    def _prune(self, events: deque[float], now: float) -> int:
        cutoff = now - self.window_sec
        while events and events[0] < cutoff:
            events.popleft()
        return len(events)


class ConnectLog:
    """The latest successful sign-in per (virtual printer, member code), for the heartbeat (v0.71). The site stores it on
    the code's row so the shop sees which members' slicers really reach the box, and from where (operator, 2026-10-18:
    "have their information about script version number sent to us when they connect to the box"). Keyed by the code's
    sha256, which the site already sent the hub; never the code. Thread-safe: MQTT and FTPS sign in on the servers'
    loops while the heartbeat reads on its own thread. Bounded: the oldest entry goes first past max_keys."""

    def __init__(self, *, max_keys: int = 1024, clock: Callable[[], float] | None = None) -> None:
        self.max_keys = max(1, int(max_keys))
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._pending: OrderedDict[tuple[str, str], dict] = OrderedDict()

    def record(self, serial: str, code_sha256: str, ip: str | None) -> None:
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        with self._lock:
            key = (serial, code_sha256)
            self._pending.pop(key, None)
            self._pending[key] = {"serial": serial, "codeSha256": code_sha256, "at": at, "ip": ip or ""}
            while len(self._pending) > self.max_keys:
                self._pending.popitem(last=False)

    def snapshot(self, limit: int = 64) -> list[dict]:
        """The newest `limit` entries not yet acknowledged, newest last."""
        with self._lock:
            return [dict(v) for v in list(self._pending.values())[-limit:]]

    def ack(self, sent: list[dict]) -> None:
        """After a good heartbeat: drop what was sent, unless a newer sign-in replaced it meanwhile."""
        with self._lock:
            for e in sent:
                key = (e["serial"], e["codeSha256"])
                if self._pending.get(key, {}).get("at") == e["at"]:
                    self._pending.pop(key, None)


CONNECTS = ConnectLog()


class MemberAuthSet:
    def __init__(
        self,
        members: tuple[VirtualPrinterMember, ...] | list[VirtualPrinterMember],
        *,
        limiter: AuthRateLimiter | None = None,
        serial: str | None = None,
        connects: ConnectLog | None = None,
    ) -> None:
        self.members = tuple(members)
        self.limiter = limiter or AuthRateLimiter()
        self.serial = serial
        self.connects = connects if connects is not None else CONNECTS

    def replace_members(
        self,
        members: tuple[VirtualPrinterMember, ...] | list[VirtualPrinterMember],
    ) -> None:
        self.members = tuple(members)

    def lookup_member_id(self, access_code: str | None) -> str | None:
        provided = access_code if isinstance(access_code, str) else ""
        provided_hash = hashlib.sha256(provided.encode("utf-8")).hexdigest()
        candidate = len(provided) >= 8
        matched: str | None = None
        for member in self.members:
            # Do not short-circuit; every configured hash gets the same compare
            # call regardless of where a match appears in the set.
            is_match = hmac.compare_digest(provided_hash, member.access_code_sha256)
            if candidate and is_match and matched is None:
                matched = member.member_id
        return matched

    def authenticate(self, access_code: str | None, ip: str | None = None) -> AuthResult:
        if self.limiter.is_limited(ip, access_code):
            return AuthResult(ok=False, rate_limited=True)
        member_id = self.lookup_member_id(access_code)
        if member_id is not None:
            if self.serial:
                try:
                    self.connects.record(self.serial, hashlib.sha256(access_code.encode("utf-8")).hexdigest(), ip)
                except Exception:  # noqa: BLE001 - bookkeeping never refuses a member who signed in
                    pass
            return AuthResult(ok=True, member_id=member_id)
        self.limiter.record_failure(ip, access_code)
        return AuthResult(ok=False)

    def record_failure(self, access_code: str | None, ip: str | None = None) -> None:
        self.limiter.record_failure(ip, access_code)


def _ip_key(ip: str | None) -> str:
    return ip or "<unknown>"


def _code_key(code: str | None) -> str:
    return code or "<missing>"
