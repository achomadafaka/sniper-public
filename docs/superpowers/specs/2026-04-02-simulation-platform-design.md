# Simulation Platform Design Spec
## Date: 2026-04-02

## Goal

A prediction market simulation platform that can:
1. Collect live orderbook and trade data from Polymarket
2. Replay historical data with honest taker-fill execution
3. Run venue-agnostic strategies against any venue's episode data
4. Compare strategy performance across venues when data is available
5. Graduate validated strategies to paper trading

**Scope:** v1 is Polymarket-first collection plus replay against existing Kalshi
episodes. No Kalshi ingestion service in v1. The schema and strategy interface
are venue-agnostic so Kalshi collection can be added later without redesign.

## Non-Goals (v1)

- No Kalshi collection service (replay existing episodes only)
- No maker/queue-position simulation (taker-only fills)
- No direct real-money execution (stays in existing Sniper system)
- No pretending sparse snapshots are L2 replay
- No multi-outcome markets (binary YES/NO only in v1)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    STRATEGIES                            │
│              (venue-agnostic Python)                     │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┴─────────────────────────────────┐
│                  REPLAY ENGINE                           │
│         Thin honest taker-fill replay over episodes      │
│         PMBench compatibility as adapter target,         │
│           not assumed as core implementation              │
│         Venue-specific fee models                        │
│         Deterministic, reproducible                      │
└───────────────────────┬─────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
┌───────┴──────┐ ┌──────┴──────┐ ┌─────┴────────┐
│ Kalshi       │ │ Polymarket  │ │ Polymarket   │
│ Episodes     │ │ Episodes    │ │ Becker       │
│ (L2, exist)  │ │ (L2, new)   │ │ (trades only)│
│ 4 now +      │ │ from        │ │ 404M trades  │
│ growing      │ │ collector   │ │ (discovery)  │
└──────────────┘ └─────────────┘ └──────────────┘
```

## Phases

### Phase 0: Canonical Schema + Market Universe Policy

**Exit gate:** Schema frozen. Venue adapter contract frozen. Classifier tested.

#### Canonical Event Schema

Raw events are stored close to venue-native shape. The compactor builds
market-wide normalized views. This preserves token-level granularity in raw
storage (Polymarket emits per-asset_id, not per-market).

Every raw event record:

| Field | Type | Description |
|-------|------|-------------|
| `venue` | string | `"polymarket"` or `"kalshi"` |
| `market_id` | string | Venue-prefixed canonical ID (`pm:{condition_id}`, `kalshi:{ticker}`) |
| `venue_market_id` | string | Venue-native ID (Polymarket: condition_id, Kalshi: market ticker) |
| `asset_id` | string, nullable | Venue-native token/contract ID. Required for Polymarket per-token events. Null for market-level events (metadata, resolution). |
| `outcome_index` | int, nullable | 0=first outcome (YES), 1=second (NO). Set when `asset_id` is present. |
| `market_type` | string | From `sim/market_type.py` classifier. Set once at market discovery. |
| `event_type` | enum | See event taxonomy below |
| `exchange_ts` | string, nullable | UTC ISO-8601 ms — when exchange says it happened. Null if venue doesn't provide it. |
| `capture_ts` | string | UTC ISO-8601 ms — when collector FIRST RECEIVED the event (not disk flush time). Non-null, non-synthesized, non-backfillable. |
| `venue_seq` | int, nullable | Venue-native sequence number if provided (Kalshi orderbook has this; Polymarket market channel does not). |
| `ingest_seq` | int | Collector-assigned monotonic counter. Required. Stable ordering when exchange_ts is null or tied. |
| `payload` | json | Event-type-specific data, venue-native shape |

#### Event Taxonomy

Raw payloads are venue-native. The compactor normalizes to market-wide views.

| Event Type | Raw Payload (Polymarket) | Raw Payload (Kalshi) |
|------------|--------------------------|---------------------|
| `book_snapshot` | Per-token: `{asset_id, bids: [{price, size}], asks: [...]}` | Per-market: `{yes_dollars: [...], no_dollars: [...]}` |
| `book_delta` | Per-token: `{asset_id, price, size, side}` | Per-market: `{seq, price, delta, side}` |
| `trade` | `{asset_id, price, size, side, trade_id}` | `{ticker, price_cents, count, side, taker_side, trade_id}` |
| `best_bba` | Per-token: `{asset_id, bid, ask}` | Per-market: `{yes_bid, yes_ask}` |
| `market_metadata` | `{question, outcomes, end_date, volume, liquidity, clob_token_ids}` | `{title, tickers, open_ts, close_ts, series_ticker}` |
| `market_resolved` | `{outcome_prices, resolved_ts}` | `{result, settled_ts}` |

#### Market Identifiers

| Venue | Raw ID | Canonical market_id |
|-------|--------|-------------------|
| Polymarket | `condition_id` | `pm:{condition_id}` |
| Kalshi | `ticker` | `kalshi:{ticker}` |

#### Outcomes

```json
{
  "outcomes": [
    {"token_id": "3627...", "label": "Yes", "index": 0},
    {"token_id": "2955...", "label": "No", "index": 1}
  ]
}
```

Supports binary and future multi-outcome.

#### Prices

Always 0.0-1.0 probability space. Kalshi cents divided by 100 on ingestion.

#### Market Type

Set once at ingestion by `sim/market_type.py`. Canonical labels:

| Label | Example |
|-------|---------|
| `crypto_threshold` | "BTC above $100K?" |
| `crypto_updown` | "Up or Down" (15-min) |
| `valuation_target` | "FDV > $1B after launch?" |
| `matchup` | "Lakers vs Celtics" |
| `sports_spread_ou` | "Spread: -3.5" |
| `social_media` | "Will Musk tweet 500 times?" |
| `geopolitical` | "US strikes Iran by Feb 28?" |
| `politics_us` | "Trump wins 2024?" |
| `economics_fed` | "Fed cuts rate?" |
| `economics_cpi` | "CPI above 3%?" |
| `economics_gdp` | "GDP growth above 2%?" |
| `weather_temp` | "NYC temp above 55F?" |
| `crypto_launch` | "Will token X launch?" |
| `deadline_event` | "Will X happen by [date]?" |
| `timebound_event` | "Will X happen in 2025?" |
| `achievement` | "Will X reach/win/hit Y?" |
| `other` | Unclassified |

#### Market Universe Policy (Polymarket collection)

- Binary 2-outcome markets only (exactly 2 outcomes — includes Yes/No, Over/Under, Team A/B, etc.)
- Volume >= $10,000
- End date within 90 days
- Active and not closed
- Max 200 active markets collected simultaneously
- Retire when resolved, closed, or < $1,000 daily volume for 7 days

#### Storage Boundary

| Layer | Location | Format | Writer | Reader |
|-------|----------|--------|--------|--------|
| Raw | VPS `~/sniper/raw/{venue}/{YYYY-MM-DD}/{HH}.jsonl.gz` | Append-only gzipped JSONL, all markets in one file per venue-hour | Collector | Compactor only |
| Normalized | Laptop `~/Desktop/polymarket-research/normalized/` | Parquet (daily per table) | Compactor | Replay engine, analysis |
| Episodes | Laptop `~/Desktop/polymarket-research/pmb/episodes/` | PMBench-compatible format | Episode builder | Replay engine |

Raw files batch all markets into one file per venue per hour. Market IDs are
inside each record. This keeps inode count bounded (24 files/day/venue, not
24 × 200 = 4,800).

Replay engine NEVER reads raw JSONL.

### Phase 1: Data Collection

Split into two responsibilities:

#### Phase 1A: Live Event Capture (VPS)

Polymarket WebSocket subscription to `book`, `price_change`, `last_trade_price`, `best_bid_ask` channels for markets passing universe policy. (`price_change` provides the `book_delta` events in the taxonomy.)

- Writes append-only gzipped JSONL to raw storage
- Sets `capture_ts` at first receipt (when the WS message arrives in the collector process, before buffering or disk flush)
- Assigns `ingest_seq` from a process-local monotonic counter
- Hourly file rotation (one file per venue per hour, all markets batched)
- Reconnection with exponential backoff
- systemd service with `Restart=always`

Resource budget: <100MB RSS, <5% CPU on 2GB VPS.

#### Phase 1B: Metadata and Settlement Sync (VPS)

Polls Gamma API every 30 minutes for:
- New market discovery (markets passing filters)
- Metadata updates (volume, liquidity changes)
- Market resolution (outcome_prices for settlement)

Reuses concepts from existing `market_outcome_poller.py`.

**Phase 1 exit gate:** Collector runs 24/7 for one week. Gap rate < 5%. Recovery from restart within 60 seconds. All raw events pass schema conformance check.

### Phase 2: Data Products + Replay Engine

#### 2A: Compaction (Laptop)

Transforms raw JSONL into normalized Parquet tables:

| Table | Fields | Source events |
|-------|--------|-------------|
| `quotes_top` | market_id, ts, yes_bid, yes_ask, no_bid, no_ask, sizes | book_snapshot, best_bba |
| `trades` | market_id, ts, side, price, size, aggressor | trade |
| `book_levels` | market_id, ts, side, levels[] | book_snapshot (optional L2) |
| `market_lifecycle` | market_id, ts, event, metadata | market_metadata, market_resolved |
| `settlements` | market_id, outcome, outcome_prices, resolved_ts | market_resolved |

Compactor rejects records missing `capture_ts`.

#### 2B: Episode Builder

Converts normalized Parquet into PredictionMarketBench episode format:
- `metadata.json` — from market_lifecycle
- `orderbook.parquet` — from quotes_top + book_levels
- `trades.parquet` — from trades
- `settlement.json` — from settlements

One episode per resolved market (or per event group for multi-ticker events).

#### 2C: Replay Engine

A thin honest taker-fill replay engine over normalized episodes. PMBench
compatibility is evaluated as an adapter target, not assumed as the core
implementation. If PMBench's agent interface fits cleanly, use it. If not,
build a simpler engine that reads the same episode format.

Requirements:
- Polymarket fee model (actual fees from data; 89.6% zero-fee maker trades)
- Kalshi fee model (7% taker, 1.75% maker on contracts × P × (1-P))
- Venue adapter interface (same strategy, different venue data)
- Batch mode (run one strategy across hundreds of episodes)
- Deterministic replay (same inputs → same outputs, always)
- Ordering by `exchange_ts` with `ingest_seq` as tiebreaker

Execution model: taker-only fills at observed best ask/bid. No queue simulation.

**Phase 2 exit gate:** Deterministic replay produces identical results on repeated runs. Fees match venue-specific models. At least 10 episodes (4 Kalshi existing + 6 Polymarket from collector) available for testing.

### Phase 3: Strategy Pipeline + Validation

#### 3A: Strategy Interface

```python
class Strategy(Agent):
    def on_episode_start(self, metadata): ...
    def act(self, ctx: AgentContext): ...
    def on_episode_end(self, result): ...
```

Venue-agnostic. Same strategy code runs on Kalshi and Polymarket episodes.

Initial strategies to implement (from evan-kolberg + academic literature):
- Deep value hold (buy cheap, hold to resolution)
- Mean reversion (buy below rolling average)
- Momentum (buy when price trending up)
- Panic fade (buy sharp drops)
- VWAP reversion (buy below VWAP)

#### 3B: Validation Harness

Every strategy evaluation includes:
- Time-based train/test split
- Monte Carlo or permutation null (stratified, not uniform)
- Holm-Bonferroni correction for multiple comparisons
- Per-segment breakdown by market_type, volume tier, entry price
- Explicit sell-side accounting (hold-to-resolution vs realized PnL)
- Pre-registered kill criteria

Uses research-methodologist agent for methodology review.
Uses strategy-evaluator agent for execution.
Uses platform-reviewer agent for spec/code review.

#### 3C: Cross-Venue Comparison

For each strategy that passes validation on one venue:
- Run on the other venue's data
- Compare: WR, PnL, ROI, Sharpe by market_type
- Identify venue-specific edge vs universal edge

#### 3D: Paper Trading Graduation

Strategies that survive validation produce a handoff note:
- Execution model and parameters
- Kill criteria for live monitoring
- Which venue and market types to deploy on
- Capital allocation recommendation

Handoff goes to the existing Sniper paper trading system. This platform does NOT execute trades.

**Phase 3 exit gate:** At least one pre-registered strategy survives out-of-sample validation with positive risk-adjusted returns after realistic fees and transaction costs.

## Dependencies Between Phases

```
Phase 0 ──→ Phase 1A + 1B (parallel)
               │
Phase 0 ──→ Phase 2C (can start with existing Kalshi episodes)
               │
Phase 1 ──→ Phase 2A + 2B (need raw data)
               │
Phase 2 ──→ Phase 3 (need episodes)
```

Phase 0 and Phase 2C can start immediately — we have the schema design and 4 Kalshi episodes.
Phase 1 starts after Phase 0 freezes the schema.
Phase 2A/2B start after Phase 1 produces data.
Phase 3 starts after Phase 2 produces episodes (but strategy development starts against existing Kalshi episodes in parallel).

## Existing Code Reuse

| Existing | Reuse |
|----------|-------|
| `sim/market_type.py` | Canonical classifier — used at ingestion |
| `replay/adapters.py` | Adapter pattern for venue abstraction |
| `market_outcome_poller.py` | Concepts for Phase 1B metadata sync |
| PredictionMarketBench (external) | Reference for episode format, agent interface, fee models |
| Becker dataset | Discovery layer for Polymarket hypotheses |
| `.claude/agents/` | 12 agent files (6 new for simulation platform + 6 existing) |

## Agent Team

| Agent | Phase | Role |
|-------|-------|------|
| schema-architect | 0, 1, 2 | Validates all data format changes |
| data-quality-monitor | 1, 2 | Monitors collector health and compaction |
| research-methodologist | 3 | Reviews study designs and findings |
| strategy-evaluator | 3 | Runs backtests and computes metrics |
| platform-reviewer | All | Adversarial review of specs, plans, code |
| web-researcher | All | Live market inspection, docs browsing |

## Resource Allocation

| Component | Runs on | Resource budget |
|-----------|---------|----------------|
| Collector (1A + 1B) | VPS | <100MB RSS, <5% CPU |
| Compactor (2A) | Laptop | DuckDB 8GB limit, batch |
| Episode builder (2B) | Laptop | Light — transforms Parquet |
| Replay engine (2C) | Laptop | Custom replay engine + PMBench-compatible episodes |
| Strategy testing (3) | Laptop | DuckDB + replay, batch |

## Risk Register

| Risk | Mitigation |
|------|-----------|
| VPS OOM from collector | 200 market cap, bounded WS connections, memory monitoring |
| Polymarket WS API changes | Version-pin subscription format, alert on connection failures |
| Sell-side contamination in backtests | Explicit accounting rules in strategy-evaluator agent |
| Overfitting to 4 Kalshi episodes | Expand episode library from collector; cross-venue validation |
| pmxt R2 access restored later | Schema supports importing pmxt data alongside our own |
| Strategy works on backtest but fails live | Paper trading stage before real money; kill criteria monitored |
