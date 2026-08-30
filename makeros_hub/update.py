"""Over-the-air self-update.

The cloud tells the agent (in the heartbeat response, `targetVersion`) which
RELEASE TAG it should be running. If that's a well-formed release newer than the
running version, the agent triggers the root update script and the service
restarts onto the new code — no SSH.

Security posture (this is the one remote-code-execution path in the system, so
it's deliberately narrow):
  - Only well-formed release tags `vX.Y.Z` are ever accepted — never a branch, a
    commit, `main`, or an arbitrary ref. The cloud cannot point the agent at
    anything but a real tagged release of the one hardcoded repo.
  - The update runs via a sudoers rule scoped to ONLY `/opt/makeros-hub/update.sh`
    (the non-root agent user can run nothing else as root).
  - Monotonic: never downgrades.
  - A cooldown stops an update loop if a target release is broken (a failed
    update would otherwise restart the old agent, which would see the target and
    retry immediately).
  - Signed release artifacts are the documented next hardening step (SECURITY.md).

The version-compare/decision logic here is pure and unit-tested; `apply_update`
is the subprocess side (integration-tested on the Pi).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("makeros-hub.update")

RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
UPDATE_SCRIPT = os.environ.get("MAKEROS_HUB_UPDATE_SCRIPT", "/opt/makeros-hub/update.sh")
STATE_PATH = Path(
    os.environ.get("MAKEROS_HUB_UPDATE_STATE", "/var/lib/makeros-hub/last_update.json")
)
# Don't re-attempt the SAME target more often than this — avoids hammering a
# broken release (failed update -> systemd restarts old agent -> sees target).
ATTEMPT_COOLDOWN_SEC = 900
# v0.55: a target that has been INSTALLED this many times and still isn't what the running agent reports means the
# release itself is wrong (v0.51–v0.54 bumped pyproject but not __version__ → the hub reinstalled + restarted itself
# every 15 min for an hour, mid-print). Stop, say so, and wait for a NEW target instead of looping forever.
MAX_ATTEMPTS_PER_TARGET = 3
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Update transport (dual-review 2026-07-20 — Docker portability): 'systemd' (default; sudo root script → transient
# unit → systemctl restart), 'exit' (container: record the target + exit EXIT_FOR_UPDATE_CODE so an orchestrator
# repulls the pinned image — no sudo/systemd), or 'disabled' (never self-update).
UPDATE_MODE = os.environ.get("MAKEROS_HUB_UPDATE_MODE", "systemd").strip().lower()
EXIT_FOR_UPDATE_CODE = 75  # EX_TEMPFAIL — a defined "please repull me" exit code for the orchestrator


def parse_version(s) -> tuple[int, int, int] | None:
    """'v0.3.0' or '0.3.0' -> (0, 3, 0); None if malformed."""
    if not isinstance(s, str):
        return None
    m = RELEASE_TAG_RE.match(s if s.startswith("v") else "v" + s)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_release_tag(s) -> bool:
    """A strict release tag: vMAJOR.MINOR.PATCH. The safety gate before we ever
    hand a value to git/the updater."""
    return isinstance(s, str) and bool(RELEASE_TAG_RE.match(s))


def is_commit_sha(s) -> bool:
    """A full 40-hex git commit SHA — the content-trust pin the cloud sends alongside a target tag."""
    return isinstance(s, str) and bool(COMMIT_SHA_RE.match(s))


def is_newer(target, current) -> bool:
    t, c = parse_version(target), parse_version(current)
    return bool(t and c and t > c)


def should_update(current_version, target_version) -> bool:
    """Pure decision: update only TO a well-formed release tag that is strictly
    newer than what's running. Never a non-release ref, never a downgrade."""
    return is_release_tag(target_version) and is_newer(target_version, current_version)


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(d: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(d), encoding="utf-8")
    except OSError as e:
        log.warning("could not persist update state: %s", e)


def _request_exit(code: int) -> None:
    """Exit the process so a container orchestrator repulls the pinned image. A seam so tests assert w/o exiting."""
    raise SystemExit(code)


def recently_attempted(target: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    st = _read_state()
    return st.get("target") == target and (now - float(st.get("at", 0))) < ATTEMPT_COOLDOWN_SEC


def attempts_exhausted(target: str) -> bool:
    st = _read_state()
    return st.get("target") == target and int(st.get("attempts", 0)) >= MAX_ATTEMPTS_PER_TARGET


def apply_update(tag: str, expected_sha: str | None = None) -> bool:
    """Trigger an update to a validated release tag. Behavior follows MAKEROS_HUB_UPDATE_MODE:
      - 'systemd' (default): run the sudo root script, which (with expected_sha) verifies the tag resolves to
        EXACTLY that commit before installing, then restarts via an independent transient unit.
      - 'exit': record the target + exit EXIT_FOR_UPDATE_CODE so a container orchestrator repulls the pinned image.
      - 'disabled': ignore.
    `expected_sha` is the cloud's content-trust pin (the commit the tag MUST resolve to); a compromised git host
    can't serve other code. Returns True only when a systemd trigger launched cleanly."""
    if not is_release_tag(tag):
        log.error("refusing to update to non-release tag %r", tag)
        return False
    if expected_sha is not None and not is_commit_sha(expected_sha):
        log.error("refusing update to %s: malformed target SHA %r", tag, expected_sha)
        return False
    if UPDATE_MODE == "disabled":
        log.info("OTA: update to %s requested but MAKEROS_HUB_UPDATE_MODE=disabled — ignoring", tag)
        return False
    prev = _read_state()
    attempts = int(prev.get("attempts", 0)) + 1 if prev.get("target") == tag else 1
    _write_state({"target": tag, "at": time.time(), "attempts": attempts})
    if UPDATE_MODE == "exit":
        log.warning("OTA: MAKEROS_HUB_UPDATE_MODE=exit — recorded target %s; exiting %d for the orchestrator to "
                    "repull the pinned image", tag, EXIT_FOR_UPDATE_CODE)
        _request_exit(EXIT_FOR_UPDATE_CODE)
        return False  # unreachable in prod (_request_exit raises); returns only under a test seam
    trust = f" @ {expected_sha[:12]}…" if expected_sha else " (UNVERIFIED — cloud sent no target SHA)"
    log.warning("OTA: triggering update to %s%s via %s (service will restart)", tag, trust, UPDATE_SCRIPT)
    cmd = ["sudo", UPDATE_SCRIPT, tag] + ([expected_sha] if expected_sha else [])
    try:
        subprocess.run(cmd, check=True, timeout=120)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log.error("OTA: update trigger to %s failed: %s", tag, e)
        return False


# v0.54 (owner): updates and hot-fixes apply ANYTIME by default — finals week never has an idle fleet, and the durable
# dispatch/progress state makes a restart mid-print safe. MAKEROS_HUB_OTA_WAIT_IDLE=1 opts back into waiting for idle.
OTA_WAIT_IDLE = os.environ.get("MAKEROS_HUB_OTA_WAIT_IDLE", "0").strip() == "1"


def should_defer_update(statuses) -> bool:
    """v0.53: never restart the agent while a printer is preparing/printing/paused. Pure over the heartbeat statuses
    (PrinterStatusDTO dicts): gcodeState RUNNING/PAUSE/PREPARE or activity state printing/paused ⇒ defer."""
    for st in statuses if isinstance(statuses, list) else []:
        if not isinstance(st, dict):
            continue
        if str(st.get("gcodeState", "")).upper() in {"RUNNING", "PAUSE", "PREPARE"}:
            return True
        if str(st.get("state", "")).lower() in {"printing", "paused"}:
            return True
    return False


def maybe_update(current_version: str, target_version, target_sha=None) -> bool:
    """Decide + (if appropriate) trigger an update. `target_sha` (optional) is the cloud's content-trust pin.
    Returns True if a systemd update launched; honors the cooldown so a broken target can't loop."""
    if not isinstance(target_version, str) or not should_update(current_version, target_version):
        return False
    if attempts_exhausted(target_version):
        log.error("OTA: target %s was installed %d times but this agent still reports %s — the release is not "
                  "reporting its own version (bump makeros_hub/__init__.py:__version__); refusing to loop until a "
                  "NEW target is set", target_version, MAX_ATTEMPTS_PER_TARGET, current_version)
        return False
    if recently_attempted(target_version):
        log.info("OTA: target %s attempted recently — waiting out the cooldown", target_version)
        return False
    return apply_update(target_version, target_sha if is_commit_sha(target_sha) else None)
