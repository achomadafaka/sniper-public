# SwissMiss Source-Position Copy V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the target-only SwissMiss source-position anatomy report and a paper-only `source_position_copy_v1` strategy that enters only after SwissMiss has committed enough capital to a target-slice position.

**Architecture:** Keep `shadow_copy_v1` intact as the row copier. Add a new source-observation store, derive source-position state from observations each cycle, and register a new strategy plugin that emits one paper entry per `(strategy, wallet, condition, asset, trigger_version)`. Add a separate target-anatomy module that describes SwissMiss's target-slice build behavior and selects one pre-registered trigger before paper deployment.

**Tech Stack:** Python, SQLite, pytest, existing `StrategyPlugin` framework, Polymarket activity rows, platform tag metadata from the winning-wallet thesis extractor.

---

## Source Docs

- Spec: `docs/superpowers/specs/2026-05-31-swissmiss-target-position-copy-design.md`
- Strategy precedent: `sniper/strategies/shadow_copy_v1.py`
- Strategy tests precedent: `tests/test_shadow_copy_v1.py`
- Strategy API: `sniper/strategies/base.py`
- Registry: `sniper/strategies/registry.py`
- Existing poller precedent: `scripts/poll_shadow_copy_targets.py`
- Thesis types: `sniper/analysis/winning_wallet_thesis/types.py`

## File Structure

- Create `sniper/strategies/source_position_copy_v1.py`
  - Owns source observation schema, idempotent inserts, source-position aggregation, target filters, threshold detection, strategy plugin, and plugin report.
  - Does not call live APIs.
  - Does not reuse `shadow_copy_pending_signals`.

- Modify `sniper/strategies/registry.py`
  - Registers `SourcePositionCopyV1Strategy`.

- Create `tests/test_source_position_copy_v1.py`
  - Covers observation idempotency, filters, cumulative threshold logic, stale/drift rejection, duplicate prevention, paper-only guard, source-exit policy, registry exposure.

- Create `sniper/analysis/winning_wallet_thesis/target_position_anatomy.py`
  - Pure functions for target-slice historical anatomy and trigger selection.
  - Accepts in-memory source observations and resolution/outcome fields.
  - Produces JSON-serializable report data.

- Create `tests/winning_wallet_thesis/test_target_position_anatomy.py`
  - Covers target-universe counts, threshold grid, default trigger, fallback trigger, no PnL-max threshold selection, exit-value decomposition.

- Create `scripts/poll_source_position_copy_targets.py`
  - Opt-in source observation poller.
  - Dry-run by default.
  - Requires `--enable-live-api` to fetch.
  - Normalizes activity rows into `source_position_observations`.

- Create `tests/test_poll_source_position_copy_targets.py`
  - Covers dry-run no API calls, activity normalization, deterministic activity key, idempotent insert summary.

## Shared Constants

Use these exact names in code and tests:

```python
PLUGIN_ID = "source_position_copy_v1"
SURFACE_VERSION = "source_position_copy_v1"
DEFAULT_TRIGGER_VERSION = "swissmiss_filtered_v1"
SWISSMISS_WALLET = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"
```

Default parameters:

```python
DEFAULTS = {
    "target_wallets": [SWISSMISS_WALLET],
    "accepted_tags": ["sports", "ufc", "cfb", "mlb"],
    "rejected_tags": ["geopolitics", "middle east", "iran", "elections", "macro", "politics"],
    "entry_price_min": 0.30,
    "source_commitment_threshold_usdc": 25000.0,
    "source_commitment_window_hours": 6,
    "source_commitment_mode": "net_buy_cost",
    "max_threshold_age_minutes": 20,
    "max_price_drift_from_source_vwap_pct": 10.0,
    "paper_size_usdc": 25.0,
    "max_concurrent": 15,
    "exit_policy": "hold_to_resolution",
    "material_source_exit_share": 0.50,
    "execution_mode": "paper",
    "trigger_version": DEFAULT_TRIGGER_VERSION,
}
```

---

### Task 1: Source Observation Store

**Files:**
- Create: `sniper/strategies/source_position_copy_v1.py`
- Test: `tests/test_source_position_copy_v1.py`

- [ ] **Step 1: Write failing schema and idempotency tests**

Add this fixture and tests to `tests/test_source_position_copy_v1.py`:

```python
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import init_db  # noqa: E402
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    PLUGIN_ID,
    SWISSMISS_WALLET,
    ensure_source_position_copy_tables,
    insert_source_observation,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    ensure_source_position_copy_tables(c)
    yield c
    c.close()


def _obs(**overrides):
    base = {
        "activity_key": "tx-1:0",
        "wallet": SWISSMISS_WALLET,
        "condition_id": "cond-sports-1",
        "asset_id": "asset-yes",
        "outcome": "YES",
        "side": "BUY",
        "price": 0.55,
        "size": 10000.0,
        "usdc_size": 5500.0,
        "observed_ts": 1_800_000_000,
        "title": "UFC fighter A vs fighter B",
        "event_slug": "ufc-fight-night-1",
        "event_id": "event-1",
        "tags": ["sports", "ufc"],
        "source_endpoint": "data-api-user-activity",
        "raw_json": {"id": "tx-1"},
    }
    base.update(overrides)
    return base


def test_source_observation_table_is_idempotent(conn):
    ensure_source_position_copy_tables(conn)
    ensure_source_position_copy_tables(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_position_observations'"
    ).fetchone()
    assert row is not None


def test_insert_source_observation_dedups_by_activity_key(conn):
    first = insert_source_observation(conn, **_obs())
    second = insert_source_observation(conn, **_obs(price=0.56))

    assert first is not None
    assert second is None

    row = conn.execute(
        "SELECT price, tags_json, raw_json FROM source_position_observations WHERE activity_key='tx-1:0'"
    ).fetchone()
    assert row["price"] == 0.55
    assert json.loads(row["tags_json"]) == ["sports", "ufc"]
    assert json.loads(row["raw_json"]) == {"id": "tx-1"}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_source_observation_table_is_idempotent tests/test_source_position_copy_v1.py::test_insert_source_observation_dedups_by_activity_key -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.strategies.source_position_copy_v1'`.

- [ ] **Step 3: Implement table and insert helper**

Create `sniper/strategies/source_position_copy_v1.py` with:

```python
"""Source-position copy v1.

Paper-only strategy that observes a source wallet's cumulative position and
enters only after a pre-registered commitment threshold crosses.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .base import (
    RESOLUTION_LOSS,
    RESOLUTION_WIN,
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

PLUGIN_ID = "source_position_copy_v1"
SURFACE_VERSION = "source_position_copy_v1"
DEFAULT_TRIGGER_VERSION = "swissmiss_filtered_v1"
SWISSMISS_WALLET = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"

DEFAULTS: dict[str, Any] = {
    "target_wallets": [SWISSMISS_WALLET],
    "accepted_tags": ["sports", "ufc", "cfb", "mlb"],
    "rejected_tags": ["geopolitics", "middle east", "iran", "elections", "macro", "politics"],
    "entry_price_min": 0.30,
    "source_commitment_threshold_usdc": 25000.0,
    "source_commitment_window_hours": 6,
    "source_commitment_mode": "net_buy_cost",
    "max_threshold_age_minutes": 20,
    "max_price_drift_from_source_vwap_pct": 10.0,
    "paper_size_usdc": 25.0,
    "max_concurrent": 15,
    "exit_policy": "hold_to_resolution",
    "material_source_exit_share": 0.50,
    "execution_mode": "paper",
    "trigger_version": DEFAULT_TRIGGER_VERSION,
}


def ensure_source_position_copy_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_position_observations (
            activity_key    TEXT PRIMARY KEY,
            wallet          TEXT NOT NULL,
            condition_id    TEXT NOT NULL,
            asset_id        TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            side            TEXT NOT NULL,
            price           REAL NOT NULL,
            size            REAL NOT NULL,
            usdc_size       REAL NOT NULL,
            observed_ts     INTEGER NOT NULL,
            title           TEXT NOT NULL,
            event_slug      TEXT NOT NULL,
            event_id        TEXT NOT NULL,
            tags_json       TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            raw_json        TEXT NOT NULL,
            created_ts      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_position_observations_wallet_time
          ON source_position_observations(wallet, observed_ts)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_position_observations_position
          ON source_position_observations(wallet, event_id, condition_id, asset_id, observed_ts)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_position_copy_state (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id             INTEGER NOT NULL,
            wallet                  TEXT NOT NULL,
            event_id                TEXT NOT NULL,
            condition_id            TEXT NOT NULL,
            asset_id                TEXT NOT NULL,
            trigger_version         TEXT NOT NULL,
            state                   TEXT NOT NULL,
            reason                  TEXT NOT NULL DEFAULT '',
            threshold_cross_ts      INTEGER,
            threshold_cross_price   REAL,
            source_vwap_at_cross    REAL,
            source_net_cost_at_cross REAL,
            source_net_shares_at_cross REAL,
            entered_position_id     TEXT,
            updated_ts              INTEGER NOT NULL,
            UNIQUE(strategy_id, wallet, condition_id, asset_id, trigger_version)
        )
        """
    )
    conn.commit()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def insert_source_observation(
    conn: sqlite3.Connection,
    *,
    activity_key: str,
    wallet: str,
    condition_id: str,
    asset_id: str,
    outcome: str,
    side: str,
    price: float,
    size: float,
    usdc_size: float,
    observed_ts: int,
    title: str,
    event_slug: str,
    event_id: str,
    tags: list[str],
    source_endpoint: str,
    raw_json: dict[str, Any],
) -> int | None:
    ensure_source_position_copy_tables(conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO source_position_observations
              (activity_key, wallet, condition_id, asset_id, outcome, side, price,
               size, usdc_size, observed_ts, title, event_slug, event_id, tags_json,
               source_endpoint, raw_json, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(activity_key),
                str(wallet).lower().strip(),
                str(condition_id),
                str(asset_id),
                str(outcome).upper(),
                str(side).upper(),
                float(price),
                float(size),
                float(usdc_size),
                int(observed_ts),
                str(title or ""),
                str(event_slug or ""),
                str(event_id or ""),
                _json_dumps([str(t).lower().strip() for t in tags if str(t).strip()]),
                str(source_endpoint or ""),
                _json_dumps(raw_json or {}),
                int(time.time()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_source_observation_table_is_idempotent tests/test_source_position_copy_v1.py::test_insert_source_observation_dedups_by_activity_key -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/strategies/source_position_copy_v1.py tests/test_source_position_copy_v1.py
git commit -m "feat(strategy): add source-position observation store"
```

---

### Task 2: Target Filters and Source-Position Aggregation

**Files:**
- Modify: `sniper/strategies/source_position_copy_v1.py`
- Test: `tests/test_source_position_copy_v1.py`

- [ ] **Step 1: Write failing filter and aggregation tests**

Append to `tests/test_source_position_copy_v1.py`:

```python
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    SourceObservation,
    SourcePositionState,
    _build_source_positions,
    _matches_target_filter,
    _parse_params,
)


def _source_observation(**overrides) -> SourceObservation:
    data = _obs()
    data.update(overrides)
    return SourceObservation(
        activity_key=data["activity_key"],
        wallet=data["wallet"],
        condition_id=data["condition_id"],
        asset_id=data["asset_id"],
        outcome=data["outcome"],
        side=data["side"],
        price=data["price"],
        size=data["size"],
        usdc_size=data["usdc_size"],
        observed_ts=data["observed_ts"],
        title=data["title"],
        event_slug=data["event_slug"],
        event_id=data["event_id"],
        tags=data["tags"],
        source_endpoint=data["source_endpoint"],
        raw_json=data["raw_json"],
    )


def test_target_filter_accepts_sports_and_rejects_geopolitics():
    params = _parse_params({"params": {}})
    sports = _source_observation(tags=["sports", "ufc"], price=0.44)
    geopolitics = _source_observation(tags=["sports", "iran"], price=0.44)

    assert _matches_target_filter(sports, params) == (True, "")
    assert _matches_target_filter(geopolitics, params) == (False, "rejected_tag:iran")


def test_target_filter_rejects_missing_tags_and_low_price():
    params = _parse_params({"params": {}})

    missing_tags = _source_observation(tags=[], price=0.44)
    low_price = _source_observation(tags=["sports"], price=0.20)

    assert _matches_target_filter(missing_tags, params) == (False, "missing_tags")
    assert _matches_target_filter(low_price, params) == (False, "entry_price_below_min:0.2")


def test_build_source_position_aggregates_adds_sells_and_vwap():
    observations = [
        _source_observation(activity_key="a", price=0.50, size=10000, usdc_size=5000, observed_ts=100),
        _source_observation(activity_key="b", price=0.60, size=20000, usdc_size=12000, observed_ts=200),
        _source_observation(activity_key="c", side="SELL", price=0.70, size=5000, usdc_size=3500, observed_ts=300),
    ]

    positions = _build_source_positions(observations)

    key = (SWISSMISS_WALLET, "event-1", "cond-sports-1", "asset-yes")
    state = positions[key]
    assert state.buy_cost == 17000
    assert state.sell_proceeds == 3500
    assert state.net_buy_cost == 13500
    assert state.buy_shares == 30000
    assert state.sell_shares == 5000
    assert state.net_shares == 25000
    assert round(state.buy_vwap, 4) == 0.5667
    assert state.build_style == "averaged_up"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_target_filter_accepts_sports_and_rejects_geopolitics tests/test_source_position_copy_v1.py::test_build_source_position_aggregates_adds_sells_and_vwap -q
```

Expected: FAIL with missing `SourceObservation` or `_build_source_positions`.

- [ ] **Step 3: Implement data types, params, filter, and aggregation**

Add to `sniper/strategies/source_position_copy_v1.py` after `DEFAULTS`:

```python
@dataclass(frozen=True)
class SourceObservation:
    activity_key: str
    wallet: str
    condition_id: str
    asset_id: str
    outcome: str
    side: str
    price: float
    size: float
    usdc_size: float
    observed_ts: int
    title: str
    event_slug: str
    event_id: str
    tags: list[str] = field(default_factory=list)
    source_endpoint: str = ""
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourcePositionState:
    wallet: str
    event_id: str
    condition_id: str
    asset_id: str
    outcome: str
    title: str
    event_slug: str
    tags: list[str]
    first_buy_ts: int | None = None
    first_buy_price: float = 0.0
    last_observed_ts: int = 0
    buy_cost: float = 0.0
    sell_proceeds: float = 0.0
    buy_shares: float = 0.0
    sell_shares: float = 0.0
    observations: list[SourceObservation] = field(default_factory=list)

    @property
    def net_buy_cost(self) -> float:
        return self.buy_cost - self.sell_proceeds

    @property
    def net_shares(self) -> float:
        return self.buy_shares - self.sell_shares

    @property
    def buy_vwap(self) -> float:
        return self.buy_cost / self.buy_shares if self.buy_shares > 0 else 0.0

    @property
    def build_style(self) -> str:
        buys = [o for o in self.observations if o.side == "BUY"]
        if len(buys) <= 1:
            return "single_shot"
        prices = [o.price for o in buys]
        if prices[-1] > prices[0]:
            return "averaged_up"
        if prices[-1] < prices[0]:
            return "averaged_down"
        return "flat_add"


def _parse_params(strategy: dict[str, Any]) -> dict[str, Any]:
    raw = strategy.get("params") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DEFAULTS, **raw}
    return merged


def _normalized_tags(tags: list[str]) -> set[str]:
    return {str(t).lower().strip() for t in tags if str(t).strip()}


def _matches_target_filter(obs: SourceObservation, params: dict[str, Any]) -> tuple[bool, str]:
    tags = _normalized_tags(obs.tags)
    if not tags:
        return False, "missing_tags"
    rejected = _normalized_tags(list(params.get("rejected_tags") or []))
    hit_rejected = sorted(tags & rejected)
    if hit_rejected:
        return False, f"rejected_tag:{hit_rejected[0]}"
    accepted = _normalized_tags(list(params.get("accepted_tags") or []))
    if accepted and not (tags & accepted):
        return False, "accepted_tag_missing"
    if not obs.asset_id:
        return False, "missing_asset_id"
    if float(obs.price) < float(params.get("entry_price_min") or 0.0):
        return False, f"entry_price_below_min:{float(obs.price):g}"
    return True, ""


def _position_key(obs: SourceObservation) -> tuple[str, str, str, str]:
    return (
        obs.wallet.lower().strip(),
        str(obs.event_id or obs.event_slug or ""),
        str(obs.condition_id),
        str(obs.asset_id),
    )


def _build_source_positions(
    observations: list[SourceObservation],
) -> dict[tuple[str, str, str, str], SourcePositionState]:
    positions: dict[tuple[str, str, str, str], SourcePositionState] = {}
    for obs in sorted(observations, key=lambda o: (int(o.observed_ts), str(o.activity_key))):
        key = _position_key(obs)
        state = positions.get(key)
        if state is None:
            state = SourcePositionState(
                wallet=obs.wallet.lower().strip(),
                event_id=str(obs.event_id or obs.event_slug or ""),
                condition_id=str(obs.condition_id),
                asset_id=str(obs.asset_id),
                outcome=str(obs.outcome).upper(),
                title=obs.title,
                event_slug=obs.event_slug,
                tags=list(obs.tags),
            )
            positions[key] = state
        state.observations.append(obs)
        state.last_observed_ts = max(state.last_observed_ts, int(obs.observed_ts))
        if obs.side == "BUY":
            if state.first_buy_ts is None:
                state.first_buy_ts = int(obs.observed_ts)
                state.first_buy_price = float(obs.price)
            state.buy_cost += float(obs.usdc_size)
            state.buy_shares += float(obs.size)
        elif obs.side == "SELL":
            state.sell_proceeds += float(obs.usdc_size)
            state.sell_shares += float(obs.size)
    return positions
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_target_filter_accepts_sports_and_rejects_geopolitics tests/test_source_position_copy_v1.py::test_target_filter_rejects_missing_tags_and_low_price tests/test_source_position_copy_v1.py::test_build_source_position_aggregates_adds_sells_and_vwap -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/strategies/source_position_copy_v1.py tests/test_source_position_copy_v1.py
git commit -m "feat(strategy): aggregate source-position state"
```

---

### Task 3: Threshold Trigger Engine

**Files:**
- Modify: `sniper/strategies/source_position_copy_v1.py`
- Test: `tests/test_source_position_copy_v1.py`

- [ ] **Step 1: Write failing threshold tests**

Append to `tests/test_source_position_copy_v1.py`:

```python
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    _detect_threshold_crossing,
)


def _state_from(observations: list[SourceObservation]) -> SourcePositionState:
    return next(iter(_build_source_positions(observations).values()))


def test_threshold_does_not_fire_on_probe_buy():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000}})
    state = _state_from([
        _source_observation(activity_key="probe", price=0.50, size=5000, usdc_size=2500, observed_ts=100),
    ])

    trigger = _detect_threshold_crossing(state, params, now_ts=200)

    assert trigger.should_enter is False
    assert trigger.reason == "threshold_not_crossed"


def test_threshold_fires_after_cumulative_adds():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000, "source_commitment_window_hours": 6}})
    state = _state_from([
        _source_observation(activity_key="a", price=0.50, size=20000, usdc_size=10000, observed_ts=100),
        _source_observation(activity_key="b", price=0.60, size=25000, usdc_size=15000, observed_ts=200),
    ])

    trigger = _detect_threshold_crossing(state, params, now_ts=250)

    assert trigger.should_enter is True
    assert trigger.reason == "threshold_crossed"
    assert trigger.cross_ts == 200
    assert trigger.net_buy_cost == 25000
    assert round(trigger.source_vwap, 4) == 0.5556


def test_sells_before_threshold_reduce_net_commitment():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000}})
    state = _state_from([
        _source_observation(activity_key="a", price=0.50, size=30000, usdc_size=15000, observed_ts=100),
        _source_observation(activity_key="b", side="SELL", price=0.55, size=10000, usdc_size=5500, observed_ts=150),
        _source_observation(activity_key="c", price=0.60, size=25000, usdc_size=15000, observed_ts=200),
    ])

    trigger = _detect_threshold_crossing(state, params, now_ts=250)

    assert trigger.should_enter is False
    assert trigger.reason == "threshold_not_crossed"


def test_stale_threshold_crossing_rejects():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000, "max_threshold_age_minutes": 20}})
    state = _state_from([
        _source_observation(activity_key="a", price=0.50, size=50000, usdc_size=25000, observed_ts=100),
    ])

    trigger = _detect_threshold_crossing(state, params, now_ts=100 + 21 * 60)

    assert trigger.should_enter is False
    assert trigger.reason.startswith("threshold_crossing_stale:")


def test_price_drift_rejects():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000, "max_price_drift_from_source_vwap_pct": 10}})
    state = _state_from([
        _source_observation(activity_key="a", price=0.50, size=50000, usdc_size=25000, observed_ts=100),
    ])

    trigger = _detect_threshold_crossing(state, params, now_ts=110, copy_price=0.60)

    assert trigger.should_enter is False
    assert trigger.reason == "price_drift_exceeded:20.00%"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_threshold_does_not_fire_on_probe_buy tests/test_source_position_copy_v1.py::test_threshold_fires_after_cumulative_adds tests/test_source_position_copy_v1.py::test_sells_before_threshold_reduce_net_commitment tests/test_source_position_copy_v1.py::test_stale_threshold_crossing_rejects tests/test_source_position_copy_v1.py::test_price_drift_rejects -q
```

Expected: FAIL with missing `_detect_threshold_crossing`.

- [ ] **Step 3: Implement trigger data type and detector**

Add to `sniper/strategies/source_position_copy_v1.py`:

```python
@dataclass(frozen=True)
class ThresholdTrigger:
    should_enter: bool
    reason: str
    cross_ts: int | None = None
    cross_price: float = 0.0
    source_vwap: float = 0.0
    net_buy_cost: float = 0.0
    net_shares: float = 0.0


def _detect_threshold_crossing(
    state: SourcePositionState,
    params: dict[str, Any],
    *,
    now_ts: int,
    copy_price: float | None = None,
) -> ThresholdTrigger:
    threshold = float(params.get("source_commitment_threshold_usdc") or 0.0)
    max_window_seconds = int(float(params.get("source_commitment_window_hours") or 0.0) * 3600)
    max_age_seconds = int(float(params.get("max_threshold_age_minutes") or 0.0) * 60)
    max_drift_pct = float(params.get("max_price_drift_from_source_vwap_pct") or 0.0)

    buy_cost = 0.0
    sell_proceeds = 0.0
    buy_shares = 0.0
    sell_shares = 0.0
    first_buy_ts = state.first_buy_ts

    for obs in sorted(state.observations, key=lambda o: (int(o.observed_ts), str(o.activity_key))):
        if obs.side == "BUY":
            buy_cost += float(obs.usdc_size)
            buy_shares += float(obs.size)
            if first_buy_ts is None:
                first_buy_ts = int(obs.observed_ts)
        elif obs.side == "SELL":
            sell_proceeds += float(obs.usdc_size)
            sell_shares += float(obs.size)

        net_cost = buy_cost - sell_proceeds
        if net_cost < threshold:
            continue

        cross_ts = int(obs.observed_ts)
        if first_buy_ts is not None and max_window_seconds > 0 and cross_ts - int(first_buy_ts) > max_window_seconds:
            return ThresholdTrigger(False, f"threshold_outside_window:{cross_ts - int(first_buy_ts)}s")
        if max_age_seconds > 0 and int(now_ts) - cross_ts > max_age_seconds:
            return ThresholdTrigger(False, f"threshold_crossing_stale:{int(now_ts) - cross_ts}s")

        vwap = buy_cost / buy_shares if buy_shares > 0 else 0.0
        observed_copy_price = float(copy_price) if copy_price is not None else float(obs.price)
        if vwap > 0 and max_drift_pct > 0:
            drift_pct = abs(observed_copy_price - vwap) / vwap * 100.0
            if drift_pct > max_drift_pct:
                return ThresholdTrigger(False, f"price_drift_exceeded:{drift_pct:.2f}%")

        return ThresholdTrigger(
            True,
            "threshold_crossed",
            cross_ts=cross_ts,
            cross_price=observed_copy_price,
            source_vwap=vwap,
            net_buy_cost=net_cost,
            net_shares=buy_shares - sell_shares,
        )

    return ThresholdTrigger(False, "threshold_not_crossed")
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_threshold_does_not_fire_on_probe_buy tests/test_source_position_copy_v1.py::test_threshold_fires_after_cumulative_adds tests/test_source_position_copy_v1.py::test_sells_before_threshold_reduce_net_commitment tests/test_source_position_copy_v1.py::test_stale_threshold_crossing_rejects tests/test_source_position_copy_v1.py::test_price_drift_rejects -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/strategies/source_position_copy_v1.py tests/test_source_position_copy_v1.py
git commit -m "feat(strategy): detect source commitment threshold"
```

---

### Task 4: Copy-State Idempotency and Conflict Guards

**Files:**
- Modify: `sniper/strategies/source_position_copy_v1.py`
- Test: `tests/test_source_position_copy_v1.py`

- [ ] **Step 1: Write failing state and conflict tests**

Append to `tests/test_source_position_copy_v1.py`:

```python
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    _already_entered,
    _has_opposite_side_conflict,
    record_copy_state,
)


def test_already_entered_state_blocks_duplicate_entry(conn):
    record_copy_state(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        event_id="event-1",
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
        state="entered",
        reason="threshold_crossed",
    )

    assert _already_entered(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
    )


def test_source_flip_to_opposite_outcome_rejects_for_v1():
    observations = [
        _source_observation(activity_key="a", asset_id="asset-yes", outcome="YES", side="BUY", usdc_size=30000),
        _source_observation(activity_key="b", asset_id="asset-no", outcome="NO", side="BUY", usdc_size=30000),
    ]

    assert _has_opposite_side_conflict(observations) == (True, "opposite_side_conflict")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_already_entered_state_blocks_duplicate_entry tests/test_source_position_copy_v1.py::test_source_flip_to_opposite_outcome_rejects_for_v1 -q
```

Expected: FAIL with missing `record_copy_state` or `_has_opposite_side_conflict`.

- [ ] **Step 3: Implement state helpers and side conflict guard**

Add to `sniper/strategies/source_position_copy_v1.py`:

```python
def record_copy_state(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    wallet: str,
    event_id: str,
    condition_id: str,
    asset_id: str,
    trigger_version: str,
    state: str,
    reason: str,
    trigger: ThresholdTrigger | None = None,
    entered_position_id: str | None = None,
) -> None:
    ensure_source_position_copy_tables(conn)
    conn.execute(
        """
        INSERT INTO source_position_copy_state
          (strategy_id, wallet, event_id, condition_id, asset_id, trigger_version,
           state, reason, threshold_cross_ts, threshold_cross_price, source_vwap_at_cross,
           source_net_cost_at_cross, source_net_shares_at_cross, entered_position_id, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, wallet, condition_id, asset_id, trigger_version)
        DO UPDATE SET
          state=excluded.state,
          reason=excluded.reason,
          threshold_cross_ts=excluded.threshold_cross_ts,
          threshold_cross_price=excluded.threshold_cross_price,
          source_vwap_at_cross=excluded.source_vwap_at_cross,
          source_net_cost_at_cross=excluded.source_net_cost_at_cross,
          source_net_shares_at_cross=excluded.source_net_shares_at_cross,
          entered_position_id=COALESCE(excluded.entered_position_id, source_position_copy_state.entered_position_id),
          updated_ts=excluded.updated_ts
        """,
        (
            int(strategy_id),
            str(wallet).lower().strip(),
            str(event_id or ""),
            str(condition_id),
            str(asset_id),
            str(trigger_version),
            str(state),
            str(reason),
            trigger.cross_ts if trigger else None,
            trigger.cross_price if trigger else None,
            trigger.source_vwap if trigger else None,
            trigger.net_buy_cost if trigger else None,
            trigger.net_shares if trigger else None,
            entered_position_id,
            int(time.time()),
        ),
    )
    conn.commit()


def _already_entered(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    wallet: str,
    condition_id: str,
    asset_id: str,
    trigger_version: str,
) -> bool:
    ensure_source_position_copy_tables(conn)
    row = conn.execute(
        """
        SELECT 1 FROM source_position_copy_state
        WHERE strategy_id=? AND wallet=? AND condition_id=? AND asset_id=?
          AND trigger_version=? AND state IN ('entered', 'source_reduced', 'source_exited', 'resolved')
        LIMIT 1
        """,
        (int(strategy_id), str(wallet).lower().strip(), str(condition_id), str(asset_id), str(trigger_version)),
    ).fetchone()
    return row is not None


def _has_opposite_side_conflict(observations: list[SourceObservation]) -> tuple[bool, str]:
    bought_assets = {
        (str(o.condition_id), str(o.asset_id))
        for o in observations
        if str(o.side).upper() == "BUY" and float(o.usdc_size) > 0
    }
    condition_to_assets: dict[str, set[str]] = {}
    for condition_id, asset_id in bought_assets:
        condition_to_assets.setdefault(condition_id, set()).add(asset_id)
    if any(len(asset_ids) > 1 for asset_ids in condition_to_assets.values()):
        return True, "opposite_side_conflict"
    return False, ""
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_already_entered_state_blocks_duplicate_entry tests/test_source_position_copy_v1.py::test_source_flip_to_opposite_outcome_rejects_for_v1 -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/strategies/source_position_copy_v1.py tests/test_source_position_copy_v1.py
git commit -m "feat(strategy): add source-position copy state"
```

---

### Task 5: Strategy Plugin and Registry

**Files:**
- Modify: `sniper/strategies/source_position_copy_v1.py`
- Modify: `sniper/strategies/registry.py`
- Test: `tests/test_source_position_copy_v1.py`

- [ ] **Step 1: Write failing plugin tests**

Append to `tests/test_source_position_copy_v1.py`:

```python
from sniper.strategies.base import StrategyManageState  # noqa: E402
from sniper.strategies.registry import get_strategy_plugin  # noqa: E402
from sniper.strategies.source_position_copy_v1 import SourcePositionCopyV1Strategy  # noqa: E402


def _strategy(params=None, strategy_id=101):
    return {
        "id": strategy_id,
        "wallet": "paper-wallet",
        "label": "SwissMiss Copy - Filtered",
        "strategy_type": PLUGIN_ID,
        "params": params or {},
        "status": "active",
    }


def _insert_source_obs(conn, **kwargs):
    return insert_source_observation(conn, **_obs(**kwargs))


def test_discover_emits_signal_after_source_threshold(conn):
    _insert_source_obs(conn, activity_key="a", price=0.50, size=20000, usdc_size=10000, observed_ts=int(time.time()) - 60)
    _insert_source_obs(conn, activity_key="b", price=0.60, size=25000, usdc_size=15000, observed_ts=int(time.time()) - 30)

    plugin = SourcePositionCopyV1Strategy()
    signals = plugin.discover(conn, _strategy())

    assert len(signals) == 1
    assert signals[0].wallet == SWISSMISS_WALLET
    assert signals[0].condition_id == "cond-sports-1"
    assert signals[0].current_price == 0.60
    assert signals[0].position_value == 25000
    assert signals[0].metadata["source_position_mode"] == "net_buy_cost"


def test_enter_rejects_non_paper_execution_mode(conn):
    plugin = SourcePositionCopyV1Strategy()
    signal = StrategySignal(
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        outcome="YES",
        title="UFC",
        current_price=0.60,
        position_value=25000,
        metadata={"asset_id": "asset-yes"},
    )

    decision = plugin.enter(conn, _strategy({"execution_mode": "live"}), signal, [])

    assert decision.should_enter is False
    assert decision.reason == "non_paper_execution_rejected"


def test_enter_returns_fixed_paper_size_and_marks_entered(conn):
    plugin = SourcePositionCopyV1Strategy()
    signal = StrategySignal(
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        outcome="YES",
        title="UFC",
        current_price=0.60,
        position_value=25000,
        metadata={
            "asset_id": "asset-yes",
            "event_id": "event-1",
            "trigger_version": "swissmiss_filtered_v1",
            "threshold_cross_ts": 100,
            "source_vwap": 0.5555,
            "source_net_cost": 25000,
            "source_net_shares": 45000,
        },
    )

    decision = plugin.enter(conn, _strategy({"paper_size_usdc": 25}), signal, [])

    assert decision.should_enter is True
    assert decision.execution_mode == "paper"
    assert decision.size_usdc == 25
    assert decision.reason == "source_position_threshold_crossed"
    assert _already_entered(
        conn,
        strategy_id=101,
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
    )


def test_hold_to_resolution_exit_stays_open_until_resolution(conn):
    plugin = SourcePositionCopyV1Strategy()
    strategy = _strategy({"exit_policy": "hold_to_resolution"})

    assert plugin.exit(conn, strategy, {"current_price": 0.55}, StrategyManageState()).should_exit is False
    assert plugin.exit(conn, strategy, {"current_price": 0.98}, StrategyManageState()).reason == "resolution_win"
    assert plugin.exit(conn, strategy, {"current_price": 0.02}, StrategyManageState()).reason == "resolution_loss"


def test_source_exit_policy_closes_on_material_source_reduction(conn):
    plugin = SourcePositionCopyV1Strategy()
    strategy = _strategy({"exit_policy": "source_exit", "material_source_exit_share": 0.50})
    state = StrategyManageState(current_price=0.54, metadata={"source_exit_share": 0.55})

    decision = plugin.exit(conn, strategy, {"current_price": 0.54}, state)

    assert decision.should_exit is True
    assert decision.reason == "source_material_exit"


def test_registry_exposes_source_position_copy_v1():
    plugin = get_strategy_plugin(PLUGIN_ID)
    assert isinstance(plugin, SourcePositionCopyV1Strategy)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py::test_discover_emits_signal_after_source_threshold tests/test_source_position_copy_v1.py::test_enter_rejects_non_paper_execution_mode tests/test_source_position_copy_v1.py::test_registry_exposes_source_position_copy_v1 -q
```

Expected: FAIL with missing `SourcePositionCopyV1Strategy` or registry not exposing plugin.

- [ ] **Step 3: Implement plugin**

Add to `sniper/strategies/source_position_copy_v1.py`:

```python
def _load_observations(conn: sqlite3.Connection, wallets: set[str]) -> list[SourceObservation]:
    if not wallets:
        return []
    query_marks = ",".join("?" for _ in wallets)
    rows = conn.execute(
        f"""
        SELECT activity_key, wallet, condition_id, asset_id, outcome, side, price,
               size, usdc_size, observed_ts, title, event_slug, event_id, tags_json,
               source_endpoint, raw_json
        FROM source_position_observations
        WHERE wallet IN ({query_marks})
        ORDER BY observed_ts ASC, activity_key ASC
        """,
        tuple(sorted(wallets)),
    ).fetchall()
    observations: list[SourceObservation] = []
    for row in rows:
        tags = json.loads(row["tags_json"] or "[]")
        raw = json.loads(row["raw_json"] or "{}")
        observations.append(
            SourceObservation(
                activity_key=row["activity_key"],
                wallet=row["wallet"],
                condition_id=row["condition_id"],
                asset_id=row["asset_id"],
                outcome=row["outcome"],
                side=row["side"],
                price=float(row["price"]),
                size=float(row["size"]),
                usdc_size=float(row["usdc_size"]),
                observed_ts=int(row["observed_ts"]),
                title=row["title"],
                event_slug=row["event_slug"],
                event_id=row["event_id"],
                tags=list(tags),
                source_endpoint=row["source_endpoint"],
                raw_json=dict(raw),
            )
        )
    return observations


def _open_position_count(conn: sqlite3.Connection, strategy_id: int) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE strategy_id=? AND state='open'",
            (int(strategy_id),),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


class SourcePositionCopyV1Strategy(StrategyPlugin):
    plugin_id = PLUGIN_ID
    display_name = "SwissMiss Copy - Filtered"
    description = "Paper-only source-position copy after source commitment threshold crossing."
    strategy_family = "paper_copy"
    supported_execution_modes = ("paper",)
    required_tables = ("source_position_observations", "source_position_copy_state")
    default_loop_seconds = 60

    def validate_parameters(self, parameters: dict) -> list[str]:
        params = _parse_params({"params": parameters})
        errors: list[str] = []
        if params.get("execution_mode") != "paper":
            errors.append("execution_mode must be paper")
        if float(params.get("source_commitment_threshold_usdc") or 0.0) <= 0:
            errors.append("source_commitment_threshold_usdc must be positive")
        if float(params.get("paper_size_usdc") or 0.0) <= 0:
            errors.append("paper_size_usdc must be positive")
        return errors

    def discover(self, conn: sqlite3.Connection, strategy: dict[str, Any]) -> list[StrategySignal]:
        ensure_source_position_copy_tables(conn)
        params = _parse_params(strategy)
        strategy_id = int(strategy.get("id") or 0)
        trigger_version = str(params.get("trigger_version") or DEFAULT_TRIGGER_VERSION)
        target_wallets = {str(w).lower().strip() for w in params.get("target_wallets") or [] if w}
        observations = _load_observations(conn, target_wallets)

        conflict, conflict_reason = _has_opposite_side_conflict(observations)
        if conflict:
            for state in _build_source_positions(observations).values():
                record_copy_state(
                    conn,
                    strategy_id=strategy_id,
                    wallet=state.wallet,
                    event_id=state.event_id,
                    condition_id=state.condition_id,
                    asset_id=state.asset_id,
                    trigger_version=trigger_version,
                    state="rejected",
                    reason=conflict_reason,
                )
            return []

        now_ts = int(time.time())
        signals: list[StrategySignal] = []
        for state in _build_source_positions(observations).values():
            first_obs = state.observations[0] if state.observations else None
            if first_obs is None:
                continue
            passes, reason = _matches_target_filter(first_obs, params)
            if not passes:
                record_copy_state(
                    conn,
                    strategy_id=strategy_id,
                    wallet=state.wallet,
                    event_id=state.event_id,
                    condition_id=state.condition_id,
                    asset_id=state.asset_id,
                    trigger_version=trigger_version,
                    state="rejected",
                    reason=reason,
                )
                continue
            if _already_entered(
                conn,
                strategy_id=strategy_id,
                wallet=state.wallet,
                condition_id=state.condition_id,
                asset_id=state.asset_id,
                trigger_version=trigger_version,
            ):
                continue
            trigger = _detect_threshold_crossing(state, params, now_ts=now_ts)
            if not trigger.should_enter:
                record_copy_state(
                    conn,
                    strategy_id=strategy_id,
                    wallet=state.wallet,
                    event_id=state.event_id,
                    condition_id=state.condition_id,
                    asset_id=state.asset_id,
                    trigger_version=trigger_version,
                    state="tracking" if trigger.reason == "threshold_not_crossed" else "rejected",
                    reason=trigger.reason,
                    trigger=trigger,
                )
                continue
            signals.append(
                StrategySignal(
                    wallet=state.wallet,
                    condition_id=state.condition_id,
                    outcome=state.outcome,
                    title=state.title,
                    current_price=trigger.cross_price,
                    position_value=trigger.net_buy_cost,
                    analyzed_ts=trigger.cross_ts or now_ts,
                    metadata={
                        "asset_id": state.asset_id,
                        "event_id": state.event_id,
                        "event_slug": state.event_slug,
                        "tags": list(state.tags),
                        "trigger_version": trigger_version,
                        "threshold_cross_ts": trigger.cross_ts,
                        "source_vwap": trigger.source_vwap,
                        "source_net_cost": trigger.net_buy_cost,
                        "source_net_shares": trigger.net_shares,
                        "source_position_mode": params.get("source_commitment_mode"),
                        "price_source": "source_price_proxy",
                    },
                )
            )
        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        signal: StrategySignal,
        open_positions: list[dict[str, Any]],
    ) -> StrategyEntryDecision:
        ensure_source_position_copy_tables(conn)
        params = _parse_params(strategy)
        if params.get("execution_mode") != "paper":
            return StrategyEntryDecision(False, "non_paper_execution_rejected")
        strategy_id = int(strategy.get("id") or 0)
        max_concurrent = int(params.get("max_concurrent") or 0)
        if strategy_id and max_concurrent > 0 and _open_position_count(conn, strategy_id) >= max_concurrent:
            return StrategyEntryDecision(False, f"max_concurrent={max_concurrent}_reached")

        asset_id = str(signal.metadata.get("asset_id") or "")
        event_id = str(signal.metadata.get("event_id") or "")
        trigger_version = str(signal.metadata.get("trigger_version") or params.get("trigger_version") or DEFAULT_TRIGGER_VERSION)
        trigger = ThresholdTrigger(
            True,
            "threshold_crossed",
            cross_ts=signal.metadata.get("threshold_cross_ts"),
            cross_price=float(signal.current_price),
            source_vwap=float(signal.metadata.get("source_vwap") or 0.0),
            net_buy_cost=float(signal.metadata.get("source_net_cost") or signal.position_value),
            net_shares=float(signal.metadata.get("source_net_shares") or 0.0),
        )
        record_copy_state(
            conn,
            strategy_id=strategy_id,
            wallet=signal.wallet,
            event_id=event_id,
            condition_id=signal.condition_id,
            asset_id=asset_id,
            trigger_version=trigger_version,
            state="entered",
            reason="threshold_crossed",
            trigger=trigger,
        )
        return StrategyEntryDecision(
            True,
            "source_position_threshold_crossed",
            size_usdc=float(params.get("paper_size_usdc") or 0.0),
            entry_price=float(signal.current_price),
            execution_mode="paper",
            metadata={
                **signal.metadata,
                "target_wallet": signal.wallet,
                "source_position_copy": True,
            },
        )

    def manage(self, conn: sqlite3.Connection, strategy: dict[str, Any], position: dict[str, Any]) -> StrategyManageState:
        return StrategyManageState(
            current_price=float(position["current_price"]) if position.get("current_price") is not None else None,
            signal_open=True,
            metadata=dict(position.get("metadata") or {}),
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        position: dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        current_price = float(position.get("current_price") if position.get("current_price") is not None else state.current_price or 0.0)
        if current_price >= RESOLUTION_WIN:
            return StrategyExitDecision(True, "resolution_win", exit_price=current_price, status="resolution_win")
        if current_price <= RESOLUTION_LOSS:
            return StrategyExitDecision(True, "resolution_loss", exit_price=current_price, status="resolution_loss")
        if params.get("exit_policy") == "source_exit":
            exit_share = float((state.metadata or {}).get("source_exit_share") or 0.0)
            if exit_share >= float(params.get("material_source_exit_share") or 0.50):
                return StrategyExitDecision(True, "source_material_exit", exit_price=current_price, status="early_exit")
        return StrategyExitDecision(False, "holding")

    def report(self, conn: sqlite3.Connection, strategy: dict[str, Any], positions: list[dict[str, Any]]) -> StrategyReport:
        ensure_source_position_copy_tables(conn)
        strategy_id = int(strategy.get("id") or 0)
        rows = conn.execute(
            """
            SELECT state, reason, COUNT(*) AS n
            FROM source_position_copy_state
            WHERE strategy_id=?
            GROUP BY state, reason
            ORDER BY state, reason
            """,
            (strategy_id,),
        ).fetchall()
        return StrategyReport(
            summary={
                "plugin_id": PLUGIN_ID,
                "source_positions": sum(int(r["n"]) for r in rows),
                "state_counts": {f"{r['state']}:{r['reason']}": int(r["n"]) for r in rows},
                "paper_positions": len(positions),
            }
        )
```

- [ ] **Step 4: Register plugin**

Modify `sniper/strategies/registry.py`:

```python
from .source_position_copy_v1 import SourcePositionCopyV1Strategy
```

Add to `_PLUGINS`:

```python
    SourcePositionCopyV1Strategy.plugin_id: SourcePositionCopyV1Strategy(),
```

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sniper/strategies/source_position_copy_v1.py sniper/strategies/registry.py tests/test_source_position_copy_v1.py
git commit -m "feat(strategy): add source-position copy plugin"
```

---

### Task 6: Target-Position Anatomy Module

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/target_position_anatomy.py`
- Test: `tests/winning_wallet_thesis/test_target_position_anatomy.py`

- [ ] **Step 1: Write failing anatomy tests**

Create `tests/winning_wallet_thesis/test_target_position_anatomy.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.winning_wallet_thesis.target_position_anatomy import (  # noqa: E402
    TargetAnatomyConfig,
    build_target_position_anatomy,
)
from sniper.strategies.source_position_copy_v1 import SourceObservation, SWISSMISS_WALLET  # noqa: E402


def _obs(activity_key, *, price, usdc_size, size=None, ts=100, tags=None, side="BUY", condition_id="cond-1", won=True):
    return SourceObservation(
        activity_key=activity_key,
        wallet=SWISSMISS_WALLET,
        condition_id=condition_id,
        asset_id="asset-yes",
        outcome="YES",
        side=side,
        price=price,
        size=size if size is not None else usdc_size / price,
        usdc_size=usdc_size,
        observed_ts=ts,
        title="UFC fight",
        event_slug="ufc-fight-night-1",
        event_id="event-1",
        tags=tags or ["sports", "ufc"],
        source_endpoint="fixture",
        raw_json={"won": won},
    )


def test_anatomy_filters_target_universe_and_counts_exclusions():
    observations = [
        _obs("sports", price=0.50, usdc_size=25000, tags=["sports", "ufc"]),
        _obs("iran", price=0.50, usdc_size=25000, tags=["geopolitics", "iran"], condition_id="cond-2"),
        _obs("low", price=0.20, usdc_size=25000, tags=["sports"], condition_id="cond-3"),
    ]

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig())

    assert report["target_universe"]["included_positions"] == 1
    assert report["target_universe"]["excluded_by_reason"]["rejected_tag:iran"] == 1
    assert report["target_universe"]["excluded_by_reason"]["entry_price_below_min:0.2"] == 1


def test_anatomy_threshold_grid_reports_crossing_frequency_and_roi():
    observations = [
        _obs("a", price=0.50, usdc_size=10000, ts=100),
        _obs("b", price=0.60, usdc_size=15000, ts=200),
        _obs("c", price=0.70, usdc_size=10000, ts=300),
    ]

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig())
    row = report["threshold_grid"]["25000|6h"]

    assert row["crossed_positions"] == 1
    assert row["wins"] == 1
    assert row["roi_pct"] > 0
    assert row["median_time_to_cross_seconds"] == 100


def test_anatomy_uses_default_trigger_when_supported():
    observations = []
    for idx in range(20):
        observations.extend([
            _obs(f"{idx}-a", price=0.50, usdc_size=10000, ts=idx * 1000 + 100, condition_id=f"cond-{idx}"),
            _obs(f"{idx}-b", price=0.60, usdc_size=15000, ts=idx * 1000 + 200, condition_id=f"cond-{idx}"),
        ])

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig(min_crossings_for_default=20))

    assert report["recommended_trigger"]["source_commitment_threshold_usdc"] == 25000
    assert report["recommended_trigger"]["source_commitment_window_hours"] == 6
    assert report["recommended_trigger"]["selection_reason"] == "default_supported"


def test_anatomy_fallback_selects_lowest_feasible_threshold_not_best_roi():
    observations = [
        _obs("a1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-a", won=False),
        _obs("b1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-b", won=False),
        _obs("c1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-c", won=False),
        _obs("d1", price=0.50, usdc_size=25000, ts=100, condition_id="cond-d", won=True),
    ]

    report = build_target_position_anatomy(
        observations,
        config=TargetAnatomyConfig(min_crossings_for_default=3, min_crossings_for_fallback=3),
    )

    assert report["recommended_trigger"]["source_commitment_threshold_usdc"] == 5000
    assert report["recommended_trigger"]["selection_reason"] == "fallback_lowest_feasible_threshold"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/winning_wallet_thesis/test_target_position_anatomy.py -q
```

Expected: FAIL with missing `target_position_anatomy`.

- [ ] **Step 3: Implement anatomy module**

Create `sniper/analysis/winning_wallet_thesis/target_position_anatomy.py`:

```python
"""Target-slice source-position anatomy for SwissMiss copy-paper setup."""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sniper.strategies.source_position_copy_v1 import (
    DEFAULTS,
    SourceObservation,
    _build_source_positions,
    _detect_threshold_crossing,
    _matches_target_filter,
    _parse_params,
)


@dataclass(frozen=True)
class TargetAnatomyConfig:
    thresholds_usdc: tuple[int, ...] = (5000, 10000, 25000, 50000, 100000)
    windows_hours: tuple[int, ...] = (1, 6, 24)
    min_crossings_for_default: int = 20
    min_crossings_for_fallback: int = 20
    params: dict[str, Any] = field(default_factory=dict)


def _won(obs: SourceObservation) -> bool | None:
    value = (obs.raw_json or {}).get("won")
    if value is None:
        return None
    return bool(value)


def _position_pnl_usdc(position_observations: list[SourceObservation]) -> tuple[float, float, int | None]:
    buys = [o for o in position_observations if o.side == "BUY"]
    if not buys:
        return 0.0, 0.0, None
    won_values = [_won(o) for o in buys if _won(o) is not None]
    won = won_values[-1] if won_values else None
    buy_cost = sum(float(o.usdc_size) for o in buys)
    shares = sum(float(o.size) for o in buys)
    if won is None:
        return buy_cost, 0.0, None
    payout = shares if won else 0.0
    return buy_cost, payout - buy_cost, int(won)


def build_target_position_anatomy(
    observations: list[SourceObservation],
    *,
    config: TargetAnatomyConfig,
) -> dict[str, Any]:
    params = _parse_params({"params": {**DEFAULTS, **config.params}})
    included: list[SourceObservation] = []
    excluded = Counter()
    for obs in observations:
        if obs.side != "BUY":
            included.append(obs)
            continue
        passes, reason = _matches_target_filter(obs, params)
        if passes:
            included.append(obs)
        else:
            excluded[reason] += 1

    positions = list(_build_source_positions(included).values())
    threshold_grid: dict[str, dict[str, Any]] = {}
    for threshold in config.thresholds_usdc:
        for window in config.windows_hours:
            crossed = []
            wins = 0
            losses = 0
            total_cost = 0.0
            total_pnl = 0.0
            times_to_cross: list[int] = []
            grid_params = {
                **params,
                "source_commitment_threshold_usdc": threshold,
                "source_commitment_window_hours": window,
                "max_threshold_age_minutes": 10**9,
            }
            for state in positions:
                trigger = _detect_threshold_crossing(state, grid_params, now_ts=10**12)
                if not trigger.should_enter:
                    continue
                crossed.append(state)
                if state.first_buy_ts is not None and trigger.cross_ts is not None:
                    times_to_cross.append(trigger.cross_ts - state.first_buy_ts)
                cost, pnl, won = _position_pnl_usdc(state.observations)
                total_cost += cost
                total_pnl += pnl
                if won == 1:
                    wins += 1
                elif won == 0:
                    losses += 1
            key = f"{threshold}|{window}h"
            threshold_grid[key] = {
                "threshold_usdc": threshold,
                "window_hours": window,
                "crossed_positions": len(crossed),
                "wins": wins,
                "losses": losses,
                "roi_pct": (total_pnl / total_cost * 100.0) if total_cost else 0.0,
                "median_time_to_cross_seconds": int(statistics.median(times_to_cross)) if times_to_cross else None,
            }

    default_key = "25000|6h"
    default_row = threshold_grid.get(default_key, {})
    if int(default_row.get("crossed_positions") or 0) >= config.min_crossings_for_default:
        recommended = {
            "source_commitment_threshold_usdc": 25000,
            "source_commitment_window_hours": 6,
            "source_commitment_mode": "net_buy_cost",
            "entry_price_min": float(params.get("entry_price_min") or 0.30),
            "max_threshold_age_minutes": int(params.get("max_threshold_age_minutes") or 20),
            "max_price_drift_from_source_vwap_pct": float(params.get("max_price_drift_from_source_vwap_pct") or 10.0),
            "selection_reason": "default_supported",
        }
    else:
        feasible = [
            row for row in threshold_grid.values()
            if row["window_hours"] == 6 and row["crossed_positions"] >= config.min_crossings_for_fallback
        ]
        feasible.sort(key=lambda row: row["threshold_usdc"])
        chosen = feasible[0] if feasible else default_row
        recommended = {
            "source_commitment_threshold_usdc": int(chosen.get("threshold_usdc") or 25000),
            "source_commitment_window_hours": int(chosen.get("window_hours") or 6),
            "source_commitment_mode": "net_buy_cost",
            "entry_price_min": float(params.get("entry_price_min") or 0.30),
            "max_threshold_age_minutes": int(params.get("max_threshold_age_minutes") or 20),
            "max_price_drift_from_source_vwap_pct": float(params.get("max_price_drift_from_source_vwap_pct") or 10.0),
            "selection_reason": "fallback_lowest_feasible_threshold" if feasible else "default_insufficient_sample",
        }

    return {
        "target_universe": {
            "included_positions": len(positions),
            "excluded_by_reason": dict(excluded),
        },
        "threshold_grid": threshold_grid,
        "recommended_trigger": recommended,
        "validation_note": "historical target anatomy is development data, not validation",
    }
```

- [ ] **Step 4: Run anatomy tests**

Run:

```bash
python3 -m pytest tests/winning_wallet_thesis/test_target_position_anatomy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/target_position_anatomy.py tests/winning_wallet_thesis/test_target_position_anatomy.py
git commit -m "feat(thesis): add target-position anatomy report"
```

---

### Task 7: Source Observation Poller

**Files:**
- Create: `scripts/poll_source_position_copy_targets.py`
- Test: `tests/test_poll_source_position_copy_targets.py`

- [ ] **Step 1: Write failing poller tests**

Create `tests/test_poll_source_position_copy_targets.py`:

```python
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.strategies.source_position_copy_v1 import ensure_source_position_copy_tables  # noqa: E402
from scripts.poll_source_position_copy_targets import (  # noqa: E402
    _activity_key,
    _row_to_source_observation,
    poll_once,
)


def test_activity_key_prefers_transaction_hash_and_log_index():
    row = {"transactionHash": "0xabc", "logIndex": 7}
    assert _activity_key("0xwallet", row) == "0xabc:7"


def test_activity_key_falls_back_to_deterministic_fields():
    row = {"conditionId": "c", "asset": "a", "side": "BUY", "timestamp": 123, "price": 0.5, "size": 10}
    assert _activity_key("0xwallet", row) == "0xwallet:c:a:BUY:123:0.5:10"


def test_row_to_source_observation_normalizes_trade_with_tags():
    row = {
        "type": "TRADE",
        "conditionId": "cond",
        "asset": "asset",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.50,
        "size": 100,
        "timestamp": 1000,
        "title": "UFC fight",
        "eventSlug": "ufc-fight-night",
        "eventId": "event",
        "tags": ["Sports", "UFC"],
        "transactionHash": "0xabc",
        "logIndex": 1,
    }

    obs = _row_to_source_observation(wallet="0xWallet", row=row)

    assert obs is not None
    assert obs["activity_key"] == "0xabc:1"
    assert obs["wallet"] == "0xwallet"
    assert obs["side"] == "BUY"
    assert obs["usdc_size"] == 50
    assert obs["tags"] == ["sports", "ufc"]


def test_poll_once_dry_run_makes_no_fetch_calls(tmp_path):
    db_path = tmp_path / "paper.db"
    conn = sqlite3.connect(db_path)
    ensure_source_position_copy_tables(conn)
    conn.close()

    calls = []

    def fetcher(wallet, since_ts):
        calls.append((wallet, since_ts))
        return []

    summary = poll_once(
        db_path=db_path,
        wallets=["0xwallet"],
        since_minutes=30,
        enable_live_api=False,
        max_wallets=10,
        fetch_wallet_activity=fetcher,
    )

    assert calls == []
    assert summary["rows_inserted"] == 0
    assert summary["dry_run"] is True
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_poll_source_position_copy_targets.py -q
```

Expected: FAIL with missing script.

- [ ] **Step 3: Implement poller**

Create `scripts/poll_source_position_copy_targets.py`:

```python
#!/usr/bin/env python3
"""Poll target-wallet activity into source_position_observations.

Dry-run by default. Requires --enable-live-api to call Polymarket.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    ensure_source_position_copy_tables,
    insert_source_observation,
)

logger = logging.getLogger("poll_source_position_copy_targets")


def _fetch_wallet_activity(wallet: str, since_ts: int) -> list[dict[str, Any]]:
    from sniper.markets.polymarket_api import fetch_user_activity

    rows = list(fetch_user_activity(None, wallet, start_ts=since_ts or None) or [])
    if since_ts:
        rows = [r for r in rows if int(r.get("timestamp") or 0) >= int(since_ts)]
    return rows


def _activity_key(wallet: str, row: dict[str, Any]) -> str:
    tx = row.get("transactionHash") or row.get("transaction_hash") or row.get("txHash")
    log_index = row.get("logIndex") if row.get("logIndex") is not None else row.get("log_index")
    if tx:
        return f"{tx}:{log_index if log_index is not None else 0}"
    return ":".join(
        [
            str(wallet).lower().strip(),
            str(row.get("conditionId") or row.get("condition_id") or ""),
            str(row.get("asset") or row.get("asset_id") or ""),
            str(row.get("side") or "").upper(),
            str(int(row.get("timestamp") or 0)),
            str(row.get("price") or 0),
            str(row.get("size") or 0),
        ]
    )


def _extract_tags(row: dict[str, Any]) -> list[str]:
    raw = row.get("tags") or row.get("platform_tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return sorted({str(t).lower().strip() for t in raw if str(t).strip()})


def _row_to_source_observation(*, wallet: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if str(row.get("type") or row.get("activity_type") or "").upper() != "TRADE":
        return None
    side = str(row.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
    asset_id = str(row.get("asset") or row.get("asset_id") or "")
    if not condition_id or not asset_id:
        return None
    price = float(row.get("price") or 0.0)
    size = float(row.get("size") or 0.0)
    normalized_wallet = str(wallet).lower().strip()
    return {
        "activity_key": _activity_key(normalized_wallet, row),
        "wallet": normalized_wallet,
        "condition_id": condition_id,
        "asset_id": asset_id,
        "outcome": str(row.get("outcome") or "").upper(),
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": price * size,
        "observed_ts": int(row.get("timestamp") or 0),
        "title": str(row.get("title") or ""),
        "event_slug": str(row.get("eventSlug") or row.get("event_slug") or row.get("slug") or ""),
        "event_id": str(row.get("eventId") or row.get("event_id") or ""),
        "tags": _extract_tags(row),
        "source_endpoint": "data-api-user-activity",
        "raw_json": dict(row),
    }


def poll_once(
    *,
    db_path: Path,
    wallets: list[str],
    since_minutes: int,
    enable_live_api: bool,
    max_wallets: int,
    fetch_wallet_activity: Callable[[str, int], list[dict[str, Any]]] = _fetch_wallet_activity,
) -> dict[str, Any]:
    since_ts = int(time.time()) - int(since_minutes) * 60
    summary = {
        "dry_run": not enable_live_api,
        "wallets_requested": len(wallets),
        "wallets_polled": 0,
        "rows_fetched": 0,
        "rows_inserted": 0,
        "rows_skipped_non_trade": 0,
        "rows_skipped_dedup": 0,
        "errors": [],
    }
    capped = [str(w).lower().strip() for w in wallets if str(w).strip()][:max_wallets]
    if not enable_live_api:
        logger.info("[DRY] would poll %d wallets since %s", len(capped), since_ts)
        return summary

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_source_position_copy_tables(conn)
    try:
        for wallet in capped:
            try:
                rows = fetch_wallet_activity(wallet, since_ts)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{wallet}: {type(exc).__name__}: {exc}")
                continue
            summary["wallets_polled"] += 1
            summary["rows_fetched"] += len(rows)
            for row in rows:
                normalized = _row_to_source_observation(wallet=wallet, row=row)
                if normalized is None:
                    summary["rows_skipped_non_trade"] += 1
                    continue
                inserted = insert_source_observation(conn, **normalized)
                if inserted is None:
                    summary["rows_skipped_dedup"] += 1
                else:
                    summary["rows_inserted"] += 1
    finally:
        conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll source wallet activity into source-position observations.")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--wallets", nargs="+", required=True)
    parser.add_argument("--since-minutes", type=int, default=30)
    parser.add_argument("--max-wallets", type=int, default=10)
    parser.add_argument("--enable-live-api", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    if args.since_minutes <= 0:
        print("# error: --since-minutes must be positive", file=sys.stderr)
        return 2
    if args.enable_live_api and not args.db.exists():
        print(f"# error: db not found: {args.db}", file=sys.stderr)
        return 2
    summary = poll_once(
        db_path=args.db,
        wallets=args.wallets,
        since_minutes=args.since_minutes,
        enable_live_api=args.enable_live_api,
        max_wallets=args.max_wallets,
    )
    logger.info("summary=%s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run poller tests**

Run:

```bash
python3 -m pytest tests/test_poll_source_position_copy_targets.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/poll_source_position_copy_targets.py tests/test_poll_source_position_copy_targets.py
git commit -m "feat(strategy): add source-position copy poller"
```

---

### Task 8: End-to-End Verification and Knowledge Sync

**Files:**
- Modify only if verification exposes a real defect:
  - `sniper/strategies/source_position_copy_v1.py`
  - `sniper/analysis/winning_wallet_thesis/target_position_anatomy.py`
  - tests from earlier tasks

- [ ] **Step 1: Run focused strategy tests**

Run:

```bash
python3 -m pytest tests/test_source_position_copy_v1.py tests/test_poll_source_position_copy_targets.py -q
```

Expected: PASS.

- [ ] **Step 2: Run target-anatomy tests**

Run:

```bash
python3 -m pytest tests/winning_wallet_thesis/test_target_position_anatomy.py -q
```

Expected: PASS.

- [ ] **Step 3: Run existing shadow-copy regression**

Run:

```bash
python3 -m pytest tests/test_shadow_copy_v1.py -q
```

Expected: PASS. This proves the row-copier remains unchanged while the source-position copier is added separately.

- [ ] **Step 4: Run strategy registry smoke**

Run:

```bash
python3 -m pytest tests/test_strategy_framework.py tests/test_source_position_copy_v1.py::test_registry_exposes_source_position_copy_v1 -q
```

Expected: PASS.

- [ ] **Step 5: Run schema and focused thesis checks**

Run:

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py tests/winning_wallet_thesis/test_phase2_profile_parity.py -q
```

Expected: schema validator exits `0`; focused thesis tests PASS.

- [ ] **Step 6: Add Knowledge Sync note to commit/PR body**

Use this exact block in the PR body or final merge note:

```markdown
## Knowledge Sync

- Knowledge delta: new paper-only strategy surface.
- What changed: `source_position_copy_v1` observes cumulative source positions and emits one paper entry after a pre-registered source-commitment threshold; target-position anatomy report selects the deployment trigger before forward results.
- Doctrine: still paper-only; no copy-trading advice, no live execution, no full-wallet verdict mutation.
- Supabase memory: add architecture/workflow row for the source-position copy strategy and link to `docs/superpowers/specs/2026-05-31-swissmiss-target-position-copy-design.md`.
- Legacy status: `shadow_copy_v1` remains the trade-row copier; `shadow_copy_pending_signals` is not the canonical store for source-position copy.
```

- [ ] **Step 7: Commit final verification fixes if any**

If Step 1-5 exposed a real defect and you patched it:

```bash
git add sniper/strategies/source_position_copy_v1.py sniper/analysis/winning_wallet_thesis/target_position_anatomy.py tests/test_source_position_copy_v1.py tests/test_poll_source_position_copy_targets.py tests/winning_wallet_thesis/test_target_position_anatomy.py
git commit -m "test(strategy): verify source-position copy v1"
```

If Step 1-5 passed without patching, do not create an empty commit.

---

## Acceptance Criteria

- `source_position_observations` is idempotent on `activity_key`.
- `source_position_copy_state` prevents duplicate entries per `(strategy_id, wallet, condition_id, asset_id, trigger_version)`.
- `source_position_copy_v1` rejects non-paper execution.
- Target filters accept platform-tag sports/UFC/CFB/MLB and reject geopolitics/Iran/Middle East/elections/macro/politics.
- Probe buys below threshold do not enter.
- Multiple adds can cross threshold and emit one paper signal.
- Sells before threshold reduce net commitment.
- Stale threshold crossing rejects.
- Price drift from source VWAP rejects.
- Source opposite-side conflict rejects.
- Hold-to-resolution policy stays open until resolution price.
- Source-exit policy closes on material source reduction.
- Target anatomy emits threshold grid and one pre-registered recommended trigger.
- Target anatomy fallback chooses lowest feasible threshold, not highest historical ROI.
- Poller is dry-run by default and requires `--enable-live-api` for network calls.
- Existing `shadow_copy_v1` tests still pass.

## Scope Guards

- No real-money execution.
- No live API calls in tests.
- No live strategy deployment in this implementation.
- No production DB writes.
- No mutation of wallet analyzer verdict fields.
- No threshold changes after forward paper results begin.
- No hardcoded entity keyword buckets for SwissMiss target filtering.

## Self-Review

**Spec coverage**

- Target-position anatomy: Task 6.
- Paper-only `source_position_copy_v1`: Tasks 1-5.
- Source observation store: Task 1.
- State machine and idempotency: Task 4.
- Entry logic and threshold behavior: Tasks 2-5.
- Exit logic, including hold and source-exit modes: Task 5.
- Edge cases: Tasks 2-5 and acceptance criteria.
- Reports: Task 5 plugin report and Task 6 anatomy report.
- Validation doctrine: Task 8 Knowledge Sync and scope guards.
- Non-goals: scope guards.

**Placeholder scan**

Checked the plan against the no-filler rule from the writing-plans skill.

**Type consistency**

Shared names are consistent across tasks:

- `SourceObservation`
- `SourcePositionState`
- `ThresholdTrigger`
- `SourcePositionCopyV1Strategy`
- `ensure_source_position_copy_tables`
- `insert_source_observation`
- `record_copy_state`
- `build_target_position_anatomy`
- `TargetAnatomyConfig`
