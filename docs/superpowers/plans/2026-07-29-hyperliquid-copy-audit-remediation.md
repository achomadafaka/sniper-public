# Hyperliquid copy / alt-short audit remediation plan

1. Reproduce the audit from read-only ledgers and pinned runtime evidence.
2. Add failing regressions for total-book reporting, clustered episodes, funding provenance,
   provider budgeting, candle deltas, symbol identity, and frozen cohort membership.
3. Implement the smallest reporting, capture, scheduling, and retirement changes.
4. Run focused HL tests, the audit baseline, schema validation, diff checks, and runtime smoke tests.
5. Merge a clean branch, build a content-addressed release, atomically switch the Hyperliquid and
   web pins, restart only affected observational workers, and verify heartbeats/logs/API output.
