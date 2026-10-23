# Procrastination Station shop-box agent

The always-on Linux bridge between the sovereign pstation website and the shop's physical printers. The historical package/service name remains `makeros-hub`; it does not require the old MakerOS app. This checkout is a **0.70.0 review candidate**, not evidence that the installed shop box has been updated.

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

## 0.81.0 candidate: members' file checks run on the shop box

Operator, 2026-10-22: "Let's go ahead and do the work to get it on the shop box instead of raising the linode cap". The
site's Will it print? check no longer parses a mesh on the website; it queues the file and hands it here.

- The heartbeat carries `checker` (`{format, version}` of the installed image, from a probe at start and every 15 minutes,
  and from every check's own report, plus `free`: whether the one check worker is idle), only while
  `/opt/ps-checker/run-check` is installed. The site hands a check (`checks`, with an attempt token and the `seconds` the
  hub has) only on a free beat, to a box whose checker format is at least its own; a free beat also tells the site that a
  check it thought this box was running was lost, and it is queued again at once.
- `makeros_hub/checks.py` fetches the file with the hub token into `<spool>/<id>/.check`, writes `job.json`, runs
  `sudo -n /opt/ps-checker/run-check <id> <seconds>`, sends Print it's slicer copy when asked and posts the report, then
  removes the folder. The fetch (and any wait for the site's one upload slot), the run and both posts share the check's one
  deadline. A report carries the identity of the run that wrote it; a run that was stopped carries none. Outputs are read
  only as regular files, never through a link. Anything the site refuses is dropped and the site takes the check back.
- `checker/run-check` (root-owned, installed by oes-pool-deploy `scripts/install-checker.py`) never mounts the hub's
  folder: it stages the job in `/var/lib/ps-checker/run` (root's alone), copies the inputs in and the outputs back as the
  hub's user (so a swapped folder or a link reaches only what the hub could reach itself), runs a named container (no
  network, read-only root, 2 GB, 2 CPUs, 64 processes) and kills and removes it at the deadline or on an interruption.
  Through sudo it always runs `ps-checker:current`. `tests/test_run_check_box.py` runs it for real on a box.
- `checker/` holds the container: Node 22 on Alpine and `check.mjs`, the site's own check code bundled by pstation
  `scripts/build-checker.mjs`. Its first line names the pstation commit and format; rebuild it there, never edit it here.
- The plate guard of 0.80.1 (never released alone) ships here too (operator, 2026-10-22: "see if you can dive into her
  files"): a printer is never told to start a plate its 3MF does not hold (`plate_missing`, `plate_unreadable`), and an
  assignment that names no plate prints a one-plate file's own plate and is refused as `plate_unchosen` for a file with several.

## 0.72.0 candidate: the shop slices members' checked files itself

Operator, 2026-10-18: "after we evaluate it we automatically slice it based on the material they want and the size and
then send it through to the printers". A web upload may now carry a member's checked mesh (`meshName`) and a `slice`
spec (printer model, filament type, colour and Bambu filament id, fixes: orient, brim, supports, scale) instead of a
sliced plate.

- `fetch_uploads` downloads the mesh into `<spool>/<uid>/.slice`, verifies the cloud's sha256, writes `job.json` with only
  the slicer's fields, and runs `sudo -n /opt/ps-slicer/run-slice <uid>`. The runner (slicer/run-slice, root-owned,
  installed by oes-pool-deploy `scripts/install-slicer.py`) starts the ps-slicer container on that folder only: no network,
  read-only root, 3 GB, 4 CPUs, 1024 processes, ten minutes, as the hub's user.
- The plate lands at the job's file name and goes through the same parse and report as any upload (model code, filaments,
  grams), plus a `slice` summary. A refused slice reports `sliceError` with the slicer's sentence; a slice whose report
  was lost is reported again next beat without slicing twice.
- `slicer/` holds the container: OrcaSlicer 2.4.2's official Linux AppImage pinned by sha256, Bambu's own presets resolved
  through their inheritance (the command line does not load the vendor bundle, and an unresolved preset loses its density
  and filament id), the textured plate, the model code Bambu writes into slice_info (the command line leaves it blank).
  A1 mini, P2S and X1C; the H2D waits for an extruder choice.
- The heartbeat carries `slicerReady` (the runner is installed and executable); the site offers automatic slicing and
  hands this hub slice jobs only while it is true, so an older box never receives a mesh it cannot slice.

## 0.71.0 candidate: every printer heard on every network leg, and who signs in to the virtual printers

- **Discovery** listens 8 s (was 3) on the broadcast port 2021 and the multicast group 239.255.255.250:1990, joined on
  every interface (`bambu_ssdp.py`). The 3 s window on 2021 alone heard about a third of the fleet per sweep; the shop
  box also has a second network leg now (Equipment, VLAN 20, 10.44.20.10, 2026-10-18), and a multicast join on the
  wildcard address covers only the default interface. A port that will not bind leaves the other one working.
- **Member sign-ins** (`vprinter/auth.py` ConnectLog): every successful MQTT or FTPS sign-in records the latest
  (virtual printer serial, member code sha256, client address, time). The heartbeat carries up to 64 as `vpConnects`
  and drops them after a 200 unless a newer sign-in replaced one meanwhile, so a lost beat sends them again. The code
  itself never leaves the sign-in; the site already holds the hash. The site keeps the latest per code (pstation 0165)
  and shows it to the member and the desk.
- The camera's "a refused frame is not a capture" and the next-beat retry shipped in 0.69; nothing changes there.

## 0.70.0 candidate: the phase, not the word "Printing", and status that does not wait for the heartbeat

Built on v0.69.0, following pstation `reports/Live printer status design.md` (the operator, 2026-10-15: "it says
printing but... it also looks like its been sitting there for a long time ... Maybe we show current temp vs target
temp"; "these waits are killer ... so we dont have to wait on hub heart beats").

**What each printer now reports (heartbeat and telemetry alike; every key additive, omitted when unknown).**
- **Klipper (the SV08 MAX).** `nozzleTargetC` / `bedTargetC` beside the temperatures, so 140 / 140 (held for
  calibration) reads differently from 140 / 215 (heating). `displayMessage`: the M117 text the start macro sets
  ("Bed soak 20 min before calibration" was there the whole time on 2026-10-15). `preparing`: `print_stats.state`
  is `printing` but `print_duration` is 0, i.e. the start routine (Klipper counts print time from the first extrusion).
  Progress is the slicer's M73 value (`display_status.progress`), which OrcaSlicer holds at 0 through the start routine,
  instead of the byte position that read 8 % while the file sat on its heat-wait; the byte position is used only when
  `display_status` is absent. **No ETA before the first extrusion**, then the slicer's `estimated_time` minus the time
  printed (one `GET /server/files/metadata` per new filename, cached, 32 entries; a failed fetch is retried once a
  minute and costs only the slicer ETA), else the old extrapolation. `printDurationSec`, `elapsedSec`
  (`total_duration`) and `layer` `[current, total]` from `print_stats.info` (filled by `SET_PRINT_STATS_INFO`; Orca's
  layer number is zero-based). The poll now also asks for `print_stats.info/message`, `toolhead.homed_axes` and
  `idle_timeout.state` (Moonraker omits what a printer lacks). Still no Klipper `gcodeState`: the site feeds that key
  into its plate-clear gate.
- **Bambu.** `stageCode` (`stg_cur`: 7 heating nozzle, 2 heatbed preheating, 13 homing, …; the idle sentinels −1/255
  and the boot-time 0 are omitted) with `stageSecondsLeft` (`stg_cd`), `layer` `[layer_num, total_layer_num]`,
  `nozzleTargetC` / `bedTargetC` / `chamberC` read from the flat floats (`nozzle_target_temper`, …) or, when a model
  lacks them, from the packed `device.extruder.info[].temp` / `device.bed.info.temp` / `device.ctc.info.temp` words
  of P2S/H2D firmware (current in the low 16 bits, target in the high 16), `preparing` (`gcode_state` `PREPARE`, which
  `state` still maps to idle), and `elapsedSec` from `gcode_start_time`. Fixed: progress, temperatures, `jobName` and
  the ETA were sent while the printer was offline, so stale cached values went out as if fresh.
- **Both.** `sampledAt` (unix ms): when the reading was taken, the Bambu adapter's last MQTT report or the Klipper
  adapter's last successful poll, so the site can say "updated 4 s ago" instead of dating everything by the heartbeat.

**Telemetry push.** A new thread POSTs coalesced per-printer deltas to `/api/print/hub/telemetry` every 5 s
(`MAKEROS_HUB_TELEMETRY_SEC`, or `telemetry_sec` in `config.toml`; `0` disables it, and the heartbeat then carries
status alone, as before). Values are rounded first (0.5 °C, 0.1 %, whole minutes), an unchanged printer is absent, an
idle printer is silent until its state moves, and a full snapshot of every connected printer goes out at start, after
any failure, when the site asks and every 60 s. One persistent HTTPS connection, the hub's bearer, 5 s timeouts; a 404
(a site without the route yet) is logged once and retried every minute. Nothing about cameras, commands or OTA rides
here, and the heartbeat's cadence, content and `nextPollMs` handling are untouched. The contract the site is built
against is [docs/telemetry-v1.md](docs/telemetry-v1.md).

**Wire contract (every field is additive; each side tolerates the other side at an older version).**

| Field | Direction |
| --- | --- |
| `printers[].sampledAt`, `preparing`, `nozzleTargetC`, `bedTargetC`, `chamberC`, `displayMessage`, `printDurationSec`, `elapsedSec`, `layer`, `stageCode`, `stageSecondsLeft` | hub → site, heartbeat |
| `POST /api/print/hub/telemetry` (docs/telemetry-v1.md) | hub → site, every 2–60 s |
| `{ ok, wantFull?, nextMs? }` | site → hub, telemetry reply |

## 0.69.0 candidate: fair camera capture, a forced update, nothing lost on a mid-print restart

Built on v0.68.0 with the v0.68.1 laserAck hotfix merged in, following pstation
`docs/camera-pipeline-plan-2026-10-15.md`.

**Cameras.**
- **Lanes.** Socket cameras each get their own thread, up to 16 at once: the A1s on :6000, Pi webcams, Moonraker.
  ffmpeg (RTSPS) cameras keep the 4 slots v0.68 had for everything. An ffmpeg capture starts only while the beat still
  has its full 5.5 s deadline left. Dead A1s can no longer hold the slots that the X1C, P2S, H2D and SV08 need.
- **Not started is not failed.** A capture that never started is not posted as a failure. It is counted, and it stays
  overdue, so it moves up next beat.
- **Most overdue first.** Inside a lane, captures start in order of how long each camera has been due: a state change
  or a camera with no photo yet first, then the rest by when their tier ran out. A camera that loses the race only gets
  more overdue, so wanted or first-layer cameras over the lane's capacity can no longer starve the mid-print ones. A
  failing camera counts as due only from its failed try or its breaker closing, so dead cameras take their breaker's
  share of the lane and never sit at its front. A state change stays first until a frame is accepted or the capture
  really fails, so losing one race does not hide it until its next tier. A reconnect resets the breaker and makes a
  failing camera due at once, ranked from the reconnect: a printer whose Wi-Fi flaps every beat cannot take the lane.
- **One capture per camera at a time.** A camera whose capture from an earlier beat is still running is skipped
  (counted in `cameraStats.stillRunning`), so stuck cameras can never stack threads. An HTTP snapshot has one 4 s
  deadline for its body (a camera trickling bytes no longer outlives it), and the A1's 5 s deadline covers every
  address its host resolves to. Limit: a camera trickling its HTTP headers or chunk framing can still hold its one
  worker past the deadline; it shows as failing, and it never takes a second thread.
- **The A1 deadline.** An A1 capture has one 5 s deadline for connect, TLS and a frame. Before, it was 4 s per step with
  no total. Its timeout is split by phase: `unreachable` (connect), `tls-error` (handshake), `timeout` (authed, then no
  picture).
- **New reasons.** `frozen` means the same bytes as the last accepted frame. `too-large` means over the site's
  1.5 MB cap. Neither frame is pushed.
- **The breaker.** One miss is retried next beat, whatever the camera's tier. From the second miss the camera backs off
  60, 120, 240, 480, then 600 s, capped at 120 s while printing; a failing camera stays due, paced by the breaker. It resets on a state change, a new MQTT connection (`mqttConnectedAtMs`: how a
  power-cycle shows up to the hub), a source change or a disable/enable.
- **Acknowledgement.** A frame counts as captured only after the site answers 200. 413, 415 and 422 count as failures.
  Pushes stop at the first 5xx or network error.
- **Heartbeat additions.** `cameraStats` (counters since the agent started), `piMetrics.tickWallMs` (now actually
  sent) and `piMetrics.cpuCount`.
- **Freshness hints.** The site's `cameraWant` list puts the printers it names on the 20 s tier. When it is absent,
  config-down's `cameraDense` is used, as before.
- **No camera path, no attempt.** A camera-enabled printer with no camera path (a Bambu without its access code) is no
  longer attempted and reported as failing every beat.

**Forced update.**
- With `MAKEROS_HUB_OTA_WAIT_IDLE=1`, an update target waits while a printer is preparing, printing or paused. The
  site (pstation 0141) now sends `targetVersionForce: true` beside the target once the operator presses **Update now,
  even while printing**, and only while the forced sha equals the armed one. The hub then skips that wait for that one
  target. `targetForceSha256` equal to `targetVersionSha256` is accepted too, the stricter form either side can move to.
- Nothing else is skipped: the version gate, the cooldown, the attempt cap and content trust all still apply. A force
  naming any other sha, or a flag that is not a literal `true`, is ignored.
- Since v0.54 the wait itself is opt-in. Without the variable, an update already applies mid-print.

**Nothing lost on a mid-print restart.**
- **The running Bambu print.** Its key, name, start, material and the printer's ETA are saved in
  `terminal-jobs/<printer>.json`, together with the last reported key, as one list entry that is not a job. The file
  keeps v0.68's list format: a rollback reads exactly the jobs, and its next save drops the entry, so a print the
  rollback went on to report never comes back after a later upgrade. A print that finished while the hub was down is
  now closed under the key the site already holds, ending at the printer's last ETA (no duration is sent when the
  printer gave none), and its queue job is completed. A LAN print has no task id, so before this it was never reported
  and its job was held as `outcome_unknown_after_restart`. A print already reported is not re-sent after a restart.
  A same-file print still running more than 30 minutes past the saved print's ETA may be a re-print started during
  the outage, so it is not taken for the saved print. After any such drop, every print that opens, or is found
  already finished, is marked `notDispatched` until the hub durably records its next dispatch (a start whose clear
  cannot be saved is refused), so no walk-up or re-print can complete the old queue job:
  it is held for staff instead of being closed by the wrong print. A marked print's report is held back until that
  old job's `held` has been delivered, because the site matches an unbound finished print to the printer's
  `printing` job by time; the site ignores the `notDispatched` key itself. A report with neither a real name (`subtask_name`; the
  generic `gcode_file` plate path counts for nothing) nor a task id never decides which print a saved one was: the hub
  waits for the first report that names it, and for a saved print the printer gave a task id, for that id. An MQTT reconnect handles the running print the same way: the cached
  name and task id are dropped, and the print is confirmed again by the new connection's reports. A print whose task id matches the saved one is
  always kept, whatever name a partial report gives it. The
  ETA is kept current while a print runs or is paused, so a restart short of that never drops a print. A failed write
  is retried on the next report instead of being remembered as saved.
- **Commands.** The command guard (`dispatched-commands.json`) and undelivered command results
  (`command-results.json`) are durable. The site re-sends a command after 10 minutes without a result, and that re-send
  no longer runs a pause or a dryer cycle twice. Undelivered results are cleared only after the heartbeat that carried
  them, and only the 64 that heartbeat carried.
- **A finished dispatch is never started again.** A queue job that completes or is held keeps the manager's guard for
  that assignment (job and `assignmentSeq`) for 24 hours instead of dropping it. After a crash before its
  `uploading` report, the site still holds the job `assigned` and re-sends it in the very beat that resolves it; that
  re-send used to start the print a second time (present since v0.58). A re-queued job (a new seq) still dispatches.
  The guard is written durably before the adapter persists the resolved dispatch, so a second crash between the two
  cannot leave neither.
- **LAN prints and task ids.** A print counts as id-less (a LAN print) once it has reported task id "0" for 5 minutes
  of running. Only then is a task id seen across a restart or reconnect treated as another print's. A real id that
  lands a few frames after the start (the hub's own starts send "0") never splits the print.
- **Fresh connections.** A Bambu printer that has not reported its state on the current MQTT connection is refused a
  start (`printer_stale`). Right after a restart or a reconnect it reads as idle, or as its state from before the drop,
  even when it is printing. The adapter itself also refuses a printer whose fresh report says it is preparing,
  printing or paused (`printer_busy`), before the upload and again just before the start goes out.
- **Update attempts.** The cooldown and attempt record (`last_update.json`) is now fsynced with its directory, so a
  power cut cannot lose an attempt and let a broken release past the cap.
- **A backlog of finished prints.** A heartbeat carries at most 256 terminal reports, and its 200 now acknowledges
  only those 256. Before, a backlog past 256 after a long outage (tens of printers) was acknowledged in full and the
  rest deleted unsent.

The whole state audit, the force procedure and how to confirm a clean return are in
[docs/hotfix-runbook.md](docs/hotfix-runbook.md).

**Wire contract (every field is additive; each side tolerates the other side at an older version).**

| Field | Direction |
| --- | --- |
| `POST /api/print/hub/camera?…&failed=1&reason=<r>` | hub → site |
| `printers[].mqttConnectedAtMs`, `piMetrics.tickWallMs`, `piMetrics.cpuCount`, `cameraStats` | hub → site, heartbeat |
| `cameraWant: string[]` (≤ 64 printer ids) | site → hub, heartbeat response |
| `targetVersionForce: true` (pstation 0141), or `targetForceSha256` equal to `targetVersionSha256` | site → hub, heartbeat response |

**Capacity: tens of printers and Pi webcams on one shop box.** These figures are estimates. `cameraStats` and
`tickWallMs` measure them from the first 0.69 beat.
- **ffmpeg, the CPU cost.** At most 4 run at once, the same peak as v0.68. A slot starts a capture only while 5.5 s of
  the 8 s beat are left, so the lane delivers about 8 RTSP frames per beat at roughly 2 s per capture. That covers
  about 18 RTSP printers mid-print (one frame per 90 s each), or 8 on the 20 s tier at the same time. Beyond that,
  `notStarted` rises and every RTSP photo gets older in proportion, most overdue first, so none starves: with N RTSP
  cameras due every beat, each gets a frame about every N/8 to N/4 beats (12 due: every 1.5 to 3 beats). A capture that
  never started is never posted as a failure. A camera whose last post was a failure keeps showing "Capture failed"
  on the site until its next turn, which comes within those few beats, far inside the site's 15-minute hint. To relieve CPU,
  lower `FF_SLOTS` in `printers/camera.py` (when `ffMs` max nears 5.5 s, or `childCpuMs` climbs during builds). To get
  more frames, raise it only once `cpuCount` and `loadAvg1m` show headroom.
- **Socket cameras.** These are almost free on CPU: the threads wait in `recv`. Up to 16 run at once, and the rest queue
  inside the beat. A healthy Pi snapshot takes about 0.1 to 0.3 s, so 16 threads clear well over a hundred per beat. A
  dead camera holds a thread for at most 5 s, about 6 times an hour.
- **Pushes to the site, the next ceiling.** Frames go to the site one after another, each a fresh HTTPS POST of
  roughly 0.1 to 0.5 s, so the beat grows with frames per beat. At 40 printers mid-print that is about 18 frames, or 2
  to 9 s. Idle printers add little (one frame per 600 s each). Watch `tickWallMs`. The fixes, held back until it is
  needed, are keep-alive and raw bytes instead of base64.
- **Memory.** Each frame is held in base64 (about 270 KB for a 200 KB JPEG) until the beat ends: about 10 MB at 30
  frames, against the service's 600 MB cap.

## 0.68.1 candidate: laser runs and file verdicts kept until the website names them

A hotfix to 0.68.0 (codex release review of pstation `aa9433b`). 0.68.0 dropped every closed cutting run and file
verdict a heartbeat carried as soon as that heartbeat got a 200, so a run or verdict the website failed to write was
lost for good. The agent now drops only what the response's `laserAck` (`runKeys`, `fileIds`) names, and keeps the
rest for the next beat. A website that sends no `laserAck` at all (one from before it), or one malformed in any part, has nothing dropped: everything
is re-sent, and the website stores it by key, so nothing doubles. What a website that never acknowledges can hold the
agent to is bounded by the existing caps: 32 closed runs per laser (31 while one is open) and 32 file verdicts, oldest
dropped first. Nothing else changed.

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
and reports them on the heartbeat until the cloud has them; open and unacked runs are saved to
`/var/lib/makeros-hub/laser-runs.json`, so a restart loses none. It polls the laser gate's `/status` and has the gate
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

### v0.73: members' paper prints on the shop's Epson (2026-10-18)

The site meters paper against the membership allowance (100 pages a month, 20 in colour, a dense page counts twice). The
heartbeat says `paperReady` when /opt/ps-paper/run-paper is installed (oes-pool-deploy `scripts/install-paper.py`); the
site's answer may then carry `paper: {host, jobs}`. For each job, one at a time and off the heartbeat thread
(`makeros_hub/paper.py`): fetch the file, measure and render it in the sandboxed `ps-paper` container (`paper/`: no
network, read-only, capped; Ghostscript inkcov for each page's ink, PWG raster at 300 dpi), POST the coverage to
`/api/print/hub/paper`, and only on `{"print": true}` send the raster to the Epson over IPPS (Print-Job) and follow it with
Get-Job-Attributes to completed or aborted (20 minutes at most), then POST the outcome. Tests: `tests/test_paper.py`.

Also in v0.73: **checking in when there is work.** The heartbeat stays every 30 s, but `makeros_hub/wake.py` holds one
long request open on `/api/print/hub/wait?since=<seq>`; the site answers at once when a job, a ready file, a cleared plate,
a Pause or Stop, or a paper print appears for this box, and the loop's sleep ends early so the hub checks in within a
second or two. Only new work rings it. A site without the route is left alone for ten minutes. Tests: `tests/test_wake.py`.
