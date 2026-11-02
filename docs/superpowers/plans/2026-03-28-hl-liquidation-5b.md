# Phase 5B: HL Liquidation Strategy + Minimal Paper Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the hl_liquidation_v1 strategy plugin that detects whales approaching liquidation and paper-trades counter-positions, with minimal HL paper execution engine and runner corrections for venue-aware operation.

**Architecture:** Three sequential workers: (1) HL paper execution engine (execution/hyperliquid_paper.py), (2) runner corrections (HL position loader, risk context, venue routing, signal queue), (3) strategy plugin (strategies/hl_liquidation_v1.py). Each depends on the previous.

**Tech Stack:** Python 3.13, SQLite, existing StrategyPlugin ABC, existing risk module, existing db_hyperliquid.py tables.

**Spec:** `docs/superpowers/specs/2026-03-28-hl-liquidation-5b-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| `execution/__init__.py` | Package init |
| `execution/hyperliquid_paper.py` | HL paper execution: record_hl_entry, update_hl_position, close_hl_position |
| `strategy_runner.py` | HL position loader, HL risk context, signal queue venue, HL entry/exit/manage routing |
| `strategies/hl_liquidation_v1.py` | Strategy plugin: discover, enter, manage, exit |
| `strategies/registry.py` | Register hl_liquidation_v1 |
| `db_hyperliquid.py` | Add prev_distance_pct column |
| `hl_whale_poller.py` | Store prev_distance before overwrite |
| `tests/test_hl_liquidation_strategy.py` | All tests |

---

### Worker 1: HL Paper Execution Engine

**Files:**
- Create: `execution/__init__.py`
- Create: `execution/hyperliquid_paper.py`
- Modify: `db_hyperliquid.py` (add prev_distance_pct)
- Modify: `hl_whale_poller.py` (store prev distance)
- Create: `tests/test_hl_liquidation_strategy.py` (execution tests only)

This worker builds the execution layer that records HL paper trades in `hl_positions` + `hl_fills`. The PnL model: `size` is contracts/coins, `PnL = size * price_delta * direction_sign`. No leverage multiplier on PnL. Fee on exit notional only for v1.

**Tests to write:**
- record_hl_entry creates hl_positions + hl_fills rows with correct fields
- record_hl_entry computes liquidation_price correctly (long: entry * (1 - 1/lev), short: entry * (1 + 1/lev))
- update_hl_position computes unrealized PnL as size * (mark - entry) * direction
- update_hl_position detects forced liquidation (long: mark <= liq, short: mark >= liq)
- close_hl_position computes realized PnL and fee on exit notional
- prev_distance_pct column exists and gets populated by poller

**Implementation:**
- `execution/__init__.py`: empty
- `execution/hyperliquid_paper.py`: three functions + helpers
- `db_hyperliquid.py`: add `prev_distance_pct REAL` to hl_whale_positions schema in ensure function
- `hl_whale_poller.py`: before INSERT OR REPLACE, read current distance and store as prev

**Commit:** `feat(phase5b): HL paper execution engine + prev_distance tracking`

---

### Worker 2: Runner Corrections for HL Venue

**Files:**
- Modify: `strategy_runner.py`
- Append: `tests/test_hl_liquidation_strategy.py` (runner tests)

**Depends on:** Worker 1 committed.

Four changes to strategy_runner.py:

1. **`_active_hl_positions(conn, strategy_id)`** — new function reading from `hl_positions WHERE state = 'open'`. Returns list of dicts compatible with manage/exit interface. Keys: `id`, `asset`, `side`, `size`, `entry_price`, `current_price`, `liquidation_price`, `leverage`, `margin_usdc`, `unrealized_pnl`, `opened_ts`.

2. **`_build_hl_risk_context(conn, profile, asset)`** — new function summing `hl_positions` exposure. Returns `RiskContext` with `venue="hyperliquid"`.

3. **Signal queue venue routing** — in the signal_queue insert block, derive venue from plugin.required_feeds: `venue = "hyperliquid" if any(f in ("hl_whale", "hyperliquid_ws") for f in getattr(plugin, "required_feeds", ())) else "polymarket"`.

4. **`_is_hl_plugin(plugin)`** helper — returns True if plugin uses HL feeds. Used by process_plugin_strategy to branch into HL-specific code paths for position loading, entry, manage/exit, and totals refresh.

5. **HL branch in process_plugin_strategy** — when `_is_hl_plugin(plugin)`:
   - Load positions via `_active_hl_positions` instead of `_active_strategy_positions`
   - On entry: call `record_hl_entry` from `execution/hyperliquid_paper` instead of `_record_plugin_entry`
   - On manage: call `update_hl_position` with mark price
   - On exit: call `close_hl_position` instead of `_close_plugin_position`
   - Risk: call `_build_hl_risk_context` and pass full venue params to `evaluate_entry_risk`

**Tests to write:**
- _active_hl_positions returns hl_positions rows, not strategy_positions
- _build_hl_risk_context sets venue="hyperliquid" and sums hl_positions exposure
- signal queue uses venue="hyperliquid" when plugin has hl_whale in required_feeds
- _is_hl_plugin returns True/False correctly

**Commit:** `feat(phase5b): runner HL venue routing + risk context + position loader`

---

### Worker 3: Strategy Plugin

**Files:**
- Create: `strategies/hl_liquidation_v1.py`
- Modify: `strategies/registry.py`
- Append: `tests/test_hl_liquidation_strategy.py` (strategy tests)

**Depends on:** Workers 1 and 2 committed.

The plugin implements the StrategyPlugin ABC with 4 methods:

**`discover(conn, strategy)`:**
- Read thresholds from strategy params (observe=35, entry=25, panic=15, min_notional=50000)
- Query: `SELECT * FROM hl_whale_positions WHERE margin_type = 'isolated' AND distance_pct < ? AND position_value_usd > ? ORDER BY distance_pct ASC`
- For each candidate:
  - Log if below observe but above entry threshold (watchlist)
  - Skip if `prev_distance_pct IS NOT NULL AND distance_pct >= prev_distance_pct` (not worsening)
  - If below entry threshold AND worsening: generate StrategySignal with:
    - `condition_id = asset`
    - `outcome = "LONG" if whale_side == "short" else "SHORT"` (counter-trade)
    - `wallet = whale_wallet`
    - `current_price = mark_price`
    - `position_value = whale_position_value_usd`
    - `metadata = {distance_pct, whale_side, whale_leverage, prev_distance_pct, margin_type}`

**`enter(conn, strategy, signal, open_positions)`:**
- Dedup: reject if asset already in open_positions
- Read bankroll from strategy params or InstanceProfile
- Compute sizing: `margin = clamp(bankroll * 0.01 * (0.75 + confidence), 10, 25)`, `leverage = 1.5` (2.0 if distance < panic)
- `size = margin * leverage / signal.current_price`
- Return StrategyEntryDecision(True, reason, size_usdc=margin, entry_price=signal.current_price, execution_mode="paper", metadata={leverage, size_contracts, liquidation_price, whale_wallet})

**`manage(conn, strategy, position)`:**
- Read latest mark price for this asset from `hyperliquid_snapshots` or `hl_whale_positions`
- Return StrategyManageState(current_price=mark, signal_open=True, metadata={})

**`exit(conn, strategy, position, state)`:**
- Compute price move % since entry
- Check 5 exit conditions in order:
  1. TP: move >= 2% in our direction
  2. SL: move >= 1% against us
  3. Forced liquidation: mark crossed our liq price
  4. Time stop: 30 min since opened_ts
  5. Thesis invalidated: whale position gone or distance widened past entry_threshold + 5
- Return StrategyExitDecision with appropriate reason

**Register in registry.py:** add import + entry to _PLUGINS dict.

**Tests to write:**
- discover returns signals for isolated below entry_threshold with worsening distance
- discover excludes cross-margin positions
- discover excludes stable/widening distance positions
- enter sizes from bankroll (margin=clamp(bankroll*0.01*(0.75+conf), 10, 25))
- enter computes leverage 1.5 default, 2.0 for panic
- exit triggers TP at 2%
- exit triggers SL at 1%
- exit triggers time stop at 30 min
- exit triggers thesis invalidation (whale gone)

**Commit:** `feat(phase5b): hl_liquidation_v1 strategy plugin`

---

### Final Verification

- [ ] `python3 -m pytest tests/test_hl_liquidation_strategy.py -v` — all pass
- [ ] `python3 -m pytest tests/ -q` — full suite green
- [ ] Deploy to VPS: `git push && ssh sniper@188.166.91.20 'cd ~/sniper && git pull origin main && systemctl --user restart sniper-sports-runner'`
- [ ] Verify: create a strategy with `strategy_type=hl_liquidation_v1` and confirm it runs without error in the runner logs
