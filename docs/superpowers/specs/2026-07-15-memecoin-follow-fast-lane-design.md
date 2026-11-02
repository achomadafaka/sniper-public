# Memecoin follow-activity fast lane + live-mcap fallback — design (2026-07-15)

## Why (verified 2026-07-15, live DB)

Follow-activity copies bucketed by catch latency (`entry_lag_s`), honest alpha_b cut
(paper, closed, fomo-fill-excluded):

| catch latency | n  | total    | avg/copy | WR  |
|---------------|----|----------|----------|-----|
| <60s          | 9  | +$712.64 | +$79.18  | 78% |
| 60–180s       | 13 | −$203.52 | −$15.66  | 15% |
| 180–600s      | 36 | −$516.45 | −$14.35  | 17% |

The signal is not bad — it is a **latency cliff**. Caught fresh it beats the global
feed (+$3.88/copy honest); caught minutes late it loses. Caveat: n=9, and one
Binkieee copy is +$543 of the +$713 (ex-whale +$21/copy, 7/8 non-negative —
directional, not proven). This ships as a measured bet, not a verdict; read the
fast-lane bucket EV as it grows.

Why we're slow today (verified in code): ONE background worker polls all active
curated wallets (107 now) with a 2s WAF-safe sleep per wallet (~214s/sweep), then
sleeps `MEMECOIN_FOLLOW_ACTIVITY_PERIOD_S` (300s) — worst-case catch latency ≈9min,
so the 5-min poll dumps nearly everything into the losing buckets **by construction**
(`scripts/memecoin_runner_loop.py` `_activity_worker`,
`sniper/services/memecoin_follow_activity.py:207`).

Second blocker: follow-activity events carry `their_market_cap=0.0`
(`memecoin_follow_activity.py:146`). A fresh catch on a NEW coin has no same-mint
feed history either → the real-entry mcap gate returns `unresolved` → fail-closed
`skipped_mcap`. The fast lane is pointless for real money without a live-mcap
fallback on that branch.

## What ships

### 1. Two-lane follow-activity polling

- **Fast lane** (new background worker): polls ONLY the effective armed set
  (`real_approved_user_ids` ∩ active curated — re-read every sweep so arm/revoke
  applies live) in a continuous loop: ~31 wallets × 2s ≈ 62s/sweep +
  `MEMECOIN_FOLLOW_FAST_IDLE_S` (default 5s) between sweeps → armed catches land in
  the +EV <60–70s band.
- **Slow lane** (existing worker unchanged in cadence): all OTHER active curated
  wallets on the 300s cycle (paper/coverage). When the fast lane is enabled the slow
  lane EXCLUDES armed wallets (WAF budget; correctness does not depend on this —
  `insert_event` dedups by swap id, so overlap can never double-open).
- `follow_activity_pass` gains `wallets=None` (explicit roster) and
  `exclude_uids=None` params; default behavior byte-identical.
- No new signal_source: fast-lane copies stay `follow_activity`; freshness is
  measured by `entry_lag_s` (the eval axis IS the lag bucket).
- Kill switch: `MEMECOIN_FOLLOW_FAST_ENABLED` (default on; `0` reverts to the
  single-lane behavior exactly).

### 2. Live-mcap fallback on the real-entry gate (would-skip branch only)

In `make_real_hook`, when `mcap_entry_decision` returns `unresolved` (event mcap
missing AND no same-mint history), do ONE live stats lookup (`stats_fn`, ~300ms —
this path is seconds-late by construction, the lookup is free) and re-run the same
decision on its `market_cap`, including the AUTO_TEMPLATES big-cap branch. Still
fail-closed when the lookup has no mcap. Side benefit: `eff_mc` now feeds
`entry_impact_cap_for_mcap`, so fresh catches get the correct mcap-tiered impact cap
instead of the flat base.

## Non-goals

- No change to paper arm exits, the pre-registered A/B, promote/demote governance,
  chase guard (a coin that already ran is refused regardless of catch speed), or
  staleness budgets (`MEMECOIN_REAL_FOLLOW_MAX_AGE_S` 900s stays).
- No global-feed changes — its job is unchanged; the fast lane covers the ~50% of
  curated buys the feed misses.

## Rate-limit budget (measured constants)

global feed poll ~0.67 rps + fast lane ~0.5 rps + slow lane ~0.25 rps ≈ 1.4 rps
against fomo — same per-call 2s spacing as today (`MEMECOIN_ANALYZER_SLEEP_S`).
If fomo 429s, both lanes already back off via fetch_recent_swaps' handling; the
fast lane adds no new hammering pattern, just a smaller roster more often.

## Eval (pre-registered)

Read the fast lane at ≥30 closed armed-wallet follow_activity copies with
entry_lag_s<120s (honest alpha_b cut). Success = avg/copy > global_feed honest
avg/copy over the same window. Do not peek earlier; do not read the whale-inflated
historical n=9 as proof.
