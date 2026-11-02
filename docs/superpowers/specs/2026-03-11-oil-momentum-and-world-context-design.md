# Oil Momentum Strategy & World Context Feeds — Design Spec

## Goal

Build a commodity-price momentum strategy (`oil_momentum_v1`) that detects significant oil price movements and trades related Polymarket prediction markets before they reprice. Supported by two new data feeds (news, commodities) and a shared context query layer that future strategies can also consume.

## Architecture

Approach B (Layered Feeds + Shared Context) with Codex's refinement: the context layer is a **derived query/view layer** over normalized feed tables, not a physical table. Edge logic lives in the strategy, not the context layer.

**Data flow:**

```
NewsAPI / RSS ──→ feeds/news.py ──→ news_snapshots table
                                          │
Free commodity API ──→ feeds/commodities.py ──→ commodity_snapshots table
                                                      │
                              context/ query layer ◄───┤ (read-only JOINs)
                                    │                  │
                                    ▼                  │
                          oil_momentum_v1 ◄────────────┘
                                    │
                                    ▼
                          strategy_positions (paper trades)
```

**Tech stack:**
- NewsAPI (free tier, 100 req/day) + RSS feeds (Reuters, AP — no key needed)
- yfinance (no API key) as primary commodity data provider, with fallback provider path
- Existing strategy plugin framework, feed adapter pattern, risk policy layer

---

## 1. News Feed Adapter

### `feeds/news.py` — `NewsFeedAdapter`

| Attribute | Value |
|-----------|-------|
| `feed_id` | `"news"` |
| `display_name` | `"News (NewsAPI + RSS)"` |
| `default_poll_seconds` | `900` (15 minutes) |
| `mode` | `"poll"` |

**Behavior:**
- `validate_config()`: always returns `True` — RSS works without any API key. NewsAPI is optional enrichment when `NEWS_API_KEY` is set.
- `warmup(conn)`: creates `news_snapshots` table.
- `tick(conn)`: polls configured RSS feeds first, then NewsAPI if key is available. Deduplicates before insert.

**RSS feeds (hardcoded defaults, overridable via `NEWS_RSS_FEEDS` env var):**
- Reuters World News
- AP Top Headlines
- Al Jazeera English

**Dedup strategy:**
- Canonicalize URL (strip tracking params: `utm_*`, `ref`, `source`, etc.)
- Primary dedup: hash of canonicalized URL → `article_hash`
- Fallback: if no URL or URL collision with different content, hash of `title + published_at`
- `article_hash` column has UNIQUE constraint — INSERT OR IGNORE

**Storage: `news_snapshots` table**

Raw fields (from source):
| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | auto |
| `instance_tag` | TEXT | from env |
| `feed_id` | TEXT | `"news"` |
| `source_name` | TEXT | `"newsapi"`, `"rss_reuters"`, etc. |
| `canonical_url` | TEXT | cleaned URL |
| `article_hash` | TEXT UNIQUE | dedup key |
| `title` | TEXT | article title |
| `description` | TEXT | article summary/snippet |
| `published_at` | TEXT | ISO 8601 from source |
| `payload_json` | TEXT | full raw response |
| `captured_at` | TEXT | our clock, ISO 8601 |

Derived fields (computed at ingest):
| Column | Type | Notes |
|--------|------|-------|
| `domain` | TEXT | classified by `_classify_news_domain()` — a dedicated function for news titles (see below) |
| `sentiment_keywords` | TEXT | JSON array of extracted keywords (e.g. `["sanctions", "escalation"]`) |

**Note:** `region` column deferred from v1 — no concrete consumer yet and auto-detection logic is underspecified. Can be added via migration when `geopolitical_v1` needs it.

**`_classify_news_domain(title)` function** (new, in `db.py`):
- Separate from `_classify_title_domain()` which is tuned for Polymarket market titles
- Keyword patterns tuned for news article headlines:
  - "Oil" / "Energy": oil, crude, brent, wti, petroleum, opec, refinery, pipeline, energy price, barrel
  - "Geopolitics": iran, sanctions, war, military, conflict, nato, ceasefire, invasion, troops
  - "Finance": fed, interest rate, inflation, gdp, stock market, treasury, central bank
  - "Crypto": bitcoin, ethereum, crypto, blockchain
  - Falls back to "General" if no match
- Returns a single string domain label

**Indexes:**
- `(article_hash)` — UNIQUE
- `(domain, published_at DESC)`
- `(captured_at DESC)`

**Config env vars:**
- `NEWS_API_KEY` — optional, enables NewsAPI source
- `NEWS_RSS_FEEDS` — optional, comma-separated RSS URLs (defaults built in)

---

## 2. Commodities Feed Adapter

### `feeds/commodities.py` — `CommoditiesFeedAdapter`

| Attribute | Value |
|-----------|-------|
| `feed_id` | `"commodities"` |
| `display_name` | `"Commodity Prices"` |
| `default_poll_seconds` | `1800` (30 minutes) |
| `mode` | `"poll"` |

**Behavior:**
- `validate_config()`: returns `True` (yfinance needs no API key). Logs warning if no symbols configured.
- `warmup(conn)`: creates `commodity_snapshots` table.
- `tick(conn)`: fetches current prices for all configured symbols via the active provider.

**Provider abstraction:**
- Internal `_fetch_prices(symbols) → List[dict]` dispatches to the active provider.
- v1 provider: `yfinance` — fetches spot price for each symbol.
- Provider is selected internally, not user-configurable in v1. Fallback path exists for future providers (Alpha Vantage, etc.) but only yfinance is implemented.
- Each snapshot records `provider` so we know the data source.

**Default symbols:** `BZ=F` (Brent crude), `CL=F` (WTI crude), `GC=F` (gold), `NG=F` (natural gas)

**Failure handling**: yfinance is an unofficial wrapper with no SLA. On failure per-symbol: log the exception, skip that symbol, continue to the next. Same pattern as `feeds/weather.py` (return `None` per-item on failure). If all symbols fail, the tick completes with zero snapshots stored (not an error — logged as warning).

**Change percentages are NOT stored.** All momentum/delta calculations are derived from our own stored snapshots at query time.

**Storage: `commodity_snapshots` table**

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | auto |
| `instance_tag` | TEXT | from env |
| `feed_id` | TEXT | `"commodities"` |
| `provider` | TEXT | `"yfinance"` |
| `symbol` | TEXT | `"BZ=F"`, `"CL=F"`, etc. |
| `price_usd` | REAL | spot price |
| `volume` | REAL | nullable |
| `market_ts` | TEXT | market timestamp from provider |
| `payload_json` | TEXT | full raw response |
| `captured_at` | TEXT | our clock, ISO 8601 |

**Unique constraint:** `(symbol, market_ts, provider)`

**Indexes:**
- `(symbol, captured_at DESC)`
- `(symbol, market_ts, provider)` — UNIQUE

**Config env vars:**
- `COMMODITY_SYMBOLS` — optional, comma-separated (defaults: `BZ=F,CL=F,GC=F,NG=F`)

---

## 3. Context Query Layer

### `context/` package — read-only helpers

Pure functions that JOIN across feed tables and existing Sniper tables. No writes, no side effects, no stored state.

**DB query location**: All raw SQL lives in `db.py` (per project rule: "All DB reads/writes go through `db.py`"). The `context/market_context.py` module is a **thin re-export/composition layer** that calls `db.py` helpers and combines their results. It does not contain raw SQL itself. This keeps the project rule intact while giving strategies a clean import path (`from context import find_affected_markets`).

**Files:**
- `context/__init__.py` — re-exports all public functions
- `context/market_context.py` — composition functions that call `db.py` helpers

### Functions

#### `find_affected_markets(conn, keywords, domain=None, min_volume_24h=0) → List[dict]`
- Queries `market_metadata` for open markets whose titles match any keyword
- **Implementation**: fetches open markets (up to 500, ordered by volume_24h DESC), then Python-side `any(kw.lower() in title.lower() for kw in keywords)` filter — same approach as `weather_v1.discover()` which iterates rows and checks `_title_matches_weather()`
- Optional domain filter
- Returns: `condition_id`, `title`, `category`, `end_date_iso`, `volume_24h`, `matched_keywords`

#### `commodity_price_history(conn, symbol, hours=168) → List[dict]`
- Queries `commodity_snapshots` for price time series
- Returns: `price_usd`, `market_ts`, `provider`, `captured_at`
- Ordered by `market_ts ASC`

#### `market_price_series(conn, condition_id, hours=72) → List[dict]`
- Assembles whatever price history is available from `signal_history` and `position_signals`
- Returns: `price`, `timestamp`, `source` (which table it came from)
- Ordered by timestamp ASC
- **Important limitation**: for markets no insider wallet has touched, this returns an empty list. This is expected — the strategy runner's existing `_enrich_signal_with_live_price(signal)` step (strategy_runner.py line 643) fetches the live CLOB price via Polymarket Gamma API before `enter()` is called. So `discover()` sets `current_price=0.0` and relies on the runner's enrichment step to fill in the live price. The `market_price_series()` helper is primarily useful for `manage()` and `exit()` to track price over time, and as a secondary signal in `discover()` when available.
- **`market_metadata` is NOT a price source** — it has `volume_24h` but no price column

#### `recent_news_for_market(conn, condition_id, hours=24) → List[dict]`
- Finds recent `news_snapshots` whose `domain` or `sentiment_keywords` overlap with the market's title keywords
- Returns matched articles with relevance metadata
- Used for context enrichment and operator visibility, not entry logic in v1
- **Graceful degradation**: if `news_snapshots` table does not exist (news feed never initialized), returns empty list — the `db.py` helper wraps the query in try/except for `OperationalError`

#### `insider_convergence_check(conn, condition_id, min_insider_score=55) → dict`
- Queries `position_signals` for high-insider-score wallets on this market
- Returns: `insider_count`, `total_insider_notional`, `avg_insider_score`
- Annotation only — not used as an entry gate in `oil_momentum_v1`

---

## 4. Oil Momentum V1 Strategy

### `strategies/oil_momentum_v1.py` — `OilMomentumV1Strategy`

| Attribute | Value |
|-----------|-------|
| `plugin_id` | `"oil_momentum_v1"` |
| `strategy_family` | `"commodity_momentum"` |
| `required_feeds` | `("polymarket", "commodities")` |
| `supported_execution_modes` | `("paper",)` |
| `default_loop_seconds` | `300` |

Note: `news` feed is NOT in `required_feeds` — it's used for annotation only, and the strategy should work without it.

### `discover(conn, strategy) → List[StrategySignal]`

1. Get commodity price history for configured symbols from `commodity_price_history()`
2. Compute momentum from our own snapshots:
   - `change_pct = (latest_price - price_N_hours_ago) / price_N_hours_ago * 100`
   - Default lookback: 24 hours (`momentum_window_hours` parameter)
3. If momentum exceeds threshold (default: 3.0%), search for related markets:
   - Call `find_affected_markets()` with oil/energy keywords: `["oil", "crude", "brent", "wti", "petroleum", "energy price", "barrel"]`
4. For each matched market, determine direction:
   - Parse market title for thesis direction:
     - Title contains "above", "exceed", "over", "higher", "rise" → oil-up thesis
     - Title contains "below", "under", "fall", "drop", "decline" → oil-down thesis
     - Cannot determine → skip this market
   - If commodity momentum direction matches title thesis direction → signal YES
   - If they oppose → skip (do not bet against commodity momentum in v1)
5. Set `current_price=0.0` on the signal — the strategy runner's `_enrich_signal_with_live_price()` will fetch the live CLOB price before `enter()` is called. If `market_price_series()` returns data, store it in signal metadata as supplementary context.
6. Return `StrategySignal` with `wallet="oil_momentum_bot"` (synthetic wallet identifier) and metadata including commodity momentum data, matched keywords, and direction reasoning

### `enter(conn, strategy, signal, open_positions) → StrategyEntryDecision`

Gates (in priority order):
1. **Duplicate check** — already open for this `condition_id` + `outcome`
2. **Max concurrent** — default 5 positions
3. **Max entry price** — default 0.70 (don't buy expensive odds)
4. **Market exposure cap** — default $50 per market
5. **Momentum freshness** — re-check commodity momentum at entry time; must still exceed threshold. No stale signals.
6. **Resolution date** — market must resolve within `max_days_to_resolution` (default 60 days)

Sizing: flat bet, default $10 (`bet_size_usdc` parameter). Clamped to market exposure headroom.

Insider convergence stored as annotation in entry metadata (not a gate):
```python
metadata = {
    "commodity_symbol": "BZ=F",
    "momentum_pct": 4.2,
    "momentum_window_hours": 24,
    "direction": "oil_up",
    "insider_convergence": insider_convergence_check(conn, signal.condition_id),
}
```

### `manage(conn, strategy, position) → StrategyManageState`

- Check `market_metadata.is_open`
- **Fetch live market price** via Polymarket Gamma API (same `_enrich_signal_with_live_price()` pattern the runner uses for discovery enrichment). This is the authoritative "current price" for exit decisions.
- `market_price_series()` is NOT used as the live price source — it is sparse/historical and may be empty for markets the system hasn't previously tracked
- Signal stays open while market is open

### `exit(conn, strategy, position, state) → StrategyExitDecision`

Six exit rules (v1), checked in this order:
1. **Market closed** — `is_open = 0` → `status="closed"`, `reason="market_closed"`. This exit is tied to market closure data (`is_open=0`), NOT actual resolution/outcome evidence. We don't have resolution evidence in v1.
2. **Near-certain win** — live price >= 0.97 → `status="closed"`, `reason="early_close_win"`. This is an early close based on price, NOT resolution. The market hasn't formally resolved yet but the outcome is near-certain.
3. **Near-certain loss** — live price <= 0.03 → `status="closed"`, `reason="early_close_loss"`. Same as above — early close, not resolution.
4. **Take profit** — price moved in our favor by `take_profit_pct` (ROI-based). Formula: `(current_price - entry_price) / entry_price >= take_profit_pct`. Default 0.30 = 30% ROI. Example: entered at 0.40, triggers at 0.52. → `status="closed"`, `reason="take_profit"`
5. **Stale** — position open > 14 days → `status="closed"`, `reason="stale_Nd"`
6. **Hold** — none of the above → `status="open"`, `reason="hold"`

**NOT in v1:** momentum reversal exit. Deferred until replay path can model it honestly.

### `replay_adapter()`

Returns `None` in v1. Replay is unavailable until sufficient `commodity_snapshots` history has accumulated (see Rollout section).

### Configurable Parameters

All via strategy DB row `parameters` JSON, with sensible defaults:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `momentum_threshold_pct` | `3.0` | Min 24h commodity price change to trigger |
| `momentum_window_hours` | `24` | Lookback for momentum calculation |
| `max_entry_price` | `0.70` | Max Polymarket price to enter |
| `bet_size_usdc` | `10.0` | Flat bet size |
| `max_concurrent_positions` | `5` | Max open positions |
| `max_exposure_per_market_usdc` | `50.0` | Per-market exposure cap |
| `stale_days` | `14` | Auto-exit after N days |
| `take_profit_pct` | `0.30` | Take profit threshold |
| `max_days_to_resolution` | `60` | Skip markets resolving too far out |
| `commodity_symbols` | `"BZ=F,CL=F"` | Which symbols to monitor |
| `oil_keywords` | `"oil,crude,brent,wti,petroleum,energy price,barrel"` | Market title match keywords |

---

## 5. Testing Strategy

### Unit Tests

| File | Scope | Key Tests |
|------|-------|-----------|
| `tests/test_news_feed.py` | News feed adapter | RSS parsing, NewsAPI handling, dedup by `article_hash`, RSS-only mode without API key, domain classification, URL canonicalization |
| `tests/test_commodities_feed.py` | Commodities feed adapter | Provider wrapping, snapshot storage, `(symbol, market_ts, provider)` unique constraint, missing symbol handling |
| `tests/test_context_layer.py` | Context query layer | `find_affected_markets()`, `commodity_price_history()`, `market_price_series()`, `recent_news_for_market()`, `insider_convergence_check()` — all against seeded test data |
| `tests/test_oil_momentum.py` | Strategy plugin | Discover (momentum detection + market matching + direction logic), enter (all 6 gates), manage, exit (resolution/stale/take-profit), direction parsing (oil-up/oil-down/ambiguous=skip), signal metadata contains insider convergence annotation |

### Integration Tests

- Feed registration: `news` and `commodities` in `feeds/registry.py`
- Strategy registration: `oil_momentum_v1` in `strategies/registry.py`
- Runner filters by `ENABLED_STRATEGIES=oil_momentum_v1`
- Runner enforces `required_feeds = ("polymarket", "commodities")`
- Risk policy gates apply to strategy entries
- `instance_tag` propagated to all DB artifacts

---

## 6. Rollout Plan

### Phase 1: Build & Test Locally
- Implement feeds, context layer, strategy plugin
- Full test suite green (target: ~60-80 new tests)
- Manual smoke test: verify commodity feed fetches real yfinance data, news feed parses real RSS

### Phase 2: Paper Trading on Separate VPS Instance

**Critical: separate from the insider-copy proof-bar environment.**

This runs as a completely isolated instance:
- Separate `INSTANCE_TAG` (e.g. `oil-momentum-paper`)
- **Separate DB file is mandatory** (`DB_PATH=oil_momentum.db`) — must NOT share the insider-copy proof-bar database. Mixed data makes rollback harder and risks polluting proof-bar metrics.
- Separate runtime process (own systemd unit or screen session)
- Does NOT share `strategy_positions` rows with insider-copy
- Does NOT affect the proof-bar metrics (150+ trades at insider>=60)

Example `.env` profile:
```
INSTANCE_TAG=oil-momentum-paper
INSTANCE_DISPLAY_NAME=Oil Momentum Paper
INSTANCE_ROLE=paper
ENABLED_STRATEGIES=oil_momentum_v1
ENABLED_FEEDS=polymarket,news,commodities
PAPER_ONLY=1
INSTANCE_BANKROLL_USDC=500
INSTANCE_MAX_TOTAL_EXPOSURE_USDC=500
INSTANCE_MAX_MARKET_EXPOSURE_USDC=50
INSTANCE_MAX_CONCURRENT_POSITIONS=5
INSTANCE_DAILY_LOSS_LIMIT_USDC=25
STRATEGY_LOOP_SECONDS=300
COMMODITY_SYMBOLS=BZ=F,CL=F,GC=F,NG=F
```

### Phase 3: Evaluate & Iterate

- **Replay dependency**: `oil_momentum_v1` replay is unavailable until sufficient `commodity_snapshots` history has accumulated (minimum ~2 weeks of continuous polling). A one-time historical backfill via yfinance's history API may be added as a convenience script, but is not part of the v1 critical path.
- Compare paper PnL against baseline
- Evaluate whether signals are firing on real oil-related markets
- Decide next step: tune parameters, add `geopolitical_v1`, or expand commodity scope

---

## 7. What Is NOT in V1

- No LLM classification or Claude API calls
- No `geopolitical_v1` strategy (deferred until feeds/context proven)
- No sanctions, macro, or second-order proxy markets
- No momentum reversal exit rule
- No insider convergence as entry gate (annotation only)
- No monitor-the-situation.com integration
- No replay support (deferred until snapshot history exists)
- No real trading execution mode

---

## 8. File Inventory

### New Files
| File | Purpose |
|------|---------|
| `feeds/news.py` | News feed adapter (NewsAPI + RSS) |
| `feeds/commodities.py` | Commodity price feed adapter (yfinance) |
| `context/__init__.py` | Context layer package exports |
| `context/market_context.py` | Read-only query helpers |
| `strategies/oil_momentum_v1.py` | Oil momentum strategy plugin |
| `tests/test_news_feed.py` | News feed tests |
| `tests/test_commodities_feed.py` | Commodities feed tests |
| `tests/test_context_layer.py` | Context layer tests |
| `tests/test_oil_momentum.py` | Strategy tests |

### Modified Files
| File | Change |
|------|--------|
| `feeds/registry.py` | Register `NewsFeedAdapter`, `CommoditiesFeedAdapter` |
| `strategies/registry.py` | Register `OilMomentumV1Strategy` |
| `db.py` | Add `ensure_news_snapshots_table()`, `insert_news_snapshot()`, `ensure_commodity_snapshots_table()`, `insert_commodity_snapshot()`, query helpers |
| `.env.example` | Add `COMMODITY_SYMBOLS`, `NEWS_RSS_FEEDS`, oil momentum instance profile example |
