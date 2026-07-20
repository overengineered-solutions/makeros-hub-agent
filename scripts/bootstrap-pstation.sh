#!/usr/bin/env bash
# One-time migration: re-point THIS hub from makeros → pstation. Runs as ROOT at the tail of the v0.46.0 OTA
# install (only the bootstrap release ships this file; install.sh invokes it if present, then discards the tag).
#
# RECOVERABLE BY CONSTRUCTION for every shell-visible failure (hardened over three dual-review passes 2026-07-20):
#   This box drives real printers and has NO SSH — a half-migration is unrecoverable remotely. So:
#   1. FAIL CLOSED up front: this is a live, enrolled makeros hub, so its credential + config already exist. We
#      make a VERIFIED backup of both; if we can't, we abort BEFORE enroll — nothing is touched.
#   2. An EXIT trap rewinds credential + config to the makeros originals unless the migration is verified complete
#      (DONE=1). It is defensive (set +e) so a failing step can't leave a partial rollback, and it removes a
#      freshly-written pstation credential if there was none before us.
#   3. We only commit (DONE=1) after verifying cloud_url==pstation AND a non-empty credential exist, then sync().
#   Every enroll/config/verify failure rewinds the hub fully to makeros, still working. Recovery from a failed run
#   is a STRICTLY-NEWER release (v0.46.1) with a fresh token — re-pushing v0.46.0 won't re-trigger (agent now
#   reports 0.46.0).
#   RESIDUAL (dual review): the EXIT trap can't run on abrupt power loss / SIGKILL / kernel panic. The mixed state
#   (pstation credential beside a makeros cloud_url) is only exposed in the ~1ms window between the agent's own
#   write_credential() and persist_cloud_url() (consecutive statements in `enroll`; config is writable, so it
#   flips there — this shell sed is a backstop). A crash in that sub-ms window would strand the hub → physical
#   recovery, the SAME class as any OTA on a no-SSH box. Truly closing it needs a combined atomic credential+URL
#   state in the agent runtime; deliberately not doing that here (bigger risk to the print-driving code than the
#   window it removes).
set -euo pipefail

PSTATION_URL="https://procrastinationstation.net"
PSTATION_TOKEN="Z01sFzr9fEtOWDe9y_IwyMeQSPZx1qAhuhr1zaMGCHk"
SERVICE_USER="makeros-hub"
CONFIG="/etc/makeros-hub/config.toml"
CRED="/var/lib/makeros-hub/credential"

# Guard: if the placeholder wasn't substituted at release time, do nothing (leave the hub on makeros) rather than
# firing enroll with a bogus token. Loud + non-zero so install.sh logs it; the hub is untouched either way.
case "$PSTATION_TOKEN" in
  __*) echo "bootstrap: enrollment token was not substituted at release time — refusing (hub left on makeros)" >&2; exit 1 ;;
esac

# --- fail-closed backup: verified copies of the makeros credential + config, or abort before touching anything ---
BK="$(mktemp -d)"
had_cred=0
if [ -f "$CRED" ]; then
  cp -a "$CRED" "$BK/credential" && [ -s "$BK/credential" ] \
    || { echo "bootstrap: could not make a verified backup of the credential — aborting (hub untouched)" >&2; rm -rf "$BK"; exit 1; }
  had_cred=1
fi
if [ -f "$CONFIG" ]; then
  cp -a "$CONFIG" "$BK/config.toml" && [ -s "$BK/config.toml" ] \
    || { echo "bootstrap: could not make a verified backup of config — aborting (hub untouched)" >&2; rm -rf "$BK"; exit 1; }
fi

DONE=0
restore() {
  set +e  # best-effort: run EVERY rollback step even if one fails — a partial rollback is the worst outcome
  if [ "$DONE" != "1" ]; then
    echo "bootstrap: migration did NOT complete — rewinding to makeros (hub unchanged)" >&2
    if [ "$had_cred" = "1" ]; then
      cp -a "$BK/credential" "$CRED"; chown "$SERVICE_USER:$SERVICE_USER" "$CRED"
    else
      rm -f "$CRED"  # there was no credential before us — remove any pstation credential enroll wrote
    fi
    [ -f "$BK/config.toml" ] && { cp -a "$BK/config.toml" "$CONFIG"; chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"; }
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

sync  # flush credential + config to disk so the migrated state is durable before we commit + restart
DONE=1  # commit: the migration is verified; the EXIT trap will NOT roll back
echo "bootstrap: migrated to pstation (cloud_url + per-hub credential verified) — restarting service"
# The OTA (update.sh) restarts the service after install.sh returns; this is belt-and-suspenders so the running
# agent reloads cloud_url + the new credential even if that restart path ever changes.
systemctl restart makeros-hub 2>/dev/null || true
