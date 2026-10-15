# Hub 0.70.0 release review — 2026-10-15

Reviewed `822875f..a8d44cb` against telemetry-v1, the site heartbeat, the saved live-status research, and the adapter/manager/OTA callers. Production was independently confirmed at site `bfc9378`, hub `0.69.0`. This is the hub portion of Part A, not the whole-system verdict.

## Corrected before release

| Severity | Failure | Fix and evidence |
| --- | --- | --- |
| High | A never-connected adapter entered a delta without required `sampledAt`; strict site validation could reject the healthy printers in that same batch. | Omit never-observed adapters from telemetry; the heartbeat still carries their connection failures. `makeros_hub/telemetry.py:89`, regression `tests/test_v070_review.py:11`. |
| Medium | An H2D report missing the active nozzle borrowed the inactive nozzle's temperature, potentially showing a cold nozzle while the active one was hot. | Omit the unknown reading. `makeros_hub/printers/bambu_parse.py:106`, regression `tests/test_v070_review.py:22`. |
| Medium | Newer `stage._id` reports lost their phase. | Read that layout when `stg_cur` is absent. `makeros_hub/printers/bambu_parse.py:161`, regression `tests/test_v070_review.py:29`. |
| Medium | A version response refreshed the age of cached print telemetry. | Track the last print-status report separately from connection traffic. `makeros_hub/printers/bambu.py:197`, regression `tests/test_v070_review.py:33`. |
| Medium | MQTT could mutate the shared report after its timestamp was read, mixing different samples in one response. | Copy the report under its existing lock. `makeros_hub/printers/bambu.py:290`, regression `tests/test_v070_review.py:44`. |
| Medium | A slow metadata GET delayed publishing a successful Klipper poll and dated the old reading at the end of the delay. | Record the poll before fetching metadata. `makeros_hub/printers/klipper.py:734`, regression `tests/test_v070_review.py:57`. |
| Low | Failed HTTP responses reused a connection despite the documented reconnect rule, including potentially undrained oversized bodies. | Close it on every rejected response. `makeros_hub/telemetry.py:233`, regression `tests/test_v070_review.py:74`. |
| Medium | A nonfinite `nextMs` raised during interval conversion and interrupted a flush after acceptance. | Require finite numbers. `makeros_hub/telemetry.py:73`, regression `tests/test_v070_review.py:83`. |

The packed temperature layout and newer stage shape were checked against the parser implementation in [ha-bambulab models.py](https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/models.py), as well as the saved H2D fixture. The P2S heating fixture is synthetic, not a new shop capture. Live heating values still need comparison with the printer display after activation.

## Verification

- All 52 existing test files ran individually: 1,040 passed, 30 skipped. Optional inference/platform checks account for the skips.
- All eight new regression tests fail against unmodified `a8d44cb` and pass with these fixes.
- Re-ran affected adapter, agent, dispatch, hardware, lifecycle, version and protocol suites after the changes. They pass.
- A rerun exposed a nondeterministic test: the manager lifecycle case injected samples by hand while its background HTTP poller could replay an older sample. That specific case now mocks polling startup and still exercises the real adapter, persistence, reconciliation, rescue and acknowledgement. Production calls `_record_poll` only from the poll loop; separate HTTP polling tests remain active. Evidence: `tests/test_klipper_dispatch.py:502` and `makeros_hub/printers/klipper.py:734`.
- Built the Python wheel successfully. The first packaging attempt lacked the build backend in the existing venv; isolated build dependencies resolved that without changing runtime dependencies.
- No live print, heater, door, billing or OTA command was sent. No new third-party runtime dependency.

## Doctrine assessment of this change

| Rule | Grade | Evidence |
| --- | --- | --- |
| R1.1 | Pass | Reuses adapter status and hub bearer; separate thin push preserves the heartbeat (`makeros_hub/agent.py:1584`, `makeros_hub/telemetry.py:216`). |
| R2.9 / R2.10 | Pass | No new stored credentials or connection strings; bearer stays in the request header (`makeros_hub/telemetry.py:219`). |
| R3.2 / R3.4 / R3.5 | N-A | No pricing, charge or ledger changes; lifecycle regressions rechecked. |
| R4.6 | Pass for changed response handling | Reply must be an object with `ok: true`; numeric interval must be finite (`makeros_hub/telemetry.py:73`, `:248`). This is not a certification of every pre-existing Python HTTP parser. |
| R4.8 / R4.9 | Pass | Telemetry errors are logged and confined to a separate thread (`makeros_hub/telemetry.py:259`, `:278`). |
| R5.3 / R6.12 | Pass | Concrete status defects reproduced before fixing; unknown nozzle readings are omitted, not guessed (`tests/test_v070_review.py`). |
| R7.6 / R7.16 | Pending operator approval | Release tag publication and the production OTA target remain gated by the release instructions. |
| R7.7 | N-A to hub packaging | Hub OTA pins tag plus full commit SHA; the site's image still requires build → preview → production intent. |

## Remaining limits and follow-through

The hub can publish before the site route: it backs off a missing route to one request per minute. This release alone does not make the Floor update every five seconds. The site telemetry/SSE release is next and must verify shared process state, disconnect cleanup, ownership, bounded payloads, persistence, and stale-clock behavior in preview.

The single `sampledAt` dates the latest merged status report, not each individual field; unchanged Bambu fields come from the printer's delta protocol. Klipper slicer metadata remains cached by filename; replacing a file under the same name may retain its old estimate. The separate whole-system audit must assess that cache invalidation and the telemetry loop's lack of aggregate `processed/expected` logging (R4.5). No live journal or hardware probe was performed in this review.

Release lane: after Marshall approves, publish `v0.70.0` and its release branch to the sovereign Git server, verify the public mirror's peeled commit, then give Marshall the full SHA for `/admin/hub`. Do not force an OTA during printing.
