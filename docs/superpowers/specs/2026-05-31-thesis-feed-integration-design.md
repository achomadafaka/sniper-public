# Spec - Thesis Feed Integration

**Date:** 2026-05-31
**Status:** design
**Parent:** `docs/superpowers/specs/2026-05-31-winning-wallet-thesis-extractor-design.md`
**Research note:** `docs/research/2026-05-31-winning-wallet-feed-research.md`

## 1. Goal

Add the missing feed layer under the winning-wallet thesis extractor so currently blocked methods can become testable:

- stale liquidity;
- CLV / post-entry drift;
- first-attention discovery;
- follower flow;
- market-making spread capture;
- adverse-selection-aware making;
- negRisk basket accounting;
- cross-venue and sports-reference theses;
- public-news latency.

This is still analyzer infrastructure. It does not place trades, recommend orders, size positions, or mutate wallet verdicts.

## 2. Design Choice

Use a layered feed architecture:

```
raw source adapter
  -> append-only raw event store
  -> normalized feed tables/files
  -> analyzer provider interfaces
  -> thesis method detectors
```

The analyzer must never call raw APIs inside detector logic. Detectors receive provider interfaces:

- `PricePathProvider`;
- `FillIdentityProvider`;
- `OrderBookStateProvider`;
- `ExternalReferencePriceProvider`;
- `NewsEventProvider`.

If a provider is unavailable or incomplete, detectors return `blocked_missing_feed` with the exact feed gap.

## 3. Feed Tiers

### Tier 1 - Native Polymarket State

Build first.

1. **CLOB WebSocket archive**
   - raw `book`, `price_change`, `last_trade_price`, `best_bid_ask`, `new_market`, `market_resolved`;
   - append-only JSONL/parquet;
   - derived top-of-book, spread, depth, mid, and price path.

2. **Exchange fill identity**
   - reuse `sniper.analysis.wallet_protocol.subgraph_reader`;
   - normalize `OrderFilledEvent` maker/taker fills;
   - include Neg Risk Exchange fills and split/merge/redemption rails where available.

3. **Gamma active universe**
   - active event/market/token mapping;
   - tags, series, sports metadata;
   - negRisk flag and event grouping.

Tier 1 unlocks the largest number of blocked methods and should be implemented before external feeds.

### Tier 2 - External Reference Prices

Build after Tier 1.

1. **Kalshi**
   - market/orderbook snapshots;
   - event equivalence map to Polymarket;
   - cross-venue divergence.

2. **Sports odds**
   - sportsbook implied probabilities;
   - opening/current/closing odds where available;
   - sports CLV and stale-line detection.

### Tier 3 - Public Information Timing

Build after Tier 2 unless a specific wallet cluster demands it.

1. **GDELT / NewsAPI / RSS**
   - normalized news/event timestamp;
   - entity/topic tags;
   - source domain and latency metadata.

These feeds classify public-news latency and `info_sensitive` runnability. They should not claim hidden information source.

### Tier 4 - Third-Party Archives

Optional.

Use PMXT/PAPI/EventGraph/Cornice/academic archives only as:

- historical backfill;
- event-equivalence proposal;
- independent cross-check.

Native feed data wins on conflicts.

## 4. Core Data Objects

### 4.1 `FeedEvent`

Normalized envelope for all raw feed writes:

```python
@dataclass(frozen=True)
class FeedEvent:
    source: str
    source_event_type: str
    observed_ts_ms: int
    event_ts_ms: int | None
    market_id: str | None
    condition_id: str | None
    asset_id: str | None
    event_slug: str | None
    payload: dict
    payload_hash: str
```

Rules:

- `observed_ts_ms` is our capture time.
- `event_ts_ms` is source-provided time, if present.
- `payload_hash` makes appends idempotent.
- Raw payload is kept for audit.

### 4.2 `OrderBookPoint`

Derived from CLOB messages:

```python
@dataclass(frozen=True)
class OrderBookPoint:
    asset_id: str
    ts_ms: int
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    bid_depth_1: float
    ask_depth_1: float
    bid_depth_5: float
    ask_depth_5: float
    source: str
```

This powers `PricePathProvider` and spread/depth methods.

### 4.3 `FillEvent`

Derived from exchange/subgraph rows:

```python
@dataclass(frozen=True)
class FillEvent:
    tx_hash: str
    log_id: str
    ts: int
    maker: str
    taker: str
    maker_asset_id: str
    taker_asset_id: str
    maker_amount: float
    taker_amount: float
    condition_id: str | None
    event_slug: str | None
    price: float | None
    source: str
```

This powers `FillIdentityProvider`, first-attention, follower flow, and microstructure methods.

### 4.4 `ExternalReferencePoint`

For Kalshi/sports/news-derived references:

```python
@dataclass(frozen=True)
class ExternalReferencePoint:
    source: str
    reference_type: str
    ts: int
    event_key: str
    side: str
    probability: float | None
    raw_value: float | str | None
    confidence: str
    payload: dict
```

`event_key` must come from an explicit equivalence map, not fuzzy matching at detector time.

## 5. Storage

Use append-only local files first:

```
runs/feed_archive/raw/<source>/<YYYY-MM-DD>.jsonl
runs/feed_archive/derived/orderbook_points/<YYYY-MM-DD>.parquet
runs/feed_archive/derived/fills/<YYYY-MM-DD>.parquet
runs/feed_archive/derived/external_refs/<source>/<YYYY-MM-DD>.parquet
```

Reason:

- avoids production DB mutation during build;
- preserves raw audit trail;
- can be promoted to Supabase/Postgres later if volume justifies it.

Every writer must be idempotent by `(source, payload_hash)` or source-native id.

## 6. Provider Interfaces

### 6.1 `BookPricePathProvider`

Implements existing `PricePathProvider`:

```python
path(condition_id: str, *, start_ts: int, end_ts: int) -> list[tuple[int, float]] | None
```

Inputs:

- condition-to-asset mapping from Gamma;
- orderbook point archive.

Returns:

- mid-price path for the chosen side;
- `None` if feed unavailable;
- `[]` if feed exists but no observations in window.

### 6.2 `OnchainFillIdentityProvider`

Implements existing `FillIdentityProvider`.

Required methods:

```python
available: bool
fills_for_market(condition_id: str, *, start_ts: int, end_ts: int) -> list[FillEvent]
fills_for_wallet(wallet: str, *, start_ts: int, end_ts: int) -> list[FillEvent]
```

Used by:

- first-attention discovery;
- follower flow;
- market-maker diversity;
- maker/taker classification.

### 6.3 `ExternalReferencePriceProvider`

```python
reference_path(event_slug: str, side: str, *, start_ts: int, end_ts: int) -> list[ExternalReferencePoint] | None
```

Used by:

- Kalshi divergence;
- sportsbook stale line;
- sports CLV.

### 6.4 `NewsEventProvider`

```python
events_for_cluster(cluster_scope: dict, *, start_ts: int, end_ts: int) -> list[ExternalReferencePoint] | None
```

Used by:

- public-news latency;
- info-sensitive runnability.

## 7. Capture Jobs

### 7.1 CLOB WebSocket Capture

Input universe:

- active markets from Gamma;
- token ids for both sides;
- optional focus list from active-winner clusters.

Behavior:

- subscribe in bounded batches;
- reconnect with backoff;
- send heartbeat/ping;
- append every raw message;
- emit capture health: connected assets, messages/min, reconnects, stale subscriptions.

Do not derive book state inside the WebSocket loop beyond minimal parsing. Raw capture must stay simple.

### 7.2 Orderbook Deriver

Reads raw WebSocket messages and writes `OrderBookPoint`.

Rules:

- `book` resets local book state;
- `price_change` mutates price levels;
- `best_bid_ask` can be used as a fast top-of-book source, but full book state wins when available;
- invalid or out-of-order messages are logged and skipped, not guessed.

### 7.3 Fill Indexer

Use existing Goldsky/subgraph code first.

Behavior:

- page by id cursor;
- checkpoint per wallet or per time window;
- normalize maker/taker fills into `FillEvent`;
- dedupe by source id.

### 7.4 Gamma Universe Refresher

Behavior:

- paginate active events/markets;
- map condition id -> token ids -> event slug -> tags/series;
- refresh frequently enough for active markets;
- write unknown/failure states separately from untagged.

## 8. Detector Upgrades

### 8.1 Stale Liquidity

Upgrade from blocked/proxy to supported proxy when `BookPricePathProvider` is available.

Evidence:

- entry price vs immediate post-entry mid;
- spread at entry;
- depth at entry;
- reprice within 5m/1h;
- dead-hour spread/depth context.

Guard:

- no proof from sparse path;
- require minimum observed book points;
- report source latency.

### 8.2 First Attention / Follower Flow

Upgrade when fill identity exists.

Evidence:

- wallet is among first N independent wallets in an event/condition;
- later independent wallets enter same side;
- exclude self and same tx;
- exclude wallet's own volume from follower volume.

Guard:

- event-level unit;
- no promotion from one event;
- follower threshold fixed before run.

### 8.3 Market Making / Adverse Selection

Upgrade when fill identity plus book path exists.

Evidence:

- maker/taker role frequency;
- spread captured at fill;
- post-fill drift against/with maker;
- cancellation/repost proxy if book order hashes are present.

Guard:

- public WebSocket trade direction alone is not enough;
- join to exchange fills where possible.

### 8.4 Cross-Venue / Sports Reference

Upgrade when reference provider exists.

Evidence:

- Polymarket price lag after Kalshi/sportsbook move;
- wallet entry during divergence;
- later convergence/resolution.

Guard:

- explicit event equivalence map required;
- account for fees, spread, and timing.

### 8.5 News Latency

Upgrade to proxy only.

Evidence:

- wallet entry before/after first public event timestamp;
- cluster-level repeated timing pattern.

Guard:

- never claim hidden source;
- classify as `info_sensitive` when runnable only with external event feed/speed.

## 9. Failure Modes

- **Silent feed absence:** forbidden. Provider unavailable must surface as `blocked_missing_feed`.
- **Raw feed overwrite:** forbidden. Raw feed is append-only.
- **Historical/orderbook hindsight:** detectors must use only observations available at entry time.
- **Fuzzy event matching in detector:** forbidden. Event equivalence belongs in an upstream mapping layer.
- **Third-party truth drift:** third-party feeds are provenance-tagged and lower priority than native/on-chain sources.
- **WebSocket disconnections:** capture health must report gaps; detectors must read coverage.

## 10. Build Order

1. Feed contract + raw append store.
2. CLOB WebSocket capture and orderbook deriver.
3. Goldsky/on-chain fill normalizer.
4. Providers into winning-wallet thesis runner.
5. Upgrade stale-liquidity, first-attention, and microstructure detectors.
6. Gamma active-universe refresher hardening.
7. Kalshi/sports external reference providers.
8. News provider.
9. Optional third-party historical archive importers.

## 11. Acceptance Criteria

- Cached profile output shows fewer `blocked_missing_feed` findings only when the corresponding provider is available.
- When a feed is absent, the same blocked findings remain visible.
- No live trading endpoints are called.
- No production DB writes by default.
- Tests cover provider unavailable, provider empty window, and provider populated window separately.
- Feed capture can run in dry-run/sample mode without touching analyzer verdicts.

## 12. Sources

- Polymarket API overview: https://docs.polymarket.com/api-reference/introduction
- Polymarket CLOB trading overview: https://docs.polymarket.com/trading/overview
- Polymarket market WebSocket: https://docs.polymarket.com/market-data/websocket/market-channel
- Polymarket WebSocket overview: https://docs.polymarket.com/market-data/websocket/overview
- Polymarket contracts: https://docs.polymarket.com/resources/contracts
- Polymarket market/tag fetching: https://docs.polymarket.com/market-data/fetching-markets
- Kalshi orderbooks: https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks
- The Odds API: https://api.theoddsapi.com/docs
- GDELT Cloud: https://docs.gdeltcloud.com/
- NewsAPI: https://newsapi.org/docs/endpoints
- Polymarket order-book microstructure paper: https://arxiv.org/abs/2604.24366
- Polymarket lifecycle dataset paper: https://arxiv.org/abs/2604.20421
