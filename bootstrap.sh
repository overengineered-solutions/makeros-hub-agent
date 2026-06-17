#!/usr/bin/env bash
# One-paste hub setup. The web admin's "add a hub" panel generates the exact
# command; you flash a Pi, SSH in, and paste ONE line:
#
#   curl -fsSL https://raw.githubusercontent.com/overengineered-solutions/makeros-hub-agent/<tag>/bootstrap.sh \
#     | sudo bash -s -- --token <TOKEN> --cloud-url <URL> --ref <tag>
#
# It installs prerequisites, clones the pinned release, installs the agent,
# enrolls with the one-time token, and starts the service. Hub online in ~30s.
# (Pinned to a reviewed release tag — never mutable main; see SECURITY.md.)
set -euo pipefail

TOKEN="" CLOUD_URL="" REF="v0.3.0"
while [ $# -gt 0 ]; do
  case "$1" in
    --token)     TOKEN="${2:-}"; shift 2 ;;
    --cloud-url) CLOUD_URL="${2:-}"; shift 2 ;;
    --ref)       REF="${2:-}"; shift 2 ;;
    *) echo "bootstrap: unknown arg '$1'" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo (the pipe should end in '| sudo bash')." >&2; exit 1; }
[ -n "$TOKEN" ] && [ -n "$CLOUD_URL" ] || { echo "Need --token and --cloud-url." >&2; exit 1; }
echo "$REF" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "bad --ref '$REF' (expect vX.Y.Z)." >&2; exit 1; }

REPO="https://github.com/overengineered-solutions/makeros-hub-agent.git"

echo "==> prerequisites (git, python3-venv, python3-pip, ffmpeg, iputils-arping)"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  # ffmpeg is OPTIONAL — only the X1/H2/P2S RTSPS:322 camera path uses it (the
  # agent degrades to no-frame if it's absent).
  # iputils-arping is REQUIRED for v0.39.0+ per-VP IP allocator — `arping`
  # checks for IP collisions before claiming an address on the LAN.
  # iproute2 (/sbin/ip) ships by default on every Debian/Pi image; install -y
  # is idempotent.
  apt-get install -y -qq git python3-venv python3-pip ffmpeg iputils-arping iproute2
elif ! command -v git >/dev/null 2>&1; then
  echo "git not found and no apt-get — install git + python3-venv (+ ffmpeg for X1/H2/P2S cameras; iputils-arping for the per-VP IP allocator), then re-run." >&2
  exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "==> cloning $REF"
git clone --depth 1 --branch "$REF" "$REPO" "$TMP/src"
cd "$TMP/src"

echo "==> installing the agent"
./install.sh

echo "==> enrolling this hub"
sudo -u makeros-hub makeros-hub enroll --token "$TOKEN" --cloud-url "$CLOUD_URL"

echo "==> starting the service"
systemctl enable --now makeros-hub

cat <<DONE

✓ Done. Your hub is enrolling now — it'll show online in the admin in ~30s.
  Watch it:   journalctl -u makeros-hub -f      ('heartbeat ok 200')
  From here, updates are over-the-air — no SSH needed (toggle Auto-update in the admin).
DONE
