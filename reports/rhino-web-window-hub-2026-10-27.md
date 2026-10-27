# Rhino web window: hub first draft, v0.87.0

Built contract §4's hub side on `ww-toolpath-2026-10-27`. The version is 0.87.0 in both version files; no tag, push or deployment.

- The fetched report's `slice.slicedSha256` hashes the final spool 3MF, including reused slices and prepared pieces. The mesh's existing `sha256` keeps its original meaning. The 480 KiB picture budget and omission logic remain in place.
- `makeros_hub/toolpath.py` streams the selected ZIP entry into disk-backed geometry tables and writes little-endian PSTP v1, gzip-compressed. It handles linear and XY arc motion, absolute/relative positioning and extrusion, coordinate resets, tools, wipes, retraction, object markers and arbitrary bounded slicer feature names. Unsupported motion rejects the whole preview with a line number.
- The size stages are exactly ordered: above 5,000,000 points remove travel, simplify at 0.05 mm, retain alternate layers, then simplify at 0.2 mm if still necessary. The last stage starts from the original points to avoid accumulating approximation error. Flags, tolerance and original counts describe what happened. Over 8,000,000 points or 64 MiB decoded means `too_large`.
- `makeros_hub/toolpaths.py` persists a dormant request before the fetched report, arms it only after acceptance, generates in one child at niceness 19, and uploads on a separate daemon worker. The child has a 192 MiB address-space limit; the hub service itself is capped at 600 MiB. No parser work runs on the heartbeat interpreter.
- Uploads carry the required bearer authentication, media type, gzip encoding, sliced SHA and plate. 204 finishes; 404 and slice mismatch stop; 400/413 log and stop; not-ready uses 10/20/40/80/160/300-second backoff, capped at one hour. Connection failures, 429 and 5xx use the same bounded retry. Pending bytes, expiry and next attempt survive restart. A new assignment gets its own request even if the 3MF bytes match the previous assignment, so a move/re-slice can repopulate the site's invalidated preview. A 404 stays quiet and is not retried. Existing byte-upload callers retain their integer status result.
- Preview errors enter the existing warning/error log ring sent in heartbeat diagnostics. They never change the job, slice, approval or printer commands. Sidecars live in the submission directory, disappear with that directory, and are removed by the worker if the individual spool file disappears.

Limits are 16 KiB per line, 1 GiB streamed G-code, 32,000,000 source points and 100,000 layers. Generation has a 180-second deadline, with a parent-process kill at 185 seconds including startup and hashing. This leaves approximately four times the measured six-million-point test duration for slower hardware and scheduling; real shop hardware remains unmeasured. Temporary geometry is disk-backed and unnamed, so process exit also releases it.

Measured on this build host, Python 3.12.3, x86_64; the full suite was running concurrently. Times exclude synthetic-file creation and upload:

| Input | Source moves | Layers | Output points | Parse | Parse + PSTP/gzip | Decoded bytes | Gzip bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Real twisted tower | 98,177 | 450 | 102,293 | 0.6668 s | 0.7839 s | 670,529 | 501,934 |
| Synthetic million moves | 1,000,000 | 1,000 | 1,001,000 | 5.3054 s | 6.2194 s | 6,034,082 | 2,722,521 |

The million-move input uses 1,000 layers of 1,000 relative-extrusion moves, X stepping by 0.1 mm, Y cycling through `(i * 97 % 1000) / 10`, and Z increasing by 0.2 mm per layer. The real fixture is checked in as `tests/fixtures/twisted-tower.gcode.gz`, copied from the supplied synthetic-model slice. Its golden extrusion/travel lengths are 459,149.155227 / 32,824.214885 mm; deposited filament is 15,073.27565 mm.

Both files were also generated through the actual low-priority, memory-capped child process from compressed 3MF entries and decoded independently afterward. The real sample completed in 0.795 seconds at 33,456 KiB peak child RSS; the million-move file completed in 8.133 seconds at 34,540 KiB. Both produced exactly the gzip sizes above.

Gates:

- `tests/test_toolpath.py`: independent binary decoder, real-fixture counts and lengths, arcs, modes, resets, features, tools, wipes/retraction, malformed motion, ZIP streaming, all limits, bounded simplification error, each size stage and an actual generated six-million-point file. That file retains 3,000,000 points on alternate layers with flags 7 and tolerance 0.05 mm; its standalone test took 47.05 seconds.
- `tests/test_toolpaths.py`: fetched hash and acceptance ordering, isolated storage/generation/worker failures, diagnostic evidence, upload headers and every answer, restart recovery, backoff/expiry, stale slices, spool cleanup, durable-state validation and the real low-priority child process.
- Existing plate-fact, slice, upload, dispatch, HTTP, heartbeat and version tests guard compatibility. Final quick parser/upload checks: 92 passed, with the separately exercised six-million-point case deselected. Final assignment-sequence integration checks: 87 passed, 2 existing skips.
- `PYTHONPATH=. ~/.pyvenv/bin/pytest -q tests`: latest full run had 1,675 passed, 42 existing skips and 158,202 passing subtests in 268.21 seconds; only the two explicitly allowed `test_real_splitjoints` cases failed, both because the installed slicer image lacks `numpy`. The final tiny-circle fix is also covered by the 92 passing quick parser/upload checks above. No test was deleted or changed to skip.
- `git diff --check` passes.

Contract readings and unresolved gaps:

- Like prototype C, rendered geometry starts at the first layer marker. Startup coordinates/modes are tracked, but startup calibration moves do not count as slice segments. `sourceSegments` counts spatial source moves; an arc counts once before tessellation. Alternate layers means layers 1, 3, 5, and so on in print order.
- Arc support is the Orca XY plane with relative I/J centres, positive/negative R, full circles and helical Z. Other planes, inches, coordinate transforms, numbered/checksummed motion and unsupported motion parameters fail explicitly. Unknown commands, including proprietary firmware macros, are ignored as required by the contract. Bambu's fixture uses T65535 to unload; it retains the previous physical tool and marks subsequent movement as travel.
- An explicit M82/M83 controls extrusion independently of XYZ mode; before either appears, extrusion follows G90/G91. A physical tool selection clears that tool's inferred retract debt. Extrusion with no feature marker gets the name `Unknown`.
- A split-parent report is a manifest, not one printable plate: it receives the staged file's hash, but no single picture or toolpath. Each fetched child piece gets its own actual slice hash and preview.
- **The contract has no hub failure/status endpoint.** Local `failed`/`too_large` states and line-number diagnostics reach staff through existing heartbeat logs, but the site cannot obtain the member-facing unavailable reason through the defined toolpath POST. No extra interface was invented. The site needs an agreed failure signal to guarantee that a failed preview cannot remain `pending`.
- A crash after the site accepts the fetched report but before the hub durably arms its request can leave that request dormant if the site never repeats the fetch. Armed retries survive restart; closing the acknowledgement gap requires a reconciliation contract. The hub deliberately does not upload an unconfirmed slice.

Not verified: real Rhino, a Mac, the new site's decoder and endpoint, the live site's older-version integration, or shop-box scheduling/RSS. No site/plugin changes, external network requests, production access, slicer-image changes, firmware changes or printer commands were made. New integration problems left open are the missing failure signal and acknowledgement-gap reconciliation above.
