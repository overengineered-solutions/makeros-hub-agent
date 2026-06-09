#!/usr/bin/env bash
# Install the makeros native-print hub agent on a Raspberry Pi (or any systemd
# Linux). Idempotent — safe to re-run for upgrades. Run with sudo from the repo.
#
#   sudo ./install.sh
#
# The printer-adapter slice (PR 5) adds the agent's first runtime dependency,
# paho-mqtt (Bambu LAN telemetry), installed into a venv at $INSTALL_DIR/venv.
# Enrollment + heartbeat stay stdlib-only, so the agent still runs (printers just
# report 'agent_missing_paho') even if the venv step is skipped — but the
# installer sets it up so the Bambu adapter works out of the box.
set -euo pipefail

SERVICE_USER=makeros-hub
INSTALL_DIR=/opt/makeros-hub
VENV="$INSTALL_DIR/venv"
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

echo "==> python venv + deps ($VENV)"
PYBIN="$VENV/bin/python"
if [ ! -x "$PYBIN" ]; then
  if ! python3 -m venv "$VENV" 2>/dev/null; then
    echo "    venv module missing — installing python3-venv/python3-pip via apt"
    if command -v apt-get >/dev/null; then
      apt-get update -qq && apt-get install -y -qq python3-venv python3-pip
      python3 -m venv "$VENV"
    else
      echo "    !! could not create a venv and apt-get isn't available."
      echo "    !! install python3-venv yourself, then re-run. Continuing without paho —"
      echo "    !! enrollment/heartbeat work; printers will report 'agent_missing_paho'."
    fi
  fi
fi
if [ -x "$PYBIN" ]; then
  "$PYBIN" -m pip install --quiet --upgrade pip || true
  # paho-mqtt v2 (CallbackAPIVersion.VERSION2 / MQTTv311). piwheels has a
  # pure-python wheel for the Pi — no compiler needed.
  if "$PYBIN" -m pip install --quiet "paho-mqtt>=2.0,<3"; then
    echo "    paho-mqtt installed"
  else
    echo "    !! paho-mqtt install failed (offline?) — adapters report 'agent_missing_paho'"
    echo "    !! until you run: sudo $PYBIN -m pip install 'paho-mqtt>=2.0,<3'"
  fi
  chmod -R a+rX "$VENV"
fi

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
# Prefer the venv python (has paho-mqtt); fall back to system python3, which
# still runs enroll + heartbeat (stdlib-only).
PY=/opt/makeros-hub/venv/bin/python
[ -x "$PY" ] || PY=python3
exec env PYTHONPATH=/opt/makeros-hub "$PY" -m makeros_hub "$@"
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
