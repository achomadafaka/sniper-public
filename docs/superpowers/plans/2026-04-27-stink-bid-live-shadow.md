# Stink Bid Live Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture live orderbook snapshots for stink-bid candidates and evaluate whether strict historical fills are plausible after visible queue depth.

**Architecture:** Keep real-capital execution out of scope. Add a public CLOB orderbook wrapper, a standalone shadow module with pure queue math, and JSONL output for later paper/live comparison. The module is usable manually first; runner integration comes after enough live observations exist.

**Tech Stack:** Python, Polymarket public CLOB `/book`, JSONL artifacts, pytest.

---

### Task 1: Public Orderbook Wrapper

**Files:**
- Modify: `data/polymarket_client.py`
- Test: `tests/backtest/test_polymarket_client_orderbook.py`

- [x] Write a failing test that `PolymarketClient.get_orderbook("tok")` calls `https://clob.polymarket.com/book` with `{"token_id": "tok"}`.
- [x] Run `python3 -m pytest -q tests/backtest/test_polymarket_client_orderbook.py -q`; expected failure before implementation.
- [x] Add `get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]`.
- [x] Re-run the test; expected pass.

### Task 2: Shadow Queue Model

**Files:**
- Create: `scripts/stink_bid_live_shadow.py`
- Test: `tests/backtest/test_stink_bid_live_shadow.py`

- [x] Write failing tests for:
  - depth ahead at bid price includes better and equal bids
  - a trade must clear visible depth ahead plus `min_fill_size`
  - `/book` payload parsing normalizes strings to floats
  - JSONL observation output includes queue result fields
- [x] Run `python3 -m pytest -q tests/backtest/test_stink_bid_live_shadow.py -q`; expected import failure before implementation.
- [x] Add dataclasses: `BookLevel`, `OrderbookSnapshot`, `ShadowOrder`, `TradePrint`, `ShadowFillEvaluation`.
- [x] Add pure helpers: `parse_orderbook_snapshot`, `depth_ahead_for_bid`, `evaluate_shadow_fill`, `build_shadow_order`, `write_shadow_observation`.
- [x] Add `ShadowOrderMonitor` for wiring trade prints to latest visible book depth.
- [x] Add `capture_once()` and CLI args for manual snapshot capture.
- [x] Re-run tests; expected pass.

### Task 3: Full Verification

**Files:**
- Test existing strict simulator and optimizer files.

- [x] Run:

```bash
python3 -m pytest -q \
  tests/backtest/test_polymarket_client_orderbook.py \
  tests/backtest/test_stink_bid_live_shadow.py \
  tests/backtest/test_stink_bid_smoke_sim.py \
  tests/backtest/test_stink_bid_optimizer.py \
  tests/backtest/test_two_sided_stink_bid_sim.py
```

- [x] Expected: all tests pass.

### Task 4: First Live Shadow Use

Manual command shape:

```bash
python3 scripts/stink_bid_live_shadow.py \
  --condition-id <condition_id> \
  --token-id <favorite_token_id> \
  --favorite-price <current_favorite_price> \
  --dip-pct 0.15 \
  --min-fill-size 50 \
  --output data/live_shadow/stink_bid_orderbook.jsonl
```

Expected JSONL fields include:

- `bid_price`
- `depth_ahead`
- `best_bid`
- `best_ask`
- `spread`
- `book_hash`
- `tick_size`
- `min_order_size`

### Follow-Up Work

- [x] Add active tennis candidate discovery so the command can watch all current H2H markets.
- [x] Join trade-tap events to captured book snapshots and call `evaluate_shadow_fill()` live.
- [x] Add systemd unit for paper-only shadow monitoring.
- Promote only after live shadow shows queue-fillable events, not just price-cross events.

### Task 5: Shadow Daemon

**Files:**
- Create: `scripts/stink_bid_shadow_daemon.py`
- Create: `tests/backtest/test_stink_bid_shadow_daemon.py`
- Create: `systemd/sniper-stink-bid-shadow.service`

- [x] Write tests for Gamma token parsing, live favorite selection from orderbooks, and routing trade ticks into `ShadowOrderMonitor`.
- [x] Implement a daemon that:
  - discovers active tennis H2H markets via existing Gamma helper
  - picks the live favorite from current orderbooks
  - refreshes visible book snapshots every 60 seconds
  - subscribes the existing trade tap to watched favorite tokens
  - writes `book_snapshot` and `trade_evaluation` JSONL rows
- [x] Add systemd service using top strict config defaults plus live safety cap: `dip_pct=0.15`, `min_favorite=0.65`, `max_favorite=0.90`, `min_fill_size=50`.
- [x] Add `--once` mode for discovery/snapshot smoke runs without starting the websocket loop.

### Task 6: Queue Model Sensitivity

**Files:**
- Modify: `scripts/stink_bid_live_shadow.py`
- Create: `scripts/analyze_stink_bid_shadow.py`
- Test: `tests/backtest/test_analyze_stink_bid_shadow.py`

- [x] Emit queue outcomes for four assumptions on every trade evaluation:
  - `price_cross`: price and size only
  - `optimistic`: no visible depth ahead
  - `midpoint`: half visible depth ahead
  - `pessimistic`: all visible same/better bid depth ahead
- [x] Add analyzer that counts fillability by queue model from JSONL.
- [x] Use this to decide whether historical strict maker fills are too pessimistic, too optimistic, or plausible.

Analyzer command:

```bash
python3 scripts/analyze_stink_bid_shadow.py data/live_shadow/stink_bid_shadow.jsonl \
  --output data/live_shadow/stink_bid_shadow_summary.json
```
