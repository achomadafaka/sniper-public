# HL Trader Intelligence — repurposing the Polymarket analyzer for Hyperliquid

Date: 2026-06-22
Status: design → phase 1 build
Branch: research/hl-measurement-audit-leadlag

## Premise

Hyperliquid publishes every account's positions, fills, leverage, and liquidation price
(`clearinghouseState`, `userFills`, `fetch_leaderboard`). That makes the Polymarket
wallet-analyzer doctrine directly applicable: **wallet intelligence → skill extraction →
copyability gate → archetype → (later) forward-paper follow.** This is the "WATCH" layer of the
professional-day-trader engine — seeing what the actual pros do — and it doubles as the
discovery feed for the liquidation-cluster map.

This is NOT copy-trading advice or a live trigger. Like the Polymarket analyzer, it extracts
reusable theses, **rejects** weak/uncopyable patterns, and names the data needed to validate.

## The hard lesson it must encode (from Polymarket)

The counterfactual/copyability gate killed every Polymarket "copyable" wallet (SwissMiss,
0x20ad75 — all downgraded). The same will happen on HL: leaderboard PnL is pure survivorship,
and a large share of HL profit is **market-maker rebate / latency / degen-leverage variance —
uncopyable.** The analyzer's job is to reject those. Expect mostly rejections; that's it working.

## What already exists (reuse, don't rebuild)

- `sniper/markets/hyperliquid_api.py`: `fetch_leaderboard()`, `fetch_user_fills(addr)`,
  `fetch_clearinghouse_state(addr)`.
- `scripts/screen_hl_traders.py`: fills-based skill screen (sample, profit factor, concentration, WR).
- `scripts/hl_wallet_breakdown.py`: per-coin/side edge-location breakdown.
- We already harvest active HL addresses for the cluster-map.

## What's missing (this build)

1. **Copyability/archetype classification** — `userFills.crossed` (taker vs maker) + `fee` sign
   (negative = rebate) directly separate a directional taker from a market-maker. NEW; this is
   the SwissMiss gate, computable from fills.
2. **Funding-inclusive true PnL** — existing screen omits funding; flag it and fold in later.
3. **Live leaderboard discovery + persistence filter** (positive across multiple windows;
   risk-adjusted by roi, not raw PnL).
4. **(Later) forward position ledger** — snapshot `clearinghouseState` over time to act on NEW
   entries and reconstruct funding. Powers both smart-money-follow and the true cluster-map.

## Phase 1 (this slice) — the analyzer brain

`sniper/hyperlive/trader_intel.py` (pure, TDD):
- `reconstruct_ledger(fills, funding=None)` → realized PnL = Σ closedPnl − Σ fee (fee sign-aware:
  negative fee is a rebate); `funding_included` honesty flag; gross win/loss, profit factor.
- `skill_metrics(fills)` → n_closes, win_rate, profit_factor, top-coin concentration, single-fill
  PnL concentration (one lucky leveraged hit?).
- `archetype(fills)` → maker_frac = share of fills with `crossed == False`; classify:
  `market_maker` (maker_frac high, rebate-driven) / `degen_leverage` (few closes or PnL
  concentration high) / `directional` (taker-dominant, many closes, PF>1) / `mixed`.
- `copyability(metrics, arch)` → `copyable` only if `directional` AND taker-dominant AND PF>1 AND
  enough sample AND not concentration-dominated. Returns reason. (The full price-band
  counterfactual needs forward data — phase 2; maker_frac is the available proxy now.)
- `assess_trader(fills, state)` → the combined verdict dataclass-like dict.

`scripts/screen_hl_smart_money.py`: pull live leaderboard → persistence + risk-adjusted prefilter
→ run each candidate through `assess_trader` → print the copyable cohort + the rejected (with
reason). Read-only; one `fetch_user_fills` per candidate.

## Verdict vocabulary

`copyable` | `market_maker` | `degen_leverage` | `low_sample` | `unprofitable` | `mixed`.

## Honesty rules

- Funding omission is flagged, not hidden.
- `copyable` here means "directional skill not explained by maker rebate or one leveraged hit" —
  it is NOT yet the price-band counterfactual (phase 2) and NOT a live trigger.
- Backtests/leaderboard PnL run 5–10× optimistic; persistence-across-windows is the first real filter.
- Paper/research only. No orders, no keys, no real-money follow.

## Phase 2+ (not now)

Forward position-snapshot ledger → price-band counterfactual (could a follower enter near their
fill, net of 9bps?) → forward-paper follow with pre-registered milestones (200/500/1000, Holm,
window-clustered bootstrap, reusing the existing rig).
