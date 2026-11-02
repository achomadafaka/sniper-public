# Hyperliquid Market Radar Fusion Engine — Design

**Date:** 2026-06-20
**Status:** approved for implementation
**Scope:** live paper trading only. No private keys, no authenticated order endpoints, no live orders.

## Thesis

The current Hyperliquid microstructure lane is too narrow: it starts with a fixed asset list and fires simple single-signal rules. Hyperliquid has a broad perp universe, so the strategy must first decide **which markets deserve attention**, then decide whether any market has a paper-tradeable setup.

The edge hypothesis is temporary cross-signal pressure:

- a market becomes unusually active for itself;
- liquidity is still good enough to enter and exit;
- order flow, book pressure, funding/OI, liquidation stress, whale stress, or news catalyst agree;
- the exit can be tied to the same thesis instead of a fixed arbitrary stop.

BTC/ETH/SOL are allowed, but they are not special. The engine scans the active universe and subscribes only to the best candidates within WebSocket limits.

## Hard Boundaries

- Paper only.
- No private keys.
- No live order endpoint.
- No production DB mutation.
- No expected-PnL or sizing advice to users.
- No edge claim from synthetic or short live data.
- Every interval records a decision, a skip, or a market-selection state.
- Missing/stale critical feeds fail closed.

## Data Sources

Primary Hyperliquid public sources:

- `metaAndAssetCtxs` / `activeAssetCtx`: active universe, volume, OI, funding, premium, mark, oracle.
- `l2Book`, `bbo`: visible depth, spread, book pressure.
- `trades`: aggressive flow and burst detection.
- `allMids` / `candleSnapshot`: broad universe movement and regime context.
- `clearinghouseState`, `userFillsByTime`, existing tracked wallets: whale/leaderboard research inputs.
- `userEvents` / fill liquidation flags when available: liquidation evidence.

Repo sources to reuse:

- `sniper/algos/hl_microstructure/`: live paper harness, visible-book fills, persistence.
- `sniper/persistence/db_hyperliquid.py`: existing liquidation/whale tables.
- `sniper/markets/hyperliquid_api.py`: public wallet and leaderboard reads.
- `sniper/services/news_intel.py`, `sniper/feeds/news.py`, `sniper/markets/source_meta.py`: news fetch, source metadata, timestamp discipline.
- `sniper/algos/btc5m/`: return, z-score, realized-volatility, and trend/reversion feature ideas only.

Do not reuse Polymarket `trade_ideas` as Hyperliquid orders. News is a catalyst feature, not an instruction.

## Architecture

### 1. Market Radar

Build a candidate list from the full active Hyperliquid universe. Reject delisted, zero-volume, missing-mid, too-wide, or shallow markets. Rank surviving assets by:

- 24h notional volume;
- open-interest size;
- funding absolute value;
- premium absolute value;
- recent volatility / movement once observed;
- catalyst score;
- liquidation/whale stress score;
- current feed freshness.

The runner subscribes to a bounded top-N set and can rotate assets at a configurable interval. Existing open paper positions remain managed even if their asset drops out of the top-N.

### 2. Catalyst Radar

Catalyst radar maps news/headlines to Hyperliquid symbols. It records:

- asset;
- headline/source;
- published/received timestamp;
- catalyst type: listing, delisting, exploit, governance, legal/regulatory, macro, ecosystem, meme/social;
- direction when inferable;
- confidence;
- source reliability/diversity.

The first implementation is deterministic keyword/entity matching against existing news feeds plus source metadata. It never opens a trade by itself.

### 3. Liquidation and Whale Stress

Liquidation detector becomes an input lane, not a standalone strategy. It contributes:

- liquidation distance stress from tracked whale positions;
- notional at risk;
- isolated-vs-cross quality flag;
- direction of expected squeeze or unwind;
- data age.

If stale or unsupported, it contributes `0` and a quality flag. It cannot force a trade alone.

### 4. Fusion Scorer

Each asset gets a `RadarScore`. Each paper decision gets a `FusionDecision`.

The scorer combines:

- microstructure pressure;
- flow burst;
- book imbalance;
- volatility state;
- funding/OI stress;
- catalyst score;
- liquidation/whale stress;
- data quality penalties.

Entry requires a minimum total score and at least two agreeing source families. Exit is thesis-based:

- take profit when pressure target is reached or trail activates;
- stop loss when adverse move exceeds volatility-adjusted threshold;
- signal decay when the source families disappear;
- source contradiction when opposite pressure dominates;
- data stop when book/feed quality breaks.

### 5. Paper Execution

Continue taker-only visible-book paper fills. Paper positions store:

- market-selection score at entry;
- entry thesis;
- source-family votes;
- exit plan;
- dynamic stop/target;
- stale-feed and contradiction exits.

### 6. UI

Replace the Hyperliquid microstructure table with a cockpit:

- **Paper status**: live paper yes/no, real money no.
- **Market radar**: ranked assets and why selected/rejected.
- **Active thesis**: plain-English reason for each open paper trade.
- **Source votes**: book, flow, OI/funding, liquidation/whale, news.
- **Exit plan**: current stop/target/decay trigger.
- **Lane health**: stale feeds, missing catalyst, too-wide spread, shallow book.
- **Performance**: paper PnL by source family and asset.

The old liquidation tab can stay hidden or be removed from the primary Hyperliquid nav after the fusion cockpit shows liquidation stress inline.

## Acceptance Criteria

- The runner supports `--auto-select-assets`.
- Fixture mode proves the radar can rotate away from BTC into a better synthetic market.
- At least one paper lane uses fusion scoring instead of single-signal entry.
- News/catalyst and liquidation inputs are optional but visible as source votes.
- UI clearly states paper-only status, selected markets, trade thesis, and exit plan.
- Focused tests cover market selection, catalyst matching, fusion entry/skip, dynamic exits, API payload, and CLI fixture mode.
- No live order or private-key surface exists.
