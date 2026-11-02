# Hyperliquid Microstructure Paper Engine — Design

**Date:** 2026-06-20
**Status:** implementation lane
**Branch:** `feat/hl-microstructure-paper`
**Scope:** live paper trading only. No private keys, no live orders, no authenticated exchange actions.

## Thesis

The old BTC 5m spot algorithm is too coarse for Hyperliquid execution. The useful edge, if any, is in short-horizon conditional pressure:

- aggressive trade flow,
- book pressure and spread/depth changes,
- volatility bursts and exhaustion,
- funding/open-interest regime,
- liquidation/whale stress,
- cross-exchange lead-lag.

The system should act live in paper mode while it collects data. Passive capture alone is not enough. Every decision interval must produce either a paper action or an explicit skip reason.

## Hard Boundaries

- Paper only.
- No private keys.
- No live order endpoint.
- No production DB mutation.
- No expected-PnL or sizing advice to users.
- No random train/test split for promotion.
- No edge claim from synthetic or replay-only data.
- Replay can diagnose; live paper decisions are the primary record.

## Official Data Surface

Hyperliquid public WebSocket supports `l2Book`, `trades`, `bbo`, `allMids`, `candle`, `activeAssetCtx`, and user-oriented streams. `l2Book` includes levels and a book timestamp; `trades` include side, price, size, hash, time, users, and `tid`; `bbo` sends best bid/ask changes; `activeAssetCtx` carries mark, oracle, funding, OI, and 24h volume.

Hyperliquid info endpoints support REST recovery and context: `l2Book`, `candleSnapshot`, `metaAndAssetCtxs`, `clearinghouseState`, `fundingHistory`, `predictedFundings`, and `userFees`. Fees are user-tiered, so the paper engine must persist the configured fee profile and treat it as an assumption until a real account-specific `userFees` read is supplied.

Sources:

- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees

## Architecture

Package:

`sniper/algos/hl_microstructure/`

Initial command:

`python3 scripts/run_hl_microstructure_paper.py --assets BTC,ETH,SOL --db-path data/hl_microstructure_paper.sqlite`

The command opens public Hyperliquid WebSocket streams, maintains per-asset book/trade/context state, builds decision-time features, runs strategy lanes, simulates taker paper fills against visible book depth, and persists all decisions.

## Strategy Lanes

All lanes run in parallel. A lane can enter, exit, hold, or skip.

1. `baseline_no_trade`
   - Always skips.
   - Control lane.

2. `flow_momentum`
   - Enters with aggressive trade-flow imbalance when short return confirms direction.
   - Intended to test continuation after real taker pressure.

3. `book_pressure`
   - Enters when book depth imbalance and flow point the same way while spread is acceptable.
   - Intended to test whether queued liquidity imbalance predicts next move.

4. `vol_spike_reversal`
   - Enters against an extreme short move only when flow starts to fade.
   - Intended to test exhaustion, not quiet mean reversion.

Future lanes:

- liquidation pressure,
- whale entry follow,
- funding/OI unwind,
- cross-exchange lead-lag.

These stay out of the first implementation unless their required data is available live.

## Feature Snapshot

Each feature snapshot is keyed by `(asset, decision_ts_ms, feature_version)`.

Required fields:

- `mid`
- `best_bid`
- `best_ask`
- `spread_bps`
- `bid_depth_5`
- `ask_depth_5`
- `book_imbalance_5`
- `buy_notional_10s`
- `sell_notional_10s`
- `flow_imbalance_10s`
- `trade_count_10s`
- `return_5s`
- `return_15s`
- `realized_vol_60s`
- `funding`
- `open_interest`
- `mark_price`
- `oracle_price`
- `source_age_ms`

Known-at-time rule:

Only observations with `receive_ts_ms <= decision_ts_ms` may influence a feature. Missing data stays `null`; it does not get imputed.

## Paper Execution

Taker-only first.

Entry and exit fills cross the visible book:

- long entry: buy asks,
- long exit: sell bids,
- short entry: sell bids,
- short exit: buy asks.

If the full fixed paper notional is not fillable, the decision is recorded as `SKIP` with reason `insufficient_depth`.

Default assumptions:

- `notional_usdc=25`
- `leverage=1`
- `taker_fee_rate=0.00045` unless configured
- no compounding
- one open paper position per `(policy, asset)`

The fee rate is a persisted assumption. A strategy cannot be promotion-ready until a live account-specific fee source is recorded.

## Persistence

Dedicated SQLite namespace:

- `hlm_raw_events`
- `hlm_feature_snapshots`
- `hlm_decisions`
- `hlm_positions`
- `hlm_fills`

Every paper decision records:

- policy,
- asset,
- action,
- reason,
- decision timestamp,
- feature snapshot id,
- book timestamp,
- fee assumption,
- code version,
- status.

No interval is silent: if there is no trade, it is a skip with a reason.

## Readiness Gate

Ready to start live paper trading when:

- CLI starts without private key or order flags.
- Fixture mode runs without network.
- Real mode is capable of connecting to public WS.
- Unit tests prove book fill semantics, known-at-time feature filtering, policy skips/actions, and persistence.
- Decisions and skips persist.
- No code path can submit live orders.

Promotion beyond paper requires a later spec and explicit approval.

## Initial Reuse

Reuse:

- `sniper/algos/btc5m/features.py` ideas for z-score, realized volatility, EMA, volume spike.
- `sniper/algos/polymarket_crypto5m/` patterns for known-at-time, feature snapshots, skip reasons, and readiness gates.
- `sniper/markets/hyperliquid_api.py` for future public wallet/leaderboard reads.
- `scripts/hl_research_v2.py` methodology ideas: per-asset testing, block bootstrap, non-overlap thinning.

Do not reuse directly:

- `sniper/execution/hyperliquid_paper.py` because it hardcodes fee assumptions and lacks visible-book fills.
- `sniper/strategies/hl_liquidation_v1.py` because it starts from one thesis before data quality is established.
- `sniper/feeds/hyperliquid_ws.py` because it wraps an archived streamer.

## First Build

Build only the live paper-action harness:

- book state + taker fill,
- rolling feature state,
- simple policy lanes,
- paper position manager,
- persistence,
- fixture-mode CLI,
- public-WS real-mode CLI.

No UI in this pass.
