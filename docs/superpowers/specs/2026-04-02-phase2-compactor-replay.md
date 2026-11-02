# Phase 2: Compactor + Episode Builder + Replay Engine

## Goal

Transform raw JSONL from the VPS collector into normalized Parquet tables,
build episodes from resolved markets, and provide an honest taker-fill
replay engine for strategy testing.

## Scope

- **2A: Compactor** — raw JSONL → typed Parquet tables (laptop)
- **2B: Episode Builder** — Parquet → Polymarket episode format (laptop)
- **2C: Replay Engine** — taker-fill simulation over episodes (laptop)

## Non-Goals

- No orderbook reconstruction from deltas (store deltas as-is)
- No maker/queue simulation
- No sampling or aggregation in the pipeline — full fidelity at every stage
- No PMBench format compatibility in v1 — Polymarket episodes use their own
  native format (probability space, per-asset structure). Kalshi episodes
  use PMBench format as-is. The replay engine consumes both via adapters.

## Input: Raw Data Shape (verified from live collector 2026-04-02)

Event type distribution (from 1000-event sample):
- `book_delta`: 97% — price_change fan-out. Contains `{asset_id, price, size, side, best_bid, best_ask}`. The `best_bid`/`best_ask` fields ARE present in production data (verified 2026-04-03 against live VPS raw files). This makes book_delta the primary source for `quotes_top`.
- `best_bba`: 2% — top-of-book updates. Contains `{asset_id, best_bid, best_ask}`.
- `book_snapshot`: 0.6% — full depth (100+ levels). Contains `{asset_id, bids: [{price, size}], asks: [{price, size}]}`.
- `trade`: 0.3% — filled trades. Contains `{asset_id, price, size, side, timestamp}` and, in current live capture, also carries `fee_rate_bps` and `transaction_hash`. These fields should be preserved when present rather than discarded.

Rate: ~55K events/hour, ~14MB/hour compressed, ~960MB/day raw.

Raw file format: `{raw_dir}/{venue}/{YYYY-MM-DD}/{HH}.jsonl` (plain text during
collection, `.jsonl.gz` after rotation). Records conform to `sim.schema.RawEvent`.

## 2A: Compactor

### Design Principle

The compactor is a stateless per-record transform. It reads raw JSONL files,
routes each event by `event_type` into the correct Parquet table, preserves
ALL fields. No state, no reconstruction, no sampling, no aggregation.

"Stateless" means: each record is processed independently. The compactor does
not track orderbook state, does not join across event types, and does not
depend on having seen prior records. Different event types require different
payload field extractions (routing logic), but no cross-record state.

### Output Tables

All tables partitioned by date (`YYYY-MM-DD`). Stored under
`~/Desktop/polymarket-research/normalized/{table_name}/`.

#### `trades.parquet`

One row per trade event.

| Column | Type | Source |
|--------|------|--------|
| market_id | string | event.market_id |
| asset_id | string | event.asset_id |
| capture_ts | string | event.capture_ts |
| exchange_ts | string | event.exchange_ts (nullable) |
| ingest_seq | int | event.ingest_seq |
| price | float | payload.price (parsed to float) |
| size | float | payload.size (parsed to float) |
| side | string | payload.side |
| fee_rate_bps | int (nullable) | payload.fee_rate_bps when present |
| transaction_hash | string (nullable) | payload.transaction_hash when present |
| market_type | string | event.market_type |
| venue | string | event.venue |

Note: these fields are present in the current live WS capture, but the
compactor should still treat them as nullable because not every venue/event
guarantees them.

#### `quotes_top.parquet`

Best bid/ask per asset per timestamp. Sourced from ALL THREE event types:
- `book_delta` (97% of events) — contains `best_bid`/`best_ask` in payload (verified on live data)
- `best_bba` (2%) — contains `best_bid`/`best_ask`
- `book_snapshot` (0.6%) — top level of bids/asks arrays

This gives ~55K quote updates/hour — high-frequency top-of-book timeline.

| Column | Type | Source |
|--------|------|--------|
| market_id | string | event.market_id |
| asset_id | string | event.asset_id |
| capture_ts | string | event.capture_ts |
| exchange_ts | string | event.exchange_ts (nullable) |
| ingest_seq | int | event.ingest_seq |
| best_bid | float | book_delta/best_bba: payload.best_bid; book_snapshot: bids[0].price |
| best_ask | float | book_delta/best_bba: payload.best_ask; book_snapshot: asks[0].price |
| bid_size | float (nullable) | book_snapshot: bids[0].size; book_delta: payload.size only when side=BUY and price=best_bid; best_bba: null |
| ask_size | float (nullable) | book_snapshot: asks[0].size; book_delta: payload.size only when side=SELL and price=best_ask; best_bba: null |
| source_event | string | "book_delta", "best_bba", or "book_snapshot" |
| venue | string | event.venue |

#### `book_levels.parquet`

Full orderbook depth from `book_snapshot` events only. One row per level.
ALL levels stored — no truncation.

| Column | Type | Source |
|--------|------|--------|
| market_id | string | event.market_id |
| asset_id | string | event.asset_id |
| capture_ts | string | event.capture_ts |
| ingest_seq | int | event.ingest_seq |
| side | string | "bid" or "ask" |
| price | float | level.price |
| size | float | level.size |
| level_index | int | position in bids/asks array |
| venue | string | event.venue |

#### `book_deltas.parquet`

Raw book deltas (97% of all events). Stored for optional future L2
reconstruction. One row per delta.

| Column | Type | Source |
|--------|------|--------|
| market_id | string | event.market_id |
| asset_id | string | event.asset_id |
| capture_ts | string | event.capture_ts |
| exchange_ts | string | event.exchange_ts (nullable) |
| ingest_seq | int | event.ingest_seq |
| price | float | payload.price |
| size | float | payload.size |
| side | string | payload.side |
| best_bid | float | payload.best_bid |
| best_ask | float | payload.best_ask |
| venue | string | event.venue |

Note: This table stores ~97% of raw event volume (~900MB/month compressed).
If storage becomes a concern and L2 reconstruction is not needed, this table
can be skipped or retention-limited.

#### `market_lifecycle.parquet`

Market metadata and resolution events. Replaces the separate `settlements`
table from the platform spec — settlement data is a subset of lifecycle
events where `event_type = "market_resolved"`.

| Column | Type | Source |
|--------|------|--------|
| market_id | string | event.market_id |
| capture_ts | string | event.capture_ts |
| ingest_seq | int | event.ingest_seq |
| event_type | string | "market_metadata" or "market_resolved" |
| market_type | string | event.market_type |
| payload | string | JSON-serialized full payload |
| venue | string | event.venue |

### Compactor Behavior

1. Reads ONLY rotated `.jsonl.gz` files — never active `.jsonl` files.
   Active files are still being written and would produce partial/duplicate data.
2. Parses each line via `sim.serialize.jsonl_to_event()`
3. Rejects records that fail round-trip validation (logs rejection count)
4. Routes by `event_type` to the appropriate table builder
5. Writes Parquet files partitioned by date
6. Idempotency: tracks processed `.jsonl.gz` filenames via a manifest file.
   Parquet is flushed per-file before marking processed and saving the manifest.
   Re-running on an already-processed file is a no-op. Since `.jsonl.gz` files
   are immutable after rotation, path-based dedup is safe.
   **Known limitation:** if `writer.flush()` succeeds but the process dies
   before `manifest.save()`, rerunning will re-append the same rows (Parquet
   concat is additive). A future fix could use write-ahead dedup on
   `ingest_seq` or atomic manifest writes.
7. Reports: records read, records written per table, records rejected

### Compactor Does NOT

- Reconstruct orderbook state from deltas
- Aggregate or downsample any data
- Join across event types
- Modify any field values
- Touch raw files (read-only)
- Resolve `outcome_index` (that's the episode builder's job)

## 2B: Episode Builder

Converts normalized Parquet tables for a SINGLE RESOLVED MARKET into a
self-contained episode directory.

### Polymarket Episode Format

Polymarket episodes use their own format — NOT PMBench format. The replay
engine consumes both Polymarket and Kalshi episodes via venue adapters.

```
episodes/polymarket/{market_id}/
  metadata.json     — market info, tokens, timestamps, fee metadata / token fee rates
  quotes.parquet    — time-ordered top-of-book (from quotes_top)
  trades.parquet    — time-ordered trades
  book_depth.parquet — full orderbook snapshots (from book_levels, optional)
  settlement.json   — resolution outcome
```

### Outcome Index Resolution

The collector stores `outcome_index = None` on all events because the WS
parser doesn't have market context. The episode builder resolves this by
joining against `market_lifecycle` metadata:

1. From the `market_metadata` event, extract the `tokens` array:
   `[{token_id: "...", label: "Yes", index: 0}, {token_id: "...", label: "No", index: 1}]`
2. Build a lookup: `asset_id → outcome_index`
3. Resolve fee metadata:
   - preserve `fees_enabled`, `enable_order_book`, and `fee` from `market_metadata`
   - if `fees_enabled` is true, the caller must supply per-token fee rates via
     the `fee_rates` parameter (a dict of `asset_id → max_fee_rate`). These are
     cached in episode `metadata.json` as `token_fee_rates`. Automatic fetching
     from the Polymarket fee-rate endpoint is deferred to a future helper.
   - if a fee-enabled market is missing token fee rates, the episode is rejected
4. Enrich all trades and quotes with `outcome_index` before writing to episode
5. Transform per-asset quotes into per-market YES/NO view for the episode

### Settlement Format

```json
{
  "market_id": "pm:0x...",
  "outcome": "No",
  "outcome_prices": ["0.01", "0.99"],
  "resolved_ts": "2026-04-15T12:00:00.000Z"
}
```

### Episode Validation

An episode must have:
- At least 1 trade
- At least 1 quote
- A valid settlement with a clear winner (one price > 0.99)
- Resolved `outcome_index` for all trades and quotes
- Token fee metadata for any fee-enabled market

## 2C: Replay Engine

### Execution Model

Taker-only fills at observed best ask (for buys) or best bid (for sells).

For each agent action:
- Agent calls `place_order(ticker, side, count)`
- Engine looks up current best ask/bid from the quotes timeline
- **Liquidity check:** fill count is capped at observed top-of-book size.
  If the agent orders 1000 but the best ask has 100 available, only 100 fill.
  Remaining is rejected (no walking the book in v1).
- If no quote exists at this timestamp, order rejects
- **Fee:** explicit Polymarket taker fee model using episode-cached token fee
  rates. The metadata sync stores `fees_enabled`/`fee` eligibility metadata,
  but the episode builder must fetch and cache token fee rates for fee-enabled
  assets before replay. For fee-enabled markets, replay applies
  `fee = max_fee_rate × price × (1 - price) × quantity` using the cached token
  rate. For fee-disabled markets, fee is 0. If a fee-enabled asset is missing
  a cached fee rate, the episode is invalid unless an explicit override is
  supplied. NEVER silently default taker fees to 0.
- Position tracked until settlement

### Ordering

Events ordered by `exchange_ts` with `ingest_seq` as tiebreaker.
When `exchange_ts` is null, use `capture_ts` as fallback. Ordering:
1. `exchange_ts` (if present)
2. `capture_ts` (fallback when exchange_ts is null)
3. `ingest_seq` (final tiebreaker for same-timestamp events)

### What Makes It Honest

- No queue simulation — you can't rest orders
- No guaranteed fills — rejects when no quote or insufficient liquidity
- Fills capped to observed top-of-book size. When `bid_size`/`ask_size`
  is null, carry forward the last known explicit top size for that side from:
  - `book_snapshot` top-of-book levels, or
  - `book_delta` rows where the delta touched the new top price on that side
    (`BUY` at `best_bid`, `SELL` at `best_ask`)
  If no explicit top size has ever been observed for that side, fill is rejected.
- Fee is explicit, never zero for taker fills
- Deterministic: same episode + same strategy = same result, always

### Venue Adapters

The replay engine reads episodes through venue adapters:

- **Polymarket adapter:** reads Polymarket episode format (probability space,
  per-asset structure, Polymarket fee model)
- **Kalshi adapter (deferred):** reads PMBench episode format as-is (cents,
  per-market structure, Kalshi fee model). Deferred until Kalshi episode data
  exists — no Kalshi data source is currently available.

Same strategy code runs on both via the adapter abstraction.

### Strategy Interface

```python
class Strategy:
    def on_episode_start(self, metadata: dict) -> None: ...
    def act(self, ctx: ReplayContext) -> None: ...
    def on_episode_end(self, result: dict) -> None: ...
```

`ReplayContext` provides:
- `get_markets()` — current quotes for all tickers
- `get_positions()` — current holdings
- `get_cash()` — available balance
- `place_order(ticker, side, count)` — execute a trade
- `get_time()` — current simulation timestamp

### Batch Mode

```python
engine = ReplayEngine(starting_cash=1000.0)
results = engine.run(strategy, episode_dirs=["path/to/ep1", "path/to/ep2"])
```

Returns per-episode `EpisodeResult`: PnL, starting/ending cash, fills, settlement payout, outcome.

## Phase 2 Exit Gate

1. Compactor processes at least 24h of raw data into all 5 Parquet tables
2. Episode builder produces at least 1 valid Polymarket episode from a resolved market
3. Replay engine runs a trivial strategy (buy-and-hold) on that episode,
   producing deterministic non-zero results on repeated runs
4. All Parquet files are fully readable (no truncation, no corruption)
5. Known-answer test: manually calculate expected PnL for a simple strategy
   on a Polymarket episode, verify engine matches within $0.01
   (Kalshi exit gate deferred — no Kalshi episode data available yet)

## Design Constraints (from prior sessions)

- Full fidelity through the pipeline — sample only at display time
- All orderbook levels stored (100+ per snapshot) — no truncation
- Compactor is stateless and idempotent (manifest-tracked)
- Replay engine reads normalized Parquet, never raw JSONL
- Two episode formats: Polymarket native + Kalshi/PMBench
- Taker fees use cached token fee rates for fee-enabled markets, never a silent 0 default
- Fills capped to observed liquidity — carry forward only explicit top sizes, not arbitrary delta sizes
- Null exchange_ts falls back to capture_ts for ordering
- Compactor processes ONLY rotated .jsonl.gz, never active .jsonl
- Settlement detection: meta sync fetches both active AND recently-closed markets
- book_delta best_bid/best_ask verified present in live data (2026-04-03)
