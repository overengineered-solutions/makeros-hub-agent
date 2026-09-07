#!/usr/bin/env bash
# Install the makeros native-print hub agent on a Raspberry Pi (or any systemd
# Linux). Idempotent — safe to re-run for upgrades. Run with sudo from the repo.
#
#   sudo ./install.sh
#
# The printer-adapter slice adds runtime dependencies in the venv:
# paho-mqtt (Bambu LAN telemetry) and cryptography (virtual-printer TLS certs).
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
      echo "    !! install python3-venv yourself, then re-run. Existing agent code is unchanged."
      exit 1
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
  if "$PYBIN" -m pip install --quiet "cryptography>=42,<46"; then
    echo "    cryptography installed"
  else
    echo "    !! cryptography install failed (offline?) — virtual printer reports dependency missing"
    echo "    !! until you run: sudo $PYBIN -m pip install 'cryptography>=42,<46'"
  fi
  # V5 AI failure-watch — optional ONNX inference + image preprocess. The agent
  # gracefully falls back to the no-op stub detector when these aren't
  # installed (older hubs predate this), so an offline / failing install
  # never breaks heartbeats. onnxruntime pulls numpy as a transitive dep.
  # ~16 MB DL onnxruntime + ~6 MB Pillow = ~22 MB total; arm64 wheels for Pi.
  # A touch-file marker under the StateDirectory records the OUTCOME so the
  # agent can read it on boot + surface to the cloud diag (no PR-time wheel
  # resolution surprise stays silent).
  mkdir -p "$STATE_DIR"
  if "$PYBIN" -m pip install --quiet "onnxruntime>=1.17,<2" "Pillow>=10,<13"; then
    echo "    onnxruntime + Pillow installed (AI failure-watch ready)"
    touch "$STATE_DIR/.install.onnxruntime.ok"
    rm -f "$STATE_DIR/.install.onnxruntime.failed" 2>/dev/null || true
  else
    echo "    !! onnxruntime/Pillow install failed (offline?) — AI failure-watch falls back to stub"
    echo "    !! until you run: sudo $PYBIN -m pip install 'onnxruntime>=1.17,<2' 'Pillow>=10,<13'"
    touch "$STATE_DIR/.install.onnxruntime.failed"
    rm -f "$STATE_DIR/.install.onnxruntime.ok" 2>/dev/null || true
  fi
  chmod -R a+rX "$VENV"
fi

# Do not replace working code when the required runtime cannot start. Optional
# camera inference may degrade, but member printer intake must be available.
"$PYBIN" -c 'import paho.mqtt.client; import cryptography.x509' || {
  echo "Required printer dependencies are unavailable; existing agent code is unchanged." >&2
  exit 1
}

echo "==> state dir -> $STATE_DIR (credential lands here, 0700, owned by $SERVICE_USER)"
mkdir -p "$STATE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
chmod 0700 "$STATE_DIR"

echo "==> config -> $CONFIG_DIR/config.toml"
[ ! -L "$CONFIG_DIR" ] && [ ! -L "$CONFIG_DIR/config.toml" ] || { echo "Refusing linked config path" >&2; exit 1; }
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  cp "$HERE/config.toml.example" "$CONFIG_DIR/config.toml"
  echo "    (created from template — enroll writes cloud_url here automatically)"
fi
# enroll runs as $SERVICE_USER and persists the enrolled cloud_url into this
# file, so it must be writable by that user. It's non-secret config (the bearer
# credential lives 0600 in $STATE_DIR). Unconditional so re-runs fix ownership.
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$CONFIG_DIR/config.toml"
chmod 0700 "$CONFIG_DIR"

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

echo "==> sudoers (the service user may run ONLY scoped hub root helpers)"
SUDOERS=/etc/sudoers.d/makeros-hub
# v0.39.0 adds the per-model VP IP allocator (Option A). It needs to:
#   - add/remove individual /32 IPs on the primary interface (one per VP)
#   - arping target addresses to detect collisions before claiming
# The root-owned helper validates /32 aliases against the primary LAN subnet or
# reserved loopback range. No sudoers argument wildcards or arbitrary ip flags.
{
  echo "$SERVICE_USER ALL=(root) NOPASSWD: $INSTALL_DIR/update.sh"
  echo "$SERVICE_USER ALL=(root) NOPASSWD: $INSTALL_DIR/tailscale-setup.sh"
  echo "$SERVICE_USER ALL=(root) NOPASSWD: $INSTALL_DIR/vp-network.py"
} > "$SUDOERS.tmp"
chmod 0440 "$SUDOERS.tmp"
if visudo -cf "$SUDOERS.tmp" >/dev/null 2>&1; then
  mv -f "$SUDOERS.tmp" "$SUDOERS"
  echo "    OTA self-update + Tailscale setup + per-VP IP allocation enabled (web-controlled)"
else
  rm -f "$SUDOERS.tmp"
  echo "    !! sudoers validation failed — previous policy retained; install refused" >&2
  exit 1
fi

echo "==> systemd unit"
cp "$HERE/systemd/makeros-hub.service" /etc/systemd/system/makeros-hub.service
systemctl daemon-reload

# --- staged code install ---
echo "==> code -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
STAGED="$(mktemp -d "$INSTALL_DIR/.code-XXXXXX")"
BACKUP="$INSTALL_DIR/previous"
RESTART=0; CHANGED=0; COMMITTED=0
FILES=(makeros_hub update.sh tailscale-setup.sh vp-network.py)
finish_install() {
  rc=$?
  trap - EXIT
  if [ "$CHANGED" = 1 ] && [ "$COMMITTED" = 0 ]; then
    echo "Install did not complete; restoring the previous code and root helpers." >&2
    for item in "${FILES[@]}"; do
      rm -rf "$INSTALL_DIR/$item"
      [ ! -e "$BACKUP/$item" ] || cp -a "$BACKUP/$item" "$INSTALL_DIR/$item"
    done
  fi
  rm -rf "$STAGED"
  if [ "$RESTART" = 1 ] && [ "$COMMITTED" = 0 ]; then
    systemctl start makeros-hub || { echo "Agent did not restart; inspect journalctl -u makeros-hub" >&2; rc=1; }
  fi
  exit "$rc"
}
trap finish_install EXIT
cp -r "$HERE/makeros_hub" "$STAGED/makeros_hub"
cp "$HERE/update.sh" "$STAGED/update.sh"
cp "$HERE/scripts/tailscale-setup.sh" "$STAGED/tailscale-setup.sh"
cp "$HERE/scripts/vp-network.py" "$STAGED/vp-network.py"
chown -R root:root "$STAGED"
chmod -R a+rX,go-w "$STAGED"
chmod 0755 "$STAGED/update.sh" "$STAGED/tailscale-setup.sh" "$STAGED/vp-network.py"
rm -rf "$BACKUP"; mkdir "$BACKUP"
for item in "${FILES[@]}"; do
  [ ! -e "$INSTALL_DIR/$item" ] || cp -a "$INSTALL_DIR/$item" "$BACKUP/$item"
done
systemctl is-active --quiet makeros-hub && RESTART=1
[ "$RESTART" = 0 ] || systemctl stop makeros-hub
CHANGED=1
for item in "${FILES[@]}"; do
  rm -rf "$INSTALL_DIR/$item"
  mv "$STAGED/$item" "$INSTALL_DIR/$item"
done
if [ "$RESTART" = 1 ]; then systemctl start makeros-hub; fi
COMMITTED=1
# --- end staged code install ---

# ── one-time pstation migration ── only the bootstrap release ships scripts/bootstrap-pstation.sh, so this is a
# no-op for every normal install/upgrade. Failure is swallowed on purpose: a failed enroll leaves the hub on
# makeros (recoverable), and we still want THIS install to succeed so the agent lands on v0.46.0 and reports it —
# makeros then auto-clears the OTA target (hasReachedVersion) instead of retrying a dead token every cooldown.
if [ -f "$HERE/scripts/bootstrap-pstation.sh" ]; then
  echo "==> pstation bootstrap (one-time migration off makeros)"
  bash "$HERE/scripts/bootstrap-pstation.sh" \
    || echo "    !! pstation bootstrap did not complete — hub stays on makeros. Retry = a STRICTLY-NEWER release with a fresh token; re-pushing the same tag won't re-trigger (the agent now reports this version). See last-bootstrap.log / cloud diag lastErrors.update for why."
fi

cat <<DONE

Installed. Next:
  1) Mint a token at <cloud>/admin/hub and run the command it shows
     (it includes --cloud-url, which enroll saves into config.toml):
       sudo -u $SERVICE_USER makeros-hub enroll --token <token> --cloud-url <url>
  2) Start the loop:
       sudo systemctl enable --now makeros-hub
       journalctl -u makeros-hub -f      # 'heartbeat ok 200' every ~30s
DONE
