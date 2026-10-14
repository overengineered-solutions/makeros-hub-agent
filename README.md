# Procrastination Station shop-box agent

The always-on Linux bridge between the sovereign pstation website and the shop's physical printers. The historical package/service name remains `makeros-hub`; it does not require the old MakerOS app. This checkout is a **0.68.0 review candidate**, not evidence that the installed shop box has been updated.

The source version is `makeros_hub/__init__.py`; `pyproject.toml` must match. Read [SECURITY.md](SECURITY.md) for the boundaries and limitations.

## What members experience

1. On `/print`, a member finishes membership/training/waiver requirements and chooses computer setup or website upload.
2. The Windows/Mac setup installs or updates official stable OrcaSlicer only when needed, joins the private member network and adds the configured PS printer pools: PS A1 Minis, PS X1 Carbons, PS P2S Printers and PS H2D Printers. P1S is not part of the operator's fleet. First-time Orca welcome/plugin approvals remain visible. Members' own profiles stay in place.
3. OrcaSlicer sees a virtual printer for a model, with the loaded spool choices for its fleet. Upload and MQTT print intent are paired by authenticated member and filename, then persisted and registered with the website.
4. A transfer finishing in Orca means delivery to the queue. `/print` shows physical progress. The website selects a compatible idle printer, checks plate/filament requirements and requires the bed to have been cleared. The box rechecks dispatch conditions and reports the physical result.
5. Members collect their part, confirm the real bed is clear, and inspect any charge on the website. They never need a physical printer password or shell access to the box.

Linux members can upload a sliced .3mf for a supported Bambu model on the website. The older OctoPrint-style HTTP print-host path remains available for **staff-assisted** work: it carries no verified model and its jobs are held for review. Sovol jobs currently require staff assistance; do not promise automatic model-safe dispatch through that legacy path.

## Install and enroll — operator only

Use a reviewed checkout of the approved sovereign release, and compare `git rev-parse HEAD` to the independently recorded release commit before running `sudo ./install.sh`. The installer needs Python 3.11+, systemd and sudo; Debian/Pi OS uses `python3-venv`, `python3-pip`, `iproute2`, `iputils-arping`, git, and optionally ffmpeg for RTSPS cameras. Required `paho-mqtt`/`cryptography` imports must succeed. ONNX/Pillow are optional and failures are surfaced.

Provision Tailscale once through its [official Linux package repository instructions](https://tailscale.com/download/linux). The service helper only configures an installed client; it does not execute a downloaded root shell script. Reserve any VP LAN alias range outside DHCP, and keep the printer, member and management networks separated as described in SECURITY.md.

From `/admin/hub`, mint a one-use token and run the displayed enrollment command on the box within fifteen minutes. It runs as `makeros-hub` and names the deployment that minted it. After successful enrollment:

```sh
sudo systemctl enable --now makeros-hub
journalctl -u makeros-hub -n 80 --no-pager
```

Confirm the recent heartbeat, expected version, printer list, actual spools/plate status and VP reachability. A green heartbeat alone is insufficient. Inspect logs privately; filenames and device identifiers may be personal data even when credentials are redacted.

`bootstrap.sh` is an optional operator helper from a reviewed checkout. It requires `--ref vX.Y.Z --sha <reviewed-40-hex-commit>`, `--cloud-url`, and the enrollment token; it verifies the canonical clone before installing. Do not pipe an unreviewed mutable network script into sudo. A token belongs in the private enrollment workflow, never a review document or shell-history screenshot.

## Files and services

| Location | Purpose |
| --- | --- |
| `/opt/makeros-hub` | Root-owned agent code, venv and validated root helpers |
| `/etc/makeros-hub/config.toml` | Non-secret HTTPS cloud origin and runtime settings; service-writable for enrollment |
| `/var/lib/makeros-hub/credential` | Private bare hub bearer, mode 0600 |
| `/var/lib/makeros-hub` | Durable spool, submission/progress state, VP certificates and diagnostics; preserve as a unit |
| `/opt/makeros-hub/previous` | Previous code/root helpers from the most recent install; not a full venv backup |

The main heartbeat loop refreshes physical adapters and virtual printers, sends telemetry and durable reports, accepts assignments, reconciles authorized commands, and manages recovery. Camera/failure-watch are optional and normally notify-only. Config environment overrides are documented in source; `repo` in old config files is ignored by the root updater.

## Troubleshooting and recovery

- **No heartbeat:** check power, route/DNS, disk space and `systemctl status makeros-hub`, then its log. 401 requires a fresh enrollment; an empty or broken JSON acknowledgement is not success. Do not replace credentials with an empty value.
- **Member cannot connect:** check membership requirements, member network account, pending/completed device grants, VP health and the setup problem code. Removal of a network device and revocation of printer codes are separate actions. Code changes cannot cancel a running physical print.
- **Upload uncertain:** the box retains a file when the website response cannot confirm whether the job was created. Check the member's job list and staff queue using its time and filename before resending. Compare the private spool SHA/UID with the job record if needed. Remove only a specifically reconciled abandoned file; never blanket-delete spool or outbox state.
- **Queue waits:** read the member-visible reason. Check model, compatible loaded materials, plate type and actual plate clearance. Do not bypass a gate simply to clear the queue. Legacy jobs without a model require staff-assisted handling or cancellation/re-upload through the supported path.
- **Low disk:** intake refuses further files before exhausting the box. Preserve pending files/state; clear only verified terminal/abandoned files under the shop retention policy. Manual HTTP has a 64 MiB effective cap and one buffered upload; website and streaming FTPS have their own limits.
- **Re-enrollment or host change:** stop the service first; mint a fresh token on the intended app, enroll with `--force` only when replacing this box's existing credential, and confirm config persistence before restarting. A failed config write now returns failure, not a ready message.
- **Unexpected print status:** compare physical machine state and the assigned job before repeating a start. Durable dispatch/progress records exist to prevent replay. Do not delete them to clear an alarm.

## Updates and rollback

Back up the private state/config and installed runtime first. Set a reviewed newer release tag and exact commit in `/admin/hub`; follow the reported version and errors. The root gate refuses downgrades and same-version reinstalls even for direct service-user calls. The install stages code and helpers, stops the service during replacement, and restores them on a failed swap/start. Required dependency failure stops before replacing code.

The dependency venv is not transactionally rolled back. For an operator rollback or same-version repair, clear the app's pending update target, stop the service, restore the verified prior runtime/venv and root helpers from the full backup, preserve compatible durable state, then start and verify. Do not erase update cooldown state to force a retry without diagnosing the failure. Do not restore private device credentials from an old backup over deliberate revocations.

Before accepting a production rollout, perform a small supervised canary: member setup/first launch on Windows and Mac, two distinct member uploads with identical filenames, queue/plate/filament gates, an agent restart during a safe test print, confirmed completion/charge, and denied access after membership/device revocation. Verify actual firewall/Headscale rules and physical printer LAN isolation separately. Source tests do not establish those physical facts.

## Develop and verify

```sh
python3 -m pytest -q    # tests/conftest.py gives each test its own state dirs; plain unittest discover does not
bash -n install.sh bootstrap.sh scripts/tailscale-setup.sh laser_gate/install.sh
sh -n update.sh
```

Tests use fake equipment and local sockets; NumPy enables the real YOLO preprocessing/decoding checks. The suite includes root-repository/downgrade checks, isolated staging failure recovery, HTTP redirect and acknowledgement rejection, multipart binary fidelity, spool durability, cross-member MQTT/FTPS behavior and printer dispatch recovery. Never point a test at live equipment.

## 0.68.0 candidate: Klipper/SV08 dispatch, laser status and runs, Turn on and Load, the laser gate

**Klipper printers (the SV08 Max first).** The website can now send a queue job to a Klipper printer. The agent
refuses a G-code sliced for another model (its `printer_model` must equal the printer's model on the website),
checks that klippy is ready and the printer is between prints (no start behind a screen macro), records the dispatch
before it uploads, streams the file to Moonraker's `POST /server/files/upload`, then starts it with
`POST /printer/print/start`. Progress, the running print's job key and the terminal job are durable across a restart
or an adapter rebuild, so one print keeps one key and is reported once. Moonraker's `cancelled` reports as cancelled.
The adapter talks only to the printer's `moonrakerUrl` as entered on the website, with Moonraker's own paths: no port
is assumed, so `http://<printer>` (nginx on port 80 in front of a loopback-only Moonraker) works as well as `:7125`.
The OctoPrint-style shim now also sends what an OrcaSlicer G-code says about itself (`printerModel`, `estGrams`,
`requiredFilaments`); the fields are omitted when absent, and whether such a job still waits for review is the
website's decision.

**Adapter retry (2e26af0).** A Bambu adapter whose start failed is retried on the 120 s backoff from the heartbeat,
without waiting for a config edit (a Klipper adapter keeps polling on its own). Unacked finished prints rescued from a
rebuilt adapter are keyed by `jobKey`, so a rebuild (a model edit, a recovered printer) no longer reports the same
print twice.

**Laser status and cutting runs.** The OMTech laser's plug readings arrive from Home Assistant on a second listener (`POST /ha/laser`, port 8788, one
allowed source address, a bearer from a 0600 file). The agent derives each laser's state (off, on but not cutting,
cutting, unknown) and its cutting runs (a run closes after 60 s without cutting, on power-off, or when telemetry stops)
and reports them on the heartbeat until the cloud has them. It polls the laser gate's `/status` and has the gate
`/check` member files the cloud offers. All of this is inert without an `[laser]` table and the operator's secret files.

Two laser commands exist and nothing else: `laser_power_on` POSTs to an HA webhook (HA decides, then runs
`switch.turn_on`) and `laser_load` fetches a checked file from the cloud and hands it to the gate's `/store`. There is
no off command. Both carry an expiry the agent enforces. They are routed before any printer adapter is looked up. The HA
YAML and the hub's settings are in [docs/laser-ha.md](docs/laser-ha.md); the gate is a separate service with its own
installer and is never installed or updated by this agent.

**The laser gate (`laser_gate/`, its own 0.1.0).** A stdlib-only service in front of the OMTech's Ruida controller:
`POST /check` (never contacts the controller), `POST /store` (checks again, then stores the file in the controller's
memory, refusing while the head moves) and `GET /status`, and nothing else; members start a job at the laser's panel.
Among its checks, a file is refused unless every command is on its allowlist, every power is within the member's material cap, and
every cut's layer has its power and speed set before it.
It runs as its own `laser-gate` user under `laser_gate/install.sh`, with nftables rules in their own boot unit.
The agent never imports it, and this repo's `install.sh` and `update.sh` never mention it (G13), so a hub OTA cannot
install or change it. The agent waits 60 s for `/check` and 120 s for `/store`. The command table and the store
envelope are unverified against the real controller and operator sample files; see
[docs/laser-gate.md](docs/laser-gate.md) for T0-T7 before enabling Load.

## 0.67.0 candidate: observed H2D hardware and external cameras

Normalizes observed H2D nozzle/AMS bindings, advertises them through the existing VP pool and refuses missing or incompatible task nozzle metadata before upload/start. `ams_mapping2` contains AMS/slot objects; it is not a second-nozzle selector. AMS HT slot command addresses use the raw AMS id (128 here), not that id multiplied by four. Actual Orca/plugin command capture and a supervised physical canary remain required.

An explicit HTTP camera snapshot overrides the native source for any printer. Every frame/failure upload carries a `camera-v1` identity derived from vendor/model/host/serial/Moonraker URL/snapshot URL; changing that source makes a fresh capture due. uStreamer offline placeholders are rejected.

Paired pstation rollout: install the reviewed hub candidate FIRST and verify accepted images/version against the current website, then ship the preview-tested website with required source identity and migration 0093. Old website versions ignore the new query parameter; new versions reject source-less uploads. Updating the live OTA target requires independent operator approval. The separate adapter-retry branch is not included in this candidate.
