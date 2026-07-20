#!/usr/bin/env bash
# One-time migration: re-point THIS hub from makeros → pstation. Runs as ROOT at the tail of the v0.46.1 OTA
# install (only the bootstrap release ships this file; install.sh invokes it if present, then discards the tag).
#
# v0.46.1 vs v0.46.0 (v0.46.0's enroll failed remotely with no visible cause — no SSH to the box):
#   * PRIVILEGE DROP: use `runuser -u` (the systemd-correct root→user drop, no pam/tty) instead of `sudo -u`,
#     which is the prime suspect for the v0.46.0 failure inside the transient OTA unit. Falls back to `sudo -n`.
#   * SELF-DIAGNOSING: everything is logged to $BOOTLOG (incl. a reachability probe to pstation + the exact enroll
#     error). The agent reads that file on its next start and surfaces its tail into the cloud diag
#     (lastErrors.update), so a failed remote migration is finally visible without SSH.
#
# RECOVERABLE BY CONSTRUCTION for every shell-visible failure (unchanged from the 3 dual-review passes):
#   fail-closed verified backup of credential+config → EXIT trap rewinds both unless verified-complete (DONE=1) →
#   commit only after cloud_url==pstation AND a non-empty credential, then sync(). A failed run leaves the hub
#   fully on makeros. Residual: the EXIT trap can't run on abrupt power loss (~1ms window; accepted). Retry after a
#   failure is a STRICTLY-NEWER release (the agent reports this version, so makeros won't re-target the same tag).
set -euo pipefail

PSTATION_URL="https://procrastinationstation.net"
PSTATION_TOKEN="__PSTATION_ENROLL_TOKEN__"
SERVICE_USER="makeros-hub"
CONFIG="/etc/makeros-hub/config.toml"
CRED="/var/lib/makeros-hub/credential"
BOOTLOG="/var/lib/makeros-hub/last-bootstrap.log"

: > "$BOOTLOG" 2>/dev/null || BOOTLOG=/tmp/makeros-last-bootstrap.log
blog(){ printf '%s %s\n' "$(date -u +%FT%TZ 2>/dev/null || echo now)" "$*" >> "$BOOTLOG" 2>/dev/null || true; }

# Drop to the service user WITHOUT a tty. runuser is the systemd-blessed root→user path (no pam auth, no tty);
# sudo -u inside a `systemd-run` transient unit is the suspected v0.46.0 failure. Fall back to sudo -n if needed.
as_service_user(){
  if command -v runuser >/dev/null 2>&1; then runuser -u "$SERVICE_USER" -- "$@"
  else sudo -n -u "$SERVICE_USER" "$@"; fi
}

blog "v0.46.1 bootstrap start: euid=$(id -u 2>/dev/null) user=$(id -un 2>/dev/null) runuser=$(command -v runuser || echo none) sudo=$(command -v sudo || echo none)"

case "$PSTATION_TOKEN" in
  __*) blog "REFUSE: enrollment token not substituted at release time"; echo "bootstrap: token not substituted — hub left on makeros" >&2; exit 1 ;;
esac

# --- fail-closed verified backup: makeros credential + config, or abort before touching anything ---
BK="$(mktemp -d)"
had_cred=0
if [ -f "$CRED" ]; then
  cp -a "$CRED" "$BK/credential" && [ -s "$BK/credential" ] \
    || { blog "ABORT: credential backup failed"; echo "bootstrap: credential backup failed — hub untouched" >&2; rm -rf "$BK"; exit 1; }
  had_cred=1
fi
if [ -f "$CONFIG" ]; then
  cp -a "$CONFIG" "$BK/config.toml" && [ -s "$BK/config.toml" ] \
    || { blog "ABORT: config backup failed"; echo "bootstrap: config backup failed — hub untouched" >&2; rm -rf "$BK"; exit 1; }
fi

DONE=0
restore(){
  set +e  # best-effort: run EVERY rollback step even if one fails
  if [ "$DONE" != "1" ]; then
    blog "REWIND to makeros (migration did not complete)"
    if [ "$had_cred" = "1" ]; then cp -a "$BK/credential" "$CRED"; chown "$SERVICE_USER:$SERVICE_USER" "$CRED"; else rm -f "$CRED"; fi
    [ -f "$BK/config.toml" ] && { cp -a "$BK/config.toml" "$CONFIG"; chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"; }
  fi
  rm -rf "$BK"
  # make the diagnostic log readable by the agent (makeros-hub) so it can surface it to the cloud
  chmod 0644 "$BOOTLOG" 2>/dev/null; chown "$SERVICE_USER:$SERVICE_USER" "$BOOTLOG" 2>/dev/null
}
trap restore EXIT

# Reachability probe (bogus token → expect http=400/token_invalid if the Pi can reach pstation). Distinguishes a
# network/TLS problem from an enroll/permission problem when I read this back from the cloud diag.
if command -v curl >/dev/null 2>&1; then
  blog "reachability: $(curl -sS -m 15 -o /dev/null -w 'http=%{http_code} tls_verify=%{ssl_verify_result} t=%{time_total}s' -X POST "$PSTATION_URL/api/print/hub/enroll" -H 'content-type: application/json' -d '{"token":"reachability-probe","agent":{"version":"probe","os":"probe","hostname":"probe"}}' 2>&1 || echo "curl-failed rc=$?")"
else
  blog "reachability: (curl not present)"
fi

blog "enroll: dropping to $SERVICE_USER via $(command -v runuser >/dev/null 2>&1 && echo runuser || echo sudo) …"
if enroll_err=$(as_service_user /usr/local/bin/makeros-hub enroll --token "$PSTATION_TOKEN" --cloud-url "$PSTATION_URL" --force 2>&1); then
  blog "enroll: OK"
else
  rc=$?
  blog "enroll: FAILED rc=$rc :: $enroll_err"
  echo "bootstrap: enroll failed rc=$rc — hub stays on makeros" >&2
  exit "$rc"
fi

# Flip cloud_url as root (enroll's own persist is best-effort). Reached only after a successful enroll.
if grep -Eq '^[[:space:]]*cloud_url[[:space:]]*=' "$CONFIG" 2>/dev/null; then
  sed -i -E "s|^[[:space:]]*cloud_url[[:space:]]*=.*|cloud_url = \"$PSTATION_URL\"|" "$CONFIG"
else
  echo "cloud_url = \"$PSTATION_URL\"" >> "$CONFIG"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"

# Verify committed end state before standing the trap down.
grep -Eq "^[[:space:]]*cloud_url[[:space:]]*=[[:space:]]*\"${PSTATION_URL}\"[[:space:]]*$" "$CONFIG" \
  || { blog "ABORT: cloud_url not pstation after write"; echo "bootstrap: cloud_url not pstation — will restore" >&2; exit 1; }
[ -s "$CRED" ] || { blog "ABORT: no credential after enroll"; echo "bootstrap: no credential — will restore" >&2; exit 1; }

sync
DONE=1
blog "MIGRATED to pstation (verified)"
rm -f "$BOOTLOG" 2>/dev/null || true  # success: the Pi is on pstation now; no diagnostic needed
echo "bootstrap: migrated to pstation (cloud_url + credential verified) — restarting service"
systemctl restart makeros-hub 2>/dev/null || true
