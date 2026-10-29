# Orca 2.4.2 Moonraker queue door

This is the hub half of the contract, unreleased. The site additions below are required before connection tests succeed.
An old/unavailable site fails closed; neither the session check nor an upload creates a job. Nothing in this HTTP door sends
commands to a printer. The normal cloud queue and existing final dispatch guards own physical printing.

## Preset address

Use `http://<hub-host>:8787`, the **hub's member-reachable hostname and ingest port**, for the configured pool.
If `ingest_port` changes, use that port in the preset. One listener serves both the Moonraker door and the existing
OctoPrint `/api/version` and `/api/files/local` endpoints.

The top-level `moonraker_pool` setting in `config.toml` defaults to `"p2s"`. It accepts `p2s`, `sv08-max`, `giga`,
`a1-mini` or `x1c`. An empty string disables the Moonraker door. An unknown name or a non-string value logs one ERROR
and disables it; existing member ingest stays available. Restart the hub after a reviewed configuration change.
A damaged Moonraker staged-file store also disables only the door.

Set `host_type=moonraker`, `printer_agent=moonraker`, and `printhost_apikey` to the member's existing **print token**.
Leave `printhost_port` empty (or matching the inline port); set `print_host_webui` to the site's member print page.
Use the stage-1 non-Bambu-vendor PS P2S preset with `use_3mf=1`. If the operator later configures another supported pool,
its preset must match: Bambu models use `use_3mf=1`; Sovol/Giga use `use_3mf=0`. H2D is unsupported.
There is no model path prefix: the configured pool applies to every Moonraker request on this listener.

Testing remains **staff/admin only until the operator switches it on**. The site's session and submit gates must enforce
that restriction; ordinary members keep their stock Bambu presets and virtual-printer sync.

Port 8788 is the laser listener; never reuse it for Moonraker.

**Planned, not built:** one stand-in address per model on the routed range, using the vendor's real ports, is described in
the machine-network greenfield report (pstation `reports/machine-network-greenfield-2026-10-29.md`, a local report not in git).
This change implements only the single-pool door on the existing ingest port.

## HTTP contract

All allowed operations require `X-Api-Key`, perform a fresh site authorization, and return `Cache-Control: no-store`.
Revoked tokens/membership return 403; unavailable authorization or spool refresh returns 503. No positive auth cache and no
offline success. A file for another model returns 400, or 404 when resolving a staged path. GET/POST requests to removed
model-prefixed URLs return 404.

| Request on the ingest port | Result |
| --- | --- |
| `GET /server/info` | `{result:{machine_name:"PS P2S queue",model_pool:"p2s",moonraker_version:"makeros-hub-0.88.0",klippy_state:"ready"}}`. Ready means the authenticated queue door has spool capacity, not that a printer is idle. |
| `GET /server/files/roots` | `{result:[{name:"gcodes",permissions:"rw"}]}`. |
| `GET /server/database/item?namespace=lane_data` | `{result:{namespace:"lane_data",value:{lane1:{lane:"0",material:"PLA",color:"#FFFFFF",bed_temp:0,nozzle_temp:0},...}}}`. |
| `POST /server/files/upload` | Multipart `file`, `root=gcodes`, optional `plateindex` (1–64), empty `path`, optional `print`. Returns HTTP 201 `{result:{item:{path:"<opaque-uid>/<safe-name>",root:"gcodes"},print_started:false}}`. Even `print=true` only stages. |
| `POST /printer/print/start` | JSON `{filename:"<returned-path>"}`. Returns `{result:{jobId:"...",status:"queued"}}`; eligibility, member/pool ownership, file hash, size and facts are checked. |
| Other requests under `/server/`, `/printer/`, `/machine/` or `/access/` | HTTP 405 with `{error:{code,message}}` directing members to the PS site's **Your prints and printer controls**. This includes G-code, pause/resume/cancel, temperature changes, deletes and alternate methods. |

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
   to use the ingest-port address for the configured pool and preserve the existing fallback presets. Keep the testing
   gate enabled for staff/admin only. Member tokens remain local connection settings.

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
128 MiB free-disk reserve, one upload shared with legacy ingest, four connections total on the shared listener, 120-second
body deadline, 4 KiB start JSON, 1,024 ZIP entries, 2 MiB ZIP directory (checked before loading), 2 GiB total expanded size,
200:1 maximum expansion ratio, 10 MiB per metadata entry. ZIP64/multidisk archives and
duplicate/traversal/encrypted/unsupported-compression entries are refused.

## Ten-minute operator test after both halves are available in the approved test environment

1. **0–2 minutes:** use Orca 2.4.2 and the stage-1 PS P2S preset, matching the configured `p2s` pool. Set the
   ingest URL/API key above. Test connection; an invalid token must fail. Keep the existing connection available as fallback.
2. **2–4:** Sync filaments in Prepare. Compare material/colour against the shop's current P2S spools; repeat sync.
   Remove/reorder a test spool, then empty the test pool and sync: stale choices must clear. Make the site
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

Automated verification for the shared ingest listener (2026-10-29):

- Before the implementation, the new default-pool HTTP test failed with 404 on `/server/info`; six disabled/invalid-config
  cases failed because the single-pool setting did not exist.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh -k 'moonraker or H9RunLoop' --tb=short`:
  **116 passed, 3 skipped, 1,725 deselected, 5 subtests passed**.
- `PATH=/home/marsh/.pyvenv/bin:$PATH ./scripts/test.sh --tb=short`:
  **1,800 passed, 42 skipped, 2 failed, 165,065 subtests passed** in 258.05 seconds. Both failures are in the unchanged
  `tests/test_real_splitjoints.py` (`fit` and `floors`): the cached slicer image cannot import NumPy. This full run is
  **not green**. No image or dependency was fetched or modified. The one warning is the intentional duplicate-ZIP fixture.
- The README's shell syntax checks and `git diff --check` passed.

One self-review covered the whole diff: routing and configuration, member/pool isolation, submission and money behavior,
privacy, wording, and tests. The connection-limit description and operator test now match the single listener.
The old port configuration, extra listeners and listener heartbeat field have been removed. Cloud commit/retry tests use a
fake control plane; the separate site's staff gate, transaction and preset installer still require paired verification.
No commit, release, deployment or live-system request was made.
