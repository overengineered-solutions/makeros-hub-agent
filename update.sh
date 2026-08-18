#!/bin/sh
# /opt/makeros-hub/update.sh <vX.Y.Z>
#
# Over-the-air agent update. The agent (running as the non-root makeros-hub user)
# invokes this via a narrow sudoers rule that permits ONLY this script. It then
# re-launches the real work in an INDEPENDENT systemd transient unit, so the
# `systemctl restart makeros-hub` it performs at the end can't kill the update
# mid-flight (a service can't cleanly restart itself).
#
# Safety: only ever acts on a real release tag (vX.Y.Z) of the one hardcoded
# repo — never a branch, a commit, or an arbitrary ref.
set -eu

TAG="${1:-}"
EXPECTED_SHA="${2:-}"
echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || {
  echo "makeros-hub update: refusing non-release tag '$TAG'" >&2
  exit 2
}
# Optional content-trust pin: the commit the tag MUST resolve to (the cloud sends it in the heartbeat). Validate
# the shape here so a malformed value can't slip past to the compare below.
if [ -n "$EXPECTED_SHA" ]; then
  echo "$EXPECTED_SHA" | grep -Eq '^[0-9a-f]{40}$' || {
    echo "makeros-hub update: malformed target SHA '$EXPECTED_SHA'" >&2
    exit 2
  }
fi

# Code host: read `repo` from config.toml (bootstrap wrote it), so OTA pulls from the SAME mirror the install
# used. Falls back to the sovereign default if the key is absent (e.g. a hub installed before this key existed).
# The host does NOT need to be trusted: the content-trust SHA check below rejects any clone whose HEAD is not
# the exact commit the cloud pinned, so a read-only public mirror is sufficient and GitHub is not required.
CONFIG="${MAKEROS_HUB_CONFIG:-/etc/makeros-hub/config.toml}"
REPO_DEFAULT="https://git.overengineeredsolutions.org/makeros-hub-agent.git"
REPO="$(sed -n 's/^[[:space:]]*repo[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$CONFIG" 2>/dev/null | head -1)"
[ -n "$REPO" ] || REPO="$REPO_DEFAULT"
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
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "makeros-hub OTA: cloning $TAG"
# No --depth: a dumb-HTTP-served mirror does not support shallow clones (this repo is small).
git clone --branch "$TAG" "$REPO" "$TMP/src"
cd "$TMP/src"
# Content-trust: the tag must resolve to the EXACT commit the cloud pinned. A compromised/re-pointed tag on the
# git host fails here BEFORE anything runs as root. (dual-review 2026-07-20; roadmap: signed-tag verification.)
if [ -n "$EXPECTED_SHA" ]; then
  GOT="$(git rev-parse HEAD)"
  if [ "$GOT" != "$EXPECTED_SHA" ]; then
    echo "makeros-hub OTA: REFUSING $TAG — resolved to $GOT, expected $EXPECTED_SHA" >&2
    exit 3
  fi
  echo "makeros-hub OTA: content-trust verified ($TAG @ $EXPECTED_SHA)"
else
  echo "makeros-hub OTA: WARNING — no target SHA provided; installing $TAG UNVERIFIED" >&2
fi
./install.sh
echo "makeros-hub OTA: installed $TAG — restarting service"
systemctl restart makeros-hub
echo "makeros-hub OTA: done -> $TAG"
