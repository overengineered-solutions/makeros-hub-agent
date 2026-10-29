# Moonraker port fix — 2026-10-29

Working-tree changes only across the four requested checkouts. No commit, release, deployment, printer command or
shop/site connection. The fixed defaults are **8810 SV08 MAX, 8811 Giga, 8812 P2S, 8813 A1 mini, 8814 X1C**.
The inventory in `docs/moonraker-door.md` is from checked-in sources, not a live socket survey.

## Files changed in this fix

Earlier builders' changes are retained. This list describes this fix relative to those supplied working trees.

- Hub: `config.toml.example`, `makeros_hub/{config,ingest,agent,moonraker}.py`,
  `tests/{test_moonraker_ports,test_moonraker,test_agent,test_laser,test_v069}.py`,
  `docs/moonraker-door.md`, and this report. Config supplies explicit ports; each failed bind is skipped and logged;
  every heartbeat carries model listener status. Existing test doubles now implement that status field.
- Site A: `src/lib/hub-health.ts`, `test/unit/{hub-health,moonraker-door}.test.ts`, `docs/moonraker-door-site.md`.
  Bounded status reaches only the authenticated hub's metrics and the existing admin health line. Old reports clear
  when absent from a later beat; unknown fields are discarded. No member-facing surface or money behavior changed.
- Site B: `src/lib/orca-setup.ts`, `test/unit/{orca-setup-runtime,setupcodes}.test.ts`,
  `test/fixtures/orca-p2s-runtime.ps1`, `docs/ps-orca-test.md`. Both installer renderers share the fixed P2S address.
  The setup-credential test now supplies the ordinary-member auth mock required by the earlier staff gate.
- Network D: `headscale/{acl.hujson,RUNBOOK.md}`, `network/shop.json`, `scripts/box-equipment-leg.py`,
  `pipeline/test-phase8-cutover.sh`. Only the trial ACL range changes behavior; router/UFW edits are notes.
  The runbook's pool ACL apply lane is a proposal only, with no implementation or added authority.

## Verification

All requests in the collision tests target temporary local listeners with fake submit/authorization functions.
Postgres tests used two disposable local `postgres:17` containers and isolated database clones; both containers were
removed after the checks. Native Windows fixtures used fake services and disposable files, not installed Orca settings.

- Regression before the fix: the real model-port collision test failed with `EADDRINUSE` in the old constructor;
  the fixed-P2S test failed because setup emitted 8790; the site status test failed because status was discarded;
  the updated network assertions failed twice against the old ACL range.
- Hub full command: `PATH=/home/marsh/.pyvenv/bin:$PATH scripts/test.sh`. Initial result: 1,790 passed,
  7 failed, 42 skipped. Five failures were missing status fields on old test doubles; those were repaired.
  Two real split-joint slices failed because the already-cached slicer image cannot import NumPy. No image or dependency
  was fetched or modified. The remaining suite was rerun with `--deselect tests/test_real_splitjoints.py`: **1,795 passed, 42 skipped,
  2 deselected, 165,456 subtests passed** in 243.86 s. The warning is the intentional duplicate-ZIP-entry fixture.
- Final focused hub check: `PYTHONPATH=. /home/marsh/.pyvenv/bin/pytest -q tests/test_moonraker_ports.py`: **11 passed**,
  including first/middle/last/all failed binds, real member uploads on 8787, fixed defaults, overrides and malformed config.
- Site A focused: `pnpm exec vitest run test/unit/hub-health.test.ts test/unit/moonraker-contract.test.ts`:
  **36 passed**. `test/unit/moonraker-door.test.ts` against a fresh migrated Postgres clone: **18 passed**, including
  real heartbeat persistence, other-hub isolation, stale-status clearing and existing staff-only admission/money checks.
- Site B focused: setup/runtime/download/member-bytes/Mac suites: **70 passed** initially, with two Windows launches
  blocked by WSL's sandbox socket restriction. Native runtime/UI rerun outside that restriction: **37 passed**.
  Corrected `test/unit/setupcodes.test.ts` against a fresh Postgres clone: **34 passed**.
- Complete unit commands: `PATH=/tmp/ps-ports-no-docker:$PATH pnpm exec vitest run` in each site checkout
  (Docker unavailable, matching the unit lane). Site A: 1,721 passed, 1,228 skipped, three native-Windows launch failures;
  Site B: 1,703 passed, 1,211 skipped, four native-Windows launch failures. All those native runtime/UI cases passed on
  their approved local reruns: **30 tests in A, 37 in B**. Skipped DB cases ran in the separate Postgres lane below.
- Postgres lane: `PGC=ps-port-fix-site-pg ISO_PORT=55631 bash tests/isolation.sh` (A), and
  `PGC=ps-port-fix-setup-pg ISO_PORT=55632 bash tests/isolation.sh` (B). Both passed **16 schema checks**.
  A's broad run: 1,180 passed / 2 failed; B's broad run: 1,160 passed / 5 failed. B's three setup-auth fixture failures
  were repaired and its 34-test suite passed on rerun. The two remaining failures in each checkout are the unchanged
  `access-cut.test.ts` calendar-date expectations: expected November 5, got November 4. The existing implementation
  uses 168 elapsed hours across fall daylight saving; at this run's local time that ends on the preceding calendar date.
  The isolated access-cut rerun reproduced both failures; neither access-cut code nor its test differs from HEAD here.
  These broad gates are **not reported as green**. No billing policy was changed for this port fix.
- Network: `bash pipeline/test-phase8-cutover.sh`: **68 passed**; `bash pipeline/test-headscale-door.sh`: **30 passed**.
  `python3 pipeline/unifi-door.py selftest`, `python3 pipeline/agents-door.py selftest`,
  `python3 scripts/box-equipment-leg.py selftest`, `python3 scripts/box-firewall-tidy.py selftest`: all passed.
- ESLint for changed TypeScript files and tests: passed. `git diff --check`: passed in all four checkouts.
  A local cross-check confirmed all five documented defaults match the hub, setup writes 8812, the operator ACL agrees,
  router firewall data is unchanged, and both testing constants remain true.

## Review and remaining limits

One self-review pass covered the complete fix diff: bind/error cleanup, config defaults and malformed settings,
heartbeat wiring, member/hub isolation, money, privacy, wording, tests, all port references and ACL scope. It found
additional old test doubles and the setup auth mock; those were corrected. The admin message now uses plain language
while metrics retain the fixed OS error code. No member identifiers, tokens or exception text are added to status.

Live port availability, actual forwarding, Headscale daemon validation/traffic, native Mac behavior, GUI sync and
physical printing remain unverified. Another build must supply persistent members-daemon forwarding and local LAN
isolation, and implement/review the proposed commit-bound, operator-tapped pool ACL apply lane. The cached slicer test
image needs NumPy before its real-slice tests can pass; the independent access-cut date assertions need review.
Custom hub model ports require matching presets/forwards/ACL; setup currently writes default P2S 8812.
No unresolved port-selection decision remains. Staff-only testing stays on; the operator still controls every release.
