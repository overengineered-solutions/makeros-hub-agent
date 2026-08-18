#!/usr/bin/env bash
# One-paste hub setup. The web admin's "add a hub" panel generates the exact
# command; you flash a Pi, SSH in, and paste ONE line:
#
#   curl -fsSL https://git.overengineeredsolutions.org/bootstrap.sh \
#     | sudo bash -s -- --token <TOKEN> --cloud-url <URL> --ref <tag>
#
# It installs prerequisites, clones the pinned release, installs the agent,
# enrolls with the one-time token, and starts the service. Hub online in ~30s.
# (Pinned to a reviewed release tag — never mutable main; see SECURITY.md.)
set -euo pipefail

TOKEN="" CLOUD_URL="" REF="v0.3.0"
# The code host. Default is the sovereign mirror served read-only over HTTPS by the pool; the admin "add a
# hub" panel can pass --repo to override. GitHub is no longer required: integrity does not depend on the host
# — the cloud pins the exact commit SHA in each heartbeat and update.sh/this script refuse any clone whose
# HEAD does not match it (see SECURITY.md, content-trust). So a read-only public mirror is sufficient.
REPO="https://git.overengineeredsolutions.org/makeros-hub-agent.git"
while [ $# -gt 0 ]; do
  case "$1" in
    --token)     TOKEN="${2:-}"; shift 2 ;;
    --cloud-url) CLOUD_URL="${2:-}"; shift 2 ;;
    --ref)       REF="${2:-}"; shift 2 ;;
    --repo)      REPO="${2:-}"; shift 2 ;;
    *) echo "bootstrap: unknown arg '$1'" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo (the pipe should end in '| sudo bash')." >&2; exit 1; }
[ -n "$TOKEN" ] && [ -n "$CLOUD_URL" ] || { echo "Need --token and --cloud-url." >&2; exit 1; }
echo "$REF" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "bad --ref '$REF' (expect vX.Y.Z)." >&2; exit 1; }


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
# No --depth: a dumb-HTTP-served mirror does not support shallow clones, and this repo is small.
git clone --branch "$REF" "$REPO" "$TMP/src"
cd "$TMP/src"

echo "==> installing the agent"
./install.sh

echo "==> recording the code source for over-the-air updates"
# update.sh reads `repo` from here so OTA uses the same mirror this bootstrap did (not a hardcoded host).
CFG=/etc/makeros-hub/config.toml
if [ -f "$CFG" ] && ! grep -Eq '^[[:space:]]*repo[[:space:]]*=' "$CFG"; then
  printf '\n# Code host for OTA self-update (written by bootstrap; see update.sh). Non-secret.\nrepo = "%s"\n' "$REPO" >> "$CFG"
fi

echo "==> enrolling this hub"
sudo -u makeros-hub makeros-hub enroll --token "$TOKEN" --cloud-url "$CLOUD_URL"

echo "==> starting the service"
systemctl enable --now makeros-hub

cat <<DONE

✓ Done. Your hub is enrolling now — it'll show online in the admin in ~30s.
  Watch it:   journalctl -u makeros-hub -f      ('heartbeat ok 200')
  From here, updates are over-the-air — no SSH needed (toggle Auto-update in the admin).
DONE
