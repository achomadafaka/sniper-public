# Hyperliquid Liquidation Proximity Strategy

**Date:** 2026-03-27
**Status:** Reviewed (iteration 2 — 8 findings fixed)
**Scope:** Three sub-phases: 5A (whale polling), 5B (detection + plugin), 5C (paper execution)

## Concept

Track whale positions on Hyperliquid via the public `clearinghouseState` API. When a whale is close to liquidation on a large position, enter a counter-position to profit from the forced sell cascade.

Formula: `if whale_liq_distance < threshold% AND position_value > $min_notional AND funding_confirms_direction → enter counter-position`

## Sub-Phase 5A: Whale Position Polling Infrastructure

### Whale Discovery
- Manual seed list of known whale addresses (from HyperTracker.io, community sources, manual research)
- Store in new `hl_tracked_wallets` table: `wallet TEXT PRIMARY KEY`, `label`, `source`, `added_at`
- Address validation: must match `/^0x[0-9a-fA-F]{40}$/` before insert
- Organic growth: periodically scan `hl_trades` for new high-volume addresses, add to tracking list

### Position Polling
- New `hl_whale_positions` table (latest-snapshot model, upserted per poll):

```sql
CREATE TABLE IF NOT EXISTS hl_whale_positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT NOT NULL,
    asset             TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('long', 'short')),
    size              REAL NOT NULL,
    entry_price       REAL NOT NULL,
    mark_price        REAL NOT NULL,           -- from companion metaAndAssetCtxs call
    liquidation_price REAL,                     -- NULL for cross margin
    margin_type       TEXT NOT NULL CHECK (margin_type IN ('isolated', 'cross')),
    leverage          REAL NOT NULL DEFAULT 1.0,
    margin_used       REAL NOT NULL DEFAULT 0,
    unrealized_pnl    REAL NOT NULL DEFAULT 0,
    position_value_usd REAL NOT NULL DEFAULT 0, -- from positionValue field
    distance_pct      REAL NOT NULL DEFAULT 0,  -- computed at write time
    account_value     REAL,                     -- for cross margin proximity
    cross_mm_used     REAL,                     -- for cross margin proximity
    captured_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(wallet, asset)                       -- upsert on each poll cycle
);
```

- Poll via `POST https://api.hyperliquid.xyz/info` with `{"type": "clearinghouseState", "user": "0x..."}`
- **Also fetch mark prices** via `{"type": "metaAndAssetCtxs"}` once per poll cycle (1 extra request, shared across all wallets)
- Polling cadence: **60s** per cycle (configurable via `HL_WHALE_POLL_SECONDS`)
- Rate limit: ~5 req/s from HL. With 50 wallets + 1 mark price call = 51 requests/cycle = ~10s. 50s headroom in 60s window.
- `INSERT OR REPLACE` on `(wallet, asset)` — always latest snapshot, no unbounded growth

### Liquidation Distance Computation (computed at write time, stored as `distance_pct`)

**Isolated margin** (when `liquidationPx` is not null):
```python
mark = mark_prices[asset]  # from metaAndAssetCtxs
liq = float(position["liquidationPx"])
distance_pct = abs(mark - liq) / mark * 100
```

**Cross margin** (when `liquidationPx` is null):
```python
account_value = float(state["marginSummary"]["accountValue"])  # includes unrealized P&L
cross_mm_used = float(state["crossMaintenanceMarginUsed"])
distance_pct = max(0.0, (account_value - cross_mm_used) / account_value * 100)
```

Note: `marginSummary.accountValue` (includes unrealized P&L) is used, NOT `crossMarginSummary.accountValue` (which excludes it). This gives the real-time margin health.

### Error Handling
- Invalid address → reject at `hl_tracked_wallets` insert with validation error
- Empty `assetPositions` response → log `"wallet {addr} has no open positions"` (distinct from API error)
- API error → log warning, skip wallet this cycle, retry next cycle
- Wallet with all positions closed → DELETE from `hl_whale_positions` WHERE wallet = ?

### Implementation
- New file: `hl_whale_poller.py` — standalone polling module (like `feeds/hyperliquid_feed.py`)
- New feed adapter: `feeds/hl_whale.py` — registered as `"hl_whale"` feed (follows `feeds/hyperliquid.py` naming pattern)
- New tables in `db_hyperliquid.py`: `hl_tracked_wallets`, `hl_whale_positions`
- Update `ensure_all_hl_tables()` to call the two new `ensure_*` functions
- New migration: `migrations/014_hl_whale_tables.py`
- Runs as poll-mode feed at 60s intervals

### Tests
- Parse clearinghouseState response into hl_whale_positions rows
- Compute distance_pct correctly for isolated margin (known liquidation price)
- Compute distance_pct correctly for cross margin (account_value vs crossMaintenanceMarginUsed)
- Handle wallets with no positions (empty assetPositions → zero rows, not error)
- Dedup: same wallet polled twice → `INSERT OR REPLACE` updates, row count stays same
- Address validation: reject malformed addresses
- Mark price integration: distance uses live mark price, not entry price

## Sub-Phase 5B: Liquidation Detection + Strategy Plugin (after 5A data exists)

### Signal Generation
- Query `hl_whale_positions` for positions where `distance_pct < threshold`
- Filter: `position_value_usd > min_notional` (e.g., $100K)
- Combine with: funding rate direction (from `hyperliquid_snapshots`), OI trend
- Generate `StrategySignal` with asset, direction (opposite to whale's position), confidence

### Entry Logic
- Counter-trade the whale: if whale is long and close to liquidation → short
- Size: proportional to whale position size and proximity (closer = larger)
- Max leverage: from venue risk limits (default 3x)

### Exit Logic
- Take profit: 2% move in our direction
- Stop loss: 1% adverse move
- Time stop: 30 minutes (cascade should play out fast)
- Liquidation price breach: forced exit (our own position)

### Plugin
```python
class HlLiquidationV1Strategy(StrategyPlugin):
    plugin_id = "hl_liquidation_v1"
    strategy_family = "hl_liquidation"
    required_feeds = ("hyperliquid", "hl_whale")
    default_loop_seconds = 30
```

## Sub-Phase 5C: HL Paper Execution Engine (shared with all HL strategies)

As designed in the master plan Phase 5E: `execution/hyperliquid_paper.py` with `hl_positions`, `hl_fills`, funding tracking, liquidation price computation, VWAP entry.

## Files (Phase 5A only — first build)

| File | Change |
|------|--------|
| `hl_whale_poller.py` | New: clearinghouseState + metaAndAssetCtxs polling |
| `feeds/hl_whale.py` | New: FeedAdapter wrapping the poller (follows `feeds/hyperliquid.py` pattern) |
| `db_hyperliquid.py` | Add `ensure_hl_tracked_wallets_table()` + `ensure_hl_whale_positions_table()`, update `ensure_all_hl_tables()` |
| `migrations/014_hl_whale_tables.py` | Migration for existing DBs |
| `feeds/registry.py` | Register `hl_whale` adapter |
| `web_settings.py` | `HL_WHALE_POLL_SECONDS=60`, `HL_WHALE_TRACKED_ASSETS` |
| `tests/test_hl_whale_poller.py` | Parsing, distance computation, dedup, validation |

## Constraints
- Paper-only. No live HL execution.
- 60s poll cadence for data collection (5B strategy runs at 30s using cached data).
- Manual seed list for v1 whale discovery. No auto-scanning yet.
- Do NOT build the strategy plugin (5B) or execution engine (5C) in this sub-phase.
- Phase boundary: 5A only collects data. 5B consumes it.
