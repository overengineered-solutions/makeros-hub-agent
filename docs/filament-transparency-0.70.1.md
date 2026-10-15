# 0.70.1 — transparent filament identity

The physical tray matcher previously discarded alpha, treating transparent `00000000` and opaque `000000FF` as the same color. It also admitted cleared alpha-zero trays. The virtual-printer pool exposed those cleared trays to slicers.

This patch preserves non-opaque alpha during physical selection and refuses alpha-zero tray entries without a nonzero 16-hex RFID tag. The same presence rule filters the virtual pool. Six-digit opaque colors still match their `FF` equivalents; material, product ID, nozzle and fullest-spool selection remain in force.

Two regressions fail on 0.70.0 and pass here. They cover 3MF capture through physical mapping, distinguish otherwise identical clear/opaque spools, reject cleared slots, and preserve transparent colors in the virtual pool and external holder. Existing tests ran one file at a time; the wheel builds successfully. No runtime dependency or installer change.

The companion pstation change must preserve alpha at submission/storage and require hub 0.70.1 for non-opaque assignments. Either release may land first: until both are active, transparent jobs wait. Lost alpha in existing site records cannot be reconstructed safely; re-submit verified slicer settings. Cancelled jobs must not be replayed.

No live printer command or hardware print trial was performed. Generic untagged clear spools remain unverified. Publish only after operator approval; normal OTA defers while printers are busy. Do not force a mid-print update.
