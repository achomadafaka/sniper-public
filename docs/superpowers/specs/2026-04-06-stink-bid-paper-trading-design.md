# Stink Bid Paper Trading — Limit Order Paper Test for Tennis Match Markets

## Goal

Paper-test a maker strategy on Polymarket tennis match-winner markets:
place a resting bid 30% below the pre-match favorite's price, wait for
panic sells during the match, hold to resolution. Evaluate whether panic
fills happen, how often, and whether they're profitable after fees.

This is a **paper-only** system. No real orders are placed. Fill detection
uses real trade events from the WS stream to determine whether a resting
bid *would have* filled.

## Why Paper, Not Replay

The replay engine (Phase 2C) is taker-only. It cannot simulate resting
limit orders, queue position, or passive fills. Those are explicit
non-goals of the replay engine. A live paper test with real market data
is the correct tool for this strategy.

## Strategy Summary (Moon Dev WTA Stink Bid)

1. Place lowball bids 30% below market price on the favorite
2. If a panic sell dumps the price, scoop cheap shares
3. Hold to expiration — the favorite usually wins → collect $1/share
4. Refresh bids every 15 minutes
5. Only bid during first two sets; cancel after
6. Never double up on a match already in

---

## 1. Order Model

### Discovery

Find active tennis match-winner markets on Polymarket:
- Use Polymarket category/tags if available and reliable
- Fall back to title regex via existing patterns in `filters.py`
  (WTA, ATP, tournament name, "vs" patterns)
- Once a market is identified, lock by `condition_id` + `token_id`.
  No re-matching on subsequent cycles.

### Qualification

A market qualifies for a stink bid if ALL of:
- It is an active tennis match-winner market
- The favorite fixed at the **first eligible observation** (the first
  observation after market discovery that also has ESPN match-state
  coverage) has price > `min_favorite_price` (default: 0.55)
- No existing paper position on this match
- No prior filled, cancelled, or expired order on this match
- Market 24h volume > `min_market_volume` (default: 10,000)
- ESPN score feed has a matching live event (v1: no fallback)

### Favorite Definition

- **Fixed at first eligible observation.** The outcome with the higher
  price when the market first passes qualification is the favorite.
  Recorded as `favorite_token_id`. Never switched.
- If the favorite's price later drops below 0.50 (flip), stop repricing.
  Existing resting bid stays but no cancel-replace. No re-anchoring to
  the new leader.

### Bid Computation

```
bid_price = round(favorite_price × (1 - dip_pct), 2)  # tick-rounded to $0.01
bid_price = max(bid_price, min_bid_price)               # floor at min_bid_price
```

Defaults: `dip_pct = 0.30`, `min_bid_price = 0.05`.

Example: favorite at $0.70 → bid at $0.49. Favorite at $0.55 → bid at
$0.385 → rounded to $0.39.

### Refresh / Reprice

- Every `refresh_interval` (default: 15 minutes), recompute the target
  bid based on the favorite's current price.
- **Reprice only if** the new target differs from the current bid by at
  least `reprice_threshold` (default: 0.02 absolute or 5% relative,
  whichever is smaller). Otherwise leave the bid alone.
- **Stop repricing if:**
  - Original favorite has flipped (price < 0.50)
  - Match has `sets_completed >= 2` (via ESPN)
  - Order is already filled, cancelled, or expired

### Order Lifecycle

One order per match. No re-entry after fill, cancel, or expiry.

States: `resting → filled | cancelled | expired`

- `resting`: bid is active, being monitored for fills and repriced
- `filled`: a qualifying trade triggered the fill (see Fill Model)
- `cancelled`: explicit cancel — 3rd set gate, favorite flip + manual,
  strategy pause, ESPN outage (sustained)
- `expired`: market resolved or closed before fill or cancel

### Storage

**New table: `paper_limit_orders`** — separate from `order_intents` /
`order_fills`, which are for real exchange orders. Paper limit orders
never touch the exchange.

```sql
CREATE TABLE paper_limit_orders (
    order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL,
    condition_id    TEXT NOT NULL,
    market_title    TEXT NOT NULL,
    event_slug      TEXT NOT NULL DEFAULT '',
    favorite_token_id TEXT NOT NULL,
    favorite_token_label TEXT NOT NULL,
    favorite_price_at_entry REAL NOT NULL,
    bid_price       REAL NOT NULL,
    dip_pct         REAL NOT NULL,
    min_fill_size   REAL NOT NULL DEFAULT 50,
    state           TEXT NOT NULL DEFAULT 'resting'
        CHECK (state IN ('resting', 'filled', 'cancelled', 'expired')),
    cancel_reason   TEXT NOT NULL DEFAULT '',
    placed_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT NOT NULL DEFAULT '',
    espn_event_id   TEXT NOT NULL DEFAULT '',
    tour            TEXT NOT NULL DEFAULT '',
    player1         TEXT NOT NULL DEFAULT '',
    player2         TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX uq_paper_limit_orders_strategy_match_terminal
    ON paper_limit_orders(strategy_id, condition_id);
```

The strategy enforces **one order per match, no re-entry** at query time
and via the `(strategy_id, condition_id)` uniqueness invariant. A match
may have at most one paper limit order row for a given strategy across
its full lifecycle.

---

## 2. Fill Model

### Dual-Label Evaluation

Every resting order is evaluated against two fill criteria simultaneously:

- **`strict_fill`** (primary): a real trade on the favorite's token at
  `price <= bid_price` with `size >= min_fill_size` (default: 50,
  configurable). This creates the paper position and drives PnL.
- **`loose_fill`** (sensitivity): a real trade on the favorite's token
  at `price <= bid_price`, any size. Logged as annotation only — no
  position, no PnL impact.

### Fill Rules

- Fill triggers from **real trade events** (`last_trade_price` from the
  WS stream), not from quote/BBO moves.
- Primary trigger: `trade_price <= bid_price`. The stream's `side` field
  is logged on every fill event but **not used as a gate** in v1. Side
  semantics need explicit validation before they can be trusted for
  maker-fill direction logic.
- **Once per order.** First qualifying trade wins. After fill, no
  further evaluation.
- If `loose_fill` triggers but `strict_fill` doesn't, that is recorded
  as a **sensitivity gap** — logged with both trade details for later
  analysis.

### Fill Source

Use a **dedicated lightweight trade tap** for watched tennis tokens.
This is a new module, separate from `stream_low_buys.py`.

Why not reuse `stream_low_buys.py`:
- `stream_low_buys.py` filters for "interesting" trades via
  `_trigger_decision(...)`
- it applies `is_excluded_market()` before queueing
- it is production logic for followed-wallet / low-buy discovery, not a
  complete unfiltered market-data feed

The dedicated trade tap:
- opens a market-data WS connection
- subscribes only to `last_trade_price` for the currently watched
  `favorite_token_id`s
- emits all trade prints for those watched tokens with no large-bet or
  discovery filtering
- updates its watchlist dynamically as paper limit orders are placed,
  filled, cancelled, expired, or removed

Verified event fields available from `last_trade_price`:
`price`, `size`, `side`, `asset_id`, `market`, `timestamp`.

### Fill Logging

```sql
CREATE TABLE paper_limit_fills (
    fill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES paper_limit_orders(order_id),
    fill_label      TEXT NOT NULL CHECK (fill_label IN ('strict', 'loose')),
    trade_price     REAL NOT NULL,
    trade_size      REAL NOT NULL,
    trade_side      TEXT NOT NULL DEFAULT '',
    trade_token_id  TEXT NOT NULL DEFAULT '',
    trade_tx_hash   TEXT NOT NULL DEFAULT '',
    trade_ts        TEXT NOT NULL,
    bid_price       REAL NOT NULL,
    slippage        REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
```

`slippage = bid_price - fill_price`. Positive = better fill than bid.

---

## 3. Logging and Evaluation

### Lifecycle Event Log

Every state transition and decision is recorded:

```sql
CREATE TABLE paper_limit_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER,
    event_type      TEXT NOT NULL,
    state_before    TEXT NOT NULL DEFAULT '',
    state_after     TEXT NOT NULL DEFAULT '',
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
```

Event types:

| Event | Details |
|---|---|
| `order_placed` | market_id, token_id, favorite_price, bid_price, dip_pct, market_title, event_slug, favorite_token_label, min_favorite_price_used, tour, player1, player2 |
| `order_repriced` | old_bid, new_bid, favorite_price_now, reason |
| `order_cancelled` | reason (sets_completed, favorite_flip, strategy_pause, espn_outage, manual) |
| `order_expired` | reason (market_closed, market_resolved) |
| `order_skipped` | reason (no_favorite_above_threshold, prior_order_exists, favorite_flipped, no_score_feed, market_excluded, below_volume, max_resting_reached, max_daily_fills_reached) |
| `fill_strict` | trade_price, trade_size, trade_side, trade_tx_hash, trade_ts, bid_price, slippage |
| `fill_loose` | same fields — logged even when strict doesn't trigger |
| `position_opened` | entry_price, size_usdc, fee_estimate |
| `position_settled` | settlement_price, pnl, hold_duration_hours, outcome (win/loss) |

### Evaluation Metrics

Run after accumulating data:

- **Fill rate**: % of resting bids that fill (strict vs loose)
- **Opportunity rate**: of all qualified matches, how many ever had a
  trade at or below the stink-bid zone? (Different from fill rate —
  measures whether panic dips happen at all)
- **Win rate**: % of filled positions that settle at $1.00
- **PnL**: net profit after fees across all strict fills
- **Fill quality**: median fill price vs. settlement price
- **Sensitivity gap**: orders where loose triggered but strict didn't —
  count, and would those fills have been profitable?
- **Time-in-market**: median hold duration from fill to settlement
- **Dip frequency**: how often does the favorite's price drop
  `dip_pct`+ during a match?

---

## 4. Match-State Gating

### ESPN Score Feed

New module: `feeds/tennis_scores.py`

**Data source:**
- ATP: `https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard`
- WTA: `https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard`
- Free, no auth, JSON response.

**Response structure** (verified):
```
events[] → tournaments
  groupings[] → draw sections
    competitions[] → individual matches
      competitors[] → players
        athlete.displayName → player name
        linescores[] → set-by-set scores
      status.type.description → "In Progress" | "Final" | etc.
```

Implementation must descend: `events → groupings → competitions →
competitors`. Match rows are NOT at the top level.

### Match Lookup

```python
def get_match_state(
    tour: str,          # "atp" or "wta"
    player1: str,
    player2: str,
    event_date: str | None = None,
) -> MatchState | None
```

Matching constraints (all required):
- Same tour (ATP/WTA)
- Date match (if provided)
- Both player **surnames** must match (case-insensitive)

Returns `None` if no match found. The market is then ineligible (no
fallback in v1).

Once matched, cache the ESPN event/competition ID. Don't re-match every
poll.

### MatchState

```python
@dataclass
class MatchState:
    sets_completed: int          # max of both players' linescores length
    score_by_set: list[tuple]    # [(6,4), (3,6), ...] per completed set
    match_status: str            # scheduled | live | final | postponed | suspended
    source: str                  # "espn_atp" | "espn_wta"
```

### Gating Rule

- **Cancel resting bid when `sets_completed >= 2`.**
  This is a conservative proxy for third-set entry. We do not claim to
  detect the exact moment the 3rd set starts — only that two sets have
  completed.
- **No new bids** on matches with `sets_completed >= 2`.
- **No time-window fallback in v1.** If ESPN doesn't cover the match,
  it's skipped.

### Polling

- Poll ESPN every 60 seconds.
- Cache responses. Only re-fetch when cache is stale.

### Outage Behavior

- After 3 consecutive failed polls: pause new order placement. Log
  `order_skipped` with reason `espn_outage`.
- After 10 minutes sustained outage: cancel all resting bids. Log
  `order_cancelled` with reason `espn_outage`.
- Resume normal operation when polls succeed again.

---

## 5. Safety Rails

### Configurable Limits

| Parameter | Default | Description |
|---|---|---|
| `max_resting_orders` | 3 | Max simultaneous resting bids |
| `max_daily_fills` | 5 | Max fills per calendar day |
| `max_daily_exposure_usdc` | 100 | Hard dollar cap per day |
| `max_position_size_usdc` | 25 | Per-fill position size |
| `min_market_volume` | 10,000 | Skip illiquid markets |
| `min_favorite_price` | 0.55 | Minimum favorite confidence |
| `min_bid_price` | 0.05 | Floor for computed bid price |
| `dip_pct` | 0.30 | Bid discount from favorite price |
| `min_fill_size` | 50 | Strict-fill minimum trade size |
| `reprice_threshold` | 0.02 | Minimum bid change for cancel-replace |
| `refresh_interval_sec` | 900 | Reprice cadence (15 min) |
| `espn_poll_interval_sec` | 60 | Score feed poll cadence |
| `espn_outage_cancel_min` | 10 | Minutes before outage cancels bids |

All stored in `strategies.parameters_json`. Not hardcoded.

### Integration with Existing Systems

- Every fill runs through `evaluate_entry_risk()` from `risk/policy.py`
  — respects global position limits, total exposure, daily loss limits.
- Circuit breaker: if the strategy is paused (manually or by circuit
  breaker), all resting bids are cancelled immediately.
- `supported_execution_modes = ("paper",)` — no real execution in v1.

---

## 6. Architecture

### New Files

| File | Responsibility |
|---|---|
| `feeds/tennis_scores.py` | ESPN score feed polling + MatchState |
| `feeds/polymarket_trade_tap.py` | Lightweight watched-token trade feed (`last_trade_price` only) |
| `strategies/stink_bid_v1.py` | Strategy plugin (discover/enter/manage/exit) |
| `paper_limit_engine.py` | Paper limit order manager (place/reprice/fill-check/cancel) |

### Integration Points

- **Discovery + Qualification**: `stink_bid_v1.discover()` finds tennis
  markets via `filters.py`, qualifies them, returns signals.
- **Order Management**: `paper_limit_engine` places/reprices/cancels
  paper bids. Called by the strategy plugin's `manage()` method.
- **Fill Detection**: `paper_limit_engine` receives unfiltered watched-
  token trade events from `feeds/polymarket_trade_tap.py`. It does not
  depend on `stream_low_buys.py`.
- **Match-State**: `stink_bid_v1.manage()` calls
  `get_match_state()` to check sets completed. Cancels via
  `paper_limit_engine` when gate triggers.
- **Position Lifecycle**: on strict fill, creates a
  `strategy_positions` row via the existing strategy runner path.
  Settlement uses existing resolution detection.

### What Is NOT Built

- No real CLOB limit orders. Paper only.
- No L2 orderbook reconstruction.
- No queue simulation.
- No Kalshi support.
- No non-tennis markets (strategy is tennis-specific).

---

## 7. Exit Criteria

The paper test succeeds if after 50+ qualified matches:

1. We can answer: "Do panic fills happen, how often, at what prices?"
2. We have fill rate (strict vs loose) with enough data to distinguish
   signal from noise
3. We have realized PnL after fees for strict fills
4. We have sensitivity gap analysis (strict vs loose)
5. We have opportunity rate (how often does the price even touch the
   stink-bid zone?)

The paper test does NOT need to prove the strategy is profitable. It
needs to prove whether the *mechanism* works — do fills happen? If
fills happen, profitability analysis follows. If fills never happen,
the strategy is dead regardless of theoretical edge.
