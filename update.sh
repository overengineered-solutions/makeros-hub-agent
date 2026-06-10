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
echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || {
  echo "makeros-hub update: refusing non-release tag '$TAG'" >&2
  exit 2
}

REPO="https://github.com/overengineered-solutions/makeros-hub-agent.git"
SELF="/opt/makeros-hub/update.sh"

# Phase 1 — invoked by the agent, still inside the service's cgroup. Detach into
# our own transient unit and return immediately.
if [ "${MAKEROS_OTA_DETACHED:-}" != "1" ]; then
  if command -v systemd-run >/dev/null 2>&1; then
    exec systemd-run --collect --quiet --unit="makeros-hub-ota" \
      --setenv=MAKEROS_OTA_DETACHED=1 "$SELF" "$TAG"
  fi
  # Fallback when systemd-run is unavailable: detach with setsid.
  MAKEROS_OTA_DETACHED=1 setsid "$SELF" "$TAG" </dev/null >/dev/null 2>&1 &
  exit 0
fi

# Phase 2 — running in the transient unit (own cgroup). Do the real work.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "makeros-hub OTA: cloning $TAG"
git clone --depth 1 --branch "$TAG" "$REPO" "$TMP/src"
cd "$TMP/src"
./install.sh
echo "makeros-hub OTA: installed $TAG — restarting service"
systemctl restart makeros-hub
echo "makeros-hub OTA: done -> $TAG"
