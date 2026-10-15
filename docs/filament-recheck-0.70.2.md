# Hub 0.70.2: recheck filament after upload

A Bambu spool can change while FTPS uploads a sliced file. Previously, the final busy check reused the pre-upload tray mapping without revalidating its material, color or freshness. A synthetic adapter test replaces white PLA with ABS during upload and observes Start still being published; moving the PLA to another tray and aging the report also reproduce the defect.

The adapter now runs the existing translator again after upload, using the original virtual selection and the newest status. A wrong spool or stale report holds the job before any dispatch record or Start command. A matching spool moved to another tray gets a fresh physical mapping. Existing refusal messages and site retry behavior are preserved.

Review scope: `BambuAdapter.start_print`, all callers through `PrinterManager`, live tray translation, structured H2D mapping, freshness, dispatch persistence and replay guards. A second protocol review checked that retranslation starts from the original virtual command rather than feeding physical mappings back through the translator, and that both successful and refused paths preserve durable dispatch ordering.

Verification: all three regression subcases fail against 0.70.1 and pass after the fix. The full pytest suite passed **1,051 tests with 30 skips**, one file at a time with its per-test state isolation. The release's two version constants match. Wheel packaging is checked before publication. An initial unittest invocation bypassed pytest's fixture isolation and produced environment errors; it is not counted as the release gate.

No physical printer was commanded. This recheck cannot detect an unreported physical change or make the firmware and hub share an atomic lock. Unknown external-spool requirements and malformed plate metadata remain separate audit findings; this release closes the upload-time recheck gap only.

Doctrine: R1.1 Pass (reuse `translate_print_trays`); R4.6 Pass (same strict tray/nozzle input validation, checked twice); R5.3 Pass (existing held reasons); R6.12 Pass (three failing-before/passing-after cases); R2.10 Pass (no secrets or member data); R7.6 pending operator publication approval and target selection.
