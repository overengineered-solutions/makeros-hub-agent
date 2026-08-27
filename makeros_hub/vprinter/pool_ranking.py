"""Pool RANKING — which filaments a Virtual Printer advertises when more are loaded than it has slots
(member-setup-bundle.md §10.2; the operator's requirement verbatim: "most-used and most-recently-added
colors and types are shown as options"). This is the layer makeros's own curation-view comment named and
never built ("RANKING — pinned top-4, auto-promote-on-load — layers on top once that tracking exists").

Two persisted signals per filament key (material|filamentId|color — the live_pool dedupe key):
  first_seen   — when the key FIRST appeared in any AMS ("auto-promote-on-load": a spool loaded this week
                 always makes the cut, newest first).
  use_minutes  — decayed active-print minutes: each observe() tick adds the tick's span to the key loaded
                 in the ACTIVE tray (tray_now) of every PRINTING printer. Time-weighted usage — a 10-hour
                 print IS heavy use — with a 30-day half-life so last month's fad fades.

Selection policy v1 (tunable constants below):
  capacity = the VP's units×trays. Under capacity → everything shows (ranking moot).
  Over → (a) every key first seen within RECENT_DAYS, newest first; (b) remaining slots by decayed
  use_minutes (desc); (c) stable key-sort tie-break. The RETURNED selection is then re-ordered by stable
  key-sort so slot layout only changes when SET MEMBERSHIP changes — minimal ams.version churn.

State persists next to vp-bindings.json; a lost/corrupt file self-heals (everything reads as newly seen,
which over-includes for a week — the harmless direction). Pure logic + injectable clock; IO at the edges.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("makeros-hub.vprinter")

DEFAULT_STATE_PATH = Path("/var/lib/makeros-hub/pool-ranking.json")
RECENT_DAYS = 7.0
HALF_LIFE_DAYS = 30.0
USAGE_RESERVE_FRACTION = 0.25   # over-capacity, at least this share stays with usage-tier winners (codex #3:
                                # 'most-used AND most-recently-added' is a conjunction — capacity+1 fresh spools
                                # must never evict every heavy-use color for a week)
PERSIST_MIN_INTERVAL_SEC = 60.0
MAX_TRACKED_KEYS = 512   # a shop cannot plausibly rotate more distinct spools; bound the file


class PoolRankingState:
    """Owns the persisted signals. Single caller (the heartbeat thread) — not thread-safe by design."""

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH, *, now: Optional[float] = None):
        self._state_path = state_path
        self._keys: dict[str, dict[str, float]] = {}
        self._dirty = False
        self._last_persist = 0.0
        self._load(now if now is not None else time.time())

    # ----- persistence (best-effort; ranking must never sink a heartbeat) -----

    def _load(self, now: float) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            keys = raw.get("keys") if isinstance(raw, dict) else None
            if isinstance(keys, dict):
                for k, v in list(keys.items())[:MAX_TRACKED_KEYS]:
                    if not isinstance(k, str) or not isinstance(v, dict):
                        continue
                    fs = v.get("first_seen")
                    um = v.get("use_minutes")
                    lu = v.get("last_use")
                    self._keys[k] = {
                        "first_seen": float(fs) if isinstance(fs, (int, float)) and fs > 0 else now,
                        "use_minutes": float(um) if isinstance(um, (int, float)) and um >= 0 else 0.0,
                        "last_use": float(lu) if isinstance(lu, (int, float)) and lu > 0 else now,
                    }
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - corrupt state self-heals as empty
            log.warning("pool-ranking: state unreadable (%s) — starting fresh", exc)

    def persist(self, now: float, *, force: bool = False) -> None:
        if not self._dirty:
            return
        if not force and (now - self._last_persist) < PERSIST_MIN_INTERVAL_SEC:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "keys": self._keys}, indent=1), encoding="utf-8")
            tmp.replace(self._state_path)
            self._dirty = False
            self._last_persist = now
        except OSError as exc:
            log.warning("pool-ranking: persist failed: %s", exc)

    # ----- signals -----

    def _decayed(self, key: str, now: float) -> float:
        rec = self._keys.get(key)
        if rec is None:
            return 0.0
        age_days = max(0.0, (now - rec["last_use"]) / 86400.0)
        return rec["use_minutes"] * (0.5 ** (age_days / HALF_LIFE_DAYS))

    def observe(self, loaded_keys: list[str], active_keys: list[str], tick_minutes: float, now: float) -> None:
        """One heartbeat's evidence: every currently-LOADED key stamps first_seen if new; every ACTIVE key
        (the tray_now of a printing printer) accrues the tick span. Bounded; unknown growth evicts the
        stalest keys."""
        for key in loaded_keys:
            if key not in self._keys:
                self._keys[key] = {"first_seen": now, "use_minutes": 0.0, "last_use": now}
                self._dirty = True
        span = max(0.0, min(float(tick_minutes), 10.0))   # a wedged clock can't mint hours of "use"
        for key in active_keys:
            rec = self._keys.get(key)
            if rec is None:
                rec = {"first_seen": now, "use_minutes": 0.0, "last_use": now}
                self._keys[key] = rec
            rec["use_minutes"] = self._decayed(key, now) + span
            rec["last_use"] = now
            self._dirty = True
        if len(self._keys) > MAX_TRACKED_KEYS:
            for k in sorted(self._keys, key=lambda x: self._keys[x]["last_use"])[: len(self._keys) - MAX_TRACKED_KEYS]:
                del self._keys[k]
            self._dirty = True

    def select(self, candidate_keys: list[str], capacity: int, now: float) -> list[str]:
        """The §10.2 policy over CANDIDATES (already deduped, any order). Returns the chosen keys in
        STABLE KEY-SORT order — membership carries the ranking; layout stays deterministic."""
        cands = sorted(set(candidate_keys))
        if len(cands) <= capacity:
            return cands
        recent_cutoff = now - RECENT_DAYS * 86400.0
        recent = [k for k in cands if self._keys.get(k, {}).get("first_seen", 0.0) >= recent_cutoff]
        recent.sort(key=lambda k: (-self._keys.get(k, {}).get("first_seen", 0.0), k))
        non_recent = [k for k in cands if k not in set(recent)]
        # The recent tier is capped so usage winners keep a reserved share whenever non-recent candidates
        # exist — the requirement is a conjunction, not recency-first-take-all.
        reserve = min(len(non_recent), max(1, int(capacity * USAGE_RESERVE_FRACTION))) if non_recent else 0
        chosen: list[str] = recent[: max(0, capacity - reserve)]
        if len(chosen) < capacity:
            rest = [k for k in cands if k not in set(chosen)]
            rest.sort(key=lambda k: (-self._decayed(k, now), k))
            chosen.extend(rest[: capacity - len(chosen)])
        return sorted(chosen)
