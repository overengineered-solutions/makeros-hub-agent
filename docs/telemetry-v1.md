# Telemetry contract v1: hub → site status deltas

From hub v0.70.0 the agent pushes per-printer status deltas every few seconds to the site, beside the 30 s heartbeat.
The site is built against this file. The heartbeat is unchanged in cadence and content apart from carrying the same
new per-printer keys (so the database row gets them even when telemetry is off).

Why: the operator, 2026-10-15: "it says printing but... it also looks like its been sitting there for a long time, same
with the bambu printers is there a way to give more granular information, like maybe its heating up etc. Maybe we show
current temp vs target temp" and "these waits are killer i feel like i want you to build the mqtt connection now so we
dont have to wait on hub heart beats". The accepted design (pstation `reports/Live printer status design.md`) keeps the
heartbeat for commands, dispatch, cameras and OTA and adds this thin push. It is not a broker and not WebSockets; MQTT
stays the upgrade path.

## Request

`POST <cloud base>/api/print/hub/telemetry`, the same bearer and headers as the heartbeat
(`Authorization: Bearer <hub credential>`, `Content-Type: application/json`, `Accept: application/json`), on one
persistent `http.client` HTTPS connection the hub keeps open between sends and re-creates after any error. Timeouts
are 5 s.

```json
{
  "v": 1,
  "sentAt": 1760540405123,
  "full": false,
  "printers": [
    {
      "printerId": "<uuid>",
      "sampledAt": 1760540404980,
      "connected": true,
      "state": "printing",
      "gcodeState": "RUNNING",
      "stageCode": 7,
      "stageSecondsLeft": 42,
      "displayMessage": "Bed soak 20 min before calibration",
      "preparing": false,
      "progressPct": 15.0,
      "etaMinutes": 65,
      "printDurationSec": 1212,
      "elapsedSec": 1800,
      "layer": [12, 80],
      "nozzleC": 180.5,
      "nozzleTargetC": 220.0,
      "bedC": 60.0,
      "bedTargetC": 60.0,
      "chamberC": 35.0,
      "jobName": "Clamshell Parts Box"
    }
  ]
}
```

| Key | Type | Source |
| --- | --- | --- |
| `v` | `1` | contract version |
| `sentAt` | unix ms | when the hub built the body |
| `full` | bool | see *Full and delta* |
| `printers[].printerId` | string (uuid) | the site's machine id, as in the heartbeat |
| `printers[].sampledAt` | unix ms | the newest reading used: the Bambu adapter's last print-status report (not a version reply or command acknowledgement), the Klipper adapter's last successful poll before any metadata lookup. Never-observed printers are omitted from telemetry; their connectivity still rides the heartbeat |
| `connected` | bool | the heartbeat's `connectionState == "connected"` |
| `state` | `idle` / `printing` / `paused` / `offline` / `error` | the heartbeat's activity state; `offline` or `error` when not connected |
| `gcodeState` | string, max 32 | Bambu only, the raw `gcode_state` (`RUNNING`, `PREPARE`, `FINISH`, …); the site's plate-clear gate reads it, so Klipper never sends one |
| `stageCode` | int | Bambu `stg_cur`, falling back to `stage._id` on newer firmware. Omitted for the idle sentinels (`-1` on X1, `255` on P1/A1), and `0` ("printing") is omitted unless `gcode_state` is `RUNNING`, `PAUSE` or `PREPARE` (at boot the printer reports 0 where it means idle; after a print it keeps the last value). Unknown codes (the H2D/P2S-era 59–77) are forwarded as is: show them as working |
| `stageSecondsLeft` | int ≥ 0 | Bambu `stg_cd`, the countdown of the current stage; only beside `stageCode` |
| `displayMessage` | string, max 120 | Klipper `display_status.message` (the M117 / `SET_DISPLAY_TEXT` text the start macro sets); omitted when empty |
| `preparing` | bool | Klipper: `print_stats.state == "printing"` and `print_stats.print_duration == 0` (the start routine: soak, home, QGL, mesh — Klipper counts print time from the first extrusion). Bambu: `gcode_state == "PREPARE"`. Present while connected (Klipper: whenever Moonraker reports `print_stats`) |
| `progressPct` | number 0–100 | Klipper: `display_status.progress × 100` (the slicer's M73 value; OrcaSlicer writes `M73 P0` as the file's first executable line, so it stays 0 through the start routine), `virtual_sdcard.progress` only when `display_status` is absent. Bambu: `mc_percent` |
| `etaMinutes` | int or null | Klipper: nothing until `print_duration > 0`; then `estimated_time − print_duration` from the file's metadata when known (one `GET /server/files/metadata` per new filename, cached), else `print_duration × (1 − p) / p`. Bambu: `mc_remaining_time` (minutes) |
| `printDurationSec` | int | Klipper `print_stats.print_duration` |
| `elapsedSec` | int | Klipper `print_stats.total_duration`; Bambu `now − gcode_start_time` while a print is under way |
| `layer` | `[current, total]` | Klipper `print_stats.info.current_layer/total_layer` (filled by `SET_PRINT_STATS_INFO`; OrcaSlicer's `CURRENT_LAYER=[layer_num]` is zero-based); Bambu `layer_num/total_layer_num`. Omitted until a total is known |
| `nozzleC`, `nozzleTargetC` | number °C | Klipper `extruder.temperature/target`. Bambu: flat `nozzle_temper/nozzle_target_temper`, each falling back to the packed `device.extruder.info[].temp` of the active extruder (low 16 bits current, high 16 target) on P2S/H2D firmware; omitted when the active extruder or its reading is unknown |
| `bedC`, `bedTargetC` | number °C | Klipper `heater_bed.temperature/target`; Bambu flat `bed_temper/bed_target_temper`, falling back to packed `device.bed.info.temp` |
| `chamberC` | number °C | Bambu `chamber_temper`, falling back to the low word of `device.ctc.info.temp`; never Klipper, never A1/P1 (no sensor) |
| `jobName` | string, max 300 | Klipper `print_stats.filename`; Bambu `subtask_name`, else `gcode_file` |
| `toolhead` | `{homed, x?, y?, z?}` | v0.76, Klipper only: `homed` is `toolhead.homed_axes` (`"xyz"`, `""` after `M84`), `x`/`y`/`z` the commanded `toolhead.position` in mm, rounded to 0.01 (the frame the hub's jog range check uses) |
| `speedPct` | int | v0.76, Klipper `gcode_move.speed_factor × 100` (the M220 value) |
| `zOffset` | number mm | v0.76, Klipper `gcode_move.homing_origin[2]` (the `SET_GCODE_OFFSET` Z), rounded to 0.001 |
| `fanPct` | int 0–100 | v0.76, Klipper part fan: `fan.speed × 100`, else `fan_generic fan0` (the SV08's M106 macro drives fan0 and fan1 together) |
| `light` | bool | v0.76, Klipper `output_pin main_led.value > 0`; omitted on a printer without that pin |
| `hold` | `{minutes, ceilingMinutes}` | v0.82, Klipper: the printer's heater hold (`gcode_macro _pstation_hold`, pstation `printers/*/config`): a print stopped on an error or paused keeps the bed warm, the nozzle off and the motors on. `minutes` held so far, `ceilingMinutes` when the printer turns everything off itself. Present only while `active` is true; a transition key, so it starts and ends at once. The heartbeat (not this push) also carries `heaterHold: true` on a printer that runs the hold, and a Klipper error's own words as `errorReason` |

Every key except `printerId` and `sampledAt` is omit-when-unknown. The heartbeat carries the same values under the
same names, except that the heartbeat keeps its historical `nozzleTempC` / `bedTempC` for what this contract calls
`nozzleC` / `bedC`.

## Full and delta

Values are rounded before they are compared or sent: temperatures to 0.5 °C, `progressPct` to 0.1 %, `etaMinutes`
to whole minutes, `printDurationSec` and `elapsedSec` to whole seconds.

- A **delta** (`"full": false`) carries only the printers whose rounded values changed since the last send the site
  accepted, and within a printer only the changed keys plus `printerId` and `sampledAt`. A key the hub no longer knows
  (the M117 message cleared, the ETA gone) is sent as `null`, so the site can clear it. A printer that is not printing,
  paused or preparing is silent while only its temperatures drift; it is sent the moment `connected`, `state`,
  `gcodeState`, `preparing` or `jobName` moves (so a printer going offline, finishing — `gcodeState` `FINISH` — or
  starting is never missed), and from v0.76 when `toolhead` or `light` moves (a jog, homing or the light from the
  screen). A delta with no printers is not sent at all.
- A **full** (`"full": true`) carries every connected printer with every known key. It is sent at start, after any
  non-2xx or transport error, when the site asks (`wantFull`), and at least every 60 s.

## Reply

`200 { "ok": true }`. The site may add:

- `"wantFull": true` — the next send is a full.
- `"nextMs": <ms>` — the send interval from now on, clamped by the hub to 2000–60000 (default 5000, config
  `MAKEROS_HUB_TELEMETRY_SEC`).

Anything else counts as a failure and the next send is a full: a non-2xx status, a transport error, a 200 whose body
is not a JSON object with `"ok": true` (an empty or proxy 200 is not acceptance; the heartbeat has the same rule).
A **404** means the site does not have the route yet: the hub logs it once, retries every 60 s and never crashes, so
hub 0.70 can ship before the site's route does.

## What does not ride here

Cameras, commands, OTA, jobs, queue reports: all of that stays on the 30 s heartbeat, whose handling of `nextPollMs`
is untouched. Nothing in this push identifies the printer beyond the site's own `printerId` (no serial, IP or access
code, the heartbeat's rule).

## Hub-side knobs

- `MAKEROS_HUB_TELEMETRY_SEC` (env) or `telemetry_sec` in `config.toml`: the cadence in seconds, default 5. `0`
  disables the push; the heartbeat then carries status alone, as before 0.70. Values under 2 run at 2.
- The push runs on its own daemon thread (`telemetry` in `makeros_hub/telemetry.py`), reads the adapters' status once
  a second, and never raises into the heartbeat loop. Any failure is logged (the first at WARN, repeats at DEBUG).
