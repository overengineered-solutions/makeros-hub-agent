# Orca 2.4.2 / 2.5.0-beta Moonraker queue door

Build H3 (2026-10-29) adds host-based model selection as an uncommitted hub change for review. The operator has already verified a 2.4.2 send through
hub 0.89.1; this build has only local automated verification. The site contract below must remain in place.
An old/unavailable site fails closed; neither the session check nor an upload creates a job. Nothing in this HTTP door sends
commands to a printer. The normal cloud queue and existing final dispatch guards own physical printing.

## 0.92.0: staged uploads and plate waits

Paired site build: `uploads-plates-2026-10-30`. Upload-only now registers an **uploaded, processing** record on the site;
it still creates no queue job. After saving and syncing the file, hash, facts and optional selected-plate picture, the
hub flushes Orca's existing HTTP 201 response, then immediately sends the notice in that request's own worker. It does
not wait for a heartbeat or for the notice before answering Orca. The site must persist and display the record promptly
to meet the one-second visibility target; network/site latency and outages can delay visibility.

The new hub-bearer-authenticated endpoint is `POST /api/print/hub/vp-staged`, using the same hub credential as `vp-submit`:

```text
{memberToken, submissionUid, fileName, fileSha256, fileSizeBytes,
 printerModel, fileFacts, plateImage?, stagedAt}
```

`fileFacts` is the existing version-1 object below. `plateImage` is the optional base64 PNG described under plate pictures;
G-code omits it. `stagedAt` is the UTC ISO timestamp when staging finished and stays unchanged on retries. Success is
HTTP 200 `{ok:true,uploadId:<nonempty string>}`. The site must authenticate the hub and resolve the member token, enforce
the existing member/model gates, and atomically deduplicate on the hub's `submissionUid`, binding it to the member and
immutable file facts. A repeated identical notice returns the same receipt, without adding another upload or job.

The uploads table gains nullable `notified_at`, `notice`, `copy_upload_id` and `copied_at` columns. The pending notice (including the member token) is
committed with the staged row in the private mode-0600 database. A confirmed acknowledgement sets `notified_at` and clears
the payload with SQLite secure deletion enabled. Failures log one warning per upload per process until acknowledged,
with UID only, never token, response body or exception text. The existing minute maintenance pass starts a separate retry
worker; network waits hold neither the file-operation lock nor the listener thread. An unacknowledged notice and its file
survive restart, normal expiry and job-release cleanup until acknowledged or explicitly released as an unused upload.
The former seven-day cutoff is removed so a prolonged site outage cannot discard pending work; spool quotas bound storage.
After acknowledgement an unstarted upload has at least 24 hours of retention. Existing quotas still refuse new uploads
when full; they never evict pending work to make room. Older rows migrate in place; they have no saved member token and
cannot be retroactively notified.

After the notice is acknowledged, a `.3mf` of **at most 64 MiB** is streamed unchanged to
`PUT /api/print/hub/vp-staged/<uploadId>/file`, with the hub bearer, `Content-Type: application/octet-stream` and
`Content-Length`. The site checks the original size and SHA-256 and keeps the bytes for its reslice flow. G-code and
larger 3MF files are not copied; their notices already describe the file type and size. The eligible file's `uploadId`
is persisted in `copy_upload_id` with the notice acknowledgement before attempting the PUT. HTTP 200 records `copied_at`;
an acknowledged notice or copy is not sent again. A lost remote/local acknowledgement retries the same UID or file URL,
so the site must handle these retries idempotently.

Copies run after Orca's flushed reply, following the immediate notice; neither request waits for a heartbeat. Copy
failure logs one warning line per upload per process until success, containing only the UID. The same minute maintenance
worker retries notices and copies, outside the file-operation lock and listener thread. A copy may finish before or
after Start. A pending copy retains its spool across restart, expiry and ordinary job release until acknowledged or
explicitly released as an unused upload. After a delayed copy succeeds, an unused stage has at least another 24 hours;
a released job can then be cleaned up. Existing spool quotas bound this retention. Rows acknowledged by an older build
without saving `uploadId` cannot be retroactively copied.

A successful heartbeat may include `moonrakerReleasedUploads: [<submissionUid>, ...]` for deleted or expired unused
uploads (at most 256 lowercase 32-hex UIDs). Only rows still in `staged` state are removed, including pending notices or
copies; unknown or repeated UIDs are harmless. `queued` and uncertain `submitting` rows remain for ordinary job
reconciliation. Cleanup persists a release marker before removing the spool directory and row, so a restart or
maintenance pass completes interrupted deletion. A busy file lock defers cleanup; the site must repeat the release
list on later heartbeats while cleanup may still be needed. This does not change `moonrakerReleasedJobs` handling.

Fresh session authorization remains required before an upload or start. If the notice endpoint fails after authorization,
the local upload still succeeds and is retried. An authorization outage still returns 503; revoked tokens still return
403. This change does not cache authorization or bypass nozzle validation.

Start retains the existing `POST /api/print/hub/submit` route and `hubSubmissionUid` field. **The site must match that
field to the staged endpoint's `submissionUid`**, promoting the same upload into one job. The separate Bambu virtual
printer path still uses `/api/print/hub/vp-submit`. Deduplication must also cover a start arriving before a delayed notice:
the late notice must reuse the job's upload identity and must not recreate an unqueued record or regress its status.
Both submit endpoints must accept a plate that no printer currently has as an ordinary successful queued receipt with
a site-visible wait reason, keeping physical dispatch blocked until compatible. A reason on a successful queued response
is not a refusal. Token and nozzle refusals remain unchanged; a repeated start returns the original job receipt.

The site build must supply the upload list/device-page display, timestamps, processing status, queue action, plate wait
wording and the atomic promotion/deduplication above. It must preserve the existing staff/member gates and download checks.
No real site, OrcaSlicer or printer has been tested here. The operator verifies those after the paired site is live and
the hub target is set to **0.92.0**. This checkout's source/package versions remain unchanged for release review.

## Preset address

Each model has its own hostname on the same hub address and port. Orca identifies a Moonraker connection by host and port,
so separate names let staff keep several models configured at once. The parallel platform build must supply these DNS names.
If `ingest_port` changes, use that port in every preset. One listener serves both Moonraker and the existing OctoPrint
`/api/version` and `/api/files/local` endpoints; OctoPrint does not use this model routing.

| Preset address | Model pool | Sliced file |
| --- | --- | --- |
| `http://p2s-printers.oes-mesh.internal:8787` | `p2s` | 3MF |
| `http://x1c-printers.oes-mesh.internal:8787` | `x1c` | 3MF |
| `http://a1mini-printers.oes-mesh.internal:8787` | `a1-mini` | 3MF, PLA only |
| `http://h2d-printers.oes-mesh.internal:8787` | `h2d` | 3MF, two explicitly mapped nozzles |
| `http://sv08-printers.oes-mesh.internal:8787` | `sv08-max` | G-code, one filament |
| `http://giga-printers.oes-mesh.internal:8787` | `giga` | G-code, one filament |

These six mappings are `DEFAULT_MOONRAKER_HOSTS`, the defaults for the optional `[moonraker_hosts]` table in `config.toml`:

```toml
[moonraker_hosts]
"p2s-printers.oes-mesh.internal" = "p2s"
"x1c-printers.oes-mesh.internal" = "x1c"
"a1mini-printers.oes-mesh.internal" = "a1-mini"
"sv08-printers.oes-mesh.internal" = "sv08-max"
"giga-printers.oes-mesh.internal" = "giga"
"h2d-printers.oes-mesh.internal" = "h2d"
```

An explicit table replaces the defaults; an empty table leaves every name using `moonraker_pool`. Put top-level settings
before the table. An entry with an unknown pool or invalid DNS name logs one ERROR at config load and is
ignored. A malformed table logs one ERROR and uses no host mappings. No request adds entries or changes configuration.

Selection uses only the request's single `Host` header, ignoring case, its numeric port and one trailing dot. DNS names
are bounded to 253 characters and labels to 63. Missing, duplicate, malformed or unknown hosts, and IP addresses, use
`moonraker_pool`; HTTP headers exceeding the server's own size limit are rejected. Forwarding headers and the hostname
in an absolute-form request line never select a pool. The selected pool applies to fresh site authorization, authorized
printers' lane data, upload model checks and staged-file ownership at start. A Host header supplies no authorization.

The top-level fallback `moonraker_pool` setting in `config.toml` defaults to `"p2s"`. It accepts `p2s`, `sv08-max`, `giga`,
`a1-mini`, `x1c` or `h2d`. An empty string disables the Moonraker door. An unknown name or a non-string value logs one ERROR
and disables it; existing member ingest stays available. Restart the hub after a reviewed configuration change.
A damaged Moonraker staged-file store also disables only the door. With the default fallback, today's
`http://100.64.0.1:8787` P2S presets work unchanged. Disabling the door also disables all named Moonraker routes.

Set `host_type=moonraker`, `printer_agent=moonraker`, and `printhost_apikey` to the member's existing **print token**.
Leave `printhost_port` empty (or matching the inline port); set `print_host_webui` to the site's member print page.
Use the matching non-Bambu-vendor PS preset: Bambu models use `use_3mf=1`; Sovol/Giga use `use_3mf=0`.
H2D requires a slice for the PS H2D preset with explicit filament-to-nozzle mapping. There is no model path prefix.

Testing remains **staff/admin only until the operator switches it on**. The site's session and submit gates must enforce
that restriction; ordinary members keep their stock Bambu presets and virtual-printer sync.

Port 8788 is the laser listener; never reuse it for Moonraker.

**Planned, not built:** one stand-in address per model on the routed range, using the vendor's real ports, is described in
the machine-network greenfield report (pstation `reports/machine-network-greenfield-2026-10-29.md`, a local report not in git).
H3 implements hostname selection on the existing ingest port; it does not add those stand-in addresses or vendor ports.

## HTTP contract

All allowed operations require `X-Api-Key`, perform a fresh site authorization, and return `Cache-Control: no-store`.
Revoked tokens/membership return 403; unavailable authorization or spool refresh returns 503. No positive auth cache and no
offline success. A file for another model returns 400, or 404 when resolving a staged path. GET/POST requests to removed
model-prefixed URLs return 404.

| Request on the ingest port | Result |
| --- | --- |
| `GET /server/info` | `{result:{machine_name:"PS P2S queue",model_pool:"p2s",moonraker_version:"makeros-hub-<version>",klippy_state:"ready"}}`. Ready means the authenticated queue door has spool capacity, not that a printer is idle. |
| `GET /server/files/directory?path=gcodes` | `{result:{dirs:[],files:[],disk_usage:{free:<usable-bytes>}}}`. Fresh authorization and the same quota/disk checks as upload; no member filenames or directory contents. Other queries are refused. |
| `GET /server/files/roots` | `{result:[{name:"gcodes",permissions:"rw"}]}`. |
| `GET /server/database/item?namespace=lane_data` | `{result:{namespace:"lane_data",value:{lane1:{lane:"0",material:"PLA",color:"#FFFFFF",bed_temp:0,nozzle_temp:0,filament_id:"GFA00"},...}}}`. |
| `POST /server/files/upload` | Multipart `file`, `root=gcodes`, optional `plateindex` (1–64), empty `path`, optional `print`. Returns HTTP 201 with top-level `item:{path:"<opaque-uid>/<safe-name>",root:"gcodes"}`, `print_started:false`, `print_queued:false`, and the same object under `result` for 2.4.2. Even `print=true` only stages. |
| `POST /printer/print/start` | JSON `{filename:"<returned-path>"}`. Returns `{result:{jobId:"...",status:"queued"}}`; eligibility, member/pool ownership, file hash, size and facts are checked. |
| Other requests under `/server/`, `/printer/`, `/machine/`, `/access/`, or `/websocket` | HTTP 405 with `{error:{code,message}}` directing members to the PS site's **Your prints and printer controls**. This includes G-code, pause/resume/cancel, temperature changes, deletes and alternate methods. |

Lane data uses the existing VP live-pool builder and ranking, filtered by the freshly authorized printer IDs before deriving
a member view. Bambu spools come from heartbeat statuses; Klipper's staff-set spool comes from the site's current record,
only while that printer is connected. Refresh invalidates the previous snapshot first; snapshots expire after
`max(90 seconds, 2 × heartbeat interval)`. Only occupied spools are sent, in the existing ranked order, numbered consecutively
from `"0"`; 16 is a ceiling, never a required count. Empty pools return `{}`. Each lane includes `filament_id` only when its
ranked tray's `tray_info_idx` is a string matching `[A-Za-z0-9_+-]{1,32}`. Material, colour and zero (unknown) temperatures
remain unchanged. The beta uses this ID to select a compatible base preset, including hidden presets; 2.4.2 ignores it and
still uses material type. No profile identity is invented from colour or temperature.

The upload facts, submit contract and dispatch do not consume display lane numbers. File filament slots and their
material/colour/nozzle facts still drive the physical AMS translator; the existing dispatch regression proves display lane
0 can resolve to physical tray 1. The 16-filament file limit remains a ceiling, not a padding requirement.

An entirely empty pool makes both Orca versions try Happy Hare, which this door refuses. In beta source that path returns
false without rebuilding the AMS payload (`MoonrakerPrinterAgent.cpp:614–641,885–887`), so clearing old GUI selections in
that case is **not guaranteed**. Do not represent a stale GUI lane as a loaded spool. The operator must check this in the GUI.

The original sliced bytes are preserved. G-code is limited to the known Klipper model aliases and one used filament/nozzle.
Bambu requires a sliced 3MF, consistent native model ID (`N7`, `N1`, `BL-P001`, `O1D`), matching profile alias, selected plate,
and material/colour/nozzle facts. Only H2D (`O1D`) accepts two extruders. Missing or conflicting model IDs are refused,
never stamped or repaired.
A1 mini remains PLA-only. Unknown weight/bed stays null; the site must review missing cost and apply its normal bed rules.
The hub door reads the bed value but applies no Bambu bed admission check to any pool (`printers/ingest_facts.py`). There is
therefore no hub Klipper exemption to add. A regression submits Klipper G-code without `curr_bed_type`; the paired site's
admission must exempt Klipper from its Bambu bed rule just as its heartbeat does.
Multi-plate files need an explicit plate; single-plate files can use their sole actual plate number.

For H2D, the existing capture reader cross-checks each selected-plate filament's `group_id` (zero-based logical extruder)
against that plate's `filament_maps` (one-based), then applies `Metadata/project_settings.config`'s
`physical_extruder_map`. Each required filament carries `extruderId` (0 right, 1 left) and the corresponding G-code
`nozzle_diameter` entry in logical extruder order. The header must name exactly two valid diameters even for a one-nozzle
job. Missing/conflicting mapping is refused with: “This file does not say which nozzle each filament uses. Reslice it
for the PS H2D preset.” No installed diameter is assumed; existing dispatch guards still compare against observed hardware.
When G-code omits per-filament usage statistics, H2D takes used slots from the verified selected plate. Supplied statistics
still must agree. The five single-extruder pools retain their prior facts and refusals.

Local evidence (2026-10-30): the existing Orca 2.4.2 fixture
`tests/fixtures/h2d-auto-for-match-2026-10-20.slice_info.config`, `capture._route_nozzles`, and the slicer wrapper's
`nozzle_map`; read-only inspection also found 104 H2D archives under the operator's Windows
`usb-h2d-backup-2026-10-28` directory. `02FloorPlates.gcode.3mf` (plate 1, right), `36x-Clips.gcode.3mf` (plate 4, left),
and `FlyingProp-Set-Quality.gcode.3mf` (plate 1, both) carry the mapping above and `nozzle_diameter = 0.4,0.4`.
Their project-wide `filament_map` differs from the selected plate, so it is deliberately not a fallback. Older exports
missing `group_id`, such as `Ceiling.gcode.3mf`, require reslicing. No upstream slicer source checkout was found;
OrcaSlicer, the live site and physical H2D operation remain unverified.

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
   carrying `trayInfoIdx`/`idx` and optional `extruderId:0` for single-extruder pools; H2D requires `extruderId:0|1` on every
   filament and a matching installed nozzle for that extruder. Validate agreement between top-level and nested facts. Protocol requests
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

4. Supply staff presets for all five hostnames above, with each model's correct sliced-file format and native model ID.
   Keep the existing `MOONRAKER` testing constant in `src/lib/moonraker-contract.ts` and `PS_P2S_TESTING` in
   `src/lib/orca-p2s-setup.ts` enabled, with the staff bypass. Session and submit must enforce the staff gate for every pool.
   Preserve ordinary members' setup downloads byte-for-byte and run `orca-member-bytes` in the site build.
   Member tokens remain local connection settings. DNS provisioning belongs to the parallel platform build.

## Durability and limits

Files use the existing `<spool>/<32-hex-submission-uid>/<safe-name>` layout. `.moonraker.sqlite3` stores ownership, facts,
timestamps and receipt, plus the private pending notice described for 0.92.0 above. A process/file lock spans file
operations; SQLite FULL synchronization records
`receiving → staged → submitting → queued`. Files/directories are fsynced before staging. Starts are serialized; a restart
recovers the same UID, and a recorded receipt is replayed without another cloud submission. A new upload intentionally
creates a new UID, even for identical bytes.

Unstarted uploads expire after 24 hours (pending notices/copies extend retention as described above); interrupted receiving files
are removed on startup/maintenance. Queued files with no pending notice or copy
and their records are released after the existing durable queue reports prove `printing` or `completed`. `uploading`,
`held`, an absent assignment, a timeout, or a failed start is not proof of handover. Before those reports can be acknowledged
and removed from the outbox, reconciliation must succeed. A busy upload/start lock defers cleanup to the next heartbeat
without blocking that heartbeat. An older hub's queued rows are covered by the same reconciliation.

For jobs cancelled or removed before handover (including jobs moved to another hub), the site may explicitly return
`moonrakerReleasedJobs: [<queue-job-id>, ...]` in a successful heartbeat. Maximum 256 nonempty IDs, each at most 128
characters. The site must repeat this authoritative list on later heartbeats while cleanup may still be needed. It must
only name this hub's jobs whose local file is no longer needed: a confirmed terminal/gone job, or a job safely handed to
another hub with its bytes preserved. A job merely absent from `assignments` must never enter this list. No new network
request is made to discover missing jobs. **This optional response field requires the paired site build.**

Release only matches rows with a recorded `queued` receipt. `submitting` rows remain intact even if a matching job ID is
mentioned; retrying the original staged path can recover the site's receipt using the same UID. Release first persists a
`released` marker, then removes bytes and the row. Startup and periodic maintenance retry interrupted deletion. A released
path returns 404 and cannot mint another job. While a receipt is retained, repeated starts still return the same job.
The existing 256-record / 4-GiB quotas bound active and uncertain records; sustained uncertainty can still fill them and
requires reconciliation, never blind deletion. The local store has an additive column migration, with no version bump.

Other bounds: 16 unstarted stages per member, configured per-file limit (default 1 GiB), 1 MiB multipart overhead,
128 MiB free-disk reserve, one upload shared with legacy ingest, 32 connections total on the shared listener, six per identified device,
10-second request-line timeout, 120-second body deadline, 4 KiB start JSON, 1,024 ZIP entries,
2 MiB ZIP directory (checked before loading), 2 GiB total expanded size,
200:1 maximum expansion ratio, 10 MiB per metadata entry. ZIP64/multidisk archives and
duplicate/traversal/encrypted/unsupported-compression entries are refused.
All hostnames share those connection and upload limits, the staged-file store and quotas. Changing model names does not
give a device another six connections; admission uses device evidence before any HTTP header is read.

## Ten-minute operator test after both halves are available in the approved test environment

1. **0–2 minutes:** use Orca 2.5.0-beta with `use_printer_agents=false` and the stage-1 PS P2S preset, matching the configured `p2s` pool. Set the
   ingest URL/API key above. Test connection; an invalid token must fail. Keep the existing connection available as fallback.
2. **2–4:** Sync filaments in Prepare. Compare material/colour against the shop's current P2S spools; repeat sync.
   Remove/reorder a test spool, then empty the test pool and sync: record whether stale choices remain; the source does not guarantee clearing. Make the site
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

For H3, repeat connection, Sync, upload and start for each named preset while keeping the others configured. Confirm the
selected model and its spools, refusal of an X1C slice on the P2S name, and the old IP-based P2S connection. This is still
an operator trial to perform after the paired builds are available; no DNS, GUI or physical-print verification is claimed here.

Not verified here: the actual beta binary, GUI/cache behaviour, Windows/Mac installation, paired site changes, or any
physical print. The M624 check does not restore missing markers; such a PS plate deliberately reports no skippable objects.
Keep the existing member presets and downloads byte-for-byte unchanged, with `MOONRAKER` / `PS_P2S_TESTING` still enabled
in the site. This checkout contains no site/download files and cannot run that site's `orca-member-bytes` test.

## Beta source audit (local files, no network)

References below are to the supplied sparse source under
`/tmp/claude-1000/-home-marsh-projects-pstation/c72320da-3e62-4be4-829a-7f3d24214d16/scratchpad/orca-250-profiles/`;
comparison source is the sibling `orca-src/`. These establish source behaviour, not installed-binary verification.

`src/slic3r/Utils/MoonrakerPrinterAgent.cpp` changes the connect/status path from `#if 0` in 2.4.2
(lines 2084–2101) to `#if 1` in beta (2316–2337). Every newly active endpoint/query is:

| Newly active request | Beta source lines | Door response |
| --- | --- | --- |
| `GET /printer/objects/query?print_stats&virtual_sdcard&extruder&heater_bed&fan` | 1223,2318–2328 | Plain 405 control guidance. Initial status failure does not fail connection. |
| `GET /websocket` upgrade | 120,1651,2335 | Plain 405 control guidance; no printer monitor or command channel. |
| `GET /printer/objects/list` after websocket handshake | 1324,1673 | Plain 405; not reached after our refused upgrade. |
| `GET /server/files/directory?path=gcodes` | 1344–1364,1711–1715,1759–1765,1925–1936 | Authenticated capacity response as above. New storage query; the upstream caller is inside its status stream. |
| Websocket JSON-RPC `server.connection.identify`, `printer.objects.subscribe` | 1659–1666,1717–1726 | Unreachable after the refused upgrade. No JSON-RPC command transport added. |

Sync still uses `GET /server/database/item?namespace=lane_data` (794) and the optional Happy Hare fallback
`GET /printer/objects/query?mmu` (897). No new sync URL; the new field is the string `filament_id` (864–879).
Connect still uses `GET /server/info` (1204). Agent upload/start URLs did not change: `/server/files/upload` (2183),
`/printer/gcode/script` (1285,409–410), and the existing helper `/printer/print/start` (2248).
Raw G-code commands remain refused; the helper is not an alternate member control path.

The PS non-Bambu preset's normal send uses `src/slic3r/Utils/Moonraker.cpp`, not the agent's raw-G-code start.
There are no new Print Host URLs: `server/info` (72), `server/files/roots` (125), `server/files/upload` (242),
`printer/print/start` (184). **Its upload response changed:** beta reads top-level `item.path` (283), sends
`print=true` for Upload and Print (266), checks `print_started` / `print_queued` (294–295), and falls back to explicit
start (326). The dual response shape preserves 2.4.2's `result.item.path` while making beta use the opaque stored path.

Keep the beta's experimental `use_printer_agents` preference **false**, its upstream default
(`src/libslic3r/AppConfig.cpp:724–727`). `src/slic3r/GUI/Plater.cpp:13838–13842` selects the legacy send for that setting
and a non-Bambu vendor. Enabling it selects the machine dialog: that path depends on monitor/storage state and uses raw
G-code start, so it is outside this door's supported trial. This does not disable the Moonraker agent used for filament sync.
The installer build must preserve this distinction and the PS profile's real Bambu filament IDs.

## Skippable objects and billing

`PrinterManager.dispatch_assignments` reads the selected plate through `parse_plate_objects` and includes the result in
its `uploading` report. The shared reader now requires an executable `M624` line with an argument in that same plate's
G-code, as well as the existing enabled-label metadata and 2–64 object count. Comments mentioning M624, M6240, markers on
another plate, missing G-code or unreadable data produce no objects. The scan is streamed and bounded; it does not edit
sliced bytes. Without markers no objects or object boxes are reported, so the site must not offer Skip or apply skipped-part
billing credits. Real hardware behaviour and restored marker generation belong to a separate approved trial/build.

## Admin door health and device evidence

Every heartbeat includes `moonrakerDoor: {state, refusals}`. States are `ok`, `off` (disabled/unbound), and
`store_unavailable` (initialization, maintenance or file-store failure). Successful maintenance restores `ok`.
`ok` describes the local door/store, not printer readiness or guaranteed site availability. Refusals are counts since this
process started, keyed only by `400,403,404,405,408,409,410,413,502,503,507,other`: at most 12 keys, saturated at 2,147,483,647.
No member, filename, token, device, or exception text enters this field. The parallel site build must validate and display it.

For every accepted connection on the shared ingest listener, a loopback socket peer is looked up using stdlib HTTP over the Unix
socket configured by top-level `members_tailscale_socket` (default `/run/tailscale-members.sock`). The request is
`GET /localapi/v0/whois?addr=<actual-peer-ip:port>`, with a 1-second socket timeout and a 64-KiB response limit. The original
peer port is needed for tailscaled's userspace forwarding map. IPv6 uses bracketed addresses. Forwarding headers are ignored.
The lookup runs in the connection's handler thread before reading its request, never in the accept loop. Non-loopback LAN
peers never cause a lookup. Missing socket, timeout, non-200 or invalid response uses the shared unknown-device bucket;
it does not itself refuse a request. The result is reused when submitting a print.

Both OctoPrint upload submit and Moonraker start submit optionally add
`device: {nodeId: <Node.StableID, else ID as string>, name: <Node.ComputedName, else Name, trimmed to 64 characters>}`.
Node IDs are bounded to 128 alphanumeric/underscore/hyphen characters; names must be printable. Absent/invalid evidence is
omitted. Only this small projection is sent; no WhoIs user identity, addresses or full node data are forwarded. Each actual
submit logs one INFO line with `source=tailnet|lan|unknown` and node ID only; a local receipt replay is not another submit.
The print token remains the sole member identity and gate. The site build must accept/store optional evidence without
using it to refuse or authorize a print. Unix socket access and tailscaled ProxyMapper behaviour are unverified on hardware.

## Plate pictures on submit

Moonraker start, OctoPrint upload submit and virtual-printer submit include optional `plateImage`, a base64 PNG read from
the original sliced 3MF. They reuse the fetch path's `plate_picture` reader: first `Metadata/ps_views_N.png`, then
`Metadata/plate_N.png`, then `Metadata/plate_N_small.png`. The decoded image is at most `PLATE_PICTURE_MAX` (192 KiB).
Moonraker and virtual printers use their selected plate; a virtual-printer job with no selected plate omits the image.
OctoPrint uses its existing plate-1 default. Missing, unreadable, invalid or oversized pictures are omitted, with a smaller
fallback used when available. Picture availability never refuses a print. G-code uploads continue without a picture.

This supplies the sliced view at queue submission, before dispatch. It does not capture printer screenshots. The paired
site build must store and display this optional image beside the printer's existing camera pictures, keep member-visible
changes behind `MOONRAKER` / `PS_P2S_TESTING` with the existing staff bypass, and verify `orca-member-bytes`. No site or
installer files are present or changed in this checkout; those gates and member download bytes are not verified here.

## Per-device connection space

The shared ingest listener retains its global limit of 32 handler threads. Each attributed tailnet node may hold six
connections; a LAN device is counted by its actual peer IP. All peers that cannot be attributed share one unknown bucket.
This is a device limit, not an account limit: devices sharing a LAN source IP share space, and one member using two tailnet
nodes has two buckets. Tokens and forwarding headers do not choose the bucket or authorize more connections.

When a seventh connection arrives, the listener closes that device's oldest idle connection, using accept order, and
serves the new one. It never evicts another device. An eviction logs one line containing the node ID (or `-` when none is
known), without device names, peer addresses or tokens. The lookup uses the same one-second timeout described above.
Connections awaiting attribution still occupy global slots; a burst can temporarily fill all 32 before lookups finish.

An upload is protected from eviction before body reading begins, through local storage and the submit response. Moonraker
start requests receive the same protection so replacement cannot interrupt a queue receipt. Upload deadlines and the
existing single-upload limit still apply. If all six connections in a bucket are protected, the new connection closes;
an active upload is never sacrificed to make room. Closed handlers return their global slots when they finish.

## Build H2 verification

Local verification included the full suite and one whole-diff self-review. The tests use fake
ingest sockets, a fake Unix WhoIs server and local HTTP listeners. No real printer, shop host, site, preview or tailscaled
is contacted. This build makes no commit, version bump, release or deployment.

- Before implementation, the new picture and connection regressions produced **17 failures, 9 passes** against the old
  code: missing submit pictures, absent device limits and absent upload protection.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh -k 'ingest or device_evidence or submit_pictures or entry_points or moonraker' --tb=short`:
  **216 passed, 3 skipped, 1,705 deselected, 487 subtests passed**. All six fresh-interpreter entry-point checks passed.
- After strengthening the other-device isolation check and adding both real-handler upload checks,
  `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh -k 'ingest_connections or upload_survives_more_connections' --tb=short`:
  **7 passed, 3 skipped, 1,916 deselected**.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh --tb=short`:
  **1,882 passed, 42 skipped, 2 failed, 167,325 subtests passed**, in 288.15 seconds. Both failures are the unchanged
  `tests/test_real_splitjoints.py` cases (`fit`, `floors`): the cached slicer image cannot import NumPy, matching the
  Build H failure recorded below. The full suite is **not green**. No image or dependency was fetched or changed.
  The one warning is the intentional duplicate-ZIP fixture.
- `git diff --check` passed.

One self-review covered the whole diff for correctness, member isolation, money, privacy, wording, tests and import cycles.
The connection test now makes another device own the globally oldest connection and proves it survives replacement.
Admission and upload protection share a lock, so an evicted handler cannot begin an upload from buffered bytes. A refused
new connection is removed from device accounting immediately; every worker returns exactly one global slot on exit.
No queue, price, billing, member authorization or download rules were changed. Only existing leaf-module imports were
extended for the picture reader.

Unverified: real tailscaled forwarding/WhoIs behaviour, installed slicers, physical printing, site display and staff gates,
and member download bytes. There are no open implementation questions for H2. The paired site build must supply the
display and gate checks above; the slicer image needs its NumPy dependency before the two existing slicing tests can pass.

## Build H verification

Results are recorded below after the final local suite and one whole-diff self-review. Tests use fake services and temporary
files; no real printer, shop host, site/preview, tailscaled, or external network is contacted. No commit, version bump,
release or deployment is made by this build.

- Before implementation, the focused regressions produced **17 failures** for lane shape/IDs, beta upload and storage,
  websocket refusal, reconciliation and health. The M624 regression was also checked against `HEAD`: the old reader
  reported two objects without markers; the working tree reported none.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh -k 'moonraker or device_evidence or threemf_objects or test_dispatch or plate_facts or entry_points' --tb=short`:
  **209 passed, 5 skipped, 1,673 deselected, 10 subtests passed**. This includes all six fresh-interpreter/service-entry checks.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh -k 'printing_report_survives or cleanup_never or H9RunLoop' --tb=short`:
  **3 passed, 3 skipped, 1,885 deselected** after the recovery fixes.
- Final `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh --tb=short`:
  **1,847 passed, 42 skipped, 2 failed, 166,655 subtests passed**, in 296.93 seconds. Both failures are the unchanged
  `tests/test_real_splitjoints.py` cases (`fit`, `floors`): the cached slicer image cannot import NumPy. This matches the
  previously recorded environment failure; the full suite is **not green**. No image or dependency was fetched or changed.
  The one warning is the intentional duplicate-ZIP fixture.
- `bash -n install.sh bootstrap.sh scripts/tailscale-setup.sh laser_gate/install.sh`, `sh -n update.sh`, and
  `git diff --check` passed.

One self-review covered the whole code/test/documentation diff for correctness, member isolation, billing, privacy,
wording, test coverage and fresh-import safety. It caught two recovery gaps: blocking on the file-operation lock could
stall the heartbeat, and acknowledging a report after failed reclamation could lose the cleanup evidence. Reclamation
now defers on a busy lock and keeps the durable reports until it succeeds. Both cases have regression checks. The review
also corrected the beta send-path instructions and explicitly records the empty-pool GUI limitation.
