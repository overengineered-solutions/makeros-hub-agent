# Orca 2.4.2 Moonraker queue door

This is the hub half of the contract, unreleased. The site additions below are required before connection tests succeed.
An old/unavailable site fails closed; neither the session check nor an upload creates a job. Nothing in this HTTP door sends
commands to a printer. The normal cloud queue and existing final dispatch guards own physical printing.

## Preset addresses

Use the **hub's member-reachable hostname**, never a physical printer's address. Fixed defaults (independent of `ingest_port`):

| Model pool | Exact `print_host` form | `use_3mf` |
| --- | --- | --- |
| `sv08-max` | `http://<hub-host>:8810` | `0` |
| `giga` | `http://<hub-host>:8811` | `0` |
| `p2s` | `http://<hub-host>:8812` | `1` |
| `a1-mini` | `http://<hub-host>:8813` | `1` |
| `x1c` | `http://<hub-host>:8814` | `1` |

Set `host_type=moonraker`, `printer_agent=moonraker`, and `printhost_apikey` to the member's existing **print token**.
Leave `printhost_port` empty (or matching the inline port); set `print_host_webui` to the site's member print page.
Use a non-Bambu-vendor PS preset for Moonraker filament sync. P2S needs the stage-1 preset; H2D is deliberately unsupported.
The original OctoPrint endpoint remains on the configured ingest port (default 8787). Change model ports only in the
optional `[moonraker_ports]` table in `config.toml`; omitted models keep the defaults. Example:

```toml
[moonraker_ports]
sv08-max = 8810
giga = 8811
p2s = 8812
a1-mini = 8813
x1c = 8814
```

Restart the hub after a reviewed config change, and update the matching Orca presets, userspace forwards and operator-only
Headscale ACL together. The setup build currently writes the default P2S port, 8812; it does not discover custom ports.
A custom ingest port never shifts this map. Port 0 is reserved for local test sockets, not an operator configuration.
A failed model bind logs an ERROR with model, port and fixed OS error code, skips that model, and continues binding the
others. It never closes the existing member ingest listener or a successful model listener. Fix the collision and restart
the hub to retry. Each heartbeat carries `moonrakerListeners`, for example
`[{"modelPool":"p2s","port":8812,"state":"failed","error":"EADDRINUSE"}]`; successful entries have `state:"listening"`
and no error. Site Build A validates these entries into `hubs.last_metrics` and shows failures on the admin hub health line.
No credentials, member identities or exception text are included. A failed ingest bind remains a separate ingest failure.

### Existing port inventory (offline, 2026-10-29)

**8810–8814 is unused in the checked-in sources below. Live socket availability is unverified.** No shop, printer, preview
or production connection was made. This inventory includes other hosts, container-only ports, disabled defaults and probe
candidates conservatively; it does not claim they all listen on the shop box. `platform/` below means
`oes-pool-deploy` (also checked against the supplied `oes-pool-deploy-doorports` checkout). Scanned hub code/docs/config,
`platform/network/shop.json`, every `scripts/install-*.py` and `box-*.py`, `apps/`, `cron/`, plus pipeline, Headscale and
Compose service definitions. Cron adds no listener; its jobs call existing HTTPS services. The fixed-range test parses
this whole table and rejects any overlap.

<!-- existing-port-inventory -->
| Existing/reserved ports | Use and checked-in evidence |
| --- | --- |
| `22,2022` | Shop/pool SSH and forced-command doors; `platform/AGENT-OPERATIONS.md`, `scripts/install-agents-door.py`, `install-box-lane.py`. |
| `53,67,68` | Gateway DNS/DHCP (UDP and DNS TCP); `platform/network/shop.json`. |
| `80,443` | HTTP/HTTPS proxies, apps, UniFi console/door, Headscale coordination and DERP; `platform/compose.yaml`, `apps/`, `network/shop.json`; Klipper HTTP proxy in hub README. |
| `322,6000` | Bambu printer camera RTSPS/TLS destinations; `printers/rtsp_camera.py`, `bambu_camera.py`. |
| `554,1883,8000,8081,9000` | LAN camera/MQTT/web probe candidates, not confirmed listeners; `platform/pipeline/agents-door.py:PROBE_PORTS`. |
| `631` | Paper printer IPPS destination; `makeros_hub/paper.py` (other device). |
| `990,8883,50000-50009` | Virtual printer FTPS, MQTT TLS and passive data; `vprinter/manager.py`, `platform/headscale/acl.hujson`, `network/shop.json`. |
| `1900` | Obsolete SSDP firewall rule, removed by `platform/scripts/box-firewall-tidy.py`; reserved to avoid reusing an old service port. |
| `1984,8555` | go2rtc API/WebRTC defaults explicitly disabled by `platform/scripts/install-camera-gateway.py`. |
| `1990,2021` | Bambu discovery multicast/broadcast UDP; `bambu_ssdp.py`, `vprinter/manager.py`, platform firewall notes. |
| `3000,3002` | Virtual printer bind services; `vprinter/manager.py`, `platform/network/shop.json`; app containers also use 3000 (`apps/`). |
| `3478,41641` | STUN/WireGuard UDP; `platform/headscale/config.yaml`, `headscale/RUNBOOK.md` firewall record. |
| `5000` | Registry on pool/loopback; `platform/pipeline/shipyard-trust.sh`, `apps/`. |
| `5432` | Postgres container; `platform/compose.yaml` (private network only). |
| `7125` | Physical Klipper Moonraker destination; `lan_scan.py` (not this queue door). |
| `7650` | Laser gate HTTP loopback/private listener; `docs/laser-gate.md`, `config.toml.example`. |
| `7788` | Shop-box Conclave categorizer; `platform/apps/{pstation,oefm,hoa,wannaplay}.yaml`, `headscale/RUNBOOK.md`. |
| `8080,9090` | Headscale container HTTP/metrics; `platform/headscale/config.yaml`; 8080 also a LAN camera probe. |
| `8123` | Home Assistant destination on its Pi; `platform/network/shop.json`, `docs/laser-ha.md`. |
| `8440-8459` | Pool preview range; `platform/compose.yaml`, `apps/*-preview.yaml`, `headscale/acl.hujson`; 8443 also go2rtc SRTP default (disabled). |
| `8554` | Shop-box go2rtc loopback RTSP; `platform/scripts/install-camera-gateway.py`. |
| `8787` | Existing member OctoPrint ingest; `config.py`, `platform/network/shop.json`. |
| `8788` | Hub laser telemetry listener; `laser.py`, `docs/laser-ha.md`. Never use for a model door. |
| `8790` | Operator approval server on pool 100.64.0.6; `platform/AGENT-OPERATIONS.md`, `headscale/acl.hujson`. |
| `12445` | UniFi Access on the console; `platform/scripts/install-door-bridge.py`, `pipeline/door-bridge.py`. |
<!-- /existing-port-inventory -->

Testing remains staff/admin only: the site's session and submit gates apply on every model port and on the diagnostic
paths on 8787. Network Build D grants only `operator@` access to 8810–8814 on `tag:print-hub`; it adds no member or LAN
allow. The all-interface hub bind is unchanged. Persistent userspace forwarding, local LAN isolation and the pool ACL
apply lane still need their separate reviewed implementations and operator taps; see Network Build D's runbook.

**Why ports, not path prefixes:** v2.4.2 `GUI_App.cpp:3643` calls `MachineObject::dev_id_from_address`
(`DeviceManager.cpp:366`), which strips the URL path and uses the resulting address as the device IP.
`MoonrakerPrinterAgent.cpp:1076` then rebuilds its base URL from that address. A prefix works in `Moonraker::make_url` for
upload, but disappears for filament sync and collapses model identities. Distinct ports survive both clients. The ingest
port also supports `/moonraker/<pool>/...` for HTTP diagnostics; **do not put that prefix in an Orca preset**.

## HTTP contract

All allowed operations require `X-Api-Key`, perform a fresh site authorization, and return `Cache-Control: no-store`.
Revoked tokens/membership return 403; unavailable authorization or spool refresh returns 503. No positive auth cache and no
offline success. Unknown model prefixes return 404; a file for another model returns 400, or 404 when resolving a staged path.

| Request on a model port | Result |
| --- | --- |
| `GET /server/info` | `{result:{machine_name:"PS P2S queue",model_pool:"p2s",moonraker_version:"makeros-hub-0.88.0",klippy_state:"ready"}}`. Ready means the authenticated queue door has spool capacity, not that a printer is idle. |
| `GET /server/files/roots` | `{result:[{name:"gcodes",permissions:"rw"}]}`. |
| `GET /server/database/item?namespace=lane_data` | `{result:{namespace:"lane_data",value:{lane1:{lane:"0",material:"PLA",color:"#FFFFFF",bed_temp:0,nozzle_temp:0},...}}}`. |
| `POST /server/files/upload` | Multipart `file`, `root=gcodes`, optional `plateindex` (1–64), empty `path`, optional `print`. Returns HTTP 201 `{result:{item:{path:"<opaque-uid>/<safe-name>",root:"gcodes"},print_started:false}}`. Even `print=true` only stages. |
| `POST /printer/print/start` | JSON `{filename:"<returned-path>"}`. Returns `{result:{jobId:"...",status:"queued"}}`; eligibility, member/pool ownership, file hash, size and facts are checked. |
| Everything else on a model port | HTTP 405 with `{error:{code,message}}` directing members to the PS site's **Your prints and printer controls**. This includes G-code, pause/resume/cancel, temperature changes, deletes and alternate methods. |

Lane data uses the existing VP live-pool builder and ranking, filtered by the freshly authorized printer IDs before deriving
a member view. Bambu spools come from heartbeat statuses; Klipper's staff-set spool comes from the site's current record,
only while that printer is connected. Refresh invalidates the previous snapshot first; snapshots expire after
`max(90 seconds, 2 × heartbeat interval)`. Sixteen explicit lanes, including empty-material placeholders, clear removed slots.
An empty object would trigger Orca's Happy Hare fallback (`MoonrakerPrinterAgent.cpp:727–819`). Integer zero is **unknown**:
Orca only emits temperature fields when positive (`:505–509`); catalog ranges are not invented printing temperatures.

The original sliced bytes are preserved. G-code is limited to the known Klipper model aliases and one used filament/nozzle.
Bambu requires a sliced 3MF, consistent native model ID (`N7`, `N1`, `BL-P001`), matching profile alias, selected plate,
material/colour/nozzle facts and a single extruder. Missing or conflicting model IDs are refused, never stamped or repaired.
A1 mini remains PLA-only. Unknown weight/bed stays null; the site must review missing cost and apply its normal bed rules.
Multi-plate files need an explicit plate; single-plate files can use their sole actual plate number.

## Site changes required (separate repository)

These are **required interface changes**, not claims that the site's current implementation supports them. Paths for existing
site code come from the supplied review; no site files were edited here.

1. Add `src/app/api/print/hub/moonraker/session/route.ts`: hub-bearer-authenticated `POST`, body
   `{memberToken:string,modelPool:string}`. Reuse the submit route's `resolvePrintToken`, membership/print eligibility and
   member-visible model/printer selection. Return HTTP 200:

   ```json
   {"ok":true,"submissionProtocol":"moonraker-v1","memberId":"stable-member-id",
    "modelPool":"sv08-max","printerModel":"Sovol SV08 MAX","printerIds":["printer-id"],
    "loadedSpools":{"printer-id":{"material":"PLA","colorHex":"#FFFFFF"}}}
   ```

   `printerIds` must belong to the authenticated hub, member view and requested model (at most 256 unique IDs). For every
   Klipper ID, `loadedSpools` must contain its current staff-set spool, or explicit null for empty; missing is an error.
   Bambu may return `loadedSpools:{}` because the hub has its live AMS. No job, billing or token refresh side effects.
   Invalid/revoked member token or eligibility: HTTP 403 `{ok:false}`. Do not advertise `moonraker-v1` until the submit and
   assignment changes below are active. Hub credential failure is 401; outage is 503.

2. Extend `src/app/api/print/hub/submit/route.ts`'s validated request and durable job record. Preserve existing fields
   `memberToken`, `hubSubmissionUid`, `fileName`, `fileSha256`, `fileSizeBytes`, `printerModel`, `estGrams`,
   `requiredFilaments`; add:

   ```json
   {"submissionProtocol":"moonraker-v1","printNow":true,"modelPool":"p2s","plate":3,
    "fileFacts":{"version":1,"modelPool":"p2s","printerModel":"P2S","sourcePrinterModel":"PS P2S",
      "printerModelId":"N7","plate":3,"bedType":"Textured PEI Plate",
      "requiredFilaments":[{"slot":1,"type":"PLA","color":"FFFFFFFF","usedG":4.53,"nozzleDiameterMm":0.4}],
      "estGrams":5}}
   ```

   `bedType` and `estGrams` may be null; Klipper `printerModelId` is null. Filaments use the existing schema, optionally
   carrying `trayInfoIdx`/`idx` and `extruderId:0`. Validate agreement between top-level and nested facts. Protocol requests
   require `printNow:true`; reject false/missing before any job insert. Upload-only never reaches this route.
   Extend the currently Klipper-only auto-targeting to the supported Bambu models using the existing eligibility,
   material/nozzle/bed, loaded-spool matching and missing-cost-review rules. Canonical models/aliases are in
   `makeros_hub/printers/ingest_facts.py`; do not bypass dispatch guards for PS names.

   Atomically enforce the existing hub submission UID uniqueness, bind it to the resolved member and immutable file facts
   (including pool/plate), and return the **original receipt** on an identical retry. Conflicting reuse must create no job.
   Check current authorization on retry as well. Reply HTTP 200 `{ok:true,jobId:string,submissionProtocol:"moonraker-v1"}`;
   rejection remains `{ok:false,reason:string}` (`invalid_token` where appropriate). A timeout can follow commit: retries
   use the same UID. This site transaction is essential to the one-job guarantee across a lost cloud reply.

3. Carry the persisted selected `plate` and `requiredFilaments` through the existing heartbeat assignment producer
   (`src/app/api/print/hub/heartbeat/route.ts` entry point) alongside `queueJobId`, `printerId`, `submissionUid`, `fileName`,
   `assignmentSeq`, and `useAms`. For matched AMS filaments send `useAms:true` and omit `amsMapping`/`rawPrint`: the existing
   hub translator derives physical slots from requirements. For a verified external spool send `useAms:false`.
   Omitting `useAms` defaults to external and will hold an AMS-only job. Reuse the downstream queue helpers; their internal
   paths were not inspected. Preserve `bedType`/facts
   in admission and reassignment. The hub already consumes `plate` and `requiredFilaments` in
   `PrinterManager.dispatch_assignments`; Bambu rechecks model, nozzles and live tray translation before/after upload.
   No physical tray address may be inferred from a displayed lane. Klipper final model/nozzle/idle checks remain in place;
   the site owns its staff-set spool matching.

4. Update the existing preset installers (`orca-setup.ts`, `orca-setup-mac.ts`, named in the review; full paths unverified)
   to use the model ports above and preserve the existing fallback presets. Member tokens remain local connection settings.

## Durability and limits

Files use the existing `<spool>/<32-hex-submission-uid>/<safe-name>` layout. `.moonraker.sqlite3` stores ownership, facts,
timestamps and receipt, never a token. A process/file lock spans file operations; SQLite FULL synchronization records
`receiving → staged → submitting → queued`. Files/directories are fsynced before staging. Starts are serialized; a restart
recovers the same UID, and a recorded receipt is replayed without another cloud submission. A new upload intentionally
creates a new UID, even for identical bytes.

Unstarted uploads expire after 24 hours; interrupted receiving files are removed on startup/maintenance. **Queued and
uncertain files are never automatically deleted.** There is no terminal-job reconciliation API in this change: after 256
retained records or 4 GiB of retained/reserved bytes, intake refuses with 507 and requires staff reconciliation. Do not
delete uncertain rows/files to make room; first establish the site's durable outcome and file-retention needs. Retain
receipts if terminal payloads are reclaimed, so old starts cannot recreate jobs.

Other bounds: 16 unstarted stages per member, configured per-file limit (default 1 GiB), 1 MiB multipart overhead,
128 MiB free-disk reserve, one upload shared with legacy ingest, four connections per listener (24 total), 120-second
body deadline, 4 KiB start JSON, 1,024 ZIP entries, 2 MiB ZIP directory (checked before loading), 2 GiB total expanded size,
200:1 maximum expansion ratio, 10 MiB per metadata entry. ZIP64/multidisk archives and
duplicate/traversal/encrypted/unsupported-compression entries are refused.

## Ten-minute operator test after both halves are available in the approved test environment

1. **0–2 minutes:** use Orca 2.4.2 and the PS SV08 MAX/Giga presets (PS P2S only with the stage-1 preset). Set the model
   URL/API key above. Test connection; an invalid token must fail. Keep the existing connection available as fallback.
2. **2–4:** Sync filaments in Prepare. Compare material/colour against the shop's current spools. Switch P2S → Giga → P2S;
   repeat sync. Remove/reorder a test spool, then empty the test pool and sync: stale choices must clear. Make the site
   session unavailable: sync must report failure. Check two differently tuned PLA presets/custom material and colour-only
   sync; matching colour alone does not prove the intended flow/temperature preset survived.
3. **4–6:** slice a tiny one-filament part. Upload only: **zero new queue jobs**. Upload and Print: exactly one job in
   Your prints, correct member/model/grams/selected plate. For a multi-plate P2S file, send plate 2 or 3 explicitly.
4. **6–8:** in the isolated test harness, repeat that stored path's start concurrently and across a hub restart; check
   the same job ID. Repeat with two members uploading `part.3mf`; cross-member/wrong-model starts must fail. Revoke the
   token and retry. The automated HTTP proofs in `tests/test_moonraker.py` cover these without touching a printer.
5. **8–10:** pause/cancel/G-code from Orca must return the site's control guidance. Close/reopen Orca, then sleep/wake or
   switch tailnets and recheck the selected model and Sync. Only an operator-approved tiny physical print can establish
   actual end-to-end dispatch; use the site's controls for it.

Not verified here: real Orca GUI/cache clearing, Windows/Mac preset installation, actual site transaction/eligibility
implementation, or any physical print. Orca derives filament preset identity from material, ignoring an exact preset ID
in lane data; this cannot promise distinct tuned PLA identities. Its live websocket status path is compiled out in 2.4.2.
This change does not restore PS Bambu object markers or prove multi-colour/H2D behavior. Keep the existing sync fallback
until the GUI and stage-1 gates pass.

Automated proof in this checkout (2026-10-28):
`PYTHONPATH=. ~/.pyvenv/bin/pytest -q tests --deselect tests/test_real_splitjoints.py`
completed with **1,784 passed, 42 skipped, 2 deselected, 165,171 subtests passed** in 248.91 seconds.
The one warning is the deliberately duplicated ZIP-entry fixture. The focused Moonraker suite has **73 passing tests**;
`git diff --check` and compilation of the changed Python modules also passed. Cloud commit/retry proofs use a fake control
plane; they do not establish that the separate site's transaction already implements this contract.

Port-fix verification (2026-10-29), including the full-suite limitations and cross-checkout results:
[port-fix-verification-2026-10-29.md](port-fix-verification-2026-10-29.md).
