# Universe Scanner — Recall Audit + Tier 1.5 Sentinel (agile)

Incremental scanner upgrade. New signals/scores are computed and logged in SHADOW on every
refresh; they do NOT drive live subscription until a recall horse-race beats baseline on
held-out/forward time blocks. Move fast AND never hand-tune the scanner blind.

Priority order (never optimize downstream of a broken upstream):
`scanner recall → selection quality → entry timing → exit logic`

## Hard do-not list
- No change to live subscription / top-12 selection until Step 6 promotion bar is met.
- No live orders, no keys, no prod-DB writes outside the new append-only scan log.
- Forward labels are universe-selection labels, NEVER "PnL".
- `lead_reason` (UI explanation) never defines the model; ranking ≠ explanation.
- No promotion off in-sample fit — held-out/forward blocks only.

## Step 0 — perishable all-230 log (ship today, zero risk)
Persist one row per asset per refresh for ALL ~230 markets (not just the 12). Append-only,
known-at-time. Columns: scan_ts, asset, selected_rank, selected_bool, already_watched_bool,
hand_score_v0, score_components, lead_reason, dayNtlVlm, markPx, oraclePx, midPx, funding,
premium, prevDayPx, openInterest, prior_openInterest, oi_delta, refresh_interval_s,
watchlist_state. Every refresh not logged is recall data we can never reconstruct.

## Step 1 — shadow change/rate components (parallel, shadow only)
From the all-230 cache, compute and log beside the live score (do NOT drive selection):
basis level + basis_change_150s + basis_acceleration + sign; mid_return_150s; oi_delta_pct +
oi_acceleration + oi_collapse_flag; premium_change_150s. Headline = mark-oracle
**acceleration** (a rapidly widening basis is a precursor; a long-wide basis is not). Store
`hand_score_v0` (live) and `change_score_v0` (shadow) per row.

## Step 2 — forward opportunity labels at honest resolution (parallel)
Label forward moves from FUTURE CONTEXT only (not deep book). All markets: 150s/300s abs move
+ dir (context cadence). Watched set only: 30s/60s (deep feeds). You cannot honestly compute
30s/60s for the unwatched 218 — using fine labels for all would re-bias the audit. Conservative,
never PnL: `cost_clearing_proxy_move = future_abs_move > max(10 bps, recent_spread_proxy)`.

## Step 3 — first recall readout (after a few days)
recall@12 / precision@12 vs cost-clearing proxy; lift vs volume-only and 24h-move-only;
missed-opportunity count + notional; selected-before-move vs after; time-to-detection;
stale-dislocation over-selection?; midcap-vs-major bias?

## Step 4 — Tier 1.5 sentinels (gated by Step 3; shadow, lift-judged)
Alert layer BESIDE Tier 1 (don't overload its score). S1 mark-oracle movement; S2 BTC→alt
internal lead (zero ingestion); S3 OI shock (delta/zscore/accel/collapse); S4 liquidation/whale
stress — **availability check first**: is a global liq feed queryable in one cheap call? if
per-market/subscription-gated, mark unavailable, don't block. S5 cross-venue placeholder
(null until Binance capture lands; report coverage %). **Emergency-promote:** a single large
liq (≥$500k) on an unwatched market promotes it immediately as a temp 13th, bypassing the
150s cycle + 12-cap (requires S4 global feed; else defer).

## Step 5 — hysteresis (once promotion is possible)
promote if rank ≤ 12; keep until rank > 18 for two refreshes; min_watch_duration;
cooldown_after_unsubscribe; emergency_promote overrides.

## Step 6 — score horse-race → promote (self-calibration)
Compare on held-out/forward blocks: hand_score_v0 (baseline) vs change_score_v0 vs
sentinel_score_v0 vs hybrid_score_v1. Metrics: recall@12, selected-before-move, precision@12.
Promote to live ONLY when it beats hand_score_v0 forward — never in-sample.

## Step 7 — continue to SELECTING (on a proven watch set)

## Validation
Walk-forward only; all-market recall on 150s/300s labels (fine labels only where feeds exist);
cost-clearing labels conservative, never PnL; net-of-fee+slippage hurdle; BH-FDR 0.1 across
sentinel candidates; negative controls for lead signals (wrong-asset, reversed-lag, day-shuffle);
live selection untouched until Step 6.

## Implementation order
0 log all-230 (today, perishable) · 1 shadow change/rate · 2 forward labels · 3 recall readout
· 4 sentinels+emergency (S4 availability first) · 5 hysteresis · 6 horse-race→promote · 7 SELECTING.
Steps 0–2 cost almost nothing and start the clock; everything after is gated by the data.
