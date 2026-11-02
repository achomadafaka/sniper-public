# Multi-Strategy / Multi-VPS Architecture — Design Spec

## Goal

Evolve Sniper from a single-thesis insider-copy system into a **single codebase** that can:

1. run multiple strategy families over time,
2. deploy different strategy profiles on different VPS instances without forking the repo,
3. preserve strict attribution of trades, replay runs, alerts, and risk to the **instance** and **strategy** that produced them,
4. keep the existing insider-copy proof-bar path intact and isolated from experimental bots.

This is **not** a “build a four-bot fleet now” spec. It is the architecture needed to add one experimental bot at a time without turning the app into an unstructured prototype.

## Current Constraints

Grounded in the current codebase:

- Strategy plugins already exist in [`strategies/`](/home/bigpapi/Desktop/Sniper/strategies) and implement:
  - `discover`
  - `enter`
  - `manage`
  - `exit`
- Strategy execution is orchestrated by [`strategy_runner.py`](/home/bigpapi/Desktop/Sniper/strategy_runner.py)
- Plugin registry lives in [`strategies/registry.py`](/home/bigpapi/Desktop/Sniper/strategies/registry.py)
- Shared paper defaults live in [`paper_settings.py`](/home/bigpapi/Desktop/Sniper/paper_settings.py)
- Replay v1 exists, but is currently centered on curated `signal_history`, which is suitable for insider-copy style signal replay, not yet for external-feed strategies

The architecture therefore needs to **extend** the current strategy system, not replace it.

## Design Principles

1. **One codebase, many deployment profiles**
   The app must support multiple VPS instances with different enabled strategies and feeds via config, not code forks.

2. **Shared runtime, isolated attribution**
   All trades, alerts, runs, and replay artifacts must be attributable to an `instance_tag`, `strategy_type`, and `strategy_id`.

3. **Risk controls live outside plugins**
   Plugins may propose entries/exits, but global bankroll, exposure, and kill-switch rules are enforced in shared runtime code.

4. **Feeds are adapters**
   External data integrations (weather, odds, Binance, etc.) must conform to a common feed contract and write normalized snapshots/caches, not inject custom logic directly into plugins.

5. **Replay and live use the same strategy contract**
   Strategy families must expose a replay adapter so new bots can be evaluated before paper deployment.

6. **Experimental bots must not pollute insider-copy proof metrics**
   The current proof bar remains specific to insider-copy paper trading. New strategy families get their own strategy ledger and evaluation surfaces.

## What This Enables

- Run insider-copy on one VPS and weather or sports-odds strategy on another VPS
- Run the same strategy family in multiple environments:
  - `insider-paper-main`
  - `weather-paper-us`
  - `sports-odds-paper-eu`
- Keep separate DBs per VPS for now
- Use the same UI/API/MCP surfaces to inspect strategy state across families

## 1. Strategy Plugin Contract

### Current Contract

Today a plugin subclass of `StrategyPlugin` must implement:

- `discover(conn, strategy) -> list[StrategySignal]`
- `enter(conn, strategy, signal, open_positions) -> StrategyEntryDecision`
- `manage(conn, strategy, position) -> StrategyManageState`
- `exit(conn, strategy, position, state) -> StrategyExitDecision`
- optional `report(...)`

### Required Additions

Add the following attributes and methods to [`strategies/base.py`](/home/bigpapi/Desktop/Sniper/strategies/base.py):

```python
class StrategyPlugin(ABC):
    plugin_id: str = ""
    display_name: str = ""
    description: str = ""
    strategy_family: str = "directional"
    supported_execution_modes: tuple[str, ...] = ("paper",)
    required_feeds: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    default_loop_seconds: int = 60

    def validate_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """Normalize and validate strategy parameters.

        Raise ValueError on invalid config.
        Return normalized parameter dict.
        """
        return parameters

    def replay_adapter(self) -> "StrategyReplayAdapter":
        """Return the replay adapter for this strategy family."""
        raise NotImplementedError
```

### New Replay Contract

Add a replay adapter abstraction alongside plugins:

```python
@dataclass(slots=True)
class ReplayCandidate:
    event_ts: int
    condition_id: str
    outcome: str
    entry_price: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReplayLifecycleState:
    resolved_at: int | None = None
    resolution_price: float | None = None
    exit_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class StrategyReplayAdapter(ABC):
    strategy_type: str = ""

    @abstractmethod
    def candidate_stream(self, conn, config: dict[str, Any], *, start_ts: int | None = None, end_ts: int | None = None) -> list[ReplayCandidate]:
        """Return the normalized replay candidates for this strategy."""

    @abstractmethod
    def passes_entry(self, config: dict[str, Any], candidate: ReplayCandidate, open_positions: list[dict[str, Any]]) -> tuple[bool, str]:
        """Return whether the candidate should be entered and the skip reason if not."""

    @abstractmethod
    def position_size(self, config: dict[str, Any], candidate: ReplayCandidate, bankroll_state: dict[str, Any]) -> float:
        """Return the desired notional size in USDC before shared risk clamps."""

    @abstractmethod
    def terminal_state(self, conn, candidate: ReplayCandidate) -> ReplayLifecycleState:
        """Return the terminal lifecycle event for this candidate."""
```

### Plugin Metadata Rules

Every plugin must declare:

- `required_feeds`
  - example: `("polymarket",)` for insider copy
  - `("polymarket", "weather")` for weather strategy
  - `("polymarket", "odds")` for sports arb
- `default_loop_seconds`
  - `60` for slower strategies
  - `10-15` for fast BTC-style strategies later
- `supported_execution_modes`
  - default `("paper",)`
  - real execution added only after validation

### Why This Matters

This lets the runner decide:

- whether the plugin can run on the current instance
- which feeds must be initialized
- which replay path to use
- what loop cadence is appropriate

without hardcoding strategy-specific logic into [`strategy_runner.py`](/home/bigpapi/Desktop/Sniper/strategy_runner.py).

## 2. Feeds Adapter Contract

External data clients should live in a new top-level package:

```text
feeds/
  __init__.py
  base.py
  registry.py
  polymarket.py
  weather.py
  odds.py
  binance.py
```

### Base Contract

```python
class FeedAdapter(ABC):
    feed_id: str = ""
    display_name: str = ""
    default_poll_seconds: int = 60
    mode: str = "poll"  # "poll", "stream", or "hybrid"

    def validate_config(self, env: dict[str, str]) -> None:
        """Raise ValueError if the feed cannot run with current env."""

    @abstractmethod
    def warmup(self, conn) -> None:
        """Ensure any required cache tables/state exist and optionally prime them."""

    @abstractmethod
    def tick(self, conn, now_ts: int) -> dict[str, Any]:
        """Perform one poll/update cycle. Return a small summary dict."""

    def start(self, conn) -> None:
        """Optional for stream feeds. No-op for polling feeds."""

    def stop(self) -> None:
        """Optional for stream feeds. No-op for polling feeds."""
```

### Feed Responsibilities

Feeds must:

- fetch external data
- normalize it into **cache tables** or **snapshot tables**
- expose freshness timestamps
- avoid strategy-specific business logic

Feeds must **not**:

- place trades
- decide entries/exits
- directly mutate strategy positions

### Error Handling

Each feed tick should:

- catch provider/network exceptions,
- log them with `feed_id` and `instance_tag`,
- write last-success / last-error status for observability,
- never crash unrelated feeds or the strategy runner.

### Feed Caching Rules

Each feed gets its own normalized table(s). Example:

- `weather_snapshots`
- `odds_snapshots`
- `price_candles`
- `feed_health`

For v1, feeds should write to SQLite on the local instance. Cross-instance data sharing is out of scope.

## 3. Shared Risk Control Layer

### Problem

Today several gates and caps live inline inside plugins. That is manageable for two plugins, but it will drift quickly once weather/odds/BTC strategies arrive.

### Required New Module

Add:

```text
risk/
  __init__.py
  policy.py
  models.py
```

### Core Types

```python
@dataclass(slots=True)
class RiskContext:
    instance_tag: str
    strategy_id: int
    strategy_type: str
    execution_mode: str
    bankroll_usdc: float
    open_positions: list[dict[str, Any]]
    open_market_exposure: dict[str, float]
    total_open_exposure: float
    realized_pnl_today: float
    consecutive_losses: int


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    clamped_size_usdc: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
```

### Shared Checks

Shared runtime risk policy should enforce:

- `BANKROLL_MIN_USDC`
- `MAX_TOTAL_OPEN_EXPOSURE_USDC`
- `MAX_MARKET_EXPOSURE_USDC`
- `MAX_CONCURRENT_POSITIONS`
- `MAX_POSITION_SIZE_USDC`
- `DAILY_LOSS_LIMIT_USDC`
- optional `MAX_CONSECUTIVE_LOSSES`
- `PAPER_ONLY` / `REAL_TRADING_ENABLED`
- `INSTANCE_ENABLED`

These must live in shared policy code and be applied **after** plugin sizing but **before** trade creation/execution.

### Plugin Interaction

Plugin flow becomes:

1. plugin `discover`
2. plugin `enter` proposes size/reason
3. shared risk policy clamps or rejects
4. runner records entry or skip

Plugins can still keep strategy-specific thesis checks, but global capital/risk controls must be centralized.

## 4. Instance Profile Model

Each VPS runs the same app with a different `.env`.

### Required Env Vars

```text
# Identity
INSTANCE_TAG=insider-main-paper
INSTANCE_DISPLAY_NAME=Insider Main Paper
INSTANCE_ROLE=paper
INSTANCE_ENABLED=1

# Strategy/Feed enablement
ENABLED_STRATEGIES=insider_copy_v1
ENABLED_FEEDS=polymarket

# Execution
PAPER_ONLY=1
REAL_TRADING_ENABLED=0
ALLOW_STRATEGY_EXECUTION=1
ALLOW_FEED_UPDATES=1

# Loop timing
STRATEGY_LOOP_SECONDS=60
FAST_LOOP_STRATEGIES=

# Shared risk defaults
INSTANCE_BANKROLL_USDC=1000
INSTANCE_MAX_TOTAL_EXPOSURE_USDC=1000
INSTANCE_MAX_MARKET_EXPOSURE_USDC=200
INSTANCE_MAX_CONCURRENT_POSITIONS=20
INSTANCE_DAILY_LOSS_LIMIT_USDC=50

# Bot-specific opt-in flags
ENABLE_WEATHER_BOT=0
ENABLE_ODDS_BOT=0
ENABLE_BINANCE_BOT=0

# External feed credentials
ODDS_API_KEY=
OPENWEATHERMAP_API_KEY=
BINANCE_WS_ENABLED=0
```

### Example Profiles

**Current insider-copy VPS**
```text
INSTANCE_TAG=insider-main-paper
ENABLED_STRATEGIES=insider_copy_v1,fade_v1
ENABLED_FEEDS=polymarket
PAPER_ONLY=1
REAL_TRADING_ENABLED=0
```

**Future weather VPS**
```text
INSTANCE_TAG=weather-paper-us
ENABLED_STRATEGIES=weather_v1
ENABLED_FEEDS=polymarket,weather
PAPER_ONLY=1
REAL_TRADING_ENABLED=0
INSTANCE_BANKROLL_USDC=500
```

**Future sports odds VPS**
```text
INSTANCE_TAG=sports-odds-paper
ENABLED_STRATEGIES=sports_arb_v1
ENABLED_FEEDS=polymarket,odds
PAPER_ONLY=1
REAL_TRADING_ENABLED=0
INSTANCE_BANKROLL_USDC=1000
```

### Behavior Rules

- If `INSTANCE_ENABLED=0`, runners start but do not execute entries.
- If a strategy is enabled but a required feed is missing, the runner logs a hard warning and skips the strategy.
- `PAPER_ONLY=1` must override any plugin request for real execution.

## 5. Data Tagging

### Goal

Every strategy artifact must be attributable to:

- `instance_tag`
- `strategy_type`
- `strategy_id`

### Immediate Table Additions

Add nullable then backfill/defaulted `instance_tag` to:

- `strategies`
- `strategy_positions`
- `strategy_executions`
- `strategy_runs`
- `replay_configs`
- `replay_runs`
- `mcp_audit_log`

### Feed Tables

New external feed tables should also include:

- `instance_tag`
- `feed_id`
- `captured_at`

Examples:

```sql
weather_snapshots (
  id INTEGER PRIMARY KEY,
  instance_tag TEXT NOT NULL,
  feed_id TEXT NOT NULL,
  location_key TEXT NOT NULL,
  forecast_ts INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  captured_at TEXT NOT NULL
)
```

### Migration Approach

Use the existing canonical schema path in [`db.py`](/home/bigpapi/Desktop/Sniper/db.py), not ad hoc migration scripts.

Migration steps:

1. add nullable `instance_tag`
2. backfill existing rows with `INSTANCE_TAG` if empty on startup
3. make new inserts populate it automatically
4. add indexes where needed:
   - `(instance_tag, created_at DESC)`
   - `(instance_tag, strategy_id, created_at DESC)`

### What Not to Tag Yet

Do **not** retrofit insider-copy proof-bar tables like `paper_trades` unless/until you deliberately decide to run paper trading across multiple VPSs into the same DB. Right now the recommended deployment model is **separate DB per VPS**, so `paper_trades` can stay instance-local.

## 6. File Layout

### New Top-Level Structure

```text
feeds/
  __init__.py
  base.py
  registry.py
  polymarket.py
  weather.py
  odds.py
  binance.py

risk/
  __init__.py
  models.py
  policy.py

strategies/
  base.py
  registry.py
  insider_copy_v1.py
  fade_v1.py
  weather_v1.py
  sports_arb_v1.py
  btc_5min_v1.py

replay/
  __init__.py
  adapters.py
```

### Existing Files to Extend

- [`strategies/base.py`](/home/bigpapi/Desktop/Sniper/strategies/base.py)
  - add feed declarations and replay adapter contract
- [`strategies/registry.py`](/home/bigpapi/Desktop/Sniper/strategies/registry.py)
  - add metadata listing and feed dependency introspection
- [`strategy_runner.py`](/home/bigpapi/Desktop/Sniper/strategy_runner.py)
  - initialize feeds, build risk context, filter strategies by instance profile
- [`web_settings.py`](/home/bigpapi/Desktop/Sniper/web_settings.py)
  - add env-backed instance profile settings
- [`db.py`](/home/bigpapi/Desktop/Sniper/db.py)
  - add instance-tag columns and new feed tables

## 7. Strategy Runner Changes

### Current State

[`strategy_runner.py`](/home/bigpapi/Desktop/Sniper/strategy_runner.py) currently:

- loads DB
- loads strategy plugin from registry
- runs a fixed 60s loop
- manages strategy positions in shared strategy tables

### Required Runner Additions

#### 7a. Instance Profile Load

At startup, load:

- `INSTANCE_TAG`
- `ENABLED_STRATEGIES`
- `ENABLED_FEEDS`
- `PAPER_ONLY`
- risk limits

This should become a small typed config object:

```python
@dataclass(slots=True)
class InstanceProfile:
    instance_tag: str
    display_name: str
    enabled: bool
    enabled_strategies: tuple[str, ...]
    enabled_feeds: tuple[str, ...]
    paper_only: bool
    real_trading_enabled: bool
    bankroll_usdc: float
    max_total_exposure_usdc: float
    max_market_exposure_usdc: float
    max_concurrent_positions: int
    default_loop_seconds: int
```

#### 7b. Feed Initialization

Runner startup:

1. inspect enabled strategies
2. union all `required_feeds`
3. intersect with `ENABLED_FEEDS`
4. initialize feed adapters
5. fail strategy scheduling if a required feed is unavailable

Polling feeds can be ticked each loop. Stream feeds should be started once and monitored.

#### 7c. Loop Cadence

Current fixed loop:

```python
LOOP_INTERVAL_SECONDS = 60
```

Future state:

- default from `InstanceProfile`
- strategy-specific override allowed via `plugin.default_loop_seconds`
- runner executes due strategies only, not every strategy every cycle

This is required for later fast-loop strategies like BTC 5-minute markets without forcing the entire instance into a 10-second loop.

#### 7d. Shared Risk Invocation

Before `_record_plugin_entry(...)`:

1. plugin decides thesis and proposed size
2. shared risk policy evaluates
3. runner records:
   - rejected by plugin
   - rejected by risk
   - entered after clamp

#### 7e. Data Attribution

Every insert to:

- `strategies`
- `strategy_positions`
- `strategy_executions`
- `strategy_runs`

must include `instance_tag`.

## 8. Replay Compatibility

Replay must become strategy-family aware.

### v1 Requirement

Do **not** rewrite the existing replay engine immediately. Instead:

- keep the insider-copy replay path working as-is
- add the `StrategyReplayAdapter` contract now
- require new external-data strategies to provide their own replay candidate source

Examples:

- `insider_copy_v1` replay adapter reads curated `signal_history`
- `weather_v1` replay adapter reads normalized `weather_snapshots` + linked markets
- `sports_arb_v1` replay adapter reads `odds_snapshots` + Polymarket market mapping

### Output Attribution

Saved replay configs/runs must store:

- `instance_tag`
- `strategy_type`
- normalized config JSON

This keeps replay results tied to the environment and strategy family that produced them.

## 9. Deployment Model

### Recommended Near-Term Model

One strategy family per VPS:

- VPS A: insider-copy proof-bar accumulation
- VPS B: one experimental external-data strategy

Why:

- avoids proof-metric contamination
- keeps SQLite write contention manageable
- simplifies rollback
- makes operator reasoning much easier

### Not Recommended Yet

- one giant multi-bot VPS with all feeds and all strategies enabled
- shared proof-bar metrics across strategy families
- multi-host shared SQLite

## 10. First Experimental Bot Recommendation

The architecture should support many strategies, but implementation should start with **one**:

- **`weather_v1`** for lowest integration complexity, or
- **`sports_arb_v1`** if commercial urgency outweighs feed dependency cost

Do **not** build:

- weather
- odds
- BTC
- market making

all at once.

The point of this architecture is to make **one-bot-at-a-time** expansion clean.

## 11. Out of Scope

This spec does **not** include:

- multi-user SaaS auth/billing
- fleet dashboard UI
- automatic cross-VPS orchestration
- arbitrary LLM-authored Python deployment
- order-book-level replay or market-impact simulation

Those are later layers. This spec is only the foundation that prevents the next strategy family from becoming a fork or a pile of special cases.

## 12. Acceptance Criteria

This architecture is implemented correctly when:

1. the same repo can run on two VPSs with different `INSTANCE_TAG`, `ENABLED_STRATEGIES`, and `ENABLED_FEEDS`
2. strategy artifacts are attributable to the correct `instance_tag`
3. plugins declare their feed dependencies and replay adapter
4. shared risk policy gates entries outside plugin code
5. feed adapters can be initialized and monitored by the runner without strategy-specific wiring
6. insider-copy proof-bar behavior remains unchanged on the current VPS
7. one new external-data strategy can be added without modifying unrelated plugin logic

## Recommendation

Adopt this architecture **before** building `weather_v1` or `sports_arb_v1`.

The next deliverable after this spec should be an implementation plan that builds:

1. instance profile config
2. feed adapter base + registry
3. shared risk policy layer
4. strategy plugin contract extension
5. runner integration
6. only then the first experimental bot
