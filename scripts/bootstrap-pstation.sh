#!/usr/bin/env bash
# One-time migration: re-point THIS hub from makeros → pstation. Runs as ROOT at the tail of the v0.46.0 OTA
# install (only the bootstrap release ships this file; install.sh invokes it if present, then discards the tag).
#
# WHY THIS IS RECOVERABLE (the whole safety argument):
#   `enroll --force` is ATOMIC — it overwrites the makeros credential with the pstation one AND persists cloud_url,
#   but ONLY on an HTTP 200. On any failure (bad/expired/consumed token, network) it raises SystemExit and writes
#   NOTHING; `set -e` here then aborts BEFORE we touch cloud_url. So a failed enroll leaves the hub exactly as it
#   was — still on makeros, still working — and the operator just re-mints a token and re-releases. cloud_url is
#   flipped to pstation ONLY after a successful enroll, and re-asserted as root (enroll's own persist is
#   best-effort) so the hub can NEVER end up holding a pstation credential while still pointed at makeros.
#
# The token below is a SINGLE-USE, 15-MINUTE pstation enrollment token, substituted in at release time.
set -euo pipefail

PSTATION_URL="https://procrastinationstation.net"
PSTATION_TOKEN="__PSTATION_ENROLL_TOKEN__"
SERVICE_USER="makeros-hub"
CONFIG="/etc/makeros-hub/config.toml"

# Guard: if the placeholder wasn't substituted at release time, do nothing (leave the hub on makeros) rather than
# firing enroll with a bogus token. Loud + non-zero so install.sh logs it; the hub is untouched either way.
case "$PSTATION_TOKEN" in
  __*) echo "bootstrap: enrollment token was not substituted at release time — refusing (hub left on makeros)" >&2; exit 1 ;;
esac

echo "bootstrap: enrolling this hub with pstation ($PSTATION_URL) …"
# Runs as the service user so the credential lands 0600 owned by makeros-hub (a root-written credential would be
# unreadable by the agent). --force overwrites the existing makeros credential. Atomic — see the header.
sudo -u "$SERVICE_USER" /usr/local/bin/makeros-hub enroll \
  --token "$PSTATION_TOKEN" --cloud-url "$PSTATION_URL" --force

# Reached ONLY after a successful enroll (set -e aborts otherwise). Hard-assert cloud_url as root so a best-effort
# persist failure can't strand the hub with a pstation credential + a makeros cloud_url — the one bad mixed state.
if grep -Eq '^[[:space:]]*cloud_url[[:space:]]*=' "$CONFIG" 2>/dev/null; then
  sed -i -E "s|^[[:space:]]*cloud_url[[:space:]]*=.*|cloud_url = \"$PSTATION_URL\"|" "$CONFIG"
else
  echo "cloud_url = \"$PSTATION_URL\"" >> "$CONFIG"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"

echo "bootstrap: migrated to pstation (cloud_url + per-hub credential set) — restarting service"
# The OTA (update.sh) restarts the service after install.sh returns; this is belt-and-suspenders so the running
# agent reloads cloud_url + the new credential even if that restart path ever changes.
systemctl restart makeros-hub 2>/dev/null || true
