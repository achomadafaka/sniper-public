# V6 Engine System Remediation Plan

**Goal:** Repair the V6 correctness, capacity, and reporting defects proven by the
2026-07-28 system audit without tuning policy thresholds or changing funded runtime.

**Scope:** V6 paper admission, wallet evidence, quote scheduling, quote reuse,
capture arming, state/report persistence, and bounded API reads. Existing open V2
positions retain their policy identity. Solana remains observable but new Solana
admission is disabled by default.

## Task 1: Admission and wallet evidence

1. Add RED tests proving missing and weak wallet history are neutral.
2. Add RED tests proving Solana candidates are terminally rejected while their
   admission cursor advances.
3. Add RED tests proving a closed coin cannot start a second lifecycle under the
   same policy hash.
4. Implement the smallest fixes and run the focused tests.

## Task 2: Quote capacity and request truth

1. Add RED tests proving Solana wait time cannot block EVM work and a queued action
   outranks lower-priority work in its provider lane.
2. Add a RED test proving a full exit reuses the fresh whole-position observation
   quote.
3. Add RED tests proving persisted EVM routes are attempted before broad discovery
   and repeated provider failures are semantic no-ops.
4. Implement isolated provider lanes, full-exit quote reuse, route-first quoting,
   and stable provider-failure identity. Run the focused tests.

## Task 3: Capture and catastrophe evidence

1. Add RED tests proving each successful Solana subscription ACK arms its binding.
2. Add a RED test proving dynamic EVM capture starts from the entry-time block and
   retains events from the first observed head.
3. Add RED tests proving two distinct healthy quote checks can confirm catastrophe
   even when economics match, while replaying one check remains a no-op.
4. Implement the minimal evidence plumbing and run the focused tests.

## Task 4: Persistence and reporting bounds

1. Add RED tests proving successful intent fills relabel their decision as filled.
2. Add RED tests proving repeated same-watermark snapshots reuse computed facts and
   invalidate on new evidence.
3. Add RED tests proving position detail is bounded and cursor-pageable, list views
   do not perform per-position reads, and health/report queries bound large rows.
4. Implement the fixes and run focused ledger/report/API tests.

## Task 5: Regression and live-paper cutover

1. Run all `tests/test_memecoin_v6*.py`, shared exit-flow/structure/volatility tests,
   schema validation, and `git diff --check`.
2. Commit only named remediation paths.
3. Back up the V6 paper database with SQLite's online backup API.
4. Pin the commit as new V6 and web releases; do not touch `.runtime/memecoin`.
5. Restart only the V6 paper worker and web service through their existing owners.
6. Verify exact pinned SHA, process count/command, watchdog, authenticated health,
   V6 paper endpoints, fresh heartbeat, and logs.

## Explicit exclusions

- No exit threshold, risk scale, or policy-weight tuning from the small cohort.
- No funded runner, funded environment, reservations, or funded database mutation.
- No rewriting or censoring existing V2 positions.
- No claim that the current cohort proves net edge.
- Durable Solana transaction retry and a full rolling-event accumulator need new
  state contracts; do not improvise them in this correctness patch.
