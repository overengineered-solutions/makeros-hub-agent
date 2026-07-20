#!/usr/bin/env bash
# One-time migration: re-point THIS hub from makeros → pstation. Runs as ROOT at the tail of the v0.46.0 OTA
# install (only the bootstrap release ships this file; install.sh invokes it if present, then discards the tag).
#
# ALL-OR-NOTHING (this is the whole safety argument, hardened per dual review 2026-07-20):
#   We back up the current makeros credential + config up front and install an EXIT trap that restores BOTH unless
#   the migration is verified complete. So every failure mode — bad/expired/consumed token, pstation outage, a
#   config write that fails AFTER the credential was overwritten — rewinds the hub to exactly its pre-run state:
#   makeros credential + makeros cloud_url, still working. The one dangerous mixed state (pstation credential left
#   next to a makeros cloud_url) can no longer persist. A failed run leaves the hub fully on makeros; recovery is a
#   strictly-newer release (v0.46.1) with a fresh token — NOT re-pushing v0.46.0 (the agent now reports 0.46.0, so
#   makeros won't re-target the same version).
set -euo pipefail

PSTATION_URL="https://procrastinationstation.net"
PSTATION_TOKEN="__PSTATION_ENROLL_TOKEN__"
SERVICE_USER="makeros-hub"
CONFIG="/etc/makeros-hub/config.toml"
CRED="/var/lib/makeros-hub/credential"

# Guard: if the placeholder wasn't substituted at release time, do nothing (leave the hub on makeros) rather than
# firing enroll with a bogus token. Loud + non-zero so install.sh logs it; the hub is untouched either way.
case "$PSTATION_TOKEN" in
  __*) echo "bootstrap: enrollment token was not substituted at release time — refusing (hub left on makeros)" >&2; exit 1 ;;
esac

# --- backup + restore-on-failure trap: makes the migration atomic ---
BK="$(mktemp -d)"
[ -f "$CRED" ]   && cp -a "$CRED"   "$BK/credential"  || true
[ -f "$CONFIG" ] && cp -a "$CONFIG" "$BK/config.toml" || true
DONE=0
restore() {
  if [ "$DONE" != "1" ]; then
    echo "bootstrap: migration did NOT complete — restoring makeros credential + config (hub stays on makeros)" >&2
    if [ -f "$BK/credential" ]; then cp -a "$BK/credential" "$CRED" && chown "$SERVICE_USER:$SERVICE_USER" "$CRED"; fi
    if [ -f "$BK/config.toml" ]; then cp -a "$BK/config.toml" "$CONFIG" && chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"; fi
  fi
  rm -rf "$BK"
}
trap restore EXIT

echo "bootstrap: enrolling this hub with pstation ($PSTATION_URL) …"
# Runs as the service user so the credential lands 0600 owned by makeros-hub (a root-written credential would be
# unreadable by the agent). --force overwrites the existing makeros credential; enroll writes it ONLY on HTTP 200.
sudo -u "$SERVICE_USER" /usr/local/bin/makeros-hub enroll \
  --token "$PSTATION_TOKEN" --cloud-url "$PSTATION_URL" --force

# Flip cloud_url as root (enroll's own persist is best-effort). Reached only after a successful enroll (set -e).
if grep -Eq '^[[:space:]]*cloud_url[[:space:]]*=' "$CONFIG" 2>/dev/null; then
  sed -i -E "s|^[[:space:]]*cloud_url[[:space:]]*=.*|cloud_url = \"$PSTATION_URL\"|" "$CONFIG"
else
  echo "cloud_url = \"$PSTATION_URL\"" >> "$CONFIG"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"

# VERIFY the committed end state before we tell the trap to stand down: config must point at pstation AND a
# credential file must exist + be non-empty. If either is wrong, exit non-zero → the trap rewinds to makeros.
grep -Eq "^[[:space:]]*cloud_url[[:space:]]*=[[:space:]]*\"${PSTATION_URL}\"[[:space:]]*$" "$CONFIG" \
  || { echo "bootstrap: cloud_url is not pstation after write — aborting (will restore)" >&2; exit 1; }
[ -s "$CRED" ] || { echo "bootstrap: no credential present after enroll — aborting (will restore)" >&2; exit 1; }

DONE=1  # commit: the migration is verified; the EXIT trap will NOT restore
echo "bootstrap: migrated to pstation (cloud_url + per-hub credential verified) — restarting service"
# The OTA (update.sh) restarts the service after install.sh returns; this is belt-and-suspenders so the running
# agent reloads cloud_url + the new credential even if that restart path ever changes.
systemctl restart makeros-hub 2>/dev/null || true
