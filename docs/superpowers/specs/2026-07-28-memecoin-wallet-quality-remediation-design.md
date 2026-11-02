# Memecoin wallet-quality remediation design

**Date:** 2026-07-28
**Status:** Approved for implementation and activation by operator directive
**Authority:** Wallet observation, research evidence, operator curation, and UI/API performance. No funded policy, sizing, entry, exit, or automatic arming change.

## Goal

Keep the most relevant wallets visible without manufacturing confidence or automatically changing funded authority. Fresh discovery, honest copy evidence, roster state, and historical evidence remain separate.

## Invariants

- Missing realized-PnL tiles are unknown, never zero and never a counted day.
- One wallet has at most one effective snapshot row per corrected-UTC calendar day.
- Automated logic may refuse or revoke funded admission. Only the operator may admit or re-admit.
- Re-observing an excluded wallet always clears funded approval.
- Fomo-priced fills never grade wallets or exit-policy arms.
- Wallet evidence is chain-aware and exact-event paired where comparisons claim shared entries.
- Discovery activity and quality remain separate columns; there is no composite wallet score.
- Existing funded positions, frozen contracts, exit rails, reservations, and V6 policy remain unchanged.

## 1. Snapshot truth

`record_pnl_snapshot` becomes calendar-day idempotent. A repeat updates the existing day and preserves a non-null realized tile when the repeat only carries leaderboard lifetime PnL. `windowed_realized` counts only days with a non-null realized tile. Snapshot-derived 7d and 30d verdicts require 7 and 30 valid days respectively. The runner seeds its daily latch from persisted snapshot state so restart does not repeat the leaderboard call.

No destructive cleanup of old duplicate rows is required for activation. Reads collapse old rows by day, and future writes stop growth.

## 2. Roster lifecycle

Candidate promotion validates identity and existence and commits wallet plus candidate state atomically. An excluded candidate cannot be silently promoted; the API returns `excluded_requires_reobserve`. The explicit re-observe operation sets `excluded=0`, `paused=0`, and `real_approved=0` in one transaction.

Deleting a manual wallet becomes retirement to excluded/watch-only state rather than physical deletion. Ignored candidates remain archived and can be explicitly reconsidered. No state revives automatically.

## 3. Discovery

The default Discovery queue is rolling activity, ordered by `last_seen` then rolling seven-day buy notional and count. Lifetime totals remain display-only. Rows expose exact latest network identity, current roster state, and total result count. Pagination and status filters prevent the first 200 lifetime accumulators from hiding fresh candidates.

An operator-only Reconsider section shows excluded or ignored wallets with recent feed-wide activity. Re-observe/reconsider never arms them.

## 4. Honest wallet evidence

The recommender excludes every position with any fomo-priced fill and defaults to current non-excluded roster rows. Historical/excluded evidence is available only through an explicit query flag. Robustness operates on distinct coin episodes, not repeated individual fills from the same origin coin.

The Wallet x Arm surface uses exact-origin intersections and honest fills. If an arm lacks the same origin set, it is not compared. The UI drops `+EV` and causal wording unless the returned set is exact-paired.

Analyzer FIFO accounting remains unchanged. Display and confidence fields distinguish matched lots from distinct exits and distinct coins. Partial bag evidence is insufficient. Obsolete hold-gate configuration is removed.

## 5. Churn and performance

Wallet statistics and last-signal reads become grouped queries. Pump aggregation accepts a requested UID set. Dead entry-quality work is removed from the combined route. Wallet tabs poll at 30 seconds, refresh immediately after operator mutation, and use an in-flight guard. Analyzer launches use a single process lock and return `409 already_running` rather than starting overlapping jobs.

Add only the SQLite indexes required by measured query plans. No external cache or service is introduced.

## Activation

Implementation lands in focused commits with RED-first tests. After the Knowledge Sync Gate and full memecoin suite, publish through a PR, merge through GitHub, deploy the exact merged SHA using `scripts/deploy_memecoin_runtime.sh <sha> --restart`, restart the web surface from its pinned/current workflow, and verify release SHA, one runner, heartbeat, authenticated health, reservations, kill switch, and relevant API payloads.

## Acceptance

1. Null-only snapshot days produce insufficient windows.
2. Repeating a daily pass does not add rows or erase a realized tile.
3. Excluded/nonexistent candidate promotion performs no partial mutation.
4. Re-observation returns watch-only and never funded-approved.
5. Fresh candidates outside the old lifetime top 200 are reachable through the first page or pagination.
6. Recommender and arm matrix exclude fomo-priced fills and unrelated historical wallets.
7. Exact-paired arm copy is truthful.
8. Wallet endpoints avoid per-wallet position/event scans.
9. No overlapping analyzer processes can be launched from the UI.
10. Focused tests, full memecoin tests, schema validation, compile checks, deploy dry-run, deploy, and live probes pass.
