# v0.70.3 — preserve spool presence separately from saved settings

The printer can report generic transparent PETG without an RFID tag. Alpha 00 also appears in cleared settings. Previous normalization discarded the presence state, then the virtual filament pool and final tray mapper excluded every untagged transparent setting. This hid usable generic clear filament from Orca.

The shared tray normalizer now emits optional `spoolPresent: boolean` for steady reports. Explicit absence excludes cached identities from the virtual pool and final mapping, even when an RFID identity remains. Detected presence permits a generic transparent setting. Identity fields remain intact for the site to display with uncertainty. The site needs its companion matching change; the addition is backward compatible and does not alter command or OTA behavior.

Evidence: the approved read-only LAN probe on October 15 saw state 8 with no identity and tray_exist_bits excluding that position, and state 11 with a clear PETG identity and the bit present. Existing `_tray_present` follows pybambu's loaded-state rule. This change uses that rule only for unambiguous legacy 0/3 or steady extended states; malformed values and transitional 1/2/5/17/21 remain unknown rather than reporting physical absence. No state is inferred from RGB, percentage or object key count.

Tests: `test_spool_presence.py` has 22 cases through raw normalization → pool key → physical mapping. Eleven failed before the fix. It covers both holders, generic clear, explicit absence with cached RFID, invalid types, missing state and scan transients. All existing test files were run separately under pytest: 1,073 passed, 30 skipped, no failures. Wheel build passed (`makeros_hub_agent-0.70.3-py3-none-any.whl`).

Review: shared normalizer reaches AMS/external DTOs; external whitelist updated; pool and final mapper share `cleared_tray`; bool is checked by identity, so 1 and "true" are not evidence. No fixture, path, secret, member or physical machine was modified. No hardware motion was used to test it.

Limit: presence detects a spool, not whether a manually entered material/color is correct. The website must show that uncertainty. Source timestamps per slot and AMS controls/rescan are separate work; this release does not enable them.

Doctrine: R4.6/R6.2 pass (`bambu_parse._add_tray_identity_fields`, `tray_translate.cleared_tray`, strict-state tests); R6.12 pass (raw report diagnosed before changing the parser); R7.6 pending explicit publication approval. No migration, pricing or access-policy changes.
