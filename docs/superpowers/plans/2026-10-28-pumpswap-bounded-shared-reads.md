# Bounded PumpSwap shared reads

> Execution: inline by Delta; Zeta reviews the committed SHA. Shared checkout, preserve foreign WIP.

Goal: remove historical pool walks without changing receipt membership, chain order, units, state proof, or live authority.

Contract: Alpha's 2026-10-28 operator-order amendment. MIN/MAX(slot) comes from an indexed received_at window in the same SQLite statement. The outer query searches swaps_ps_pool_slot with both slot bounds. No slots/second heuristic. A proof seed starts at ps_keep.keep_from when present, otherwise lo minus PS_ARCHIVE_AFTER_S (two days). Absent proof remains unknown.

Scope: memecoin_desk_ps_tape plus lessons, risk_tape, creator_dump_h5, ps_asof, ps_paper, ps_evidence. One shared archive-age constant with the universe worker, coordinated with Beta. Existing callers outside these modules inherit bounded PT reads. Fixtures gain the production indexes; no production index builds or tape writes.

Edge cases: NULL/bad slots cannot vanish through MIN/MAX; delayed old slots stay in the receipt window; exact cutoff excludes future slots; seed absence is not zero; malformed keep bounds and read/index failures fail closed; empty windows stay empty; no module/global connection mutation. These are observation readers, never funded entry provider calls, signing, sizing or submissions. No new RPC, lock or retry. Measure fixture p50/p90, not live fill latency.

Not handled: reading archived swaps, backfilling missing coverage, general research-script cleanup, runtime activation. Unknown state outside the approved seed interval stays unknown.

1. [x] RED: real SQLite plans require received range plus lower/upper pool-slot search; both rowid layouts, null slots, delayed slots, row limits, missing indexes, seed keep/fallback bounds and rollback/read failure.
2. [x] Implement one shared statement builder/reader. Migrate the named callers; preserve result shapes and typed refusals. No catch-to-empty fallback.
3. [x] GREEN: focused shared-reader/caller suites with -n 2, schema-reference check, diff check. Verify the test fails when bounds are removed. Record bounded-fixture p50/p90.
4. [ ] Commit owned files through scripts/crew_commit.sh. Send Zeta exact SHA and evidence; no deploy or .env changes.

Validation: 645 focused checks passed, one inherited archive-I/O expected failure. Removing the outer slot bounds makes both layout-plan regressions fail. Synthetic helper p50/p90: rowid 0.208/0.213 ms, WITHOUT ROWID 0.275/0.278 ms; two-day fallback seed with 10,000 unrelated recent rows 0.610/0.623 and 2.105/2.149 ms. No production latency measurement.
