# Replay Engine v1 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic forward replay engine that simulates copy-trade strategies against curated historical signal data with bankroll tracking, exposure limits, circuit breakers, and configurable entry gates — producing trade logs with skip reasons, equity curves, and summary stats.

**Architecture:** Three-layer design: (1) curated replay input layer in `db.py` that normalizes `signal_history` into a clean chronological signal feed — the replay engine NEVER reads raw `signal_history` directly; (2) core `replay_engine.py` module that consumes the feed and simulates trades with full capital/lifecycle rules; (3) thin exposure layers (API endpoint, MCP tool, CLI entrypoint) that call the core module. Configs and run results are persisted in two new DB tables for comparison.

**Tech Stack:** Python 3.13, SQLite, FastMCP, pytest, argparse (CLI)

**Spec:** `memory/replay-engine-brainstorm.md` (brainstorm decisions)

**Baseline:** 752 passing tests (3 pre-existing integration failures unrelated to this work)

---

## File Structure

| File | Responsibility | New/Modify |
|------|---------------|------------|
| `db.py` | Curated replay feed query + config/run persistence helpers | Modify |
| `replay_engine.py` | Core replay loop, gates, sizing, capital tracking, circuit breakers | New |
| `migrations/008_replay_tables.py` | `replay_configs` + `replay_runs` tables | New |
| `web_settings.py` | Replay config env vars | Modify |
| `routes/api_strategies.py` | `/api/replay/run`, `/api/replay/configs`, `/api/replay/runs`, `/api/replay/compare` | Modify |
| `mcp_tools/replay_tools.py` | MCP tool: `run_replay` | New |
| `sniper_mcp.py` | Wire up `register_replay_tools(mcp)` | Modify |
| `tests/test_replay_engine.py` | All replay engine tests | New |
| `tests/test_replay_api.py` | API + CLI integration tests | New |

---

## Chunk 1: Curated Replay Input Layer

The replay engine must NEVER read raw `signal_history` directly. All signal data flows through `replay_curated_signals()` in `db.py`, which applies the normalization rules agreed in brainstorming.

### Task 1: Write the curated replay feed query

**Files:**
- Modify: `db.py` (append new function at end)
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for curated feed — basic dedup and ordering**

Create `tests/test_replay_engine.py`:

```python
"""Tests for replay engine and its curated data layer."""
import sqlite3
import time
import unittest

from db import get_connection, init_db, replay_curated_signals


class TestReplayCuratedSignals(unittest.TestCase):
    """Test the curated replay input layer in db.py."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, analyzed_ts,
                       insider=70, spec=20, entry_price=0.45, is_open=1):
        """Helper to insert a signal_history row."""
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test Market', ?, ?, 100.0, ?, ?, ?, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, is_open, entry_price, entry_price,
             insider, spec, anchor_ts, analyzed_ts),
        )

    def test_basic_dedup_and_chronological_order(self):
        """First snapshot per (wallet, cond, outcome, anchor) wins; ordered by anchor_ts."""
        # Wallet A detected at t=1000, analyzed twice (t=1000, t=2000)
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=1000, insider=75)
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=2000, insider=80)
        # Wallet B detected at t=500 (should come first in output)
        self._insert_signal("0xB", "c2", "Yes", anchor_ts=500, analyzed_ts=500, insider=65)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 2)
        # Chronological by anchor_ts
        self.assertEqual(rows[0]["wallet"], "0xB")
        self.assertEqual(rows[1]["wallet"], "0xA")
        # First snapshot wins: insider=75 not 80
        self.assertEqual(rows[1]["position_insider_score"], 75)

    def test_excludes_zero_anchor_and_zero_price(self):
        """Rows with entry_anchor_ts=0 or entry_price<=0 are excluded."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=0, analyzed_ts=1000)
        self._insert_signal("0xB", "c2", "Yes", anchor_ts=1000, analyzed_ts=1000, entry_price=0)
        self._insert_signal("0xC", "c3", "Yes", anchor_ts=1000, analyzed_ts=1000, entry_price=0.50)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wallet"], "0xC")

    def test_date_range_filter(self):
        """Optional start_ts/end_ts filters on entry_anchor_ts."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=1000)
        self._insert_signal("0xB", "c2", "Yes", anchor_ts=2000, analyzed_ts=2000)
        self._insert_signal("0xC", "c3", "Yes", anchor_ts=3000, analyzed_ts=3000)

        rows = replay_curated_signals(self.conn, start_ts=1500, end_ts=2500)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wallet"], "0xB")

    def test_returns_all_required_columns(self):
        """Each row has the columns the replay engine needs."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=1000)
        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        required = {
            "wallet", "condition_id", "outcome", "title", "entry_price",
            "entry_notional", "position_insider_score", "position_speculator_score",
            "position_classification", "entry_anchor_ts", "is_open",
        }
        self.assertTrue(required.issubset(set(rows[0].keys())),
                        f"Missing columns: {required - set(rows[0].keys())}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayCuratedSignals -v`
Expected: FAIL with `ImportError: cannot import name 'replay_curated_signals' from 'db'`

- [ ] **Step 3: Implement `replay_curated_signals()` in db.py**

Append to `db.py`:

```python
def replay_curated_signals(
    conn,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[dict]:
    """Return curated, deduplicated signal feed for the replay engine.

    Normalization rules:
    - Excludes rows where entry_anchor_ts = 0 (legacy without anchor)
    - Excludes rows where entry_price <= 0 (bad price data)
    - Takes the FIRST snapshot per (wallet, condition_id, outcome, entry_anchor_ts)
      ordered by analyzed_ts ASC — captures scores at first detection
    - Returns rows in chronological order by entry_anchor_ts

    This is the ONLY function the replay engine should use to obtain signals.
    It must never read signal_history directly.

    Parameters
    ----------
    conn : sqlite3.Connection
    start_ts : int, optional
        Minimum entry_anchor_ts (inclusive).
    end_ts : int, optional
        Maximum entry_anchor_ts (inclusive).

    Returns
    -------
    list[dict]
        Curated signal rows with entry-time scores, ordered by entry_anchor_ts.
    """
    where_extra = ""
    params: list = []

    if start_ts is not None:
        where_extra += " AND sh.entry_anchor_ts >= ?"
        params.append(start_ts)
    if end_ts is not None:
        where_extra += " AND sh.entry_anchor_ts <= ?"
        params.append(end_ts)

    sql = f"""
        WITH first_seen AS (
            SELECT
                sh.wallet,
                sh.condition_id,
                sh.outcome,
                sh.title,
                sh.is_open,
                sh.entry_price,
                sh.entry_notional,
                sh.current_price,
                sh.position_insider_score,
                sh.position_speculator_score,
                sh.position_classification,
                sh.activity_state,
                sh.hold_hours,
                sh.entry_anchor_ts,
                sh.analyzed_ts,
                ROW_NUMBER() OVER (
                    PARTITION BY sh.wallet, sh.condition_id, sh.outcome, sh.entry_anchor_ts
                    ORDER BY sh.analyzed_ts ASC
                ) AS rn
            FROM signal_history sh
            WHERE sh.entry_anchor_ts > 0
              AND sh.entry_price > 0
              {where_extra}
        )
        SELECT
            wallet,
            condition_id,
            outcome,
            title,
            is_open,
            entry_price,
            entry_notional,
            current_price,
            position_insider_score,
            position_speculator_score,
            position_classification,
            activity_state,
            hold_hours,
            entry_anchor_ts,
            analyzed_ts
        FROM first_seen
        WHERE rn = 1
        ORDER BY entry_anchor_ts ASC
    """

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayCuratedSignals -v`
Expected: 4 tests PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed (3 pre-existing failures unchanged)

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_replay_engine.py
git commit -m "feat(replay): add curated signal feed query replay_curated_signals()"
```

---

### Task 2: Add outcome resolution join to curated feed

The replay engine needs to know how each signal resolved (YES won, NO won, or unresolved) to compute P&L. Add an optional outcome join.

**Files:**
- Modify: `db.py` (extend `replay_curated_signals`)
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for resolution data**

Add to `tests/test_replay_engine.py`:

```python
class TestReplayCuratedResolution(unittest.TestCase):
    """Test that curated feed includes resolution data when available."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, analyzed_ts,
                       insider=70, entry_price=0.45):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test Market', 0, ?, 100.0, ?, ?, 20, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, entry_price, entry_price, insider, anchor_ts, analyzed_ts),
        )

    def _insert_outcome(self, cond, outcome, resolved_price):
        self.conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES (?, ?, ?, 0)""",
            (cond, outcome.lower(), resolved_price),
        )

    def test_resolved_signal_has_resolved_price(self):
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=1000)
        self._insert_outcome("c1", "yes", 1.0)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolved_price"], 1.0)

    def test_unresolved_signal_has_null_resolved_price(self):
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, analyzed_ts=1000)
        # No market_outcomes row

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["resolved_price"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayCuratedResolution -v`
Expected: FAIL — `resolved_price` key missing

- [ ] **Step 3: Add LEFT JOIN to market_outcomes in the curated query**

In `db.py`, update `replay_curated_signals()` — add `mo.resolved_price` to the SELECT and a LEFT JOIN:

```python
# In the outer SELECT, add:
#     mo.resolved_price
# After FROM first_seen, add:
#     LEFT JOIN market_outcomes mo
#         ON mo.condition_id = first_seen.condition_id
#         AND mo.outcome = LOWER(first_seen.outcome)
```

The final outer SELECT becomes:

```python
    sql = f"""
        WITH first_seen AS (
            ...same CTE as before...
        )
        SELECT
            fs.wallet,
            fs.condition_id,
            fs.outcome,
            fs.title,
            fs.is_open,
            fs.entry_price,
            fs.entry_notional,
            fs.current_price,
            fs.position_insider_score,
            fs.position_speculator_score,
            fs.position_classification,
            fs.activity_state,
            fs.hold_hours,
            fs.entry_anchor_ts,
            fs.analyzed_ts,
            mo.resolved_price
        FROM first_seen fs
        LEFT JOIN market_outcomes mo
            ON mo.condition_id = fs.condition_id
            AND mo.outcome = LOWER(fs.outcome)
        WHERE fs.rn = 1
        ORDER BY fs.entry_anchor_ts ASC
    """
```

Note: `LOWER()` is required because signal_history stores "Yes"/"No" but market_outcomes stores "yes"/"no" (documented in memory).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_engine.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_replay_engine.py
git commit -m "feat(replay): add resolution join to curated signal feed"
```

---

## Chunk 2: Core Replay Engine Module

### Task 3: Define replay config dataclass and result dataclass

**Files:**
- Create: `replay_engine.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for config defaults and result structure**

Add to `tests/test_replay_engine.py`:

```python
from replay_engine import ReplayConfig, ReplayResult


class TestReplayConfig(unittest.TestCase):
    """Test ReplayConfig dataclass defaults and validation."""

    def test_default_config(self):
        cfg = ReplayConfig()
        self.assertEqual(cfg.initial_bankroll, 1000.0)
        self.assertEqual(cfg.base_size_usdc, 25.0)
        self.assertEqual(cfg.max_concurrent_positions, 20)
        self.assertEqual(cfg.max_exposure_per_market_usdc, 200.0)
        self.assertEqual(cfg.max_total_exposure_usdc, 1000.0)
        self.assertEqual(cfg.min_insider_score, 55.0)
        self.assertEqual(cfg.max_speculator_score, 70.0)
        self.assertEqual(cfg.max_entry_price, 0.70)
        self.assertIsNone(cfg.circuit_breaker_max_consecutive_losses)
        self.assertIsNone(cfg.circuit_breaker_max_drawdown_pct)

    def test_config_to_dict_roundtrip(self):
        cfg = ReplayConfig(initial_bankroll=500.0, min_insider_score=70)
        d = cfg.to_dict()
        self.assertEqual(d["initial_bankroll"], 500.0)
        self.assertEqual(d["min_insider_score"], 70)
        cfg2 = ReplayConfig.from_dict(d)
        self.assertEqual(cfg2.initial_bankroll, 500.0)
        self.assertEqual(cfg2.min_insider_score, 70)


class TestReplayResult(unittest.TestCase):
    """Test ReplayResult structure."""

    def test_empty_result(self):
        result = ReplayResult(
            config=ReplayConfig(),
            trades=[],
            skipped=[],
            equity_curve=[],
            summary={},
        )
        self.assertEqual(result.trades, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.equity_curve, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayConfig -v`
Expected: FAIL with `ImportError: cannot import name 'ReplayConfig'`

- [ ] **Step 3: Create `replay_engine.py` with dataclasses**

```python
"""Forward Replay Engine for Sniper

Deterministic forward simulation of copy-trade strategies against curated
historical signal data.  Tracks bankroll, exposure limits, position counts,
and circuit breakers.

The engine NEVER reads signal_history directly — all signal data flows
through ``db.replay_curated_signals()``.

Design constraints (from brainstorm):
- Simple fill assumptions (instant fill at signal entry_price)
- No order book simulation
- No fake microstructure precision
- Deterministic, explainable rules
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReplayConfig:
    """All parameters for a replay run.  Mirrors paper_trader gates."""

    # Capital
    initial_bankroll: float = 1000.0
    base_size_usdc: float = 25.0

    # Position limits
    max_concurrent_positions: int = 20
    max_exposure_per_market_usdc: float = 200.0
    max_total_exposure_usdc: float = 1000.0

    # Entry gates (mirror paper_trader where data is available)
    min_insider_score: float = 55.0
    max_speculator_score: float = 70.0
    max_entry_price: float = 0.70
    skip_classifications: set[str] = field(default_factory=lambda: {"arb_fund"})
    # NOTE: qualifying_roi gate is omitted in v1 — signal_history does not carry
    # wallet-level qualifying_roi.  Future: join score_history for this gate.

    # Circuit breakers (None = disabled)
    circuit_breaker_max_consecutive_losses: int | None = None
    circuit_breaker_max_drawdown_pct: float | None = None

    # Date range (entry_anchor_ts bounds)
    start_ts: int | None = None
    end_ts: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, set):
                d[k] = sorted(v)
            else:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayConfig":
        """Deserialize from dict (e.g. from DB JSON column)."""
        d = dict(d)
        if "skip_classifications" in d and isinstance(d["skip_classifications"], list):
            d["skip_classifications"] = set(d["skip_classifications"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ReplayResult:
    """Output of a single replay run."""

    config: ReplayConfig
    trades: list[dict[str, Any]]       # executed trades with entry/exit/pnl
    skipped: list[dict[str, Any]]      # skipped signals with skip_reason
    equity_curve: list[dict[str, Any]] # [{ts, bankroll, event}]
    summary: dict[str, Any]            # aggregate stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayConfig tests/test_replay_engine.py::TestReplayResult -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add replay_engine.py tests/test_replay_engine.py
git commit -m "feat(replay): add ReplayConfig and ReplayResult dataclasses"
```

---

### Task 4: Implement entry gate logic

**Files:**
- Modify: `replay_engine.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing tests for gate checks**

Add to `tests/test_replay_engine.py`:

```python
from replay_engine import ReplayConfig, _check_entry_gates


class TestEntryGates(unittest.TestCase):
    """Test individual entry gate checks."""

    def _make_signal(self, **overrides):
        base = {
            "wallet": "0xA",
            "condition_id": "c1",
            "outcome": "Yes",
            "entry_price": 0.45,
            "position_insider_score": 70,
            "position_speculator_score": 20,
            "position_classification": "insider_candidate",
            "resolved_price": 1.0,
        }
        base.update(overrides)
        return base

    def test_passes_all_gates(self):
        cfg = ReplayConfig()
        reason = _check_entry_gates(self._make_signal(), cfg, open_positions={},
                                    concurrent_count=0, total_exposure=0.0)
        self.assertIsNone(reason)

    def test_rejects_low_insider(self):
        cfg = ReplayConfig(min_insider_score=60)
        reason = _check_entry_gates(self._make_signal(position_insider_score=50),
                                    cfg, open_positions={}, concurrent_count=0,
                                    total_exposure=0.0)
        self.assertEqual(reason, "insider_score_below_min")

    def test_rejects_high_speculator(self):
        cfg = ReplayConfig()
        reason = _check_entry_gates(self._make_signal(position_speculator_score=75),
                                    cfg, open_positions={}, concurrent_count=0,
                                    total_exposure=0.0)
        self.assertEqual(reason, "speculator_score_above_max")

    def test_rejects_high_price(self):
        cfg = ReplayConfig(max_entry_price=0.60)
        reason = _check_entry_gates(self._make_signal(entry_price=0.65),
                                    cfg, open_positions={}, concurrent_count=0,
                                    total_exposure=0.0)
        self.assertEqual(reason, "entry_price_above_max")

    def test_rejects_skip_classification(self):
        cfg = ReplayConfig()
        reason = _check_entry_gates(
            self._make_signal(position_classification="arb_fund"),
            cfg, open_positions={}, concurrent_count=0, total_exposure=0.0)
        self.assertEqual(reason, "classification_skipped")

    def test_rejects_max_concurrent(self):
        cfg = ReplayConfig(max_concurrent_positions=5)
        reason = _check_entry_gates(self._make_signal(), cfg, open_positions={},
                                    concurrent_count=5, total_exposure=0.0)
        self.assertEqual(reason, "max_concurrent_positions")

    def test_rejects_total_exposure(self):
        cfg = ReplayConfig(max_total_exposure_usdc=100.0)
        reason = _check_entry_gates(self._make_signal(), cfg, open_positions={},
                                    concurrent_count=0, total_exposure=100.0)
        self.assertEqual(reason, "total_exposure_exceeded")

    def test_rejects_market_exposure(self):
        cfg = ReplayConfig(max_exposure_per_market_usdc=50.0)
        open_pos = {"c1": 50.0}  # already at cap
        reason = _check_entry_gates(self._make_signal(), cfg, open_positions=open_pos,
                                    concurrent_count=0, total_exposure=0.0)
        self.assertEqual(reason, "market_exposure_exceeded")

    def test_rejects_unresolved(self):
        """Signals with no resolution cannot produce P&L — skip them."""
        cfg = ReplayConfig()
        reason = _check_entry_gates(self._make_signal(resolved_price=None),
                                    cfg, open_positions={}, concurrent_count=0,
                                    total_exposure=0.0)
        self.assertEqual(reason, "unresolved_market")

    def test_rejects_bankroll_depleted(self):
        """Cannot enter when bankroll < base_size."""
        cfg = ReplayConfig(base_size_usdc=25.0)
        reason = _check_entry_gates(self._make_signal(), cfg, open_positions={},
                                    concurrent_count=0, total_exposure=0.0,
                                    current_bankroll=10.0)
        self.assertEqual(reason, "bankroll_depleted")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestEntryGates -v`
Expected: FAIL with `ImportError: cannot import name '_check_entry_gates'`

- [ ] **Step 3: Implement `_check_entry_gates()`**

Add to `replay_engine.py`:

```python
def _check_entry_gates(
    signal: dict,
    config: ReplayConfig,
    *,
    open_positions: dict[str, float],   # condition_id → exposure_usdc
    concurrent_count: int,
    total_exposure: float,
    current_bankroll: float | None = None,
) -> str | None:
    """Check all entry gates for a signal.

    Returns None if all gates pass, or a string skip_reason if rejected.
    Gate order mirrors paper_trader.py enter_position() sequence.
    """
    # Gate: classification
    if signal.get("position_classification") in config.skip_classifications:
        return "classification_skipped"

    # Gate: entry price
    if signal["entry_price"] > config.max_entry_price:
        return "entry_price_above_max"

    # Gate: insider score
    if signal.get("position_insider_score", 0) < config.min_insider_score:
        return "insider_score_below_min"

    # Gate: speculator score
    if signal.get("position_speculator_score", 0) >= config.max_speculator_score:
        return "speculator_score_above_max"

    # Gate: unresolved (can't compute P&L)
    if signal.get("resolved_price") is None:
        return "unresolved_market"

    # Gate: bankroll
    size = config.base_size_usdc
    if current_bankroll is not None and current_bankroll < size:
        return "bankroll_depleted"

    # Gate: max concurrent positions
    if concurrent_count >= config.max_concurrent_positions:
        return "max_concurrent_positions"

    # Gate: total exposure
    if total_exposure + size > config.max_total_exposure_usdc:
        return "total_exposure_exceeded"

    # Gate: per-market exposure
    cond = signal["condition_id"]
    market_exp = open_positions.get(cond, 0.0)
    if market_exp + size > config.max_exposure_per_market_usdc:
        return "market_exposure_exceeded"

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_engine.py::TestEntryGates -v`
Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add replay_engine.py tests/test_replay_engine.py
git commit -m "feat(replay): implement entry gate checks with skip reasons"
```

---

### Task 5: Implement the core replay loop

**Files:**
- Modify: `replay_engine.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing tests for the replay loop**

Add to `tests/test_replay_engine.py`:

```python
from replay_engine import ReplayConfig, ReplayResult, run_replay


class TestRunReplay(unittest.TestCase):
    """Test the core replay loop end-to-end."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, insider=70,
                       entry_price=0.45, spec=20):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test Market', 0, ?, 100.0, ?, ?, ?, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, entry_price, entry_price, insider, spec,
             anchor_ts, anchor_ts),
        )

    def _insert_outcome(self, cond, outcome, resolved_price):
        self.conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES (?, ?, ?, 0)""",
            (cond, outcome, resolved_price),
        )

    def test_simple_winning_trade(self):
        """One signal, resolves YES → profit."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, entry_price=0.40)
        self._insert_outcome("c1", "yes", 1.0)

        result = run_replay(ReplayConfig(initial_bankroll=1000.0, base_size_usdc=25.0),
                            conn=self.conn)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(len(result.skipped), 0)
        t = result.trades[0]
        self.assertEqual(t["entry_price"], 0.40)
        self.assertEqual(t["resolved_price"], 1.0)
        # ROI = (1.0 - 0.4) / 0.4 = 1.5 = 150%
        self.assertAlmostEqual(t["roi"], 1.5)
        # P&L = 25 * 1.5 = 37.5
        self.assertAlmostEqual(t["pnl_usdc"], 37.5)
        # Summary
        self.assertEqual(result.summary["total_trades"], 1)
        self.assertEqual(result.summary["wins"], 1)
        self.assertAlmostEqual(result.summary["final_bankroll"], 1037.5)

    def test_simple_losing_trade(self):
        """One signal, resolves NO → loss."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, entry_price=0.40)
        self._insert_outcome("c1", "yes", 0.0)

        result = run_replay(ReplayConfig(initial_bankroll=1000.0, base_size_usdc=25.0),
                            conn=self.conn)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        # ROI = (0.0 - 0.4) / 0.4 = -1.0 = -100%
        self.assertAlmostEqual(t["roi"], -1.0)
        # P&L = 25 * -1.0 = -25.0
        self.assertAlmostEqual(t["pnl_usdc"], -25.0)
        self.assertEqual(result.summary["losses"], 1)
        self.assertAlmostEqual(result.summary["final_bankroll"], 975.0)

    def test_gate_rejection_tracked(self):
        """Signal that fails gates appears in skipped list with reason."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000,
                            insider=40, entry_price=0.45)
        self._insert_outcome("c1", "yes", 1.0)

        result = run_replay(ReplayConfig(min_insider_score=55), conn=self.conn)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "insider_score_below_min")

    def test_equity_curve_tracks_bankroll(self):
        """Equity curve has entry for initial state plus one per trade."""
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, entry_price=0.40)
        self._insert_signal("0xB", "c2", "Yes", anchor_ts=2000, entry_price=0.50)
        self._insert_outcome("c1", "yes", 1.0)
        self._insert_outcome("c2", "yes", 0.0)

        cfg = ReplayConfig(initial_bankroll=1000.0, base_size_usdc=25.0)
        result = run_replay(cfg, conn=self.conn)

        # Initial + 2 trade events
        self.assertEqual(len(result.equity_curve), 3)
        self.assertAlmostEqual(result.equity_curve[0]["bankroll"], 1000.0)
        # After win: 1000 + 25*(1.0-0.4)/0.4 = 1037.5
        self.assertAlmostEqual(result.equity_curve[1]["bankroll"], 1037.5)
        # After loss: 1037.5 + 25*(0.0-0.5)/0.5 = 1037.5 - 25 = 1012.5
        self.assertAlmostEqual(result.equity_curve[2]["bankroll"], 1012.5)

    def test_circuit_breaker_consecutive_losses(self):
        """Circuit breaker halts trading after N consecutive losses."""
        for i in range(5):
            self._insert_signal(f"0x{i}", f"c{i}", "Yes",
                                anchor_ts=1000 + i * 100, entry_price=0.50)
            self._insert_outcome(f"c{i}", "yes", 0.0)  # all lose

        cfg = ReplayConfig(circuit_breaker_max_consecutive_losses=3)
        result = run_replay(cfg, conn=self.conn)
        # Should stop after 3 consecutive losses
        self.assertEqual(len(result.trades), 3)
        self.assertEqual(len(result.skipped), 2)
        for s in result.skipped:
            self.assertEqual(s["skip_reason"], "circuit_breaker_consecutive_losses")

    def test_circuit_breaker_drawdown(self):
        """Circuit breaker halts when drawdown exceeds threshold."""
        # 5 losing trades at $25 each; breaker at 8% of $1000 = $80
        for i in range(5):
            self._insert_signal(f"0x{i}", f"c{i}", "Yes",
                                anchor_ts=1000 + i * 100, entry_price=0.50)
            self._insert_outcome(f"c{i}", "yes", 0.0)

        cfg = ReplayConfig(
            initial_bankroll=1000.0,
            base_size_usdc=25.0,
            circuit_breaker_max_drawdown_pct=8.0,  # 8% = $80
        )
        result = run_replay(cfg, conn=self.conn)
        # Losses: $25 each. After 3: dd=$75 (7.5%) < 8% → OK.
        # Before trade 4: dd=$75/1000=7.5% < 8% → allowed. After: dd=$100 (10%).
        # Before trade 5: dd=$100/1000=10% >= 8% → TRIP. Trade 5 skipped.
        self.assertEqual(len(result.trades), 4)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "circuit_breaker_drawdown")

    def test_no_signals_returns_empty_result(self):
        """Empty signal feed → valid empty result."""
        result = run_replay(ReplayConfig(), conn=self.conn)
        self.assertEqual(result.summary["total_trades"], 0)
        self.assertEqual(result.summary["final_bankroll"], 1000.0)
        self.assertEqual(len(result.equity_curve), 1)  # just initial

    def test_max_concurrent_v1_instant_resolution(self):
        """v1: all trades resolve instantly, so concurrent limit is a no-op.
        This test documents the current behavior — all 3 trade because
        positions never stay open in v1 (instant resolution)."""
        for i in range(3):
            self._insert_signal(f"0x{i}", f"c{i}", "Yes",
                                anchor_ts=1000 + i * 100, entry_price=0.50)
            self._insert_outcome(f"c{i}", "yes", 1.0)

        cfg = ReplayConfig(max_concurrent_positions=2)
        result = run_replay(cfg, conn=self.conn)
        # v1: instant resolution means concurrent limit never fires
        self.assertEqual(len(result.trades), 3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_replay_engine.py::TestRunReplay -v`
Expected: FAIL with `ImportError: cannot import name 'run_replay'`

- [ ] **Step 3: Implement `run_replay()`**

Add to `replay_engine.py`:

```python
from db import replay_curated_signals


def _compute_trade_pnl(signal: dict, size_usdc: float) -> dict:
    """Compute P&L for a single trade.

    Fill assumption: instant fill at signal entry_price.
    Exit: at resolved_price (1.0 for YES win, 0.0 for YES loss).
    ROI = (resolved_price - entry_price) / entry_price
    P&L = size_usdc * ROI
    """
    entry = signal["entry_price"]
    if entry <= 0:
        return None  # guard: never divide by zero (CLAUDE.md domain rule)
    resolved = signal["resolved_price"]
    roi = (resolved - entry) / entry
    pnl = size_usdc * roi
    win = resolved > entry
    return {
        "wallet": signal["wallet"],
        "condition_id": signal["condition_id"],
        "outcome": signal["outcome"],
        "title": signal.get("title", ""),
        "entry_price": entry,
        "resolved_price": resolved,
        "size_usdc": size_usdc,
        "roi": round(roi, 6),
        "pnl_usdc": round(pnl, 4),
        "win": win,
        "entry_anchor_ts": signal["entry_anchor_ts"],
        "position_insider_score": signal.get("position_insider_score", 0),
        "position_speculator_score": signal.get("position_speculator_score", 0),
        "position_classification": signal.get("position_classification", ""),
    }


def run_replay(
    config: ReplayConfig,
    *,
    conn=None,
) -> ReplayResult:
    """Execute a deterministic forward replay against curated signal history.

    Parameters
    ----------
    config : ReplayConfig
        All gate, sizing, and capital parameters.
    conn : sqlite3.Connection, optional
        DB connection.  If None, opens one via ``db.get_connection()``.

    Returns
    -------
    ReplayResult
        Complete replay output: trades, skipped signals, equity curve, summary.
    """
    close_conn = False
    if conn is None:
        from db import get_connection
        conn = get_connection()
        close_conn = True

    try:
        signals = replay_curated_signals(
            conn, start_ts=config.start_ts, end_ts=config.end_ts,
        )
        return _run_replay_loop(config, signals)
    finally:
        if close_conn:
            conn.close()


def _run_replay_loop(config: ReplayConfig, signals: list[dict]) -> ReplayResult:
    """Core deterministic replay loop.  Pure function (no DB access)."""
    bankroll = config.initial_bankroll
    trades: list[dict] = []
    skipped: list[dict] = []
    equity_curve: list[dict] = [{"ts": 0, "bankroll": bankroll, "event": "start"}]

    # State tracking
    open_positions: dict[str, float] = {}  # condition_id → exposure_usdc
    concurrent_count = 0
    total_exposure = 0.0
    consecutive_losses = 0
    peak_bankroll = bankroll
    circuit_breaker_active = False

    for signal in signals:
        # Circuit breaker check
        if circuit_breaker_active:
            skipped.append({
                **_signal_summary(signal),
                "skip_reason": _circuit_breaker_reason(config, consecutive_losses,
                                                       bankroll, peak_bankroll),
            })
            continue

        # Check circuit breakers before gate check
        cb_reason = _check_circuit_breakers(
            config, consecutive_losses, bankroll, peak_bankroll,
        )
        if cb_reason:
            circuit_breaker_active = True
            skipped.append({**_signal_summary(signal), "skip_reason": cb_reason})
            continue

        # Entry gate check
        reason = _check_entry_gates(
            signal, config,
            open_positions=open_positions,
            concurrent_count=concurrent_count,
            total_exposure=total_exposure,
            current_bankroll=bankroll,
        )
        if reason:
            skipped.append({**_signal_summary(signal), "skip_reason": reason})
            continue

        # Execute trade
        size = min(config.base_size_usdc, bankroll)
        trade = _compute_trade_pnl(signal, size)
        trades.append(trade)

        # Update bankroll
        bankroll += trade["pnl_usdc"]
        bankroll = round(bankroll, 4)

        # Track consecutive losses
        if trade["win"]:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        # Track peak for drawdown
        if bankroll > peak_bankroll:
            peak_bankroll = bankroll

        # Equity curve entry
        equity_curve.append({
            "ts": signal["entry_anchor_ts"],
            "bankroll": bankroll,
            "event": "trade",
        })

        # v1 limitation: all trades resolve instantly (we have resolved_price).
        # concurrent_count/open_positions are never incremented, so
        # max_concurrent_positions and per-market exposure gates are effectively
        # no-ops.  This is correct for v1 because we use historical resolved
        # outcomes.  Future v2: simulate open position lifecycle over time.

    # Build summary
    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    total_pnl = sum(t["pnl_usdc"] for t in trades)
    win_rate = wins / len(trades) if trades else None
    avg_roi = sum(t["roi"] for t in trades) / len(trades) if trades else None
    max_dd = _max_drawdown(equity_curve)

    summary = {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_roi": round(avg_roi, 6) if avg_roi is not None else None,
        "total_pnl": round(total_pnl, 4),
        "max_drawdown": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd / config.initial_bankroll * 100, 2)
            if config.initial_bankroll > 0 else 0.0,
        "final_bankroll": bankroll,
        "initial_bankroll": config.initial_bankroll,
        "total_signals": len(signals),
        "signals_skipped": len(skipped),
        "circuit_breaker_triggered": circuit_breaker_active,
    }

    return ReplayResult(
        config=config,
        trades=trades,
        skipped=skipped,
        equity_curve=equity_curve,
        summary=summary,
    )


def _signal_summary(signal: dict) -> dict:
    """Extract key fields from a signal for the skipped list."""
    return {
        "wallet": signal.get("wallet"),
        "condition_id": signal.get("condition_id"),
        "outcome": signal.get("outcome"),
        "entry_price": signal.get("entry_price"),
        "position_insider_score": signal.get("position_insider_score"),
        "entry_anchor_ts": signal.get("entry_anchor_ts"),
    }


def _check_circuit_breakers(
    config: ReplayConfig,
    consecutive_losses: int,
    bankroll: float,
    peak_bankroll: float,
) -> str | None:
    """Check if any circuit breaker should trip.  Returns reason or None."""
    if (config.circuit_breaker_max_consecutive_losses is not None
            and consecutive_losses >= config.circuit_breaker_max_consecutive_losses):
        return "circuit_breaker_consecutive_losses"

    if config.circuit_breaker_max_drawdown_pct is not None and peak_bankroll > 0:
        drawdown_pct = (peak_bankroll - bankroll) / peak_bankroll * 100
        if drawdown_pct >= config.circuit_breaker_max_drawdown_pct:
            return "circuit_breaker_drawdown"

    return None


def _circuit_breaker_reason(config, consecutive_losses, bankroll, peak_bankroll):
    """Return which circuit breaker is active (for labeling skipped signals)."""
    reason = _check_circuit_breakers(config, consecutive_losses, bankroll, peak_bankroll)
    return reason or "circuit_breaker_active"


def _max_drawdown(equity_curve: list[dict]) -> float:
    """Peak-to-trough drawdown in absolute USDC terms."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]["bankroll"]
    max_dd = 0.0
    for point in equity_curve:
        bal = point["bankroll"]
        if bal > peak:
            peak = bal
        dd = peak - bal
        if dd > max_dd:
            max_dd = dd
    return max_dd
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_engine.py::TestRunReplay -v`
Expected: 8 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed (3 pre-existing failures unchanged)

- [ ] **Step 6: Commit**

```bash
git add replay_engine.py tests/test_replay_engine.py
git commit -m "feat(replay): implement core replay loop with bankroll, gates, and circuit breakers"
```

---

### Task 6: Add `to_dict()` on ReplayResult for serialization

**Files:**
- Modify: `replay_engine.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test**

```python
class TestReplayResultSerialization(unittest.TestCase):
    def test_to_dict_is_json_serializable(self):
        """ReplayResult.to_dict() must produce JSON-safe output."""
        import json
        result = ReplayResult(
            config=ReplayConfig(),
            trades=[{"wallet": "0xA", "pnl_usdc": 10.0}],
            skipped=[],
            equity_curve=[{"ts": 0, "bankroll": 1000.0}],
            summary={"total_trades": 1},
        )
        d = result.to_dict()
        serialized = json.dumps(d)
        self.assertIn('"total_trades"', serialized)
        self.assertIn('"initial_bankroll"', serialized)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayResultSerialization -v`
Expected: FAIL — `ReplayResult` has no `to_dict`

- [ ] **Step 3: Add `to_dict()` to ReplayResult**

```python
# In ReplayResult dataclass, add:
    def to_dict(self) -> dict[str, Any]:
        """Serialize entire result to JSON-safe dict."""
        return {
            "config": self.config.to_dict(),
            "trades": self.trades,
            "skipped": self.skipped,
            "equity_curve": self.equity_curve,
            "summary": self.summary,
        }
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayResultSerialization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add replay_engine.py tests/test_replay_engine.py
git commit -m "feat(replay): add ReplayResult.to_dict() for JSON serialization"
```

---

## Chunk 3: Saved Configs & Saved Runs

### Task 7: Create migration for replay_configs and replay_runs tables

**Files:**
- Create: `migrations/008_replay_tables.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for table existence after migration**

Add to `tests/test_replay_engine.py`:

```python
class TestReplayTables(unittest.TestCase):
    """Test that replay tables exist after init_db."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_replay_configs_table_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_configs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_replay_runs_table_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_runs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_replay_configs_columns(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(replay_configs)")}
        self.assertTrue({"id", "name", "config_json", "notes", "created_at"}.issubset(cols))

    def test_replay_runs_columns(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(replay_runs)")}
        self.assertTrue(
            {"id", "config_id", "config_json", "summary_json", "result_json",
             "duration_ms", "created_at"}.issubset(cols)
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayTables -v`
Expected: FAIL — tables don't exist yet

- [ ] **Step 3: Create migration file**

Create `migrations/008_replay_tables.py`:

```python
"""Migration 008: Add replay_configs and replay_runs tables."""
import sqlite3
import sys


def run(conn: sqlite3.Connection) -> None:
    """Create replay_configs and replay_runs tables for the replay engine."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS replay_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            config_json TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS replay_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER,
            config_json TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            signal_count INTEGER NOT NULL DEFAULT 0,
            trade_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (config_id) REFERENCES replay_configs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_replay_runs_config
            ON replay_runs(config_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_replay_runs_created
            ON replay_runs(created_at DESC);
    """)
    print("Migration 008: replay_configs + replay_runs tables created.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "sniper.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run(conn)
        conn.close()
    except Exception as exc:
        print(f"Migration 008 FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Register migration in init_db**

In `db.py`, find where migrations are called in `init_db()` and add migration 007. Follow the pattern of previous migration imports (search for `006` to find the location).

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayTables -v`
Expected: 4 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed

- [ ] **Step 7: Commit**

```bash
git add migrations/008_replay_tables.py db.py tests/test_replay_engine.py
git commit -m "feat(replay): add replay_configs and replay_runs DB tables"
```

---

### Task 8: Add DB helpers for config and run persistence

**Files:**
- Modify: `db.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing tests for CRUD operations**

Add to `tests/test_replay_engine.py`:

```python
from db import (
    save_replay_config,
    get_replay_config,
    list_replay_configs,
    save_replay_run,
    list_replay_runs,
    get_replay_run,
)


class TestReplayConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_and_get_config(self):
        cfg = {"initial_bankroll": 500, "min_insider_score": 60}
        config_id = save_replay_config(self.conn, name="test-cfg",
                                       config_json=cfg, notes="test run")
        self.assertIsInstance(config_id, int)

        row = get_replay_config(self.conn, config_id)
        self.assertEqual(row["name"], "test-cfg")
        self.assertEqual(json.loads(row["config_json"])["initial_bankroll"], 500)

    def test_list_configs(self):
        save_replay_config(self.conn, name="cfg-a", config_json={})
        save_replay_config(self.conn, name="cfg-b", config_json={})
        configs = list_replay_configs(self.conn)
        self.assertEqual(len(configs), 2)

    def test_duplicate_name_raises(self):
        save_replay_config(self.conn, name="dup", config_json={})
        with self.assertRaises(Exception):
            save_replay_config(self.conn, name="dup", config_json={})


class TestReplayRunPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_and_get_run(self):
        cfg_id = save_replay_config(self.conn, name="run-cfg", config_json={"a": 1})
        run_id = save_replay_run(
            self.conn,
            config_id=cfg_id,
            config_json={"a": 1},
            summary_json={"total_trades": 5},
            result_json={"trades": []},
            signal_count=100,
            trade_count=5,
            duration_ms=1234,
        )
        self.assertIsInstance(run_id, int)

        row = get_replay_run(self.conn, run_id)
        self.assertEqual(json.loads(row["summary_json"])["total_trades"], 5)
        self.assertEqual(row["duration_ms"], 1234)

    def test_list_runs_for_config(self):
        cfg_id = save_replay_config(self.conn, name="list-cfg", config_json={})
        save_replay_run(self.conn, config_id=cfg_id, config_json={},
                        summary_json={}, result_json={}, signal_count=0,
                        trade_count=0, duration_ms=100)
        save_replay_run(self.conn, config_id=cfg_id, config_json={},
                        summary_json={}, result_json={}, signal_count=0,
                        trade_count=0, duration_ms=200)

        runs = list_replay_runs(self.conn, config_id=cfg_id)
        self.assertEqual(len(runs), 2)

    def test_save_run_without_config(self):
        """Ad-hoc runs (no saved config) use config_id=None."""
        run_id = save_replay_run(
            self.conn,
            config_id=None,
            config_json={"inline": True},
            summary_json={"total_trades": 0},
            result_json={},
            signal_count=0,
            trade_count=0,
            duration_ms=50,
        )
        row = get_replay_run(self.conn, run_id)
        self.assertIsNone(row["config_id"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayConfigPersistence -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement DB helpers in db.py**

Append to `db.py`:

```python
def save_replay_config(conn, *, name: str, config_json: dict, notes: str = "") -> int:
    """Save a named replay configuration. Returns config ID."""
    cur = conn.execute(
        "INSERT INTO replay_configs (name, config_json, notes) VALUES (?, ?, ?)",
        (name, json.dumps(config_json), notes),
    )
    conn.commit()
    return cur.lastrowid


def get_replay_config(conn, config_id: int) -> dict | None:
    """Get a replay config by ID."""
    row = conn.execute(
        "SELECT * FROM replay_configs WHERE id = ?", (config_id,)
    ).fetchone()
    return dict(row) if row else None


def list_replay_configs(conn) -> list[dict]:
    """List all saved replay configs, newest first."""
    rows = conn.execute(
        "SELECT id, name, notes, created_at, updated_at FROM replay_configs ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def save_replay_run(
    conn,
    *,
    config_id: int | None,
    config_json: dict,
    summary_json: dict,
    result_json: dict,
    signal_count: int,
    trade_count: int,
    duration_ms: int,
) -> int:
    """Save a replay run result. Returns run ID."""
    cur = conn.execute(
        """INSERT INTO replay_runs
           (config_id, config_json, summary_json, result_json,
            signal_count, trade_count, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (config_id, json.dumps(config_json), json.dumps(summary_json),
         json.dumps(result_json), signal_count, trade_count, duration_ms),
    )
    conn.commit()
    return cur.lastrowid


def get_replay_run(conn, run_id: int) -> dict | None:
    """Get a replay run by ID."""
    row = conn.execute(
        "SELECT * FROM replay_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def list_replay_runs(conn, *, config_id: int | None = None, limit: int = 50) -> list[dict]:
    """List replay runs, newest first. Optionally filter by config_id."""
    if config_id is not None:
        rows = conn.execute(
            """SELECT id, config_id, summary_json, signal_count, trade_count,
                      duration_ms, created_at
               FROM replay_runs WHERE config_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (config_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, config_id, summary_json, signal_count, trade_count,
                      duration_ms, created_at
               FROM replay_runs
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayConfigPersistence tests/test_replay_engine.py::TestReplayRunPersistence -v`
Expected: 7 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_replay_engine.py
git commit -m "feat(replay): add DB helpers for config and run persistence"
```

---

## Chunk 4: API Endpoints

### Task 9: Add `/api/replay/run` endpoint

**Files:**
- Modify: `routes/api_strategies.py`
- Test: `tests/test_replay_api.py`

- [ ] **Step 1: Write failing test for the run endpoint**

Create `tests/test_replay_api.py`:

```python
"""Tests for replay engine API endpoints."""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest


class TestReplayRunEndpoint(unittest.TestCase):
    """Test POST /api/replay/run."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_replay.db")
        if "web_server" in sys.modules:
            del sys.modules["web_server"]
        cls.web = importlib.import_module("web_server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        """Seed signal_history + market_outcomes for each test."""
        conn = self.web.ctx["conn"]
        conn.execute(
            """INSERT OR IGNORE INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES ('0xA', 'c1', 'Yes', 'Test', 0, 0.40, 100, 0.40,
                       70, 20, 'insider_candidate', 'holding', 10, 1000, 1000)""",
        )
        conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES ('c1', 'yes', 1.0, 0)""",
        )
        conn.commit()

    def test_basic_replay_run(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "min_insider_score": 55,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("summary", data)
        self.assertIn("trades", data)
        self.assertIn("equity_curve", data)
        self.assertGreaterEqual(data["summary"]["total_trades"], 1)

    def test_replay_run_with_save(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": 1000,
            "save_as": "test-run-config",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("run_id", data)

    def test_replay_run_invalid_config(self):
        resp = self.client.post("/api/replay/run", json={
            "initial_bankroll": -100,
        })
        self.assertEqual(resp.status_code, 400)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_api.py::TestReplayRunEndpoint::test_basic_replay_run -v`
Expected: FAIL — 404 (route doesn't exist)

- [ ] **Step 3: Implement the endpoint**

Add to `routes/api_strategies.py`:

```python
@bp.route("/api/replay/run", methods=["POST"])
def replay_run():
    """Execute a replay run with the given config."""
    import time as _time
    from replay_engine import ReplayConfig, run_replay
    from db import save_replay_config, save_replay_run

    body = request.get_json(silent=True) or {}

    # Validate
    bankroll = float(body.get("initial_bankroll", 1000.0))
    if bankroll <= 0:
        return jsonify({"error": "initial_bankroll must be positive"}), 400

    try:
        config = ReplayConfig(
            initial_bankroll=bankroll,
            base_size_usdc=float(body.get("base_size_usdc", 25.0)),
            max_concurrent_positions=int(body.get("max_concurrent_positions", 20)),
            max_exposure_per_market_usdc=float(body.get("max_exposure_per_market_usdc", 200.0)),
            max_total_exposure_usdc=float(body.get("max_total_exposure_usdc", 1000.0)),
            min_insider_score=float(body.get("min_insider_score", 55.0)),
            max_speculator_score=float(body.get("max_speculator_score", 70.0)),
            max_entry_price=float(body.get("max_entry_price", 0.70)),
            circuit_breaker_max_consecutive_losses=(
                int(body["circuit_breaker_max_consecutive_losses"])
                if body.get("circuit_breaker_max_consecutive_losses") is not None else None
            ),
            circuit_breaker_max_drawdown_pct=(
                float(body["circuit_breaker_max_drawdown_pct"])
                if body.get("circuit_breaker_max_drawdown_pct") is not None else None
            ),
            start_ts=body.get("start_ts"),
            end_ts=body.get("end_ts"),
        )
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid config: {e}"}), 400

    conn = ctx["conn"]
    start = _time.monotonic()
    try:
        result = run_replay(config, conn=conn)
    except Exception:
        logger.exception("replay_run failed")
        return jsonify({"error": "Replay execution failed"}), 500
    duration_ms = int((_time.monotonic() - start) * 1000)

    response = result.to_dict()
    response["duration_ms"] = duration_ms

    # Optional: save the run
    save_name = body.get("save_as")
    config_id = None
    if save_name:
        try:
            config_id = save_replay_config(
                conn, name=save_name, config_json=config.to_dict(),
                notes=body.get("notes", ""),
            )
        except Exception:
            logger.warning("Could not save replay config %r (may already exist)", save_name)

    run_id = save_replay_run(
        conn,
        config_id=config_id,
        config_json=config.to_dict(),
        summary_json=result.summary,
        result_json=result.to_dict(),
        signal_count=result.summary.get("total_signals", 0),
        trade_count=result.summary.get("total_trades", 0),
        duration_ms=duration_ms,
    )
    response["run_id"] = run_id

    return jsonify(response)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_api.py -v`
Expected: PASS (adapt test setup to match actual Flask test client pattern)

- [ ] **Step 5: Commit**

```bash
git add routes/api_strategies.py tests/test_replay_api.py
git commit -m "feat(replay): add POST /api/replay/run endpoint"
```

---

### Task 10: Add config and run listing endpoints

**Files:**
- Modify: `routes/api_strategies.py`
- Test: `tests/test_replay_api.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_replay_api.py`:

```python
class TestReplayListEndpoints(TestReplayRunEndpoint):
    """Reuses the same test client setup from TestReplayRunEndpoint."""

    def test_list_configs(self):
        resp = self.client.get("/api/replay/configs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_list_runs(self):
        resp = self.client.get("/api/replay/runs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_get_run_not_found(self):
        resp = self.client.get("/api/replay/runs/99999")
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: 404 — routes don't exist

- [ ] **Step 3: Implement listing endpoints**

Add to `routes/api_strategies.py`:

```python
@bp.route("/api/replay/configs", methods=["GET"])
def replay_list_configs():
    """List all saved replay configurations."""
    from db import list_replay_configs
    try:
        return jsonify(list_replay_configs(ctx["conn"]))
    except Exception:
        logger.exception("replay_list_configs failed")
        return jsonify({"error": "Failed to list configs"}), 500


@bp.route("/api/replay/runs", methods=["GET"])
def replay_list_runs():
    """List replay runs, optionally filtered by config_id."""
    from db import list_replay_runs
    config_id = request.args.get("config_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    try:
        return jsonify(list_replay_runs(ctx["conn"], config_id=config_id, limit=limit))
    except Exception:
        logger.exception("replay_list_runs failed")
        return jsonify({"error": "Failed to list runs"}), 500


@bp.route("/api/replay/runs/<int:run_id>", methods=["GET"])
def replay_get_run(run_id):
    """Get a single replay run by ID."""
    from db import get_replay_run
    try:
        row = get_replay_run(ctx["conn"], run_id)
        if not row:
            return jsonify({"error": "Run not found"}), 404
        return jsonify(row)
    except Exception:
        logger.exception("replay_get_run failed")
        return jsonify({"error": "Failed to get run"}), 500
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_api.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add routes/api_strategies.py tests/test_replay_api.py
git commit -m "feat(replay): add config/run listing and detail API endpoints"
```

---

### Task 11: Add `/api/replay/compare` endpoint

**Files:**
- Modify: `routes/api_strategies.py`
- Test: `tests/test_replay_api.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_replay_api.py`:

```python
class TestReplayCompare(TestReplayRunEndpoint):
    """Compare two replay runs.  Reuses test client + seeded data."""

    def test_compare_two_runs(self):
        r1 = self.client.post("/api/replay/run", json={"initial_bankroll": 1000})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/replay/run", json={"initial_bankroll": 2000})
        self.assertEqual(r2.status_code, 200)

        id1 = r1.get_json()["run_id"]
        id2 = r2.get_json()["run_id"]
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)

        resp = self.client.get(f"/api/replay/compare?run_ids={id1},{id2}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("runs", data)
        self.assertEqual(len(data["runs"]), 2)
        self.assertIn("config_diff", data)
        # initial_bankroll differs between runs
        self.assertIn("initial_bankroll", data["config_diff"])

    def test_compare_missing_runs(self):
        resp = self.client.get("/api/replay/compare?run_ids=99999,99998")
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: 404

- [ ] **Step 3: Implement compare endpoint**

Add to `routes/api_strategies.py`:

```python
@bp.route("/api/replay/compare", methods=["GET"])
def replay_compare():
    """Compare two or more replay runs side by side."""
    import json as _json
    from db import get_replay_run

    run_ids_str = request.args.get("run_ids", "")
    try:
        run_ids = [int(x.strip()) for x in run_ids_str.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "run_ids must be comma-separated integers"}), 400

    if len(run_ids) < 2:
        return jsonify({"error": "Need at least 2 run_ids to compare"}), 400

    conn = ctx["conn"]
    runs = []
    for rid in run_ids:
        row = get_replay_run(conn, rid)
        if not row:
            return jsonify({"error": f"Run {rid} not found"}), 404
        runs.append({
            "run_id": rid,
            "config": _json.loads(row["config_json"]) if isinstance(row["config_json"], str) else row["config_json"],
            "summary": _json.loads(row["summary_json"]) if isinstance(row["summary_json"], str) else row["summary_json"],
            "duration_ms": row.get("duration_ms"),
            "created_at": row.get("created_at"),
        })

    # Compute config diff
    config_diff = {}
    if len(runs) >= 2:
        all_keys = set()
        for r in runs:
            all_keys.update(r["config"].keys())
        for key in sorted(all_keys):
            values = [r["config"].get(key) for r in runs]
            if len(set(str(v) for v in values)) > 1:
                config_diff[key] = values

    return jsonify({"runs": runs, "config_diff": config_diff})
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_api.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed

- [ ] **Step 6: Commit**

```bash
git add routes/api_strategies.py tests/test_replay_api.py
git commit -m "feat(replay): add /api/replay/compare endpoint for run diffing"
```

---

## Chunk 5: CLI Entrypoint

### Task 12: Add CLI mode to replay_engine.py

**Files:**
- Modify: `replay_engine.py`
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for CLI arg parsing**

Add to `tests/test_replay_engine.py`:

```python
from replay_engine import _parse_cli_args


class TestReplayCLI(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_default_args(self):
        args = _parse_cli_args([])
        self.assertEqual(args.bankroll, 1000.0)
        self.assertEqual(args.min_insider, 55.0)
        self.assertIsNone(args.config_file)

    def test_custom_args(self):
        args = _parse_cli_args([
            "--bankroll", "500",
            "--min-insider", "70",
            "--max-price", "0.60",
            "--max-concurrent", "10",
        ])
        self.assertEqual(args.bankroll, 500.0)
        self.assertEqual(args.min_insider, 70.0)
        self.assertEqual(args.max_price, 0.60)
        self.assertEqual(args.max_concurrent, 10)

    def test_config_file_arg(self):
        args = _parse_cli_args(["--config", "my_config.json"])
        self.assertEqual(args.config_file, "my_config.json")

    def test_output_json_flag(self):
        args = _parse_cli_args(["--json"])
        self.assertTrue(args.json_output)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayCLI -v`
Expected: FAIL — `cannot import name '_parse_cli_args'`

- [ ] **Step 3: Implement CLI arg parser and `__main__` block**

Add to `replay_engine.py`:

```python
import argparse
import sys


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for replay engine."""
    parser = argparse.ArgumentParser(
        description="Sniper Replay Engine — deterministic forward simulation",
    )
    parser.add_argument("--bankroll", type=float, default=1000.0,
                        help="Initial bankroll in USDC (default: 1000)")
    parser.add_argument("--size", type=float, default=25.0,
                        help="Base trade size in USDC (default: 25)")
    parser.add_argument("--min-insider", type=float, default=55.0,
                        help="Minimum insider score gate (default: 55)")
    parser.add_argument("--max-spec", type=float, default=70.0,
                        help="Maximum speculator score gate (default: 70)")
    parser.add_argument("--max-price", type=float, default=0.70,
                        help="Maximum entry price gate (default: 0.70)")
    parser.add_argument("--max-concurrent", type=int, default=20,
                        help="Max concurrent positions (default: 20)")
    parser.add_argument("--max-market-exposure", type=float, default=200.0,
                        help="Per-market exposure cap in USDC (default: 200)")
    parser.add_argument("--max-total-exposure", type=float, default=1000.0,
                        help="Total exposure cap in USDC (default: 1000)")
    parser.add_argument("--cb-losses", type=int, default=None,
                        help="Circuit breaker: max consecutive losses")
    parser.add_argument("--cb-drawdown", type=float, default=None,
                        help="Circuit breaker: max drawdown percent")
    parser.add_argument("--config", dest="config_file", default=None,
                        help="Path to JSON config file (overrides all other args)")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Output full result as JSON")
    parser.add_argument("--save-as", default=None,
                        help="Save config with this name to DB")
    parser.add_argument("--db", default=None,
                        help="Path to SQLite database (default: sniper_tracker.db)")
    return parser.parse_args(argv)


def _cli_main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for replay engine."""
    args = _parse_cli_args(argv)

    # Build config
    if args.config_file:
        with open(args.config_file) as f:
            config = ReplayConfig.from_dict(json.load(f))
    else:
        config = ReplayConfig(
            initial_bankroll=args.bankroll,
            base_size_usdc=args.size,
            min_insider_score=args.min_insider,
            max_speculator_score=args.max_spec,
            max_entry_price=args.max_price,
            max_concurrent_positions=args.max_concurrent,
            max_exposure_per_market_usdc=args.max_market_exposure,
            max_total_exposure_usdc=args.max_total_exposure,
            circuit_breaker_max_consecutive_losses=args.cb_losses,
            circuit_breaker_max_drawdown_pct=args.cb_drawdown,
        )

    # Connect
    from db import get_connection
    conn = get_connection(args.db) if args.db else get_connection()

    import time as _time
    start = _time.monotonic()
    result = run_replay(config, conn=conn)
    duration_ms = int((_time.monotonic() - start) * 1000)

    if args.json_output:
        out = result.to_dict()
        out["duration_ms"] = duration_ms
        print(json.dumps(out, indent=2))
    else:
        s = result.summary
        print(f"Replay complete in {duration_ms}ms")
        print(f"  Signals: {s['total_signals']} total, {s['signals_skipped']} skipped")
        print(f"  Trades:  {s['total_trades']} ({s['wins']}W / {s['losses']}L)")
        print(f"  Win Rate: {s['win_rate']:.1%}" if s['win_rate'] is not None else "  Win Rate: N/A")
        print(f"  P&L:     ${s['total_pnl']:+.2f}")
        print(f"  Bankroll: ${s['initial_bankroll']:.2f} → ${s['final_bankroll']:.2f}")
        print(f"  Max DD:  ${s['max_drawdown']:.2f} ({s['max_drawdown_pct']:.1f}%)")
        if s['circuit_breaker_triggered']:
            print("  ⚠ Circuit breaker triggered")

    # Optional save
    if args.save_as:
        from db import save_replay_config, save_replay_run
        try:
            cfg_id = save_replay_config(conn, name=args.save_as, config_json=config.to_dict())
        except Exception:
            cfg_id = None
        save_replay_run(
            conn, config_id=cfg_id, config_json=config.to_dict(),
            summary_json=result.summary, result_json=result.to_dict(),
            signal_count=result.summary.get("total_signals", 0),
            trade_count=result.summary.get("total_trades", 0),
            duration_ms=duration_ms,
        )
        print(f"  Saved as: {args.save_as}")

    conn.close()


if __name__ == "__main__":
    _cli_main()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_replay_engine.py::TestReplayCLI -v`
Expected: 4 tests PASS

- [ ] **Step 5: Manual smoke test (optional)**

Run: `python3 replay_engine.py --bankroll 1000 --min-insider 60 --db sniper_tracker.db`
Expected: prints summary (or "0 trades" if no local signal_history data)

- [ ] **Step 6: Commit**

```bash
git add replay_engine.py tests/test_replay_engine.py
git commit -m "feat(replay): add CLI entrypoint with argparse"
```

---

## Chunk 6: MCP Tool

### Task 13: Create MCP replay tool

**Files:**
- Create: `mcp_tools/replay_tools.py`
- Modify: `mcp_tools/__init__.py` (if tools are registered there) or wherever tools are wired up
- Test: `tests/test_replay_engine.py`

- [ ] **Step 1: Write failing test for MCP impl function**

Add to `tests/test_replay_engine.py`:

```python
class TestMCPReplayTool(unittest.TestCase):
    """Test the MCP replay tool implementation."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, insider=70,
                       entry_price=0.45):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test', 0, ?, 100, ?, ?, 20, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, entry_price, entry_price, insider, anchor_ts, anchor_ts),
        )

    def _insert_outcome(self, cond, outcome, resolved_price):
        self.conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES (?, ?, ?, 0)""",
            (cond, outcome, resolved_price),
        )

    def test_replay_tool_returns_envelope(self):
        from mcp_tools.replay_tools import replay_impl
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000)
        self._insert_outcome("c1", "yes", 1.0)

        result = replay_impl(
            initial_bankroll=1000,
            min_insider_score=55,
            conn=self.conn,
        )
        self.assertIn("generated_at", result)
        self.assertIn("data_source", result)
        self.assertIn("data", result)
        data = result["data"]
        self.assertIn("summary", data)
        self.assertIn("trade_count", data["summary"])

    def test_replay_tool_respects_gates(self):
        from mcp_tools.replay_tools import replay_impl
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, insider=40)
        self._insert_outcome("c1", "yes", 1.0)

        result = replay_impl(min_insider_score=55, conn=self.conn)
        self.assertEqual(result["data"]["summary"]["total_trades"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: `ModuleNotFoundError: No module named 'mcp_tools.replay_tools'`

- [ ] **Step 3: Create `mcp_tools/replay_tools.py`**

```python
"""Replay MCP tools for Sniper — deterministic forward simulation."""
from __future__ import annotations

from db import get_readonly_connection
from replay_engine import ReplayConfig, run_replay
from mcp_tools.schemas import make_envelope


def replay_impl(
    *,
    initial_bankroll: float = 1000.0,
    base_size_usdc: float = 25.0,
    min_insider_score: float = 55.0,
    max_speculator_score: float = 70.0,
    max_entry_price: float = 0.70,
    max_concurrent_positions: int = 20,
    max_exposure_per_market_usdc: float = 200.0,
    max_total_exposure_usdc: float = 1000.0,
    circuit_breaker_max_consecutive_losses: int | None = None,
    circuit_breaker_max_drawdown_pct: float | None = None,
    conn=None,
) -> dict:
    """Internal implementation for run_replay MCP tool."""
    config = ReplayConfig(
        initial_bankroll=initial_bankroll,
        base_size_usdc=base_size_usdc,
        min_insider_score=min_insider_score,
        max_speculator_score=max_speculator_score,
        max_entry_price=max_entry_price,
        max_concurrent_positions=max_concurrent_positions,
        max_exposure_per_market_usdc=max_exposure_per_market_usdc,
        max_total_exposure_usdc=max_total_exposure_usdc,
        circuit_breaker_max_consecutive_losses=circuit_breaker_max_consecutive_losses,
        circuit_breaker_max_drawdown_pct=circuit_breaker_max_drawdown_pct,
    )

    close_conn = False
    if conn is None:
        conn = get_readonly_connection()
        close_conn = True

    try:
        result = run_replay(config, conn=conn)
    finally:
        if close_conn:
            conn.close()

    data = {
        "summary": result.summary,
        "config": result.config.to_dict(),
        "trade_count": result.summary.get("total_trades", 0),
        "equity_curve_points": len(result.equity_curve),
        "skip_reason_counts": _count_skip_reasons(result.skipped),
    }

    return make_envelope(
        data,
        data_source="signal_history_curated",
        bias_warning="Replay uses entry-time scores from signal_history first snapshots. "
                     "Resolution prices are retrospective — this is NOT a live forward test.",
    )


def _count_skip_reasons(skipped: list[dict]) -> dict[str, int]:
    """Aggregate skip reasons for summary."""
    counts: dict[str, int] = {}
    for s in skipped:
        reason = s.get("skip_reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def register_replay_tools(mcp_server):
    """Register replay tools with the MCP server."""

    @mcp_server.tool()
    def run_replay_simulation(
        initial_bankroll: float = 1000.0,
        base_size_usdc: float = 25.0,
        min_insider_score: float = 55.0,
        max_speculator_score: float = 70.0,
        max_entry_price: float = 0.70,
        max_concurrent_positions: int = 20,
        circuit_breaker_max_consecutive_losses: int = -1,
        circuit_breaker_max_drawdown_pct: float = -1.0,
    ) -> dict:
        """Run a deterministic forward replay of the insider-copy strategy against
        curated historical signal data.  Simulates bankroll, sizing, exposure caps,
        and circuit breakers.  Returns trade count, win rate, P&L, max drawdown,
        and skip reason breakdown.  NOT a live forward test — uses retrospective
        resolution prices."""
        return replay_impl(
            initial_bankroll=initial_bankroll,
            base_size_usdc=base_size_usdc,
            min_insider_score=min_insider_score,
            max_speculator_score=max_speculator_score,
            max_entry_price=max_entry_price,
            max_concurrent_positions=max_concurrent_positions,
            circuit_breaker_max_consecutive_losses=(
                circuit_breaker_max_consecutive_losses
                if circuit_breaker_max_consecutive_losses >= 0 else None
            ),
            circuit_breaker_max_drawdown_pct=(
                circuit_breaker_max_drawdown_pct
                if circuit_breaker_max_drawdown_pct >= 0 else None
            ),
        )
```

Note: MCP tools don't support `None` defaults well, so we use `-1` as sentinel for "disabled" circuit breakers.

- [ ] **Step 4: Wire up registration in `sniper_mcp.py`**

In `sniper_mcp.py`, find where `register_analyze_tools` is called and add alongside it:

```python
from mcp_tools.replay_tools import register_replay_tools
register_replay_tools(mcp)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_replay_engine.py::TestMCPReplayTool -v`
Expected: 2 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed

- [ ] **Step 7: Commit**

```bash
git add mcp_tools/replay_tools.py tests/test_replay_engine.py
git commit -m "feat(replay): add MCP run_replay_simulation tool"
```

---

## Chunk 7: Config Env Vars + Final Polish

### Task 14: Add replay config defaults to web_settings.py

**Files:**
- Modify: `web_settings.py`

- [ ] **Step 1: Add replay engine defaults**

Append to `web_settings.py`:

```python
# === REPLAY ENGINE DEFAULTS ===
REPLAY_DEFAULT_BANKROLL = float(os.getenv("REPLAY_DEFAULT_BANKROLL", "1000.0"))
REPLAY_DEFAULT_BASE_SIZE = float(os.getenv("REPLAY_DEFAULT_BASE_SIZE", "25.0"))
REPLAY_DEFAULT_MIN_INSIDER = float(os.getenv("REPLAY_DEFAULT_MIN_INSIDER", "55.0"))
REPLAY_DEFAULT_MAX_SPEC = float(os.getenv("REPLAY_DEFAULT_MAX_SPEC", "70.0"))
REPLAY_DEFAULT_MAX_PRICE = float(os.getenv("REPLAY_DEFAULT_MAX_PRICE", "0.70"))
REPLAY_DEFAULT_MAX_CONCURRENT = int(os.getenv("REPLAY_DEFAULT_MAX_CONCURRENT", "20"))
REPLAY_DEFAULT_MAX_MARKET_EXPOSURE = float(os.getenv("REPLAY_DEFAULT_MAX_MARKET_EXPOSURE", "200.0"))
REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE = float(os.getenv("REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE", "1000.0"))
```

- [ ] **Step 2: Wire env vars into the API endpoint defaults**

Keep `ReplayConfig` defaults hardcoded (avoids circular imports — same pattern as `paper_trader.py`). Instead, update the `/api/replay/run` endpoint to use `web_settings` values when no explicit override is provided:

```python
from web_settings import (
    REPLAY_DEFAULT_BANKROLL, REPLAY_DEFAULT_BASE_SIZE,
    REPLAY_DEFAULT_MIN_INSIDER, REPLAY_DEFAULT_MAX_SPEC,
    REPLAY_DEFAULT_MAX_PRICE, REPLAY_DEFAULT_MAX_CONCURRENT,
    REPLAY_DEFAULT_MAX_MARKET_EXPOSURE, REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE,
)

bankroll = float(body.get("initial_bankroll", REPLAY_DEFAULT_BANKROLL))
# ... same pattern for other fields
```

The CLI already has its own argparse defaults. The MCP tool keeps its own defaults inline (standard MCP pattern).

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 752+ passed

- [ ] **Step 4: Commit**

```bash
git add web_settings.py
git commit -m "feat(replay): add REPLAY_DEFAULT_* env var config in web_settings.py"
```

---

### Task 15: Update .env.example with replay vars

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Append replay section to .env.example**

```bash
# === Replay Engine ===
# REPLAY_DEFAULT_BANKROLL=1000.0
# REPLAY_DEFAULT_BASE_SIZE=25.0
# REPLAY_DEFAULT_MIN_INSIDER=55.0
# REPLAY_DEFAULT_MAX_SPEC=70.0
# REPLAY_DEFAULT_MAX_PRICE=0.70
# REPLAY_DEFAULT_MAX_CONCURRENT=20
# REPLAY_DEFAULT_MAX_MARKET_EXPOSURE=200.0
# REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE=1000.0
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: add replay engine env vars to .env.example"
```

---

## Summary

| Chunk | Tasks | What it delivers |
|-------|-------|-----------------|
| 1: Curated Input | 1-2 | `replay_curated_signals()` — clean signal feed with dedup + resolution join |
| 2: Core Engine | 3-6 | `replay_engine.py` — ReplayConfig, gates, replay loop, equity curve, serialization |
| 3: Persistence | 7-8 | `replay_configs` + `replay_runs` tables + CRUD helpers |
| 4: API | 9-11 | `/api/replay/run`, `/configs`, `/runs`, `/compare` endpoints |
| 5: CLI | 12 | `python3 replay_engine.py --bankroll 1000 --min-insider 60` |
| 6: MCP | 13 | `run_replay_simulation` MCP tool with envelope |
| 7: Config | 14-15 | Env var defaults + .env.example |

**Total new tests:** ~45-50 across `test_replay_engine.py` and `test_replay_api.py`

**Build order enforces the key constraint:** curated input layer first (Tasks 1-2), then core engine (Tasks 3-6), then exposure layers (Tasks 9-13). The replay engine never touches raw `signal_history`.

**v1 known limitations (documented in code):**
- All trades resolve instantly (historical resolved_price) — `max_concurrent_positions` and `per-market exposure` gates are no-ops in v1
- `qualifying_roi` gate omitted — `signal_history` doesn't carry wallet-level qROI; future: join `score_history`
- `end_date` guard and `price_drift` gate omitted — runtime-specific, not applicable to historical replay
- `speculator_bot` sub-gate (WR + qROI threshold) omitted — `position_classification` in signal_history is position-level, not wallet-level

**Not in scope (future work):**
- UI tab for replay (existing Strategies tab patterns can be extended later)
- Config diffing UI
- Multi-run sweep automation
- Position lifecycle simulation (open positions that resolve over time vs instant resolution)
- `qualifying_roi` gate via `score_history` join
