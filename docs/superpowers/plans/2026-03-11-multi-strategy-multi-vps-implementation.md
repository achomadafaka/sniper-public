# Multi-Strategy / Multi-VPS Architecture — Implementation Plan

> Framework-first. Do not build `weather_v1` until the common infrastructure (instance profile, feeds, risk, plugin contract, runner integration) is in place and insider-copy still behaves identically.

## Goal

Implement the architecture in [`2026-03-11-multi-strategy-multi-vps-architecture.md`](/home/bigpapi/Desktop/Sniper/docs/superpowers/specs/2026-03-11-multi-strategy-multi-vps-architecture.md) so Sniper can run the **same app** on different VPS instances with different enabled strategies/feeds, shared risk controls, and strategy-family-specific replay adapters, while preserving the current insider-copy workflow.

## Non-Goals

- No fleet dashboard in this batch
- No multi-user auth/billing
- No arbitrary LLM-authored strategy code
- No cross-VPS shared DB
- No second experimental bot after `weather_v1`
- No order-book-level replay or fill simulator

## Guardrails

1. `paper_trades` and the proof bar remain insider-copy-only and instance-local.
2. Existing insider-copy paper/replay/API/MCP behavior must remain valid after the framework lands.
3. Each chunk must be independently testable and committable.
4. New strategy-family infrastructure must default to **off** unless explicitly enabled by instance profile.

## File Targets

### New files

- `instance_profile.py`
- `feeds/__init__.py`
- `feeds/base.py`
- `feeds/registry.py`
- `feeds/polymarket.py`
- `risk/__init__.py`
- `risk/models.py`
- `risk/policy.py`
- `replay/adapters.py`
- `strategies/weather_v1.py`
- `tests/test_instance_profile.py`
- `tests/test_feed_registry.py`
- `tests/test_risk_policy.py`
- `tests/test_strategy_plugin_contract.py`
- `tests/test_weather_strategy.py`

### Modified files

- `web_settings.py`
- `db.py`
- `strategy_runner.py`
- `strategies/base.py`
- `strategies/registry.py`
- `strategies/insider_copy_v1.py`
- `strategies/fade_v1.py`
- `replay_engine.py`
- `routes/api_strategies.py`
- `mcp_tools/replay_tools.py`
- `.env.example`

## Chunk 1: Instance Profile Configuration

**Goal:** Make runtime identity and strategy/feed enablement explicit and typed.

### Task 1.1: Add typed instance profile module

**Files**
- New: `instance_profile.py`
- Tests: `tests/test_instance_profile.py`

**Implement**
- `InstanceProfile` dataclass:
  - `instance_tag`
  - `display_name`
  - `role`
  - `enabled`
  - `enabled_strategies`
  - `enabled_feeds`
  - `paper_only`
  - `real_trading_enabled`
  - `bankroll_usdc`
  - `max_total_exposure_usdc`
  - `max_market_exposure_usdc`
  - `max_concurrent_positions`
  - `default_loop_seconds`
- `load_instance_profile(env: Mapping[str, str] | None = None) -> InstanceProfile`
- CSV parsing helpers for `ENABLED_STRATEGIES` and `ENABLED_FEEDS`

**Rules**
- Empty `INSTANCE_TAG` is invalid
- `paper_only=1` forces real execution off
- Unknown strategy/feed IDs are not rejected here; validation happens later

**Tests**
- default profile loads correctly
- CSV parsing normalizes case/spacing
- `paper_only` overrides real trading
- invalid numeric env vars raise `ValueError`

### Task 1.2: Wire env vars into `web_settings.py` and `.env.example`

**Files**
- Modify: `web_settings.py`
- Modify: `.env.example`

**Implement**
- Add env-backed constants for all instance profile fields
- Keep them thin wrappers; the real typed object lives in `instance_profile.py`

**Tests**
- covered indirectly by `tests/test_instance_profile.py`

### Task 1.3: Add `instance_tag` to strategy/replay/audit tables

**Files**
- Modify: `db.py`

**Implement**
- Add `_ensure_column(...)` calls for nullable `instance_tag` on:
  - `strategies`
  - `strategy_positions`
  - `strategy_executions`
  - `strategy_runs`
  - `replay_configs`
  - `replay_runs`
  - `mcp_audit_log`
- Add indexes:
  - `(instance_tag, created_at DESC)` where relevant
  - `(instance_tag, strategy_id, created_at DESC)` where relevant
- Backfill helper:
  - if `INSTANCE_TAG` exists and row `instance_tag` is null/empty, fill it on startup

**Tests**
- add schema assertion tests in `tests/test_instance_profile.py` or a focused DB test

**Commit**
- `feat(instance): add typed instance profile and tag strategy/replay tables`

## Chunk 2: Feed Adapter Base and Registry

**Goal:** Introduce feed adapters without changing trading behavior yet.

### Task 2.1: Create `feeds/base.py`

**Files**
- New: `feeds/base.py`
- Tests: `tests/test_feed_registry.py`

**Implement**
- `FeedAdapter` abstract base class with:
  - `feed_id`
  - `display_name`
  - `default_poll_seconds`
  - `mode`
  - `validate_config()`
  - `warmup()`
  - `tick()`
  - optional `start()` / `stop()`

### Task 2.2: Create feed registry

**Files**
- New: `feeds/registry.py`
- New: `feeds/polymarket.py`
- Tests: `tests/test_feed_registry.py`

**Implement**
- `NoopPolymarketFeed` or lightweight `PolymarketFeedAdapter` for current insider-copy instances
- `get_feed_adapter(feed_id)`
- `list_feed_adapters()`

**Rules**
- Do not move existing Polymarket stream/analyzer logic into feeds yet
- This chunk only establishes the contract and registry

**Tests**
- registry returns polymarket adapter
- unknown feed returns `None`
- `validate_config()` can run without external creds for polymarket

**Commit**
- `feat(feeds): add feed adapter base and registry`

## Chunk 3: Shared Risk Policy Layer

**Goal:** Pull global capital and exposure logic out of plugin-specific code paths.

### Task 3.1: Add risk models

**Files**
- New: `risk/models.py`
- Tests: `tests/test_risk_policy.py`

**Implement**
- `RiskContext`
- `RiskDecision`

### Task 3.2: Add shared policy evaluator

**Files**
- New: `risk/policy.py`
- Tests: `tests/test_risk_policy.py`

**Implement**
- `evaluate_entry_risk(ctx: RiskContext, proposed_size_usdc: float, condition_id: str) -> RiskDecision`
- enforce:
  - bankroll floor
  - max concurrent positions
  - total exposure
  - per-market exposure
  - daily loss limit
  - `paper_only` / real disabled

**Rules**
- This layer clamps or rejects size
- It must not know strategy thesis details

**Tests**
- reject when bankroll too small
- clamp when market headroom smaller than proposed size
- reject when total exposure exceeded
- reject when concurrent cap hit
- reject real execution when `paper_only=1`

**Commit**
- `feat(risk): add shared entry risk policy layer`

## Chunk 4: Strategy Plugin Contract Extension

**Goal:** Extend plugin interface for feed requirements and replay adapters without breaking current plugins.

### Task 4.1: Extend `StrategyPlugin`

**Files**
- Modify: `strategies/base.py`
- Tests: `tests/test_strategy_plugin_contract.py`

**Implement**
- add attributes:
  - `strategy_family`
  - `supported_execution_modes`
  - `required_feeds`
  - `required_tables`
  - `default_loop_seconds`
- add methods:
  - `validate_parameters()`
  - `replay_adapter()`

**Compatibility rule**
- Default implementations must preserve current plugins without forcing immediate full rewrites

### Task 4.2: Add replay adapter abstractions

**Files**
- New: `replay/adapters.py`
- Tests: `tests/test_strategy_plugin_contract.py`

**Implement**
- `ReplayCandidate`
- `ReplayLifecycleState`
- `StrategyReplayAdapter`
- `InsiderCopyReplayAdapter`

### Task 4.3: Extract insider-copy replay logic from `replay_engine.py`

**Files**
- Modify: `replay_engine.py`
- Modify: `strategies/insider_copy_v1.py`
- Modify: `strategies/fade_v1.py`
- Modify: `mcp_tools/replay_tools.py`
- Modify: `routes/api_strategies.py`
- Tests: `tests/test_replay_engine.py`, `tests/test_replay_api.py`

**Implement**
- Move candidate-source and terminal-state assumptions for insider-copy into `InsiderCopyReplayAdapter`
- Keep the **existing replay endpoints and MCP tool signatures unchanged**
- `replay_engine.py` should accept a replay adapter or resolve one from strategy type
- Default current endpoints to `insider_copy_v1` if strategy type is omitted

**Important**
- This is the step Claude explicitly called out
- Do not break `POST /api/replay/run`, compare endpoints, or MCP replay surfaces

**Tests**
- existing replay tests still pass
- adapter path returns same results as pre-extraction insider-copy path on fixture data

**Commit**
- `refactor(strategy): add replay adapter contract and extract insider-copy adapter`

## Chunk 5: Runner Integration

**Goal:** Make the runner instance-aware, feed-aware, and risk-aware while keeping insider-copy behavior stable.

### Task 5.1: Load instance profile in `strategy_runner.py`

**Files**
- Modify: `strategy_runner.py`
- Tests: `tests/test_strategy_framework.py` or new focused tests

**Implement**
- load `InstanceProfile`
- filter active strategies by `enabled_strategies`
- attach `instance_tag` to created runs/executions/positions

### Task 5.2: Initialize required feeds

**Files**
- Modify: `strategy_runner.py`
- Tests: new runner-focused tests if needed

**Implement**
- union `required_feeds` across enabled strategies
- initialize only feeds enabled by profile
- if a strategy needs a feed that is not enabled or invalid, skip the strategy and log a warning

### Task 5.3: Apply shared risk policy on entry

**Files**
- Modify: `strategy_runner.py`
- Possibly trim duplicated caps from `strategies/insider_copy_v1.py` and `strategies/fade_v1.py`
- Tests: extend strategy framework tests

**Implement**
- plugin decides thesis and proposed size
- runner builds `RiskContext`
- shared policy clamps/rejects before `_record_plugin_entry`

**Compatibility goal**
- insider-copy still produces the same accept/reject outcomes under default current config

### Task 5.4: Strategy loop cadence prep

**Files**
- Modify: `strategy_runner.py`

**Implement**
- support per-strategy due-ness using `plugin.default_loop_seconds`
- do not yet build complex scheduler; a simple “skip if not due” map is enough

**Tests**
- slower and faster plugins can coexist without forcing all strategies to run every cycle

**Commit**
- `feat(runner): add instance profile, feed init, and shared risk integration`

## Chunk 6: Weather Feed and Strategy

**Goal:** Validate the whole framework with one low-complexity experimental bot.

### Task 6.1: Add weather feed adapter

**Files**
- New: `feeds/weather.py`
- Modify: `feeds/registry.py`
- Modify: `db.py`
- Tests: `tests/test_weather_strategy.py`

**Implement**
- OpenWeatherMap-backed adapter
- normalized `weather_snapshots` table:
  - `instance_tag`
  - `feed_id`
  - `location_key`
  - `forecast_ts`
  - `payload_json`
  - `captured_at`
- `warmup()` ensures table exists
- `tick()` fetches and stores normalized snapshots

### Task 6.2: Add `weather_v1` strategy plugin

**Files**
- New: `strategies/weather_v1.py`
- Modify: `strategies/registry.py`
- Tests: `tests/test_weather_strategy.py`

**Implement**
- declare:
  - `plugin_id = "weather_v1"`
  - `required_feeds = ("polymarket", "weather")`
  - `default_loop_seconds = 60`
- `discover()`:
  - find mapped weather markets
  - combine Polymarket market metadata with cached forecast snapshots
- `enter()`:
  - simple deterministic thesis, no fancy model
- `manage()/exit()`:
  - resolution-based or explicit market-close rules only
- `replay_adapter()`:
  - reads normalized weather snapshots + market links

**Note**
- Market mapping can be initially manual or rules-based for a narrow slice
- Keep the first bot intentionally simple

### Task 6.3: Add weather-specific replay path

**Files**
- Modify: `replay_engine.py`
- Modify: `routes/api_strategies.py`
- Modify: `mcp_tools/replay_tools.py`
- Tests: `tests/test_weather_strategy.py`

**Implement**
- allow replay by `strategy_type`
- insider-copy remains default if omitted
- weather strategy uses its own adapter path

**Commit**
- `feat(weather): add first experimental strategy on shared framework`

## Chunk 7: Verification and Rollout

**Goal:** Prove the framework did not break insider-copy and that the first new strategy is deployable in paper mode.

### Task 7.1: Regression verification

Run:
- full test suite
- replay tests
- strategy framework tests
- proof-bar tests

Expected:
- insider-copy surfaces unchanged
- replay endpoints unchanged for insider-copy

### Task 7.2: Dry local validation

Validate locally:
- current insider-copy strategy runner still starts with only `polymarket` feed enabled
- replay still runs for insider-copy default config
- instance profile tags new strategy rows/runs

### Task 7.3: Experimental VPS profile template

Add documentation only:
- example `.env` block for `weather-paper-us`
- recommended second-VPS deployment steps

**Commit**
- `docs: add experimental weather instance rollout notes`

## Test Plan Summary

### New tests by chunk

- Chunk 1: `tests/test_instance_profile.py`
- Chunk 2: `tests/test_feed_registry.py`
- Chunk 3: `tests/test_risk_policy.py`
- Chunk 4: `tests/test_strategy_plugin_contract.py`
- Chunk 5: extend existing strategy runner/framework tests
- Chunk 6: `tests/test_weather_strategy.py`

### Required invariants

1. insider-copy replay still works through existing API/MCP
2. strategy runner still supports current plugins with no feed breakage
3. no new strategy can execute real mode when `PAPER_ONLY=1`
4. strategy/replay/audit rows are tagged with `instance_tag`
5. a second VPS can run a different strategy profile from the same codebase

## Commit Boundaries

Use these commit boundaries unless implementation forces a small split:

1. `feat(instance): add typed instance profile and table tagging`
2. `feat(feeds): add feed adapter base and registry`
3. `feat(risk): add shared entry risk policy layer`
4. `refactor(strategy): add replay adapter contract and extract insider-copy adapter`
5. `feat(runner): add instance profile, feed init, and shared risk integration`
6. `feat(weather): add first experimental strategy on shared framework`
7. `docs: add experimental weather instance rollout notes`

## Acceptance Criteria

The batch is complete when:

1. the current insider-copy VPS can run unchanged with `INSTANCE_TAG=insider-main-paper`
2. `strategy_runner.py` only runs strategies allowed by `ENABLED_STRATEGIES`
3. feed dependencies are declared by plugins and enforced by the runner
4. shared risk policy gates entries outside plugin code
5. replay for insider-copy works through the new adapter path without API/MCP breakage
6. `weather_v1` can run in paper mode on a separate instance profile
7. all strategy/replay/audit artifacts are attributable to `instance_tag`

## Recommendation

Implement through Chunk 5 before writing substantial `weather_v1` logic.

If Chunk 5 lands cleanly and insider-copy remains stable, then `weather_v1` becomes a safe, isolated experiment instead of a framework fork.
