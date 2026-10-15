# Hotfix runbook: updating the hub while printers are printing

From hub v0.69.0, `/admin/hub` has **Update now, even while printing** next to the update target. This page covers what
to check before forcing, what forcing does, and how to confirm the hub came back clean.

A hub restart does not stop a print. The printers keep printing from their own storage. What a restart interrupts is
the hub *watching* them, for the 30 to 90 seconds the install takes. Everything the hub needs in order to report those
prints afterwards is on disk (the table below), so a print that finishes during the gap is reported once, to its own
queue job, after the restart.

## Caveats (read these before anything else)

1. **Forcing skips only one rule.** That rule is "wait while a printer is preparing, printing or paused", and the force
   skips it for one target only. Everything else still applies. The version must be newer. The tag must resolve to the
   commit sha set on `/admin/hub`. The 15-minute cooldown and the 3-attempt cap still hold. A force left over from an
   earlier target is ignored: the site sends the force only while the forced sha equals the armed one, and saving a
   target clears it.
2. **The waiting rule is opt-in on the hub.** Since v0.54 the agent waits for idle only when its service environment
   sets `MAKEROS_HUB_OTA_WAIT_IDLE=1`. Without that setting it already updates mid-print, and the force changes
   nothing. Whether the shop box sets it can be seen only on the box (`systemctl show makeros-hub -p Environment`),
   which is Marshall's root console. A target that sits armed while printers print, with the agent version unchanged,
   means the hub is waiting.
3. **Members uploading at that moment have to send again.** An OrcaSlicer upload that is mid-transfer to a virtual
   printer during the restart fails in the slicer, and the member re-sends it. A capture that already finished is safe:
   it is in the on-disk outbox and spool.
4. **A laser Load in flight is lost.** A `laser_load` that is mid-transfer to the gate when the hub stops gets no result.
   After 5 minutes the site fails it as `no_result` and the member presses Load again. Avoid forcing while someone is
   loading a laser file.
5. **A rollback is a new release** (the hub never downgrades). See "If it did not come back clean" below.

## Before forcing

Every check below is read-only. SQL runs through `read sql pstation "<one select>"` on the pool door, as
`agent_reader`.

1. **Make sure the release is the one you mean.** The tag on `/admin/hub` must be the reviewed release. Its sha must be
   the release commit, its full suite must have passed in the network-less container, and its README section must say
   what changes.
2. **See what is printing now** and keep the result to compare against afterwards:
   ```sql
   select m.name, s.state, s.gcode_state, j.id as job, j.status, j.hub_job_ref
   from machines m join machine_status s on s.machine_id = m.id
   left join print_jobs j on j.machine_id = m.id and j.status in ('assigned', 'printing')
   where m.hub_id is not null and m.retired_at is null order by m.name
   ```
3. **Avoid two moments.** One is a job in `assigned` that has not reached `printing` yet: the hub has just sent it, and
   the printer is heating. The restart is safe even then (see "Mid-dispatch" below), but the job reaches `printing`
   one beat later. The other is a member at the laser loading a file (caveat 4).
4. **Check the box has disk space**, because state writes need it. If the hub's diagnostics show `could not persist` or
   `not durable` lines, fix that first. Without disk space nothing new can be made durable.

## Forcing

1. On `/admin/hub`, confirm the target tag and sha, press **Update now, even while printing**, and confirm. The site
   records who forced which target in its events.
2. On its next heartbeat (within about 30 s) the hub receives `targetVersionForce: true` beside the target. It logs
   `OTA: target vX.Y.Z @ <sha> FORCED while a printer is busy`, records the same line in its diagnostics, and runs
   the root update script.
3. The service stops, installs and starts again. Expect no heartbeat for 30 to 90 seconds. That shows up as one
   `hub_gap` event, then a `hub_agent_version` event.

## Confirming a clean return

1. **The hub runs the new version and beats again:**
   ```sql
   select agent_version, last_heartbeat_at, now() - last_heartbeat_at as age from hubs
   ```
   `agent_version` must be the target without the `v`, and `age` must be under a minute.
2. **Every printer reports again:** run the query from "Before forcing" step 2 again. Each printer that was printing
   still shows `printing` with the same job and the same `hub_job_ref`.
3. **Nothing was left hanging and nothing was doubled:**
   ```sql
   select at, kind, machine_id, job_id, reason from fleet_events
   where at > now() - interval '30 minutes'
     and kind in ('job_held', 'job_completed', 'print_recorded', 'print_double_billed', 'job_failed')
   order by at
   ```
   - Expect no `print_double_billed`.
   - A `job_held` with reason `outcome_unknown_after_restart` means a printer was reset or showed a different print
     after the restart. The hub never invents an outcome, so a person decides that job.
   - A `job_held` with `start_not_observed` means a start never reached the printer. Staff re-queue it.
4. **For each print that finishes after the restart:** it gets exactly one `print_recorded` and one `job_completed`
   for its own job. A print that finished *during* the gap shows them within one or two beats of the hub coming back.
5. **Commands came through:** a pause, resume, stop or dry sent just before the restart has a result in
   `hub_printer_commands` (status `done` or `failed`), and it did not run twice.

## What is kept across a restart

| State | File under `/var/lib/makeros-hub/` | After a restart |
| --- | --- | --- |
| Which queue job each printer is running (dispatch records) | `queue-progress/<printer>.json` | The job links to its print. A re-sent assignment is answered without a second upload or start. |
| Queue jobs already sent (the manager's guard) | `dispatched.json` | Same as above, one level up. |
| Queue status reports not yet delivered | `queue-reports.json` | Sent on the next beat. The site ignores a repeat. |
| Finished prints not yet acknowledged | `terminal-jobs/<printer>.json` | Sent on the next heartbeat. |
| **The print running now, and the last one reported (Bambu, v0.69)** | inside `terminal-jobs/<printer>.json`, one entry that is not a job | A print that finished during the gap closes under the key the site already holds, ending at the printer's last ETA. Without this, a LAN print, which carries no task id, was never reported. A print that was already reported is not re-sent. |
| The print running now (Klipper) | inside `terminal-jobs/<printer>.json` | Same, since v0.68. |
| **Commands already run (v0.69)** | `dispatched-commands.json` | The site re-sends a command after 10 minutes without a result. It is answered `ok` without running it again, so a pause is not repeated and a dryer cycle is not restarted. |
| **Command results not yet delivered (v0.69)** | `command-results.json` | Sent on the next beat. Before this, the site's row stayed `sent`. |
| Laser runs, open and closed | `laser-runs.json` | A run open at the restart closes at its last cutting reading. |
| Captured member jobs, and the spool | `spool/vp-outbox/`, `spool/` | Re-submitted. The site deduplicates. |
| Update attempts | `last_update.json` | The cooldown and attempt cap survive, so a broken release cannot loop. |

Not kept, on purpose:
- **Camera timing and backoff.** After a restart every camera is due once, and a dead camera backs off again within two
  failures. Costs a few extra captures, nothing else.
- **Failure-watch smoothing.** It starts cold by design.
- **Probe results.** A diagnostic, re-run from the admin page.
- **Uploads in flight** (caveats 3 and 4).
- **Unacknowledged finished prints of a printer removed from the site in the same beat.** The file stays on disk, but
  nothing reads it once the printer is gone. The window is one heartbeat.

## Mid-dispatch

A restart at any point of a dispatch never starts a print twice. The hub saves the dispatch record before the start
command leaves the box. After a restart, the site's re-sent assignment finds that record (or the manager's guard) and
is answered `uploading` with nothing sent. If the box died before the command left, the printer stays idle. Two
minutes after its first report, the job is held as `start_not_observed` for staff to re-queue. It is never retried
silently. From v0.69 the hub also refuses to start a printer that has not yet reported its state since the hub
connected (`printer_stale`). Right after a restart, a printer can look idle for a few seconds while it is in fact
mid-print. The site re-queues that refusal without counting it.

`tests/test_v069.py` covers these restart cases, and `tests/test_klipper_dispatch.py` covers the Klipper ones.

## If it did not come back clean

- **No heartbeat after 3 minutes:** the install failed and the previous code was restored, or the service is
  crash-looping. The box's journal (`journalctl -u makeros-hub -n 200`) is on Marshall's root console. Hand him that
  exact command, and nothing more.
- **A wrong release:** cut a fixed release (a rollback is v0.68.0's tree with the version bumped), set its tag and sha,
  and force it the same way. A rollback is safe for the saved prints: v0.68 reads the terminal file's jobs and drops the
  saved running print the first time it saves, so a print it then reports cannot be reported again after a later
  upgrade. Across the rollback's own restart, v0.68 behaves as v0.68 always did: a print still running is reported
  under a new key, and one that finished during the restart with no task id is held for staff.
- **Never delete state files to clear an alarm.** They are what keeps a print reported and billed once.
