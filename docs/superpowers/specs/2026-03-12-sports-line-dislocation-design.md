# Sports Line Dislocation Strategy — Design Spec

## Goal

Build a narrow, objective sports pricing bot that compares **bookmaker implied win probabilities** against **Polymarket binary game-winner prices** and enters paper trades when Polymarket is materially mispriced.

This is **not** true arbitrage. It is a pregame **line dislocation** strategy:

- sportsbook consensus provides the external anchor
- Polymarket provides the tradeable market
- the strategy buys underpriced probability when the gap is large enough

The design is intentionally narrow:

- only explicit **two-outcome head-to-head winner** markets
- no spreads
- no totals
- no props
- no draw markets
- no fuzzy title mapping
- no LLM dependency
- no real trading in v1

## Architecture

This strategy fits the multi-strategy / multi-VPS architecture already specified in:

- [`docs/superpowers/specs/2026-03-11-multi-strategy-multi-vps-architecture.md`](/home/bigpapi/Desktop/Sniper/docs/superpowers/specs/2026-03-11-multi-strategy-multi-vps-architecture.md)

**Data flow:**

```text
The Odds API ──→ feeds/odds.py ──→ odds_snapshots table
                                      │
                                      ▼
                         context/sports_context.py
                                      │
                                      ▼
                    sports_line_dislocation_v1 strategy
                                      │
                                      ▼
                         strategy_positions (paper-only)
```

**Key principle:** shared context helpers stay neutral and read-only; edge logic lives inside the strategy.

## 1. Odds Feed Adapter

### `feeds/odds.py` — `OddsFeedAdapter`

| Attribute | Value |
|-----------|-------|
| `feed_id` | `"odds"` |
| `display_name` | `"Sports Odds"` |
| `default_poll_seconds` | `300` |
| `mode` | `"poll"` |

### Provider

v1 provider is **The Odds API** behind a thin provider wrapper.

Important operational constraint:

- useful live polling generally requires a **paid plan**
- historical odds are also a paid feature

So v1 is designed to:

- run live with a paid odds key, or
- remain feed-only / research-only until a sufficient plan exists

### Scope

The feed only stores:

- whitelisted sports
- pregame **`h2h`** (moneyline / winner) markets
- bookmaker snapshots relevant to binary winner mapping

Default sport scope should stay narrow in v1:

- NBA
- MLB
- NFL
- NHL
- NCAAB

Each sport remains configurable through env vars.

### Behavior

- `validate_config()`
  - raises if `ODDS_API_KEY` missing when `odds` feed is enabled
  - validates configured sport keys and bookmaker list
- `warmup(conn)`
  - ensures required odds tables exist
- `tick(conn)`
  - pulls latest odds snapshots from The Odds API for configured sports
  - normalizes bookmaker h2h markets only
  - writes one normalized snapshot row per `(event, bookmaker, market, snapshot_ts)`

### Storage: `odds_snapshots`

This is a normalized snapshot table, not a final “truth” table.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | auto |
| `instance_tag` | TEXT | from env |
| `feed_id` | TEXT | `"odds"` |
| `provider` | TEXT | `"the_odds_api"` |
| `provider_event_id` | TEXT | provider event id |
| `sport_key` | TEXT | e.g. `basketball_nba` |
| `sport_title` | TEXT | provider sport title |
| `commence_time` | TEXT | ISO timestamp |
| `home_team` | TEXT | provider name |
| `away_team` | TEXT | provider name |
| `bookmaker_key` | TEXT | provider bookmaker id |
| `bookmaker_title` | TEXT | display name |
| `market_key` | TEXT | `h2h` only in v1 |
| `outcomes_json` | TEXT | normalized outcome list |
| `canonical_event_hash` | TEXT | normalized home/away/start hash |
| `payload_json` | TEXT | raw provider payload slice |
| `market_ts` | TEXT | provider snapshot timestamp |
| `captured_at` | TEXT | our clock |

### Outcome normalization

`outcomes_json` should contain a normalized list like:

```json
[
  {
    "name": "Boston Celtics",
    "price": -145,
    "price_decimal": 1.69,
    "implied_prob_raw": 0.592
  },
  {
    "name": "Milwaukee Bucks",
    "price": 125,
    "price_decimal": 2.25,
    "implied_prob_raw": 0.444
  }
]
```

The feed stores raw implied probabilities; **no-vig fair probabilities are computed later** by strategy logic.

### Dedup / uniqueness

Unique constraint:

- `(provider, provider_event_id, bookmaker_key, market_key, market_ts)`

Indexes:

- `(sport_key, commence_time DESC)`
- `(canonical_event_hash, market_ts DESC)`
- `(captured_at DESC)`

### Config env vars

- `ODDS_API_KEY`
- `ODDS_SPORT_KEYS`
  - default: `basketball_nba,baseball_mlb,americanfootball_nfl,icehockey_nhl,basketball_ncaab`
- `ODDS_BOOKMAKERS`
  - configurable whitelist
- `ODDS_MARKETS`
  - fixed to `h2h` in v1
- `ODDS_POLL_SECONDS`
  - optional override for feed cadence

## 2. Sports Context Query Layer

### `context/sports_context.py`

Read-only helper layer over `odds_snapshots` plus existing Polymarket tables.

All raw SQL still lives in `db.py`. `context/sports_context.py` is a thin composition layer only.

### Public helpers

#### `recent_odds_events(conn, sport_keys, hours_ahead=72) -> List[dict]`

Returns the latest bookmaker snapshots for upcoming events in the configured sports window.

Used to seed matching and strategy discovery.

#### `odds_event_history(conn, canonical_event_hash, hours=24) -> List[dict]`

Returns historical odds snapshots for one canonical event ordered by `market_ts`.

Used for:

- consensus stability checks
- later replay support

#### `find_matching_polymarket_h2h_markets(conn, home_team, away_team, commence_time, tolerance_minutes=180) -> List[dict]`

Returns candidate Polymarket markets matching:

- same two teams
- same sport family
- commence time within tolerance
- binary winner semantics
- open market

Only explicit match candidates are allowed.

This helper must **skip**:

- “team total”
- “over/under”
- “series winner”
- “spread”
- markets with ambiguous title parsing

#### `polymarket_market_outcomes(conn, condition_id) -> List[dict]`

Returns the canonical outcome labels for a Polymarket market.

Used so the strategy can map:

- sportsbook home/away team
- to a specific Polymarket outcome string

#### `insider_convergence_check(conn, condition_id, min_insider_score=55) -> dict`

Same pattern as other strategies:

- annotation only
- never an entry gate in v1

## 3. Strategy: `sports_line_dislocation_v1`

### Naming

Use `sports_line_dislocation_v1`, not `sports_arb_v1`.

The strategy is exploiting dislocation against an external line, not locking riskless arbitrage.

### Plugin metadata

| Attribute | Value |
|-----------|-------|
| `plugin_id` | `"sports_line_dislocation_v1"` |
| `strategy_family` | `"sports_line_dislocation"` |
| `required_feeds` | `("polymarket", "odds")` |
| `supported_execution_modes` | `("paper",)` |
| `default_loop_seconds` | `300` |

### Thesis

When bookmaker consensus implies a materially higher probability for Team A than Polymarket’s current Team A price, Polymarket is underpricing Team A.

The bot buys that underpriced side only when:

- the event match is explicit
- bookmaker consensus is broad enough
- bookmaker disagreement is low enough
- the edge exceeds a hard threshold
- Polymarket price is still low enough

### Discover

`discover(conn, strategy) -> List[StrategySignal]`

For each recent odds event:

1. load latest bookmaker h2h snapshots for the event
2. require at least `min_books` snapshots
3. convert bookmaker prices to implied probabilities
4. remove vig per bookmaker:
   - binary market only
   - `fair_prob_i = raw_prob_i / (raw_prob_home + raw_prob_away)`
5. aggregate fair probability across books:
   - default: median fair probability per side
6. measure consensus dispersion:
   - max-minus-min fair probability across books
7. find matching Polymarket binary winner markets
8. fetch live Polymarket prices for both outcomes using shared live-price helpers
9. compute dislocation:
   - `edge_pts = fair_prob - polymarket_price`
10. emit a signal only if:
   - event match is unambiguous
   - consensus dispersion <= threshold
   - edge_pts >= threshold
   - market has sufficient liquidity / volume

Signal metadata should include:

```json
{
  "canonical_event_hash": "...",
  "sport_key": "basketball_nba",
  "home_team": "Boston Celtics",
  "away_team": "Milwaukee Bucks",
  "commence_time": "2026-03-20T23:30:00Z",
  "selected_team": "Boston Celtics",
  "selected_side": "yes",
  "polymarket_price": 0.43,
  "fair_prob": 0.52,
  "edge_pts": 0.09,
  "books_used": 4,
  "consensus_dispersion": 0.025,
  "insider_convergence": {...}
}
```

### Entry gates

`enter(conn, strategy, signal, open_positions) -> StrategyEntryDecision`

Gates in order:

1. duplicate open position on same `condition_id + outcome`
2. max concurrent positions
3. max exposure per market
4. max total instance exposure
5. max entry price
6. event start buffer:
   - no new entries within `min_minutes_before_start`
7. minimum edge threshold still holds at entry
8. minimum bookmaker count still holds
9. bookmaker consensus dispersion still acceptable

### Sizing

v1 sizing is flat and simple:

- `bet_size_usdc` default: `$10`
- clamped by shared risk policy

No Kelly sizing in v1.

### Direction logic

Deterministic and explicit:

- if Team A fair probability exceeds Team A Polymarket price by threshold, buy Team A
- if Team B fair probability exceeds Team B Polymarket price by threshold, buy Team B
- if both sides fail threshold, skip

No “NO” side inversion logic in v1 unless required by the market/outcome labeling. The strategy should prefer matching the explicit team outcome directly.

## 4. Manage / Exit

### Manage

`manage()` should use the shared Polymarket live-price helper, not stale metadata.

It should collect:

- current Polymarket price
- scheduled event start time
- whether the market is still open
- metadata about original fair probability / edge

### Exit

Exit reasons in v1:

1. `market_resolved`
   - only when actual market closure / resolution evidence exists
2. `market_closed`
   - market no longer open, but no resolution side inferred
3. `convergence_take_profit`
   - Polymarket price converges to within `close_gap_pts` of sportsbook fair probability and trade is in profit
4. `stale_postponed`
   - event start passed by more than `postponed_grace_hours` and market remains open

Not in v1:

- stop-loss from bookmaker reversal
- intragame pricing logic
- dynamic hedge logic

### Important semantic boundary

Do **not** call pre-resolution closes `resolution_win` or `resolution_loss`.

If the bot takes profit because the dislocation closes, that is an early close, not resolution.

## 5. Configurable Parameters

Suggested strategy parameters:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `bet_size_usdc` | `10.0` | flat paper size |
| `max_concurrent_positions` | `5` | strategy-local cap |
| `max_exposure_per_market_usdc` | `50.0` | strategy-local cap |
| `max_entry_price` | `0.75` | avoid expensive contracts |
| `min_edge_pts` | `0.06` | 6 percentage points |
| `min_books` | `3` | minimum bookmaker snapshots |
| `max_consensus_dispersion_pts` | `0.04` | skip if books disagree too much |
| `min_minutes_before_start` | `30` | no late entries |
| `hours_ahead` | `72` | event search window |
| `close_gap_pts` | `0.02` | consider convergence captured within 2 pts |
| `postponed_grace_hours` | `24` | stale handling after scheduled start |
| `sport_keys` | see feed defaults | configurable sport list |

## 6. Replay Support

### Honest v1 limitation

`sports_line_dislocation_v1` should **not** pretend to support historical replay immediately.

Why:

- useful replay requires historical odds snapshots
- The Odds API historical odds are paid
- local `odds_snapshots` history starts only after feed deployment

So v1 should do one of two things:

1. `replay_adapter()` returns `None`
2. or replay is explicitly disabled until:
   - enough local odds history exists, or
   - a historical backfill/import has been performed

This should be called out clearly in UI/API/MCP:

- strategy can paper trade
- strategy cannot yet be replayed honestly

## 7. Testing

### Unit tests

- `tests/test_odds_feed.py`
  - API parsing
  - h2h-only filtering
  - canonical event hashing
  - dedup uniqueness
  - per-bookmaker snapshot normalization
- `tests/test_sports_context.py`
  - explicit event matching only
  - ambiguous title skip
  - binary outcome matching
  - odds history retrieval
- `tests/test_sports_line_dislocation.py`
  - no-vig normalization
  - median consensus calculation
  - dispersion filter
  - edge threshold
  - duplicate/max-price/max-concurrent gates
  - convergence take-profit
  - postponed stale handling

### Integration tests

- feed registry includes `odds`
- runner enforces `required_feeds = ("polymarket", "odds")`
- shared risk policy clamps or rejects size correctly
- separate instance profile loads cleanly

## 8. Rollout

### Mandatory isolation

This bot must run in a **separate DB and separate instance profile** from insider-copy.

No exceptions.

Example profile:

```env
INSTANCE_TAG=sports-line-paper
DB_PATH=/home/sniper/sniper_sports.db
ENABLED_STRATEGIES=sports_line_dislocation_v1
ENABLED_FEEDS=polymarket,odds
PAPER_ONLY=1
REAL_TRADING_ENABLED=0
INSTANCE_BANKROLL_USDC=500
ODDS_API_KEY=...
```

### Phase 1 — feed-only accumulation

Deploy the odds feed first and accumulate snapshots.

Do **not** activate the strategy immediately.

Use:

- DB queries
- MCP
- API inspection

to verify:

- event matching quality
- bookmaker coverage
- dislocation frequency
- data freshness

### Phase 2 — paper strategy activation

Once matching quality looks sane:

- create a paused strategy config
- activate paper-only
- use small flat size

### Phase 3 — evaluation

After `2-4` weeks:

- inspect signal count
- inspect edge persistence
- inspect paper P&L and skip reasons
- decide whether:
  - thresholds should be tuned
  - replay support is now feasible
  - a sharper bookmaker whitelist is needed

## 9. What Is Not In V1

- no real trading
- no spreads
- no totals
- no props
- no draw / 3-way markets
- no generic “sports arb” language
- no market-making logic
- no cross-venue execution
- no Kalshi integration
- no Kelly sizing
- no replay unless historical odds are honest and available

## 10. Recommendation

This is the best money-first next bot because it is:

- externally anchored
- high-frequency relative to copy-trading drought conditions
- easy to explain
- easy to audit
- naturally separate from the insider-copy proof bar

But it only stays honest if the scope remains tight:

- binary head-to-head winner markets only
- explicit team matching only
- scanner/feed validation first
- separate DB/runtime always

## Primary References

- The Odds API: https://the-odds-api.com/
- Historical odds notes: https://the-odds-api.com/historical-odds-data/
- Polymarket rate limits: https://docs.polymarket.com/api-reference/rate-limits
- Polymarket CLOB docs: https://docs.polymarket.com/developers/CLOB/quickstart
