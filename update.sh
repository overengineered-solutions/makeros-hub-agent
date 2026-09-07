#!/bin/sh
# /opt/makeros-hub/update.sh <vX.Y.Z>
#
# Over-the-air agent update. The agent (running as the non-root makeros-hub user)
# invokes this via a narrow sudoers rule that permits ONLY this script. It then
# re-launches the real work in an INDEPENDENT systemd transient unit, so the
# `systemctl start makeros-hub` it performs at the end can't kill the update
# mid-flight (a service can't cleanly restart itself).
#
# Safety: only ever acts on a real release tag (vX.Y.Z) of the one hardcoded
# repo — never a branch, a commit, or an arbitrary ref.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

TAG="${1:-}"
EXPECTED_SHA="${2:-}"
echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || {
  echo "makeros-hub update: refusing non-release tag '$TAG'" >&2
  exit 2
}
# Content-trust pin: the commit the tag MUST resolve to (the cloud sends it in the heartbeat). MANDATORY (audit
# 2026-08-30): without it a repointed tag on the code host would run unverified code as root — refuse.
echo "$EXPECTED_SHA" | grep -Eq '^[0-9a-f]{40}$' || {
  echo "makeros-hub update: missing/malformed target SHA '$EXPECTED_SHA' — content trust is mandatory" >&2
  exit 2
}

# This root-owned helper is the trust boundary. The service owns config.toml
# and supplies the SHA, so neither can authorize a different repository.
REPO="https://git.overengineeredsolutions.org/makeros-hub-agent.git"
SELF="/opt/makeros-hub/update.sh"

# Phase 1 — invoked by the agent, still inside the service's cgroup. Detach into
# our own transient unit and return immediately.
if [ "${MAKEROS_OTA_DETACHED:-}" != "1" ]; then
  if command -v systemd-run >/dev/null 2>&1; then
    exec systemd-run --collect --quiet --unit="makeros-hub-ota" \
      --setenv=MAKEROS_OTA_DETACHED=1 "$SELF" "$TAG" "$EXPECTED_SHA"
  fi
  # Fallback when systemd-run is unavailable: detach with setsid.
  MAKEROS_OTA_DETACHED=1 setsid "$SELF" "$TAG" "$EXPECTED_SHA" </dev/null >/dev/null 2>&1 &
  exit 0
fi

# Phase 2 — running in the transient unit (own cgroup). Do the real work.
exec 9>/run/makeros-hub-update.lock
flock -n 9 || { echo "makeros-hub OTA: another update is running" >&2; exit 4; }
# A service-user caller can bypass update.py. Refuse a downgrade HERE too,
# otherwise it could reinstall an old canonical release with a vulnerable helper.
CURRENT="$(sed -n 's/^__version__ = "\([0-9.]*\)"$/\1/p' /opt/makeros-hub/makeros_hub/__init__.py)"
python3 -I - "$CURRENT" "$TAG" <<'VERSION_GATE'
import re
import sys
current, target = sys.argv[1:]
if (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", current)
        or tuple(map(int, target[1:].split("."))) <= tuple(map(int, current.split(".")))):
    sys.exit("makeros-hub OTA: refusing unknown installed version, same version or downgrade; operator rollback requires a manual install")
VERSION_GATE
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "makeros-hub OTA: cloning $TAG"
# No --depth: a dumb-HTTP-served mirror does not support shallow clones (this repo is small).
git clone --branch "$TAG" "$REPO" "$TMP/src"
cd "$TMP/src"
# Content-trust: the tag must resolve to the EXACT commit the cloud pinned. A compromised/re-pointed tag on the
# git host fails here BEFORE anything runs as root. (dual-review 2026-07-20; roadmap: signed-tag verification.)
GOT="$(git rev-parse HEAD)"
if [ "$GOT" != "$EXPECTED_SHA" ]; then
  echo "makeros-hub OTA: REFUSING $TAG — resolved to $GOT, expected $EXPECTED_SHA" >&2
  exit 3
fi
BUILT="$(sed -n 's/^__version__ = "\([0-9.]*\)"$/\1/p' makeros_hub/__init__.py)"
[ "v$BUILT" = "$TAG" ] || { echo "makeros-hub OTA: release version does not match tag" >&2; exit 3; }
echo "makeros-hub OTA: content-trust verified ($TAG @ $EXPECTED_SHA)"
./install.sh
echo "makeros-hub OTA: installed $TAG — restarting service"
systemctl start makeros-hub
echo "makeros-hub OTA: done -> $TAG"
