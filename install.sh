#!/usr/bin/env bash
# Install the makeros native-print hub agent on a Raspberry Pi (or any systemd
# Linux). Idempotent — safe to re-run for upgrades. Run with sudo from the repo.
#
#   sudo ./install.sh
#
# No pip / venv: the enrollment-slice agent is standard-library only, so it runs
# on a fresh Raspberry Pi OS (Python 3.11) with zero dependency installs.
set -euo pipefail

SERVICE_USER=makeros-hub
INSTALL_DIR=/opt/makeros-hub
CONFIG_DIR=/etc/makeros-hub
STATE_DIR=/var/lib/makeros-hub
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo ./install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found — install it first."; exit 1; }

echo "==> service user ($SERVICE_USER)"
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> code -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/makeros_hub"
cp -r "$HERE/makeros_hub" "$INSTALL_DIR/"
chmod -R a+rX "$INSTALL_DIR"

echo "==> state dir -> $STATE_DIR (credential lands here, 0700, owned by $SERVICE_USER)"
mkdir -p "$STATE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
chmod 0700 "$STATE_DIR"

echo "==> config -> $CONFIG_DIR/config.toml"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  cp "$HERE/config.toml.example" "$CONFIG_DIR/config.toml"
  echo "    (created from template — enroll writes cloud_url here automatically)"
fi
# enroll runs as $SERVICE_USER and persists the enrolled cloud_url into this
# file, so it must be writable by that user. It's non-secret config (the bearer
# credential lives 0600 in $STATE_DIR). Unconditional so re-runs fix ownership.
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/config.toml"

echo "==> wrapper -> /usr/local/bin/makeros-hub"
cat > /usr/local/bin/makeros-hub <<'WRAP'
#!/bin/sh
exec env PYTHONPATH=/opt/makeros-hub python3 -m makeros_hub "$@"
WRAP
chmod +x /usr/local/bin/makeros-hub

echo "==> systemd unit"
cp "$HERE/systemd/makeros-hub.service" /etc/systemd/system/makeros-hub.service
systemctl daemon-reload

cat <<DONE

Installed. Next:
  1) Mint a token at <cloud>/admin/3dprinting/hubs and run the command it shows
     (it includes --cloud-url, which enroll saves into config.toml):
       sudo -u $SERVICE_USER makeros-hub enroll --token <token> --cloud-url <url>
  2) Start the loop:
       sudo systemctl enable --now makeros-hub
       journalctl -u makeros-hub -f      # 'heartbeat ok 200' every ~30s
DONE
