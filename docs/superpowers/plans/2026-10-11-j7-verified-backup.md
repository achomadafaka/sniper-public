# J7 verified Solana backup — approved 2026-10-11

Amended by `2026-10-18-j7-native-fallback.md`: Shyft admission is repaired, exact Pump hints qualify, and opaque IDs may wake an independent confirmed-wallet scan without granting transaction identity or authority.

Contract: J7 backup fills require exact transaction/wallet/mint identity, confirmed wallet signer/payment/acquisition proof, existing source-age/price/safety/sizing gates, and no unresolved native attempt. Keep fast-only on for other ordinary sources. Opaque J7 IDs cannot grant authority. No extra provider call or wait on native entry.

1. Add failing tests for exact proof, unsupported/missing receipt, identity mismatch, native filled/in-flight/unreadable/policy refusal, both delivery orders, and hook wiring.
2. Reuse confirmed detection and OnchainEv, slot_lane_verdict, receipt resume and existing reservation CAS. Add one default-off Solana J7-backup flag. Ignore only terminal unsigned J7 fast-only/proof-unknown refusals in the confirmed lane duplicate check.
3. Run focused regressions and independent branch review. Trace SXSI/SOBDOG/PACKZ historical proof gaps; retain honest timing/evidence limits.
4. Merge/push; enable only the backup flag; pinned deploy with reservation/reconciliation gates and no backup. Verify process release, effective config, callbacks, exits, unchanged limits and historical incident rows. Rollback restores flag and prior pinned release.

Not handled: opaque IDs, unsupported source instructions, missing watcher evidence, stale entries, native policy refusals, or unknown signed confirmation. No historical funded retries.
