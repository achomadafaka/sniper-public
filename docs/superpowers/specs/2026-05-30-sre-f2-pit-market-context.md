# SRE-F2 — Point-in-time market context (the counterfactual foundation)

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Purpose:** answer, for any past timestamp/block T: *which markets were tradeable, and what was each one's state (price, age, volume, crowd)?* This is what makes "the markets she SKIPPED" computable. Without it the engine has no counterfactual.

## Data reality (from the 2026-05-29 feasibility study)
- Becker `markets/*.parquet`: `created_at` (89%), `end_date` (99.7%), `clob_token_ids` (JSON `[yes,no]`), question, category. → universe membership + token↔condition map.
- Becker `trades/*.parquet`: `block_number`, `maker/taker`, `maker/taker_asset_id`, `maker/taker_amount`. **`timestamp` is 100% null** → time inferred from block (~2.1s/block; relative order exact, absolute ±1–2 weeks).
- **No order-book/liquidity** anywhere → liquidity proxied by trade density.

## Preprocessing artifacts (build once)
1. `pit_block_time` — sorted `(block_number → approx_unix_ts)`. Calibrate against a public Polygon RPC block-header sample to tighten absolute error; relative is exact.
2. `pit_token_condition` — `{outcome_token_id → conditionId}` from `clob_token_ids` (O(410K) one-time).
3. `pit_market_meta` — per condition: `created_at, end_date, category, market_type, outcome_role(s), favorite_rank, event_family, template`.
4. `pit_price_index` — per token, the trade-price time series (derived price = USDC-leg amount / token-leg amount; verified ∈[0,1]). Stored coarsened (e.g., 1-min/last-trade) for fast nearest-lookup.

## API
```python
universe_at(T, *, filters: dict | None) -> list[MarketState]   # markets with created_at <= T <= end_date (+ filters)
market_state_at(conditionId|token, T) -> MarketState           # price, age, ttr, vol_in_window, n_trades, n_wallets
price_at(token, T) -> (price, staleness_s)                      # nearest trade; staleness flags low-volume gaps
```
`MarketState`: `conditionId, token, price, price_staleness_s, market_age_s, time_to_resolution_s, frac_life_elapsed, vol_window_usdc, n_trades_window, n_distinct_wallets_window, category, market_type, outcome_role, favorite_rank, event_family`.

## Fidelity tiers (must be emitted with every answer)
- `active`: ≥ N trades within ±window → price/vol trustworthy.
- `sparse`: < N trades → price stale; usable for *membership* but flag for selection-feature tests.
- `pre_trade`: market existed (created_at ≤ T) but no trades yet → tradeable-but-illiquid.

## Counterfactual sampling (consumed by F3)
Given an engagement at (T, group), return the unpicked members of that group at T, with a `sampling` mode: `all` (small groups), `stratified` (preserve feature distribution), or `nearest_k` (propensity match on intrinsic features → scope C7).

## Honest limits (carry into every downstream stat)
- absolute-time ±weeks → calendar-event alignment (T9) is coarse from Becker; use live-API timestamps for recent wallets where precise.
- no liquidity → "could she have filled" is unanswerable historically (the stink-bid wall) → executability proven only by forward paper.
- universe coverage ~89% (null created_at) → report per-group coverage; never claim a complete skip-set.

## Acceptance
1. `universe_at(T)` count matches the feasibility-study spot check (~2,480 open on 2025-01-15).
2. token→condition round-trips a known wallet's trade to its market.
3. price_at fidelity tier correlates with trade density (active tier ⇔ dense token).
