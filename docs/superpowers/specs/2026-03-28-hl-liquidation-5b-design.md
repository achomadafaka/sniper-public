# Phase 5B+5C-min: HL Liquidation Strategy Plugin + Minimal Paper Execution

**Date:** 2026-03-28
**Status:** Reviewed (4 findings fixed)
**Scope:** Strategy plugin + thin HL execution helpers + runner corrections. NOT full Phase 5C.

## Overview

Build `hl_liquidation_v1` strategy plugin that detects whales approaching liquidation and paper-trades counter-positions. Ship with minimal HL paper execution (open, mark-to-market, close, forced liquidation). Skip funding accrual and VWAP averaging for v1.

## Architecture Decision: Option A (minimal 5C alongside 5B)

Rejected B (reuse Polymarket execution): `shares = size_usdc / entry_price` is binary-market math, wrong for leveraged perps. Would poison PnL and analytics.
Rejected C (full 5C first): delays strategy too much for little decision value in v1.

Option A scope: open HL paper position, mark-to-market, close, forced liquidation, fee handling. No funding accrual, no VWAP. Enough to validate signal quality.

## StrategySignal Mapping for HL

HL signals reuse the existing `StrategySignal` shape from `strategies/base.py:17` without extending the dataclass. Field mapping:

- `wallet` = whale address being tracked (the source wallet whose liquidation we're front-running)
- `condition_id` = asset symbol (e.g., "BTC", "ETH") — NOT a Polymarket condition_id
- `outcome` = our trade direction: "LONG" or "SHORT" (opposite of whale's side)
- `title` = human-readable description (e.g., "Counter 0xecb6.. BTC SHORT 20x @ 8.2% dist")
- `current_price` = current mark price of the asset
- `position_value` = whale's position value in USD (for eligibility filtering)
- `analyzed_ts` = timestamp of the whale poll that detected this signal
- `metadata` = `{distance_pct, whale_side, whale_leverage, margin_type, prev_distance_pct}`

Runner dedup uses `(condition_id, outcome)` = `(asset, direction)`. This means one HL signal per asset per direction, which is correct — we only counter-trade one whale per asset at a time.

## Strategy Plugin: `hl_liquidation_v1`

### `discover()` — Three-tier threshold ladder

```
observe_threshold_pct = 35    # log + rank candidates
entry_threshold_pct = 25      # paper-enter eligible
panic_threshold_pct = 15      # highest confidence entries
```

Query `hl_whale_positions WHERE distance_pct < observe_threshold_pct AND position_value_usd > min_notional AND margin_type = 'isolated'`.

Only generate `StrategySignal` for positions below `entry_threshold_pct`. Positions between observe and entry are logged but not signaled.

**Isolated vs cross margin:** Trade isolated positions only in v1. Cross-margin positions are watchlist/telemetry only (distance_pct is portfolio headroom, not asset-specific liquidation distance).

**Distance must be worsening (v1: vs previous poll only):** Compare current `distance_pct` against `prev_distance_pct`. If current >= previous (stable or widening), do not signal. Downgraded from "2-3 poll trend" to "vs previous poll" for v1 — a single `prev_distance_pct` column is sufficient and survives restarts unlike in-memory state.

Implementation: add `prev_distance_pct REAL` column to `hl_whale_positions`. The poller stores the current distance as `prev_distance_pct` before overwriting with the new value on each `INSERT OR REPLACE`.

### `enter()` — Bankroll-based sizing, not whale-based

Counter-trade the whale: whale long → we short, whale short → we long.

**Sizing (bankroll-relative):**
```python
base_margin_usdc = bankroll * 0.01  # 1% of bankroll
confidence = (entry_threshold_pct - distance_pct) / entry_threshold_pct  # 0-1
margin_usdc = clamp(base_margin_usdc * (0.75 + confidence), 10, 25)
leverage = 1.5  # default; 2.0 only for panic_threshold entries
size_contracts = margin_usdc * leverage / current_price  # derive contract qty from margin
```

Whale size is an eligibility filter (`position_value_usd > min_notional`), not a sizing input.

**Risk checks:** Use `evaluate_entry_risk()` with full HL venue parameters:
```python
risk_ctx = _build_hl_risk_context(conn, profile)  # sums hl_positions, not strategy_positions
risk_decision = evaluate_entry_risk(
    risk_ctx,
    proposed_size_usdc=margin_usdc,
    condition_id=asset,
    execution_mode="paper",
    venue_limits=VenueRiskLimits(
        max_exposure_usdc=profile.hl_max_exposure_usdc,
        max_leverage=profile.hl_max_leverage,
        max_concurrent=profile.hl_max_concurrent,
    ),
    proposed_leverage=leverage,
    asset_exposure_usdc=current_asset_exposure,
)
```

**Funding as soft confirmation, not hard blocker:** Funding rate from `hyperliquid_snapshots` is unreliable (WS flusher writes 0.0). Use as confidence modifier only, not a gate. Log when funding opposes trade direction but don't reject.

### `manage()` — Mark-to-market from snapshots

Read current mark price from `hyperliquid_snapshots` or `hl_whale_positions.mark_price`.

**PnL formula (correct for perps where `size` is contracts/coins):**
```python
unrealized_pnl = (mark_price - entry_price) * size * direction_sign
# NOT multiplied by leverage — size already reflects leveraged exposure
# direction_sign: +1 for long, -1 for short
```

Track time since entry.

### `exit()` — Five exit conditions

1. **Take profit:** mark moved ≥ 2% in our direction
2. **Stop loss:** mark moved ≥ 1% against us
3. **Time stop:** 30 minutes since entry
4. **Forced liquidation:** mark crosses our computed liquidation price
5. **Thesis invalidated:** whale position disappeared from `hl_whale_positions`, OR whale's distance widened back above `entry_threshold_pct + 5` (buffer), OR whale's position size decreased >50%

### Plugin definition

```python
class HlLiquidationV1Strategy(StrategyPlugin):
    plugin_id = "hl_liquidation_v1"
    strategy_family = "hl_liquidation"
    required_feeds = ("hyperliquid", "hl_whale")
    default_loop_seconds = 60  # aligned with whale poller cadence
```

## Minimal HL Paper Execution (5C-min)

### New file: `execution/hyperliquid_paper.py`

**PnL model:** `size` is contract quantity (coins). Notional = `size * price`. PnL = `size * price_delta * direction`. Fees charged on notional, not margin.

**`record_hl_entry(conn, strategy_id, asset, side, entry_price, size, leverage, margin_usdc)`:**
- INSERT into `hl_positions`: all fields + computed `liquidation_price`
- `liquidation_price = entry_price * (1 - 1/leverage)` for longs, `entry_price * (1 + 1/leverage)` for shorts
- INSERT into `hl_fills` with `fill_type='open'`
- Return position_id

**`update_hl_position(conn, position_id, mark_price)`:**
- `unrealized_pnl = (mark_price - entry_price) * size * direction_sign`
- Check liquidation: if (side='long' and mark <= liq_price) or (side='short' and mark >= liq_price) → force-close
- UPDATE `hl_positions` with current_price, unrealized_pnl

**`close_hl_position(conn, position_id, exit_price, reason)`:**
- `realized_pnl = (exit_price - entry_price) * size * direction_sign`
- Fee: `notional = size * exit_price; fee = notional * 0.001; realized_pnl -= fee` (0.1% taker on exit notional)
- INSERT into `hl_fills` with `fill_type='close'`
- UPDATE `hl_positions` to `state='closed'`, set realized_pnl

**NOT in v1:** funding_accumulated, VWAP avg_entry_price, partial fills, entry fee (v1 charges exit-only fee as approximation — entry fee is same magnitude and can be added in v2).

### Runner corrections (strategy_runner.py)

**Four changes required** (not three — finding #2 identified a missing one):

1. **HL-aware position loader:** New function `_active_hl_positions(conn, strategy_id)` that reads from `hl_positions WHERE state = 'open' AND strategy_id = ?`. `process_plugin_strategy()` must call this instead of `_active_strategy_positions()` for HL plugins. The returned dicts must be compatible with the plugin's `manage()`/`exit()` interface.

2. **HL risk context builder:** New function `_build_hl_risk_context(conn, profile)` that sums `hl_positions` for exposure/concurrent counts (not `strategy_positions`). Passes `venue="hyperliquid"` to `RiskContext`. The `evaluate_entry_risk` call must include `venue_limits`, `proposed_leverage`, and `asset_exposure_usdc`.

3. **Signal queue venue routing:** Line 1104 hardcodes `venue="polymarket"`. Derive from plugin: `venue = "hyperliquid" if any(f in ("hl_whale", "hyperliquid_ws") for f in plugin.required_feeds) else "polymarket"`.

4. **HL entry/exit routing:** In `process_plugin_strategy()`, detect HL plugins and route to `execution/hyperliquid_paper.py` functions. The manage/exit loop must also use the HL position loader and HL close function.

**How to detect HL plugins:** Check `any(f.startswith("hl") or f == "hyperliquid_ws" for f in plugin.required_feeds)`. This avoids adding a new ABC attribute.

### HL-aware reporting and totals

`_refresh_strategy_totals()` and `plugin.report()` currently read from `strategy_positions`. For HL strategies, these must read from `hl_positions`. The simplest approach: pass `venue` to `_refresh_strategy_totals` and have it branch on which table to query.

## Files

| File | Change |
|------|--------|
| `strategies/hl_liquidation_v1.py` | New: strategy plugin |
| `execution/__init__.py` | New: package init |
| `execution/hyperliquid_paper.py` | New: minimal HL paper execution |
| `strategy_runner.py` | HL position loader, HL risk context, signal queue venue, HL entry/exit/manage routing, HL-aware totals |
| `strategies/registry.py` | Register hl_liquidation_v1 |
| `db_hyperliquid.py` | Add `prev_distance_pct` column to hl_whale_positions |
| `hl_whale_poller.py` | Store previous distance before overwriting |
| `tests/test_hl_liquidation_strategy.py` | Strategy + execution tests |

## Tests

1. discover() returns signals only for isolated positions below entry_threshold with worsening distance
2. discover() excludes cross-margin positions
3. discover() excludes positions where distance is stable or widening
4. enter() sizes from bankroll, not whale notional
5. enter() computes correct liquidation_price for long and short
6. exit() triggers on TP, SL, time stop, forced liquidation, thesis invalidated
7. execution: record_hl_entry writes to hl_positions + hl_fills
8. execution: PnL = size * price_delta * direction (NOT multiplied by leverage)
9. execution: fee charged on exit notional (size * exit_price * 0.1%), not margin
10. execution: forced liquidation triggers when mark crosses liq price
11. runner: _active_hl_positions returns hl_positions, not strategy_positions
12. runner: risk context uses hl_positions exposure with venue="hyperliquid" and venue_limits
13. runner: signal_queue uses venue="hyperliquid" for HL plugins
14. signal mapping: condition_id=asset, outcome=direction

## Constraints

- Paper-only. No live HL execution.
- 60s discovery cadence aligned with whale poller.
- Isolated positions only for v1. Cross is watchlist.
- No funding accrual. No VWAP. No partial fills.
- Funding rate is a soft confidence modifier, not a hard blocker.
- Distance trend: v1 checks vs previous poll only (single prev_distance_pct column).
- PnL model: size is contracts, PnL = qty * price_delta * direction. No leverage multiplier on PnL.
