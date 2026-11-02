# Polymarket Crypto5m Maker Regime Evaluator Next Phases

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Use TDD for every behavior change.

**Status:** Started 2026-07-05.

**Goal:** Finish the evaluator stack after Phase 1 by adding hard-cut attribution, post-fill labels, context/profile scoring, shadow profile comparison, and later active profile selection.

**Architecture:** Keep the 1s maker loop cheap. The hot path may attach episode/profile ids and append current quote/fill rows only. Markouts, MFE/MAE, context stats, and recommendations run as cold-path replay/report code over persisted rows.

**Tech Stack:** Python 3.13, SQLite, pytest, current paper maker runner.

## Performance Boundary

These changes should not materially affect quote-loop performance if implemented this way:

- **Hard-cut attribution:** cheap. Adds no extra work on normal ticks; routes both tokens only when a hard cut is pending.
- **Post-fill labels:** safe only as cold-path replay. Do not scan future quotes/fills inside the 1s loop.
- **Context/profile stats:** cold-path aggregation only. Never bootstrap or group full history inside the runner.
- **Shadow profile comparison:** may be heavier. Run sidecar/replay or bounded background job; do not simulate all profiles in the live tick until measured.
- **Active selector:** cheap if it only reads a compact recommendation table. Unsafe if it recomputes recommendations live.

Hot-path budget:

```text
per tick:
  no full-table scans
  no bootstrap
  no historical replay
  no network calls beyond the existing desk/capture reads
  no profile simulation matrix
```

## Part 1: Hard-Cut Attribution

**Purpose:** hard-cut fills must carry fill-time episode/profile context.

**Implemented:**

- Added regression test:
  `tests/test_crypto5m_maker_runner.py::test_active_run_tags_hard_cut_fills_with_fill_time_episode_and_profile`
- Added runner helpers:
  - `active_regime_routes(...)`
  - `ensure_active_regime_episode(...)`
  - `build_profile_decisions(...)`
- Hard-cut path now creates/persists fill-time episode/profile metadata before recording hard-cut fills.
- No quote behavior change.
- Normal no-hard-cut ticks do not do the extra pre-cut route pass.

## Part 2: Post-Fill Evaluation Labels

**Purpose:** label whether fills were maker-friendly after the fact.

**Started:**

- Added pure module:
  `sniper/algos/polymarket_crypto5m/maker_regime_eval.py`
- Added tests:
  `tests/test_crypto5m_maker_regime_eval.py`
- Implemented:
  - `fill_cash_delta(fill) -> float`
  - `fill_edge_value(fill) -> float | None`
  - `future_unwind_value(fill, future_book) -> float | None`
  - `adverse_selection_bps(fill, future_book) -> float | None`
  - `mfe_mae_bps(fill, future_books) -> tuple[float | None, float | None]`

Remaining:

```text
maker_fill_evaluations persistence
cold-path replay script
3s/10s/30s future-book lookup
API/report surface
```

DB integration later:

```text
maker_quote_evaluations
maker_fill_evaluations
```

Cold-path job:

```text
scripts/evaluate_crypto5m_maker_regimes.py --paper-db ... --campaign maker-regime-v1 --lookback-windows N
```

Do not run this inside `run_polymarket_crypto5m_maker.py`.

## Part 3: Context/Profile Stats

**Purpose:** answer which quote profile works in which coarse moment.

Aggregate by:

```text
moment_schema_version
moment_key
regime_id
profile_id
profile_mode
campaign_id
```

Output table:

```text
maker_context_profile_stats
```

Required fields:

```text
n_windows
n_episodes
n_quotes
n_fills
mean_spread_capture
mean_adverse_selection_10s
mean_executable_markout_10s
mean_realized_pnl
ci_low
ci_high
status
last_updated_ts
```

Minimum first-read gates:

```text
min_windows = 20
min_episodes = 30
min_fills = 50
```

Campaign-level first read can use ~60 settled windows, but profile/context decisions need their own sample gates.

## Part 4: Shadow Profile Comparison

**Purpose:** compare actions, not regime names.

Profiles:

```text
quote_tight
quote_base
quote_wide
reduce_only
pause
exit_fast
```

For each episode/moment, compare:

```text
current actual profile
counterfactual profile decisions
fillability
missed fills
avoided bad fills
shadow_quality = exact | approximate | not_evaluable
```

Keep this sidecar/replay until measured. It is the first part that can become CPU-heavy.

## Part 5: Active Selector

**Purpose:** switch profiles from evidence, not fixed regime labels.

Do not build until Parts 2-4 have data.

Runtime selector input:

```text
current moment_key
inventory state
current active profile
recommendation table
```

Runtime selector output:

```text
profile_id
reason
confidence
fallback_used
```

Safety rules:

- safety regimes can still interrupt immediately;
- active selector cannot override hard invalid, late-window pull, or inventory caps;
- if no recommendation passes sample gates, fallback to current deterministic router;
- no auto-learning writes from the live loop.

## Current Status

- Phase 1 episode/profile logging: live.
- Part 1 hard-cut attribution: implemented and live after maker restart.
- Part 2 pure label math: implemented and tested; persistence/replay remains.
- Parts 3-5: designed here; implementation should proceed in order.
