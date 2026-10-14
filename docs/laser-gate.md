# Laser gate

The laser gate is the only network path to the OMTech Pro 80W's Ruida controller. It is a small Python service,
standard library only, with its own user, its own systemd unit and its own installer. It is not part of the hub
agent: the hub's `install.sh` and `update.sh` never touch it, and there is no OTA for it.

The rule (it also heads `laser_gate/__init__.py`): *This service can check a job file, store it in the controller's
memory under a short code, and read status. Start, stop, pause, frame, jog, home, panel keys (the panel-key port),
delete and settings writes were never written. Any new endpoint or command needs a review. No OTA: a person installs
every update.* The operator's wording names the panel-key port by number; a hard rule keeps that number out of this
code and its docs. The gate sends only to the controller's job port, 50200, and the firewall drops everything else.

Design: pstation `docs/laser-control-plan-2026-10-14.md`, plus the 2026-10-14 contract (gate section).

## What it does

It listens on one TCP port (7650) and answers only the hub, which must send a bearer token. It speaks Ruida UDP only
to one controller address, over a NIC or VLAN that is cabled only to the laser.

| Endpoint | What it does |
|---|---|
| `POST /check?maxPowerPct=N` | Checks an `.rd` file and never contacts the controller. The answer is `checked` (with the size, the layers and the highest power) or `refused` (with a plain reason, such as "command D9 00 at byte 120 isn't allowed (jog/rapid move)"). |
| `POST /store?name=MD0412&maxPowerPct=N&sha256=…` | Checks the file again, then stores it in the controller's memory under that name. |
| `GET /status` | Reads the controller's status word (moving or not) and its stored-file count. |

A file at the 4 MiB limit takes about 4 s to check on a desktop, and about 10 s in the worst case (all one-byte
commands), more on a Pi, in under 64 MB. `/store` checks the file again before it sends. The hub's timeouts for
both calls must allow for that: it waits 60 s for `/check` and 120 s for `/store`.

How a store works:
- It refuses while the head is moving.
- It sends packets of at most 1000 bytes. Packets are cut only between commands, and each one is sent only after
  the previous one is acknowledged.
- A missed ACK aborts the transfer at once. No cleanup command is sent (none exists), and the transfer is never
  resumed.
- A store succeeds only if the stored-file count goes up by exactly one.

Every datagram passes a never-send guard before it reaches the socket. A datagram is sent only if every command in
it is one of:
- an allowlisted job command, with the exact parameter length;
- the gate's own store envelope, at the start of a datagram;
- a read of the status word or of the file count.

The job-file header marker `D8 00` may travel only in a store's first packet, after the envelope.

The gate ships inert for storing. Until T0, T1 and T5 are done:
- `/store` answers `store_disabled` or `not_calibrated`;
- `/check` answers `not_calibrated`;
- `/status` works from the start.

## Shop box or Pi

Both work. The code is the same.

**Shop box, with a dedicated USB NIC or a VLAN to the laser.** It needs a USB Ethernet adapter or a VLAN-capable
switch port, and nothing else. Use `listen = "127.0.0.1:7650"` and `allowed_peer = "127.0.0.1"`, and install with
`--hub-local`.

On the shop box, isolation comes from four things:
- per-interface `forwarding=0`;
- nftables forward drops;
- an output rule that lets only the `laser-gate` uid reach the laser NIC;
- systemd's `IPAddressAllow`.

The nftables rules have their own unit, `laser-gate-firewall.service`. It loads them at every boot, before any
network, Docker or Tailscale comes up, and they hold whether or not the gate runs. The gate won't start without it.

Global `ip_forward` stays on, because Docker and Tailscale need it. The limit: the hub's OTA runs its `install.sh` as
root on this same box. "No OTA on the gate" therefore holds by convention, backed by a test that the hub's `install.sh` and
`update.sh` never mention the gate and that the agent never imports it (G13). It is not a hardware boundary.

The hub's root helper `vp-network.py` adds addresses only on an interface that carries a default route, or on `lo`
inside 198.18.0.0/15. The laser NIC has no gateway, and `install.sh` refuses to continue if it has one, so the helper
cannot touch it.

**A Raspberry Pi on a 1 m cable** (about $60–90; price UNVERIFIED). This gives the hard boundary: nothing else runs there, and no OTA
reaches it. Settings:
- `listen`: the Pi's shop-LAN address, port 7650;
- `allowed_peer`: the hub's LAN address;
- `gate_url` in the hub's `[laser]` table: `http://<pi>:7650`.

Copy the token to the hub by hand; the installer prints how.

## Before you start (prerequisites and caveats)

- **Phase 0 is done.** The controller is unplugged from the shop switch, and the shop laptop runs LightBurn over USB.
  Unplugging ends LightBurn over Ethernet for everyone, so tell staff first.
- **Make every cable and panel change with the laser off and idle.**
- **The laser NIC or VLAN has a static address and no gateway.** On the shop box with a USB NIC, for example, use a
  netplan or NetworkManager profile: `10.0.3.1/24` with no gateway, no DNS and no DHCP. No other cable or device
  joins that segment.
- **The controller is set to `10.0.3.3` / `255.255.255.0` on the panel** (Z/U menu → IP Setup+). Power-cycle the
  laser if the panel asks.
- `nftables` and Python 3.11 or newer are installed (`python3 --version`).
- Nobody contacts the controller over the network except during the tests below, with someone at the machine.
- Students don't use Load until T1–T3 pass, and an admin records that on `/admin/lasers`.

## Install

From a checkout of the hub repo at the release you are installing:

```sh
sudo ./laser_gate/install.sh --iface <laser NIC or VLAN> --hub-local     # shop box
sudo ./laser_gate/install.sh --iface eth0                                # a Pi (no --hub-local)
```

The first run writes `/etc/laser-gate/config.toml` from `laser_gate/config.example.toml` and stops. Edit it, then run
the same command again. The second run:
- creates the `laser-gate` system user;
- copies the code to `/opt/laser-gate`, root-owned;
- generates `/etc/laser-gate/token` (0600, never printed) and, with `--hub-local`, installs the same value as
  `/var/lib/makeros-hub/laser-gate-token`;
- renders `/etc/laser-gate/laser-gate.nft` and `/etc/laser-gate/unit.env`;
- installs and enables `laser-gate-firewall.service` (it loads the firewall at every boot) and `laser-gate.service`,
  then starts both.

It refuses to continue if any of these is true:
- `bind_ip` is not on `--iface`;
- the interface has a default route;
- the config does not validate.

To update, pull the new version and re-run the same command. That is the only way an update lands.

Check it:

```sh
systemctl status laser-gate-firewall laser-gate
journalctl -u laser-gate -n 20
sudo nft list table inet laser_gate
```

## Tests at the machine (the operator only)

Run these with the lid closed, scrap in the bed and a hand near the E-stop. Nobody else contacts the controller.
Each test sets the config key shown; after an edit, `sudo systemctl restart laser-gate`.

| Test | What | Sets |
|---|---|---|
| **T0** | Before `/store` can work. Over USB, run `ruida-readonly.py watch COM5 120` while LightBurn **Sends** (not Starts) a small file to memory. The address that steps by exactly one is the stored-file count. If a passive capture of LightBurn's Send on the private segment is possible, also confirm the store envelope (`E8 02`, `E7 01 <name> 00`) and the ACK byte (`0xC6` on the wire). | `file_count_addr` (hex) |
| **T1** | Storing doesn't start the job. Set `store_enabled = true` for this test (and only from now on). Store the 20 mm square. Nothing moves, the code appears on the panel, and the count goes up by one. Then press Start on the panel, on scrap. | `store_enabled` |
| **T2** | Stalls. Repeat T1 with gaps of 1, 2, 5 and 10 s between packets, including right after the first one. Record anything that moves. | `ack_timeout_s` |
| **T3** | Aborted transfer. Pull the cable halfway through a file. Nothing should move, and the next store should work. | none |
| **T4** | "Job running" status. Poll status word 0x0200 during a job started at the panel and during a pause. | none (bits noted) |
| **T5** | Minimum power. Read the plug's current at 5, 10, 15 and 20% power. The lowest power that reads 2.3 A or more is the minimum. | `min_power_pct` |
| **T6** | Fans. After any change to the laser's plug, confirm the fans still come on during a cut. | none |
| **T7** | USB and Ethernet together. Check that the laptop on USB and the gate polling don't interfere. | none |

After T1–T3 pass, an admin records "T1–T3 passed" on `/admin/lasers` and may then enable Load.

**Top risk (contract C9).** MeerK40t's catalogue calls `D8 00` "Start Process", yet it is the header marker of every
stored file. If storing a header that contains `D8 00` starts a job on RDLC-V26.78.02, T1 is the test that catches
it. Until then, the envelope always comes first in the same packet, and the guard refuses to send `D8 00` any other
way.

## Adding sample files

See `laser_gate/samples/README.md`. In short:
1. Save a cut, a score and an engrave from LightBurn with File → Save RD File.
2. Drop them in `laser_gate/samples/`.
3. Run `python3 -m laser_gate learn laser_gate/samples`.
4. Trim `ALLOWED` in `laser_gate/checker.py` to what they use.
5. Run the tests.

Until that happens, the allowlist and every parameter length are research values (MeerK40t's Ruida driver,
jnweiger/ruida-laser, kkaempf/ruida, the EduTech wiki), marked "TO CONFIRM".

To check one file by hand (no network): `python3 -m laser_gate check FILE.rd --max-power 80 --config /etc/laser-gate/config.toml`.

## Diagnostics

- **The read-only probe and the gate can't run at the same time.** `ruida-readonly.py udp:` binds UDP 40200, which
  the gate holds; the controller always answers to that port.
- **The firewall lets only the `laser-gate` uid reach the laser NIC.** `laser-gate-firewall.service` loads the rules at
  every boot, before the network, and they stay loaded when the gate stops. `systemctl restart nftables` reloads them
  by itself. `systemctl stop nftables` flushes every table, this one included, and stops the gate with it; restore both
  with `sudo systemctl start laser-gate-firewall laser-gate`. To probe over Ethernet, stop the gate, then run the probe
  as that user:

  ```sh
  sudo install -m 0644 ruida-readonly.py /tmp/ruida-readonly.py     # pstation's scripts/ruida-readonly.py
  sudo systemctl stop laser-gate
  sudo -u laser-gate python3 /tmp/ruida-readonly.py probe udp:10.0.3.3
  sudo systemctl start laser-gate
  ```
- **The gate answers 403 to everyone but `allowed_peer`, and 401 without the token.** From the hub's box, with the
  hub's copy of the token. The header goes to curl on stdin (`printf` is a shell builtin), so the token never shows in
  a process list:

  ```sh
  sudo -u makeros-hub sh -c 'printf "Authorization: Bearer %s\n" "$(cat /var/lib/makeros-hub/laser-gate-token)" | curl -s -H @- http://127.0.0.1:7650/status'
  ```
- **What `/status` says:**
  - `"controller": "down"`: no ACK within `ack_timeout_s`. Check that the laser is on, the cable is in, the panel
    shows 10.0.3.3, and `ip addr show <iface>` shows `bind_ip`.
  - `"busy": true`: a store is running.
- **Logs:** one line per `/store` (name, sha256 prefix, bytes, packets, result) and one per refusal. The token is never
  logged.

## Facts still UNVERIFIED (each one is why the gate ships inert)

- The store envelope `E8 02` + `E7 01 <name> 00`, and the ACK byte `0xC6`. Settled by T0, or by a passive capture.
- The stored-file count address. Settled by T0.
- Parameter lengths, the power and speed encodings, and the single-copy values the checker requires of the repeat
  and array commands (`NEUTRAL`: a count above one is refused as `repeat_or_array`). Settled by the samples, plus
  T1 and T5.
- Whether `D8 00` in a stored header starts the job. Settled by T1.
- The bed check assumes absolute coordinates. With LightBurn's "current position" start mode (`D8 12`), the controller
  adds the head's position at Start, which no file check can see; the controller's own soft limits cover that case.
- The sandboxing options don't apply to the gate unit's one `+` (root) start command, `sysctl`. That is systemd's
  documented behaviour for `+`, but it hasn't been tried on the shop box yet. `nft` runs in its own unsandboxed unit,
  `laser-gate-firewall.service`.
