# Desk PumpSwap phase 2 implementation

> Implement in this chat under the operator's approved source-only limits. Each stage gets Gamma's
> exact-SHA review; Lambda reviews universe worker edits. Root serializes that file.

**Goal:** Add isolated PumpSwap census, capture and research candidates without changing curve or
funded behavior. **Design:** `docs/superpowers/specs/2026-10-22-desk-pumpswap-phase2-design.md`.
**Stack:** existing Python/protobuf/SQLite, no new dependency, all pytest `-n 2`.

### A. Census ready for Alpha's canary

- Merge Lambda `aed08b522` + `dbaa28d03`, run existing worker/universe tests.
- RED: unknown zero index, legal protobuf field ordering, non-trade control frames, metric refusal,
  +30 ms abort, owned-child-only shutdown and 30-minute completion.
- Implement narrow routing/index fixes and separate canary guard/runbook. Offline tests only.
- Commit; request exact-SHA Gamma/Lambda review. Actual provider load remains unmeasured.

### B. Authenticated pool tape

- Parallel ownership: decoder worker owns new `memecoin_desk_pumpswap.py` and its tests; root owns
  universe worker/storage integration. Pure decoder reuses existing execution parser layouts only.
- RED: scoped complete logs, failed tx, all events, spoofed/truncated payload, duplicates, missing and
  conflicting map, max uint64 TEXT, receipt clocks, restart/day rollover.
- Implement `swaps_ps` / `pools`, gap-aware mapping and default-OFF capture on same subscription.
- Focused regression including curve baseline, commit, exact-SHA Gamma/Lambda review.

### C. Venue-bound candidates and ranker

- Coordinate `pool_state` names with Beta; no edits to his verdict-price/execution files.
- RED: complete causal pool pricing, unknown facts refuse, separate identity and frozen venue policy,
  cross-cohort refusal, separate ranker/first-score immutability, unchanged curve bytes and labels.
- Add new envelope/verifier, explicit consumer venue routing, pool emission builder and ranker input.
- Stage source integration/offline merged flow, focused suites, independent review, final receipt.

Review focus: no invented metadata; immediate program provenance; pre/post reserve semantics;
unknown ordering and receipt clocks; venue/cohort isolation. No live canary, outcomes or runtime work.
