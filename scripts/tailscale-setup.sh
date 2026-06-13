#!/usr/bin/env bash
# Root-scoped Tailscale setup helper. The agent may invoke only this exact path
# through sudoers, and passes the auth key on stdin.
set -euo pipefail

PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

CURL_BIN="/usr/bin/curl"
CHMOD_BIN="/usr/bin/chmod"
CHOWN_BIN="/usr/bin/chown"
ID_BIN="/usr/bin/id"
MKTEMP_BIN="/usr/bin/mktemp"
RM_BIN="/bin/rm"
SH_BIN="/bin/sh"
SHRED_BIN="/usr/bin/shred"

usage() {
  echo "Usage: tailscale-setup.sh up --hostname <hostname> | tailscale-setup.sh down" >&2
  exit 2
}

is_dry_run() {
  [ "${MAKEROS_TAILSCALE_SETUP_DRY_RUN:-}" = "1" ]
}

require_root() {
  if is_dry_run; then
    return 0
  fi
  if [ "$("$ID_BIN" -u)" -ne 0 ]; then
    echo "tailscale setup: must run as root" >&2
    exit 1
  fi
}

find_tailscale() {
  command -v tailscale 2>/dev/null || true
}

cmd="${1:-}"
[ $# -gt 0 ] && shift || true

case "$cmd" in
  up)
    require_root
    hostname=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --hostname)
          shift
          [ $# -gt 0 ] || usage
          hostname="$1"
          shift
          ;;
        *)
          usage
          ;;
      esac
    done
    [ -n "$hostname" ] || usage

    auth_key=""
    IFS= read -r auth_key || true
    if [ -z "$auth_key" ]; then
      echo "tailscale setup: missing auth key on stdin" >&2
      exit 2
    fi

    keyfile=""
    cleanup_keyfile() {
      if [ -n "${keyfile:-}" ] && [ -f "$keyfile" ]; then
        if [ -x "$SHRED_BIN" ]; then
          "$SHRED_BIN" -u "$keyfile" 2>/dev/null || "$RM_BIN" -f "$keyfile"
        else
          "$RM_BIN" -f "$keyfile"
        fi
      fi
    }
    trap cleanup_keyfile EXIT

    umask 077
    keyfile="$("$MKTEMP_BIN")"
    printf '%s' "$auth_key" > "$keyfile"
    auth_key=""
    "$CHMOD_BIN" 0600 "$keyfile"
    if [ "$("$ID_BIN" -u)" -eq 0 ]; then
      "$CHOWN_BIN" root:root "$keyfile"
    fi

    tailscale_bin="$(find_tailscale)"
    if [ -z "$tailscale_bin" ] && ! is_dry_run; then
      # The agent is pure-Python, so curl may be absent on a minimal Pi OS — find
      # curl OR wget on PATH (not a hardcoded path), and CAPTURE the install output
      # so a failure reports the real reason instead of an opaque "install failed".
      dl="$(command -v curl || true)"
      if [ -n "$dl" ]; then
        install_out="$( { "$dl" -fsSL https://tailscale.com/install.sh | "$SH_BIN"; } 2>&1 )" || {
          echo "tailscale setup: install failed: $(printf '%s' "$install_out" | tr '\n' ' ' | tail -c 280)" >&2
          exit 1
        }
      else
        dl="$(command -v wget || true)"
        if [ -z "$dl" ]; then
          echo "tailscale setup: install failed: neither curl nor wget on PATH (install one)" >&2
          exit 1
        fi
        install_out="$( { "$dl" -qO- https://tailscale.com/install.sh | "$SH_BIN"; } 2>&1 )" || {
          echo "tailscale setup: install failed: $(printf '%s' "$install_out" | tr '\n' ' ' | tail -c 280)" >&2
          exit 1
        }
      fi
      tailscale_bin="$(find_tailscale)"
    fi
    if [ -z "$tailscale_bin" ]; then
      tailscale_bin="/usr/bin/tailscale"
    fi

    if is_dry_run; then
      printf '%s up --auth-key=file:%s --hostname=%s --accept-routes=false --advertise-routes= --advertise-exit-node=false --exit-node= --ssh=false\n' "$tailscale_bin" "$keyfile" "$hostname"
      exit 0
    fi

    if ! up_err="$("$tailscale_bin" up \
      --auth-key=file:"$keyfile" \
      --hostname="$hostname" \
      --accept-routes=false \
      --advertise-routes= \
      --advertise-exit-node=false \
      --exit-node= \
      --ssh=false 2>&1)"; then
      echo "tailscale setup: tailscale up failed: $(printf '%s' "$up_err" | tr '\n' ' ' | tail -c 280)" >&2
      exit 1
    fi
    echo "tailscale setup: up completed for hostname $hostname"
    ;;
  down)
    require_root
    tailscale_bin="$(find_tailscale)"
    if [ -n "$tailscale_bin" ]; then
      if ! output="$("$tailscale_bin" down 2>&1)"; then
        case "$output" in
          *"not running"*|*"not logged in"*|*"stopped"*|*"Stopped"*)
            ;;
          *)
            echo "tailscale setup: tailscale down failed" >&2
            exit 1
            ;;
        esac
      fi
    fi
    echo "tailscale setup: down completed"
    ;;
  *)
    usage
    ;;
esac
