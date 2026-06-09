# makeros-hub-agent

The on-LAN bridge between a makerspace's 3D printers and the **makeros** cloud control
plane — the agent half of the SimplyPrint replacement. Runs on a Raspberry Pi (or any
small always-on Linux box) on the shop network.

> **Status: printer pilot (PR 5).** Enrollment + heartbeat are stdlib-only; the **Bambu LAN
> adapter** adds one dep, `paho-mqtt` (installed into a venv by `install.sh`). Klipper/Moonraker
> is the next adapter. The pure report parser (`makeros_hub/printers/bambu_parse.py`) has no deps.

## What it does

1. **Enroll once:** exchange a one-time token (minted by an admin at
   `<cloud>/admin/3dprinting/hubs`) for a durable **per-hub bearer credential**, stored
   `0600` at `/var/lib/makeros-hub/credential`. The token and credential are never logged.
2. **Heartbeat:** every ~30s, POST liveness + **per-printer status** to
   `/api/print/hub/heartbeat` with the credential. The admin dashboard shows the hub
   **online** and each printer's connection + activity state. `Restart=always` is the
   self-healing reconnect; a revoked credential 401s and the agent exits loudly.
3. **Printers (config-down):** the operator adds printers in the cloud admin UI (vendor /
   model / serial / IP + Bambu LAN access code). The agent pulls that list — including the
   access codes, over its authenticated channel — via `GET /api/print/hub/config` whenever
   the heartbeat's `configVersion` changes, then opens **one long-lived MQTT connection per
   Bambu printer** (port 8883, TLS-insecure self-signed, `bblp` + access code) and reports
   normalized status (connection + activity state, progress, temps) back in the heartbeat.
   **No secret ever goes back up on the wire** — the heartbeat carries telemetry only.

## Quick start (Raspberry Pi)

1. Flash **Raspberry Pi OS Lite (64-bit)** with the Imager (set hostname, SSH key, Wi-Fi).
   Boot, SSH in, `sudo apt update && sudo apt full-upgrade -y`.
2. Get the agent onto the Pi from a **pinned, reviewed release tag** (never mutable `main` —
   see [`SECURITY.md`](SECURITY.md) supply chain):
   ```sh
   git clone --branch v0.2.0 https://github.com/overengineered-solutions/makeros-hub-agent.git
   cd makeros-hub-agent
   sudo ./install.sh    # creates a venv + installs paho-mqtt for the Bambu adapter
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

## Develop / test (printer layer)

The Bambu **report parser** is pure (stdlib) so it's fully unit-tested without paho or a
printer; the paho I/O wrapper is thin. Tests stay stdlib `unittest`:
```sh
python3 -m unittest discover -s tests -v
```

## Layout

```
makeros_hub/
  config.py            config + credential storage (0600) + config_url
  http.py              stdlib JSON transport (post_json + get_json for config-down)
  enroll.py            one-time token -> per-hub credential
  agent.py             heartbeat loop: pull config, drive adapters, report status
  printers/
    bambu_parse.py     PURE: deep-merge report deltas + normalize to the wire DTO (no paho)
    bambu.py           paho MQTT adapter (one long-lived connection per printer)
    manager.py         reconcile adapters against the cloud config-down
  __main__.py          CLI: `makeros-hub enroll …` / `makeros-hub run`
systemd/makeros-hub.service
install.sh             service user + venv (paho-mqtt) + systemd unit
```

## Roadmap

PR 2 enrollment + heartbeat → **PR 5 (this) Bambu LAN adapter** → Klipper/Moonraker adapter →
file-send (FTPS) + job → invoice ingestion. Each adapter ships `shape_observed` + iterated
counts from its first commit. See `docs/native-print-pilot.md` in the makeros repo.
