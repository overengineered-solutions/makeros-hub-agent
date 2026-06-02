"""CLI entry. `python -m makeros_hub <cmd>` (or `makeros-hub <cmd>` via the
wrapper install.sh drops in /usr/local/bin).

  makeros-hub enroll --token <one-time> --cloud-url <https://host> [--force]
  makeros-hub run        # the heartbeat loop (what systemd runs)
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .agent import run as run_agent
from .config import load_config
from .enroll import enroll


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="makeros-hub", description="makeros native-print hub agent")
    parser.add_argument("--version", action="version", version=f"makeros-hub {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_enroll = sub.add_parser("enroll", help="exchange a one-time token for a per-hub credential")
    p_enroll.add_argument("--token", required=True, help="the one-time enrollment token from /admin/3dprinting/hubs")
    p_enroll.add_argument("--cloud-url", required=True, help="the makeros cloud base URL, e.g. https://<host>")
    p_enroll.add_argument("--force", action="store_true", help="overwrite an existing credential")

    p_run = sub.add_parser("run", help="run the heartbeat loop (what the service runs)")
    p_run.add_argument("--cloud-url", default=None, help="override the configured cloud URL")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "enroll":
        cfg = load_config(cloud_url_override=args.cloud_url)
        hub_id = enroll(cfg, args.token, force=args.force)
        print(f"Enrolled. hubId={hub_id}. Credential written. Start the loop with: "
              f"sudo systemctl enable --now makeros-hub")
        return 0

    if args.cmd == "run":
        cfg = load_config(cloud_url_override=args.cloud_url)
        return run_agent(cfg)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
