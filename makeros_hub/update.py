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


def recently_attempted(target: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    st = _read_state()
    return st.get("target") == target and (now - float(st.get("at", 0))) < ATTEMPT_COOLDOWN_SEC


def apply_update(tag: str) -> bool:
    """Trigger the root update script for a validated release tag. The script
    re-launches the actual work in an independent systemd transient unit, so the
    service restart it performs doesn't kill the update mid-flight. Returns True
    if the trigger launched cleanly."""
    if not is_release_tag(tag):
        log.error("refusing to update to non-release tag %r", tag)
        return False
    _write_state({"target": tag, "at": time.time()})
    log.warning("OTA: triggering update to %s via %s (service will restart)", tag, UPDATE_SCRIPT)
    try:
        subprocess.run(["sudo", UPDATE_SCRIPT, tag], check=True, timeout=120)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log.error("OTA: update trigger to %s failed: %s", tag, e)
        return False


def maybe_update(current_version: str, target_version) -> bool:
    """Decide + (if appropriate) trigger an update. Returns True if an update was
    launched. Honors the cooldown so a broken target can't loop."""
    if not isinstance(target_version, str) or not should_update(current_version, target_version):
        return False
    if recently_attempted(target_version):
        log.info("OTA: target %s attempted recently — waiting out the cooldown", target_version)
        return False
    return apply_update(target_version)
