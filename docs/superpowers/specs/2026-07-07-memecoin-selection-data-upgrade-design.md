# Memecoin Selection Data Upgrade — Design

**Date:** 2026-07-07
**Status:** Draft for operator review
**Follows:** `2026-07-07-memecoin-copy-hardening-design.md` (which deferred *selection* to v2 — this is that v2 selection-data layer). Consumes the analyzer in `sniper/analysis/fomo_account/` and the discovery funnel in `2026-07-06-memecoin-wallet-discovery-funnel.md`.

---

## Goal

Give trader selection a **truthful, multi-window profitability view** — "is he green lifetime, green over 7d/30d, and copyable" — built only from data fomo actually exposes. Replace the underpowered per-trade-metric correlations (n=19 stress test: every correlation NS, followers→skill was 2 outliers) with fomo's own server-side lifetime PnL + live mark-to-market + honest windowed realized PnL where the data supports it.

This is **selection input, not a score and not auto-follow.** Output is raw signals + a transparent green/red gate the operator reads. No composite ranking number (per operator: "as much data and statistics as possible, then I'll look at it myself").

---

## Verified reality (live probe, 2026-07-07)

Every claim below was confirmed against live fomo endpoints with our token, and by tracing the fomo.family UI's own network calls.

| endpoint | returns | limit (verified) |
|---|---|---|
| `/v2/users/{id}` , `/v2/users/userHandle/{handle}` | sparse profile: `averageHoldTimeSeconds`, `followers`, `numTrades`, `swapCount`, `totalVolume`, `createdAt` | **no PnL field of any kind** |
| `/v2/users/{id}/swaps` | newest ≤100 swaps; per-swap `humanUsdAmountIn/Out`, tokens, fees, `createdAt` | **hard cap at 100**; 12 paging/time-range params all no-op; **no per-trade PnL** |
| `/v2/users/{id}/balances` | live `balances[]` (current holdings + live price), `livePerpPnl`, `otherPnl`, `nativeEvmBalances[]` | **not currently ingested** |
| `/v2/leaderboard` | per-trader lifetime `totalPnL`, `totalHoldings`, `topHoldings[]` (per-token unrealized), `totalVolume`, `numTrades`, `swapCount`, `followers` | **fixed top-50 by lifetime totalPnL**; `limit` caps at 50; `timeframe`/`period`/`offset`/`page`/`skip` all no-op |

**Consequences that drive the design:**
1. **fomo exposes no windowed PnL.** The UI's 24H/7D/30D chart is reconstructed client-side from `/swaps` + `/balances`, so it inherits the same 100-swap cap (techquant's UI "30D" is really ~14 days — his 100 swaps only reach back that far).
2. **Lifetime PnL is only reliable via `/v2/leaderboard` `totalPnL`** — computed server-side over full history, *not* cap-limited. But only for the top-50 traders.
3. **The 100-swap window spans ~9 days median** (measured across 40 screened traders). So a **7d realized window fits inside the ledger we already pull** for ~65% of traders; **30d does not** for ~68% of them.
4. **`totalPnL` is mark-to-market** (realized + unrealized; `topHoldings` carry live value). A big number can be one unsold moonbag — must be decomposable.

---

## Design

Three additive data sources, one gate. Nothing here mutates the copy engine's execution — it feeds *which traders* the arms act on, which the operator controls.

### A. Leaderboard ingest — lifetime "is he green", + discovery source

A poller calls `/v2/leaderboard` (one call, top-50), and writes a snapshot table plus stamps the analyzer.

- New table `memecoin_leaderboard` (proposed): one row per trader per snapshot — `source_user_id`, `handle`, `rank`, `total_pnl`, `total_volume`, `total_holdings`, `unrealized_pnl` (summed from `topHoldings[].pnl`), `num_trades`, `swap_count`, `followers`, `snapshot_ts`.
- The top-50 becomes a **first-class discovery source** (alongside the large-buy funnel): fomo's own ranked-by-profit universe. Ingested as `memecoin_candidates` with `source='leaderboard'`, operator-gated promotion (same funnel as today).
- On the analyzer output (`memecoin_trader_analytics`), add `fomo_total_pnl`, `fomo_unrealized_pnl`, `on_leaderboard`, `leaderboard_rank` (null when the trader isn't in the top-50).

**Honesty rule:** `fomo_total_pnl` is fomo's blended lifetime number. `fomo_unrealized_pnl` (from `topHoldings`) lets the operator see how much of it is unsold bags vs realized skill. Never present `total_pnl` alone as "skill."

### B. Balances ingest — live mark-to-market

Extend the analyzer's fetch (`sniper/analysis/fomo_account/analyzer.py`) to also call `/v2/users/{id}/balances`.

- New analyzer fields: `live_holdings_value_usd` (Σ `balance.shiftedBalance × tokenFilterResult.price` over memecoin holdings), `live_holdings_count`, `unrealized_pnl_usd` (`live_holdings_value_usd − open_bag_cost_usd`; `open_bag_cost_usd` already exists), `live_perp_pnl`, `other_pnl`.
- This closes the biggest gap in the current analyzer: it reconstructs **realized** FIFO only, so a trader sitting on winners reads as flat. Balances give the unrealized leg the UI shows.

**Coverage caveat (documented in output):** `unrealized_pnl_usd` is only as complete as the ≤100-swap `open_bag_cost_usd` it subtracts from. If a bag was opened by a swap older than the window, its cost basis is unknown → mark `unrealized_partial=1`.

### C. Windowed realized PnL — 7d now, 30d forward

Add windowed realized PnL to the reconstruction (`sniper/analysis/fomo_account/metrics.py`), computed from round-trips by close-time, **gated on whether the window fits the covered span**:

- `realized_pnl_7d`, `realized_pnl_30d`: Σ realized_usd of round-trips closed within the window.
- Each carries a `*_covered` flag: `realized_7d_covered = (window_days ≥ 7)`; `realized_30d_covered = (window_days ≥ 30)`. When not covered, the value is **null**, never a misleadingly-partial number.
- Measured coverage: 7d covered for 26/40 traders, 30d for 13/40. So 7d is usable today for most; 30d mostly isn't from the ledger.

For the windows the ledger can't cover (30d, and 7d for high-velocity traders), use a **daily forward snapshot**:

- New table `memecoin_trader_pnl_snapshots` (proposed): `source_user_id`, `snapshot_ts`, `fomo_total_pnl` (null if off-leaderboard), `cum_realized_usd` (from that day's ≤100-swap window), `unrealized_pnl_usd`, `live_holdings_value_usd`.
- Windowed deltas = difference of snapshots N days apart. **Key property:** a 1-day realized delta only involves round-trips closed in that day, which fit inside 100 swaps unless the trader closes >100/day — so daily deltas are reliable even though cumulative-since-inception is not. Flag `snapshot_saturated=1` if `n_closed_today ≥ 100` (delta may undercount).
- Real 7d after 7 daily snapshots, 30d after 30. Until then, the ledger-derived `realized_pnl_7d` (where covered) is the interim answer.

### The gate (transparent, operator-read)

Not a score. A row of green/red booleans + the raw numbers behind them:

| signal | green when | source |
|---|---|---|
| lifetime green | `fomo_total_pnl > 0` **and** `fomo_unrealized_pnl / fomo_total_pnl < 0.5` (default; tunable — flags a PnL that's mostly one unsold bag) | leaderboard |
| 7d green | `realized_pnl_7d > 0` where `realized_7d_covered`, else snapshot delta | ledger / snapshot |
| 30d green | snapshot 30d delta > 0 (once accrued), else `realized_pnl_30d` where covered | snapshot / ledger |
| copyable hold | `median_hold_s` within a rideable band (not multi-week holder) | ledger (exists) |
| fresh | `last_swap_age_h` recent, `active_days` sustained | ledger (exists) |

Operator weights 7d/30d over 24h (their stated preference — 24h fluctuates). Any `*_covered=false` / `snapshot_saturated=1` / `unrealized_partial=1` renders as an explicit "insufficient data" cell, never a silent zero.

---

## Non-goals

- **No windowed PnL claimed from fomo** — it doesn't exist; anything windowed is our reconstruction or forward snapshot, labeled as such.
- **No composite score / auto-follow** — raw signals + gate only; promotion stays operator-gated.
- **No attempt to page past 100 swaps** — verified impossible; don't add dead pagination code.
- **No ranking of low-coverage traders** — `coverage_ratio` / `*_covered` gate all windowed claims.

---

## Coverage & honesty gating (cross-cutting)

Every windowed or unrealized number ships with its confidence flag. A trader with `coverage_ratio < 0.5` or uncovered windows is shown with those cells greyed, not zero-filled. The stress test that motivated this (n=19, all correlations NS, outlier-driven) is the standing reminder: **do not rank on a metric computed over a truncated slice as if it were full history.**

---

## Testing

- Analyzer: fixture with a known swap ledger + a stub `/balances` payload → assert `unrealized_pnl_usd`, `live_holdings_value_usd`, and 7d/30d covered-flags compute correctly, including the not-covered → null path.
- Leaderboard ingest: stub top-50 payload → assert `memecoin_leaderboard` rows, `fomo_unrealized_pnl` = Σ topHoldings pnl, candidate rows with `source='leaderboard'`.
- Snapshot delta: two snapshots 7 days apart → assert 7d delta = difference; `snapshot_saturated` set when `n_closed_today ≥ 100`.
- Gate: table-driven cases for each green/red + insufficient-data cell.
- Schema: `python3 scripts/brain_index.py generate` + `validate_schema_refs.py` after new tables land.

---

## Schema (proposed — additive only)

<!-- schema:external=memecoin_leaderboard -->
<!-- schema:external=memecoin_trader_pnl_snapshots -->
<!-- schema:external=memecoin_trader_analytics -->

New tables: `memecoin_leaderboard`, `memecoin_trader_pnl_snapshots`.
New columns on `memecoin_trader_analytics`: `fomo_total_pnl`, `fomo_unrealized_pnl`, `on_leaderboard`, `leaderboard_rank`, `live_holdings_value_usd`, `live_holdings_count`, `unrealized_pnl_usd`, `unrealized_partial`, `live_perp_pnl`, `other_pnl`, `realized_pnl_7d`, `realized_7d_covered`, `realized_pnl_30d`, `realized_30d_covered`.

All additive; no existing column changes; no writes to the copy-execution path.
