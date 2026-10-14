#!/usr/bin/env bash
# Install or update the laser gate. The OPERATOR runs this, as root, by hand: the gate has no OTA, and the hub's
# install.sh and update.sh never call it. Read docs/laser-gate.md first (prerequisites come before this).
#
#   sudo ./laser_gate/install.sh --iface <laser NIC or VLAN> [--hub-local]
#
#   --iface      the interface that holds bind_ip and is cabled only to the laser (e.g. eth1, enx..., eth0.30)
#   --hub-local  the hub runs on this same box: also install the gate token for it as
#                /var/lib/makeros-hub/laser-gate-token (makeros-hub, 0600)
#
# Idempotent: re-run it after editing /etc/laser-gate/config.toml or pulling a new version.
set -euo pipefail

SERVICE_USER=laser-gate
INSTALL_DIR=/opt/laser-gate
ETC=/etc/laser-gate
HUB_STATE=/var/lib/makeros-hub
HERE="$(cd "$(dirname "$0")" && pwd)"
IFACE=""
HUB_LOCAL=0

die() { echo "laser-gate install: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --iface) [ $# -ge 2 ] || die "--iface needs a value"; IFACE="$2"; shift 2 ;;
    --hub-local) HUB_LOCAL=1; shift ;;
    *) die "unknown argument: $1 (usage: --iface <laser NIC/VLAN> [--hub-local])" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run it as root: sudo $0 --iface <laser NIC/VLAN> [--hub-local]"
[[ "$IFACE" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || die "--iface <laser NIC/VLAN> is required (letters, digits, _ . -)"
[ -d "/sys/class/net/$IFACE" ] || die "no network interface named $IFACE"
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 >= 3.11 is required (tomllib)"
command -v nft >/dev/null || die "nftables (nft) is not installed: apt-get install nftables"

echo "==> service user ($SERVICE_USER)"
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> code ($INSTALL_DIR/laser_gate, root-owned, read-only to the service)"
install -d -m 0755 -o root -g root "$INSTALL_DIR" "$INSTALL_DIR/laser_gate"
rm -f "$INSTALL_DIR"/laser_gate/*.py
install -m 0644 -o root -g root "$HERE"/*.py "$INSTALL_DIR/laser_gate/"

echo "==> config ($ETC)"
install -d -m 0750 -o root -g "$SERVICE_USER" "$ETC"
if [ ! -f "$ETC/config.toml" ]; then
  install -m 0640 -o root -g "$SERVICE_USER" "$HERE/config.example.toml" "$ETC/config.toml"
  echo
  echo "Wrote $ETC/config.toml from the example. Edit it (controller_ip, bind_ip, listen, allowed_peer), then re-run:"
  echo "  sudo $0 --iface $IFACE$([ "$HUB_LOCAL" -eq 1 ] && echo ' --hub-local')"
  exit 1
fi
chown root:"$SERVICE_USER" "$ETC/config.toml"
chmod 0640 "$ETC/config.toml"

# Read the values the firewall and unit need through the gate's own validator, so a bad config stops the install.
# Each is validated IPv4 or a port number before it reaches sed.
read -r CONTROLLER_IP BIND_IP ALLOWED_PEER LISTEN_PORT TOKEN_FILE < <(cd "$INSTALL_DIR" && /usr/bin/python3 -E -s -B -c '
import sys
from laser_gate.gate import ConfigError, load_config
try:
    c, _ = load_config(sys.argv[1], need_token=False)
except ConfigError as e:
    sys.exit(f"config: {e}")
print(c.controller_ip, c.bind_ip, c.allowed_peer, c.listen_addr[1], c.token_file)
' "$ETC/config.toml") || die "fix $ETC/config.toml and re-run"
[ -n "${TOKEN_FILE:-}" ] || die "could not read $ETC/config.toml"

echo "==> laser-side interface checks ($IFACE)"
ip -4 -o addr show dev "$IFACE" | grep -qF " inet $BIND_IP/" \
  || die "bind_ip $BIND_IP is not an address on $IFACE. Give $IFACE a static $BIND_IP/24 with no gateway first."
# The hub's VP-network helper only touches an interface that carries a default route; a laser NIC with none is out
# of its reach. A default route here would also mean the laser segment is routed. Refuse both.
[ -z "$(ip -4 route show default dev "$IFACE")" ] || die "$IFACE has a default route; the laser segment must have no gateway"

echo "==> token ($TOKEN_FILE)"
if [ ! -s "$TOKEN_FILE" ]; then
  ( umask 077; /usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE" )
  echo "    generated (not shown)"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$TOKEN_FILE"
chmod 0600 "$TOKEN_FILE"

if [ "$HUB_LOCAL" -eq 1 ] && [ -d "$HUB_STATE" ] && id makeros-hub >/dev/null 2>&1; then
  echo "==> hub copy of the token ($HUB_STATE/laser-gate-token)"
  install -m 0600 -o makeros-hub -g makeros-hub "$TOKEN_FILE" "$HUB_STATE/laser-gate-token"
else
  echo "==> hub copy of the token: not installed here. On the hub's box, as root, copy the same value (never paste it"
  echo "    into chat or a ticket):"
  echo "      install -m 0600 -o makeros-hub -g makeros-hub <copy of $TOKEN_FILE> $HUB_STATE/laser-gate-token"
fi

echo "==> firewall and unit"
sed -e "s/@IFACE@/$IFACE/g" -e "s/@CONTROLLER_IP@/$CONTROLLER_IP/g" -e "s/@ALLOWED_PEER@/$ALLOWED_PEER/g" \
    -e "s/@LISTEN_PORT@/$LISTEN_PORT/g" "$HERE/laser-gate.nft" > "$ETC/laser-gate.nft"
nft -c -f "$ETC/laser-gate.nft" || die "the rendered firewall does not parse: $ETC/laser-gate.nft"
# sysctl writes a VLAN name like eth0.30 as eth0/30 (a dot would split the key)
printf 'LASER_IFACE=%s\nLASER_IFACE_SYSCTL=%s\n' "$IFACE" "${IFACE//./\/}" > "$ETC/unit.env"
chmod 0644 "$ETC/laser-gate.nft" "$ETC/unit.env"
sed -e "s/@CONTROLLER_IP@/$CONTROLLER_IP/g" -e "s/@ALLOWED_PEER@/$ALLOWED_PEER/g" \
    "$HERE/laser-gate.service" > /etc/systemd/system/laser-gate.service
chmod 0644 /etc/systemd/system/laser-gate.service
systemctl daemon-reload
systemctl enable laser-gate.service >/dev/null
systemctl restart laser-gate.service

sleep 2
systemctl is-active --quiet laser-gate.service || { journalctl -u laser-gate -n 20 --no-pager; die "the gate did not start"; }
echo
echo "laser-gate $(cd "$INSTALL_DIR" && /usr/bin/python3 -E -s -B -c 'import laser_gate; print(laser_gate.__version__)') is running."
echo "Check it: journalctl -u laser-gate -n 5    and    sudo nft list table inet laser_gate"
