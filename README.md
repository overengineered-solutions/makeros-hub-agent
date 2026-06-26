# makeros-hub-agent

The on-LAN bridge between a makerspace's 3D printers and the **makeros** cloud control
plane — the agent half of the SimplyPrint replacement. Runs on a Raspberry Pi (or any
small always-on Linux box) on the shop network.

> **Status: production (v0.45.0).** Multi-vendor fleet agent. Enrollment + heartbeat are
> stdlib-only; the venv adds `paho-mqtt` (Bambu LAN), `cryptography` (virtual-printer TLS),
> and optionally `onnxruntime`+`Pillow` (AI failure-watch). Every dep degrades gracefully
> when absent — heartbeats never break. The source-of-truth version is
> `makeros_hub/__init__.py:__version__` (reported in the heartbeat); `pyproject.toml` tracks it.

## What it does

A single cloud-cadenced **heartbeat loop** (`agent.py`) drives everything. It enrolls once for
a durable per-hub bearer, pulls **config-down** from the cloud (printers + virtual printers +
tailscale + restart nonce) whenever the heartbeat's `configVersion` changes, reconciles
per-vendor adapters, and reports rich telemetry every beat. Capabilities:

1. **Enroll once** — exchange a one-time admin-minted token (`<cloud>/admin/3dprinting/hubs`)
   for a durable **per-hub bearer credential**, stored `0600`. Tokens + credentials are never
   logged. In-band credential rotation (overlap window, no lockout) is supported.
2. **Printers (multi-vendor)** — **Bambu** over LAN MQTT (`:8883`, one long-lived connection
   per printer; pause / resume / stop, AMS dry, per-object skip) and **Klipper / Moonraker**
   over HTTP poll (read + control + job ingest), both normalized to one wire DTO so the cloud
   never branches by vendor. Access codes flow *down* over the authenticated channel; the
   heartbeat carries telemetry only — **no secret ever goes back up on the wire**.
3. **Virtual printer** — full OrcaSlicer/Bambu **LAN emulation** so a member's OrcaSlicer
   Device tab treats the hub as a real Bambu: SSDP responder + an MQTT broker + FTPS receiver +
   a self-signed CA, with multi-VP runtimes, per-member sha256 access codes, a live AMS mirror,
   and per-VP LAN-IP allocation. The member slices and hits **Send**; the job is captured,
   attributed to the member, and queued in the cloud.
4. **OrcaSlicer ingest (OctoPrint-compatible)** — the simpler `:8787` host path: a member sets
   OrcaSlicer's Print Host with their **print token** as the API key; the hub spools the sliced
   file locally and registers the job with the cloud (token → member, eligibility gate, queue).
5. **Camera** — per-printer frame capture by vendor strategy (Bambu `:6000` TLS-MJPEG;
   X1/H2/P2S RTSPS `:322` via ffmpeg; Klipper/OctoPrint HTTP snapshot), default-off, with a
   stable categorized no-frame reason vocabulary surfaced to the admin.
6. **AI failure-watch** — optional ONNX YOLO spaghetti detector, EWMA-smoothed, **notify-only**,
   default-off (activates only when model weights are pinned; otherwise a p=0.0 stub).
7. **LAN discovery** — a Moonraker HTTP sweep + a passive Bambu SSDP listener feed the admin's
   "detected on your LAN" add-printer dropdown.
8. **Self-healing** — `Restart=always`; stuck-MQTT-adapter rebuild; cloud-triggered restart via
   a config-down nonce; narrow **tag-only OTA self-update** (monotonic, cooldown); Tailscale for
   remote hub access; Pi health metrics + on-demand allowlisted read-only diagnostic probes.

## Quick start (Raspberry Pi) — one command

1. Flash **Raspberry Pi OS Lite (64-bit)** with the Imager (set hostname, SSH key, Wi-Fi).
   Boot and SSH in.
2. In the admin (`<cloud>/admin/3dprinting/hubs`) click **Mint enrollment token** and paste the
   **one command** it gives you onto the Pi. It installs prerequisites, clones the **pinned,
   reviewed release** (never mutable `main` — see [`SECURITY.md`](SECURITY.md)), installs the
   agent, enrolls this hub, and starts the service:
   ```sh
   curl -fsSL https://raw.githubusercontent.com/overengineered-solutions/makeros-hub-agent/v0.45.0/bootstrap.sh \
     | sudo bash -s -- --token <TOKEN> --cloud-url https://<cloud-host> --ref v0.45.0
   ```
3. Watch it go **online** within ~30s: `journalctl -u makeros-hub -f` (`heartbeat ok 200`). Stop
   the service → it flips **offline** after ~90s; revoke in the UI → the next heartbeat 401s.

**After this, updates are over-the-air.** Flip on **Auto-update** for the hub in the admin (or
click **Update now**) and it self-updates to new pinned-tag releases — no SSH. (Manual install
still works: `git clone --branch <tag> … && cd makeros-hub-agent && sudo ./install.sh`.)

## Config

`/etc/makeros-hub/config.toml` (non-secret): `cloud_url`, `heartbeat_sec`. Everything is
overridable by env (`MAKEROS_HUB_CLOUD_URL`, `MAKEROS_HUB_HEARTBEAT_SEC`, …) and by
`--cloud-url` on the CLI. The actual heartbeat cadence is dictated by the cloud in each
response (no redeploy to change it). AI failure-watch activates only when `MODEL_URL` +
`MODEL_SHA256` are both set.

## Develop / test

Tests are stdlib `unittest` (not pytest):
```sh
python3 -m unittest discover -s tests -v
```
The design splits **pure** logic (report parsers, YOLO pre/post-process, version-compare, EWMA,
3MF object enumeration) into stdlib-only modules so it unit-tests with **no** paho /
cryptography / onnxruntime / Pillow / ffmpeg / real printer; the I/O wrappers are thin and
integration-tested on the Pi.

## Layout

```
makeros_hub/
  agent.py             heartbeat loop: pull config, drive adapters, report telemetry
  enroll.py            one-time token -> durable per-hub bearer credential (0600)
  config.py            config + credential storage + config-down
  http.py              stdlib JSON transport
  update.py            narrow tag-only OTA self-update (monotonic, cooldown)
  discovery.py / lan_scan.py / bambu_ssdp.py   LAN discovery (Moonraker sweep + passive Bambu SSDP)
  ingest.py / multipart.py                     OctoPrint-compatible :8787 host
  probes.py            allowlisted read-only diagnostic probes
  diagnostics.py       sticky per-subsystem error rings + redacted log ring
  tailscale.py         remote-access reconcile (authKey from config-down only, never logged)
  printers/
    bambu.py / bambu_send.py / bambu_parse.py  Bambu MQTT adapter + control + PURE parser
    klipper.py                                 Moonraker HTTP-poll adapter (read + control + ingest)
    manager.py                                 reconcile adapters against config-down; dispatch commands
    jobs.py / queue_progress.py                job lifecycle tracking
    camera.py / bambu_camera.py / rtsp_camera.py   per-vendor frame capture
    failure_watch.py / onnx_detector.py / yolo_*.py   AI failure-watch (EWMA + ONNX YOLO)
    threemf_objects.py                         3MF object enumeration (per-object skip)
  vprinter/
    manager.py                                 asyncio multi-VP runtime supervisor
    mqtt_broker.py / ftp_server.py / ssdp.py / bind_server.py   the OrcaSlicer/Bambu LAN emulation
    cert.py                                    self-signed CA + leaf (delivered to cloud for the member bundle)
    auth.py                                    per-member sha256 access-code verify + rate limiting
    capture.py / report.py                     job capture + AMS-mirror push_status
    ip_allocator.py / live_pool.py / outbox.py
  __main__.py          CLI: `makeros-hub enroll …` / `makeros-hub run`
systemd/makeros-hub.service
install.sh             service user + venv (paho-mqtt, cryptography, optional onnxruntime+Pillow) + systemd unit
update.sh              root-scoped OTA apply (sudoers-pinned to this one script)
```

## Releases / OTA

Bump `makeros_hub/__init__.py:__version__` (+ this `pyproject.toml`), push an annotated
`vX.Y.Z` tag, and bump `LATEST_AGENT_VERSION` in the cloud
(`apps/web/lib/print/agent-version.ts`). Auto-update hubs converge within a heartbeat; OTA
accepts **only** well-formed `vX.Y.Z` tags of this repo, never downgrades, and runs under a
sudoers rule scoped to `update.sh` alone. See [`SECURITY.md`](SECURITY.md).
