# Radar Measurement Integrity v4 Implementation Plan

**Goal:** Replace biased forward Radar labels with state-change first-passage outcomes and EV-primary,
matched-control reporting without increasing network work.

**Architecture:** Version all corrected evidence forward. Materialize the decision-time entry and its
elapsed-zero exit from the same captured curve state, retain later state-change and horizon observations, split
local modeled cost from paced remote movement, and evaluate disjoint controls in the retained insight
layer.

## Constraints

- Paper research only. Funded and V6 paths are unchanged.
- No production DB mutation before deployment and no historical row rewrite.
- No new external call, worker cadence, queue depth, or browser automation.
- Use failing regressions before implementation and one logical commit per task.

### Task 1: Outcome ordering and entry baseline

- Add failing outcome tests for equal-millisecond temporal order.
- Add failing engine tests for one elapsed-zero observation created atomically with a v4 entry.
- Implement `opportunity-outcome-v3` and v4 entry materialization at the local decision that consumes
  the completed route receipt; do not count the remote pair latency twice.
- Preserve gap censoring and exact horizon settlement.

### Task 2: Quote cost/move decomposition

- Add failing engine tests that stale fingerprints cannot create v4 entries and that modeled cost is
  distinct from the paced remote return.
- Add failing probe tests for expected output, slippage floor, typed malformed responses, and ordinary
  provider exceptions.
- Implement `jupiter_quote_v3`, local inclusion quotes, and explicit decision/state ages.

### Task 3: Disjoint controls and EV-primary evidence

- Add failing insight tests for all-alert EV, time-exit inclusion, no-replacement hard-band matching,
  unmatched counts, creator-cluster intervals, and no hardcoded best arm.
- Implement v4-only evidence and verdict gates. Keep old results visible as withdrawn pilots.
- Rename forward eligible-buyer fields while preserving historical readers.

### Task 4: Operator clarity and coverage truth

- Add API/UI contract tests for valid/total/invalid coverage beside return, overlap and concentration,
  diagnostic status language, and `forward_sample_verified` Bags wording.
- Remove resolved-only headline claims and explain modeled cost versus observed move.
- Keep the existing bounded keyed renderer and payload ceiling.

### Task 5: Integration and runtime proof

- Run focused and full relevant tests, schema validation, compile/syntax checks, and `git diff --check`.
- Run the Knowledge Sync Gate and record the new design doctrine.
- Merge into local main, push main, deploy pinned opportunity and web releases, then verify release SHAs,
  processes, authenticated health, heartbeat freshness, gaps, storage, quote pressure, and logs.
