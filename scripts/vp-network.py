#!/usr/bin/python3 -I
"""Root-owned VP address helper. No service-owned config or arbitrary command flags."""
import ipaddress
import json
import os
import re
import subprocess
import sys

ROUTED_SPACE = ipaddress.ip_network('198.18.0.0/15')


def command(args, routes, addresses):
    if len(args) != 3 or args[0] not in ('add', 'del', 'probe'):
        raise ValueError('expected add|del|probe IPv4 interface')
    action, raw_ip, interface = args
    ip = ipaddress.IPv4Address(raw_ip)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('invalid interface')
    if interface == 'lo':
        if ip not in ROUTED_SPACE or action == 'probe':
            raise ValueError('loopback VP addresses must be in 198.18.0.0/15')
    else:
        defaults = [r for r in routes if isinstance(r, dict) and r.get('dev') == interface and r.get('dst') == 'default']
        if not defaults or any(str(ip) == r.get('gateway') for r in defaults):
            raise ValueError('not the primary LAN interface, or address is its gateway')
        hosts = [a for row in addresses if isinstance(row, dict) and row.get('ifname') == interface
                 for a in row.get('addr_info', []) if isinstance(a, dict) and a.get('family') == 'inet']
        # /32s are our aliases, never authority to extend the allowed network.
        networks = [ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}") for a in hosts
                    if isinstance(a.get('prefixlen'), int) and 0 < a['prefixlen'] < 32]
        if not any(ip in n.network and ip not in (n.ip, n.network.network_address, n.network.broadcast_address)
                   for n in networks):
            raise ValueError('VP address must be a host in the existing LAN subnet')
    if action == 'probe':
        return ['/usr/sbin/arping', '-c', '1', '-w', '1', '-I', interface, str(ip)]
    return ['/sbin/ip', 'addr', action, f'{ip}/32', 'dev', interface]


def main(args):
    if os.geteuid() != 0:
        raise ValueError('this helper must run as root')
    routes = json.loads(subprocess.check_output(['/sbin/ip', '-j', '-4', 'route', 'show', 'default'], timeout=5))
    addresses = json.loads(subprocess.check_output(['/sbin/ip', '-j', '-4', 'addr', 'show'], timeout=5))
    if not isinstance(routes, list) or not isinstance(addresses, list):
        raise ValueError('unexpected kernel network response')
    return subprocess.run(command(args, routes, addresses), timeout=5, check=False).returncode


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'VP network operation refused: {exc}', file=sys.stderr)
        sys.exit(2)
