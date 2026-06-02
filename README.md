# makeros-hub-agent

The on-LAN bridge between a makerspace's 3D printers and the **makeros** cloud control
plane — the agent half of the SimplyPrint replacement. Runs on a Raspberry Pi (or any
small always-on Linux box) on the shop network.

> **Status: enrollment pilot (PR 2).** Standard-library Python only — no `pip install`,
> no venv, no ARM-wheel concerns. It enrolls and heartbeats; **printer adapters
> (Bambu / Klipper) arrive in a later slice**, added behind the transport in `makeros_hub/http.py`.

## What it does (today)

1. **Enroll once:** exchange a one-time token (minted by an admin at
   `<cloud>/admin/3dprinting/hubs`) for a durable **per-hub bearer credential**, stored
   `0600` at `/var/lib/makeros-hub/credential`. The token and credential are never logged.
2. **Heartbeat:** every ~30s, POST liveness (agent version / os / uptime; empty
   `printers`/`jobs` until the adapter slice) to `/api/print/hub/heartbeat` with the
   credential. The admin dashboard shows the hub **online** with a ticking last-seen clock.
   `systemctl`'s `Restart=always` is the self-healing reconnect; a revoked credential 401s
   and the agent exits loudly.

## Quick start (Raspberry Pi)

1. Flash **Raspberry Pi OS Lite (64-bit)** with the Imager (set hostname, SSH key, Wi-Fi).
   Boot, SSH in, `sudo apt update && sudo apt full-upgrade -y`.
2. Get the agent onto the Pi from a **pinned, reviewed release tag** (never mutable `main` —
   see [`SECURITY.md`](SECURITY.md) supply chain):
   ```sh
   git clone --branch v0.1.0 https://github.com/overengineered-solutions/makeros-hub-agent.git
   cd makeros-hub-agent
   sudo ./install.sh
   ```
3. Mint a token at `<cloud>/admin/3dprinting/hubs`, then on the Pi run the command it shows:
   ```sh
   sudo -u makeros-hub makeros-hub enroll --token <token> --cloud-url https://<cloud-host>
   ```
4. Start the loop:
   ```sh
   sudo systemctl enable --now makeros-hub
   journalctl -u makeros-hub -f          # 'heartbeat ok 200' every ~30s
   ```
5. Watch it go **online** at `<cloud>/admin/3dprinting/hubs`. Stop the service → it flips
   **offline** after ~90s; revoke in the UI → the next heartbeat 401s.

## Config

`/etc/makeros-hub/config.toml` (non-secret): `cloud_url`, `heartbeat_sec`. Everything is
overridable by env (`MAKEROS_HUB_CLOUD_URL`, `MAKEROS_HUB_HEARTBEAT_SEC`, …) and by
`--cloud-url` on the CLI.

## Develop / test

No runtime deps. Tests are stdlib `unittest`:
```sh
python3 -m unittest discover -s tests -v
```

## Layout

```
makeros_hub/
  config.py    config + credential storage (0600)
  http.py      stdlib JSON transport with retries (swap to httpx in the adapter slice)
  enroll.py    one-time token -> per-hub credential
  agent.py     the heartbeat loop
  __main__.py  CLI: `makeros-hub enroll …` / `makeros-hub run`
systemd/makeros-hub.service
install.sh
```

## Roadmap

PR 2 (this) enrollment + heartbeat → PR 5 first printer adapter (Klipper/Moonraker
recommended first, then Bambu A1 Mini: LAN MQTT + FTPS) behind a printer-adapter interface,
with `shape_observed` + iterated counts + a daily smoke from each adapter's first commit.
See `docs/native-print-pilot.md` in the makeros repo.
