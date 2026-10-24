# integrate2 — hub — 2026-10-23

Branch `release-0.84.0`, code head `605b6afabe862a5d7ce79473ed6ec2badc7e3139` before this report. Version **0.84.0**, laser gate **0.2.0**. Local commits only; nothing pushed, tagged, deployed or changed in production.

Merged individually with `--no-ff`: `hubsmalls-2026-10-23` (`2de6c42`), `lasersend-2026-10-23` (`bc50331`), `splitjoints-2026-10-23` (`c77470a`). Also included the site head-start report's required capture-duration companion `headstart-2026-10-23` (`167ace9`, source `8a5e3cb`), after disclosing that dependency and receiving no answer to an optional clarification.

Preserved the combined slicer wrapper's warning validation/refusals, one-piece-per-pass checkpoints, all piece checks and cached plates. Commit `605b6af` carries each piece's duration and warnings to the site and cached replay, and retains the actual failing piece's warning report on a later pass. The site displays **“{piece name} · Plate {N}: {warning}”** and **“Queue estimate for the first piece”**. Firmware observations, conservative Klipper ending proof and store-only laser delivery remain intact. No tests were dropped.

Verification:

- Required bare `/home/marsh/projects/makeros-hub-agent-0.70/.venv/bin/python -m pytest -q -p no:cacheprovider`: 1,396 passed, 39 skipped, two failed because the old local slicer image lacks NumPy. Confirmed by import probe.
- Full rerun with existing `PYTHONPATH=/tmp/pstation-fixit-python` and `PS_PREPARE_PACKAGES=/tmp/pstation-fixit-python`, the same interpreter/command plus `-rs`: **1,472 passed, eight skipped**, one existing zero-volume trimesh fixture warning. No dependency installation. Includes real Orca `tests/test_real_slice.py` and both real split modes using local `ps-slicer:2.4.2-1`, this wrapper and cached geometry packages.
- Remaining skips: one laser test awaiting operator samples, seven tests requiring an installed checker box.
- Focused wrapper/manager/warning regressions passed, as did the capture/version tests and `sh -n slicer/run-slice`. `git diff --check` passed. Self-reviewed the full change against assigned base `v0.83.0`, including warning preservation, cancellation proof, expiry and store-only laser behavior.

No checker bundle vendored; site checker format remains **3**. The integrator vendors from the final site commit. Site migrations: 0218, 0219, 0220, 0222; 0221 unused. The complete integration report is `reports/integrate2-codex-build-2026-10-23.md` on site branch `cx2-integrate-2026-10-23`.

Doctrine: R1.1 **Pass**, existing manager/wrapper paths (`makeros_hub/printers/manager.py:768`, `slicer/ps_slice.py:1095`); R4.5/R4.6 **Pass for combined piece reports**, strict warning parsing and counted reports (`slicer/ps_slice.py:1177`, `makeros_hub/printers/manager.py:790`); R6.2 **Fail for release acceptance**, the companion site head-start policy does not guarantee six actual hours under overruns (`test/unit/headstart.test.ts:42` in the site). Secrets/deployment rules **N-A**.

Not checked: physical printers, laser T0–T8/calibration, VisiCut Windows/Apple Silicon/member pilots, joint-fit coupons/student building, or a fresh installed image. Release remains blocked on the actual-delay policy; waiver approval is still separate. Joint clearance remains physically unproven. The gate and slicer need their own installation; hub OTA alone does not replace them.
