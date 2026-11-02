# Stink Bid Simulator Reliability Upgrades

Date: 2026-04-25

## Goal

Make the tennis stink-bid simulator a reliable historical execution engine, not just an optimistic signal tester. The simulator should be able to answer:

- Was the favorite known from data available at that historical time?
- Would the runner have placed a resting bid with the same rounded price and lifecycle rules?
- Did a real trade occur at or below our bid while the order was active?
- Was the trade large enough to satisfy the live `min_fill_size` rule?
- Did resolution/winner mapping use only post-event result data, never future data during entry?

## Current State

The current backtest is useful for finding the shape of the edge, but it is not yet a source of truth for executable fills.

Recent results:

- `price_cross` mode found profitable configurations, including the best combined ATP/WTA run with 36 fills, 75% win rate, and positive PnL.
- `trade_confirmed` mode was much stricter and produced only a few confirmed fills from the currently fetched trade dataset.
- The audit ledger now explains skipped markets, no-fills, and fills per event.

The main reliability gap is execution parity. The live paper engine and smoke simulator do not yet share identical fill semantics.

## API Facts Checked

Official Polymarket docs confirm:

- Market data is public and split across Gamma, CLOB, and Data APIs. CLOB is used for prices/orderbooks/history, Data API is used for trades and analytics.
- `GET /prices-history` is token/asset based via `market`, supports `startTs`, `endTs`, `interval`, and `fidelity`; default fidelity is 1 minute.
- Data API `GET /trades` supports `market`, `limit`, `offset`, `takerOnly`, `user`, and `side`, with `side` values `BUY` and `SELL`.
- Authenticated CLOB `GET /trades` supports `market`, `asset_id`, `before`, `after`, cursor pagination, and returns confirmed trade records for the authenticated user.

Sources:

- https://docs.polymarket.com/market-data/overview
- https://docs.polymarket.com/api-reference/markets/get-prices-history
- https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
- https://docs.polymarket.com/api-reference/trade/get-trades

## Local Findings

Live paper engine behavior:

- `paper_limit_engine.compute_bid_price()` rounds bids to 2 decimals.
- `PaperLimitEngine.check_fill()` requires a resting order, matching favorite token, and `tick.price <= bid_price`.
- A strict fill requires `tick.size >= min_fill_size`.
- If price crosses but size is too small, the engine logs a loose/sensitivity fill and keeps the order resting.
- Strict fill price is the observed tick price, not the bid price.

Current smoke simulator behavior:

- `price_cross` fills from price history when sampled price crosses the bid, with a modeled half-spread adjustment.
- `trade_confirmed` checks real trade rows, but currently filters `side='SELL'`, does not enforce `min_fill_size`, and returns the bid as fill price instead of the observed trade price.
- The simulator computes raw bids without live 2-decimal rounding.
- The simulator reprices only downward, while the live engine's `reprice_order()` can move both directions if the diff clears threshold.

## Design Decision

Add explicit simulation modes instead of blending assumptions:

1. `exploratory_price_cross`
   - Current broad signal-finding mode.
   - Useful for discovering possible edge, not for claiming executable PnL.

2. `runner_parity_trade`
   - Mirrors the live paper runner as closely as historical data allows.
   - Uses 2-decimal bid rounding.
   - Uses real trade rows, matching token, active order window, price at or below bid, and strict `min_fill_size`.
   - Fill price is observed trade price.
   - Logs loose fills separately.

3. `ideal_strategy_trade`
   - Tests the strategy thesis with intended/fixed rules, such as down-only repricing.
   - Useful for deciding whether to change the live runner.
   - Must be labeled separately from runner parity.

## Upgrade Plan

### Phase 1: Runner-Parity Fill Model

Add `runner_parity_trade` to `scripts/simulate_stink_bid_smoke.py`.

Required config fields:

- `min_fill_size`
- `bid_rounding`
- `fill_price_mode`
- `reprice_mode`
- `favorite_flip_cancel`
- `max_resting`
- `max_daily_fills`

Implementation rules:

- Use the same bid computation as `paper_limit_engine.compute_bid_price()`.
- An order can fill only after it is placed and before cutoff/expiry/cancel.
- Trade match requires same `condition_id`, same favorite `asset`, `price <= bid`, and `size >= min_fill_size`.
- Loose fills should be recorded when price crosses but size is below `min_fill_size`.
- Fill price should be the observed trade price.
- Output should include the exact trade row evidence: timestamp, price, size, side, outcome, transaction hash.

Tests:

- Fill rejected before order start.
- Fill rejected after cutoff.
- Fill rejected for wrong asset.
- Loose fill does not create a trade.
- Strict fill uses trade price, not bid price.
- Bid rounding matches `paper_limit_engine.compute_bid_price()`.

### Phase 2: Execution Audit Ledger

Extend `SimAttempt` into a full order lifecycle ledger.

Events to emit:

- `market_seen`
- `favorite_selected`
- `order_placed`
- `order_repriced`
- `loose_fill_seen`
- `strict_fill_seen`
- `order_cancelled`
- `order_expired`
- `position_resolved`

Each event should include:

- historical timestamp used by the simulator
- favorite price at that timestamp
- current bid
- active/cancelled/resting state
- reason code
- evidence fields when sourced from a trade row

Add invariant checks:

- No fill before placement.
- No fill after cancel/cutoff.
- No decision uses future result data.
- Winner data only affects resolution, never entry.
- Favorite and bid are derived from prices available at or before the simulated cycle.

### Phase 3: Trade Data Coverage and Resume Safety

The confirmed-fill result is currently limited by partial trade ingestion. We need coverage accounting before trusting low fill counts.

Add a coverage table:

- `condition_id`
- `start_ts`
- `end_ts`
- `pages_fetched`
- `rows_fetched`
- `hit_page_cap`
- `first_trade_ts`
- `last_trade_ts`
- `fetched_at`

Backfill improvements:

- Log progress every N markets.
- Persist per-market fetch status.
- Resume without re-fetching complete windows.
- Flag markets where pagination hit the cap.
- Add a command that reports price coverage, trade coverage, alignment coverage, and result coverage before any optimizer run.

### Phase 4: Rerun Optimizer Under Strict Modes

After Phase 1-3:

- Rerun combined, ATP-only, and WTA-only with `runner_parity_trade`.
- Require minimum sample sizes before ranking configs.
- Report fills, loose fills, no-fill reasons, trade coverage, profit factor, drawdown, t-stat, and bootstrap confidence intervals.
- Split by year/tour so 2025 results do not hide weak 2026 behavior.

Decision rule:

- If strict confirmed fills remain too low after full trade coverage, the strategy is not historically execution-proven yet.
- If strict confirmed fills are enough and still profitable, move to walk-forward validation.

### Phase 5: Walk-Forward Before ML

Do not move to ML until the engine can reproduce a strict execution ledger.

After runner parity is stable:

- Train/tune on older years or earlier months.
- Validate on later months.
- Freeze params before scoring the holdout.
- Track degradation as Polymarket pricing improves.

ML readiness requires:

- Reliable no-lookahead feature store.
- Enough strict confirmed fills.
- Stable market/result alignment.
- Execution labels that distinguish strict fill, loose fill, no fill, and winner outcome.

## Immediate Next Implementation

Build Phase 1 first. The highest-value patch is to add `runner_parity_trade` and make the audit output show every rejected/accepted trade candidate around the bid.

After that, run:

```bash
python3 -m pytest -q tests/backtest/test_stink_bid_smoke_sim.py tests/backtest/test_stink_bid_optimizer.py
python3 scripts/optimize_stink_bid_params.py --fill-model runner_parity_trade --min-fills 5
```

The second command may need trade backfill coverage first; if it produces too few fills, that is an evidence gap to fix, not a reason to trust the optimistic mode.
