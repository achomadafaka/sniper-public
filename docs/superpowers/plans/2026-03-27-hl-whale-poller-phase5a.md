# Phase 5A: HL Whale Position Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll Hyperliquid whale positions via the public `clearinghouseState` API, compute liquidation proximity, and store snapshots for the future liquidation strategy plugin.

**Architecture:** New `hl_whale_poller.py` module polls tracked wallets every 60s. Companion `metaAndAssetCtxs` call provides mark prices. Distance-to-liquidation computed at write time and stored. Feed adapter wraps the poller for strategy runner integration. Two new DB tables: `hl_tracked_wallets` (seed list) and `hl_whale_positions` (latest snapshot, upserted).

**Tech Stack:** Python 3.13, requests (already in requirements), SQLite, existing `db_hyperliquid.py` + `feeds/` patterns.

**Spec:** `docs/superpowers/specs/2026-03-27-hl-liquidation-strategy-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| `db_hyperliquid.py` | `ensure_hl_tracked_wallets_table()`, `ensure_hl_whale_positions_table()`, insert/upsert helpers |
| `hl_whale_poller.py` | Standalone polling module: fetch clearinghouseState + metaAndAssetCtxs, parse, compute distance, store |
| `feeds/hl_whale.py` | Poll-mode FeedAdapter wrapping `hl_whale_poller.py` |
| `feeds/registry.py` | Register `hl_whale` adapter |
| `migrations/014_hl_whale_tables.py` | Migration for existing DBs |
| `web_settings.py` | `HL_WHALE_POLL_SECONDS`, `HL_WHALE_TRACKED_ASSETS` |
| `tests/test_hl_whale_poller.py` | All tests |

---

### Worker 1: DB Tables + Parsing + Distance Computation

**Files:**
- Modify: `db_hyperliquid.py`
- Create: `hl_whale_poller.py` (parsing functions only, not the full poller yet)
- Create: `tests/test_hl_whale_poller.py`
- Create: `migrations/014_hl_whale_tables.py`

- [ ] **Step 1: Write failing tests for DB tables and parsing**

Create `tests/test_hl_whale_poller.py`:

```python
"""Tests for Phase 5A: HL whale position polling."""
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_hyperliquid import (
    ensure_hl_tracked_wallets_table,
    ensure_hl_whale_positions_table,
    ensure_all_hl_tables,
)


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_hl_tracked_wallets_table(conn)
    ensure_hl_whale_positions_table(conn)
    return conn


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


class TestHlWhaleTables:

    def test_tracked_wallets_table_exists(self):
        conn = _get_conn()
        assert _has_table(conn, "hl_tracked_wallets")

    def test_whale_positions_table_exists(self):
        conn = _get_conn()
        assert _has_table(conn, "hl_whale_positions")

    def test_ensure_all_includes_whale_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_all_hl_tables(conn)
        assert _has_table(conn, "hl_tracked_wallets")
        assert _has_table(conn, "hl_whale_positions")

    def test_tracked_wallets_insert(self):
        conn = _get_conn()
        conn.execute(
            "INSERT INTO hl_tracked_wallets (wallet, label, source) VALUES (?, ?, ?)",
            ("0x" + "a" * 40, "test whale", "manual"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM hl_tracked_wallets").fetchone()[0] == 1

    def test_tracked_wallets_rejects_duplicate(self):
        conn = _get_conn()
        addr = "0x" + "b" * 40
        conn.execute("INSERT INTO hl_tracked_wallets (wallet, label, source) VALUES (?, ?, ?)",
                     (addr, "w1", "manual"))
        conn.commit()
        try:
            conn.execute("INSERT INTO hl_tracked_wallets (wallet, label, source) VALUES (?, ?, ?)",
                         (addr, "w2", "manual"))
            conn.commit()
            assert False, "Should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass

    def test_whale_positions_upsert(self):
        """INSERT OR REPLACE on (wallet, asset) keeps only latest snapshot."""
        conn = _get_conn()
        wallet = "0x" + "c" * 40
        conn.execute(
            """INSERT OR REPLACE INTO hl_whale_positions
               (wallet, asset, side, size, entry_price, mark_price, margin_type,
                leverage, position_value_usd, distance_pct)
               VALUES (?, 'BTC', 'long', 1.0, 68000, 68500, 'isolated', 10, 68500, 5.0)""",
            (wallet,),
        )
        conn.commit()
        # Second insert with updated mark_price
        conn.execute(
            """INSERT OR REPLACE INTO hl_whale_positions
               (wallet, asset, side, size, entry_price, mark_price, margin_type,
                leverage, position_value_usd, distance_pct)
               VALUES (?, 'BTC', 'long', 1.0, 68000, 69000, 'isolated', 10, 69000, 6.0)""",
            (wallet,),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM hl_whale_positions WHERE wallet = ?",
                             (wallet,)).fetchone()[0]
        assert count == 1  # upserted, not duplicated
        row = conn.execute("SELECT mark_price FROM hl_whale_positions WHERE wallet = ?",
                           (wallet,)).fetchone()
        assert row["mark_price"] == 69000  # latest value
```

- [ ] **Step 2: Write failing tests for address validation and distance computation**

Append to `tests/test_hl_whale_poller.py`:

```python
from hl_whale_poller import (
    validate_hl_address,
    compute_distance_isolated,
    compute_distance_cross,
    parse_whale_positions,
)


class TestAddressValidation:

    def test_valid_address(self):
        assert validate_hl_address("0x" + "a" * 40) is True

    def test_invalid_too_short(self):
        assert validate_hl_address("0xabc") is False

    def test_invalid_no_prefix(self):
        assert validate_hl_address("a" * 42) is False

    def test_invalid_non_hex(self):
        assert validate_hl_address("0x" + "g" * 40) is False

    def test_empty(self):
        assert validate_hl_address("") is False


class TestDistanceComputation:

    def test_isolated_distance(self):
        # mark=70000, liq=65000 → distance = abs(70000-65000)/70000*100 = 7.14%
        d = compute_distance_isolated(mark_price=70000, liquidation_price=65000)
        assert abs(d - 7.14) < 0.1

    def test_isolated_distance_short(self):
        # Short: mark=70000, liq=75000 → distance = abs(70000-75000)/70000*100 = 7.14%
        d = compute_distance_isolated(mark_price=70000, liquidation_price=75000)
        assert abs(d - 7.14) < 0.1

    def test_isolated_zero_mark(self):
        d = compute_distance_isolated(mark_price=0, liquidation_price=65000)
        assert d == 0

    def test_cross_distance(self):
        # account_value=100000, cross_mm=20000 → distance = (100000-20000)/100000*100 = 80%
        d = compute_distance_cross(account_value=100000, cross_mm_used=20000)
        assert d == 80.0

    def test_cross_at_liquidation(self):
        # account_value=20000, cross_mm=20000 → distance = 0%
        d = compute_distance_cross(account_value=20000, cross_mm_used=20000)
        assert d == 0.0

    def test_cross_below_liquidation(self):
        # account_value=15000, cross_mm=20000 → distance = max(0, ...) = 0%
        d = compute_distance_cross(account_value=15000, cross_mm_used=20000)
        assert d == 0.0

    def test_cross_zero_account(self):
        d = compute_distance_cross(account_value=0, cross_mm_used=0)
        assert d == 0.0


class TestParseWhalePositions:

    def test_parse_isolated_position(self):
        state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "1.5",
                        "entryPx": "68000",
                        "positionValue": "102000",
                        "unrealizedPnl": "1500",
                        "liquidationPx": "63000",
                        "marginUsed": "10200",
                        "leverage": {"type": "isolated", "value": 10},
                    }
                }
            ],
            "marginSummary": {"accountValue": "500000"},
            "crossMaintenanceMarginUsed": "5000",
        }
        mark_prices = {"BTC": 69000.0}
        rows = parse_whale_positions("0x" + "a" * 40, state, mark_prices)
        assert len(rows) == 1
        r = rows[0]
        assert r["asset"] == "BTC"
        assert r["side"] == "long"
        assert r["size"] == 1.5
        assert r["margin_type"] == "isolated"
        assert r["mark_price"] == 69000.0
        assert r["liquidation_price"] == 63000.0
        assert r["distance_pct"] > 0

    def test_parse_short_position(self):
        state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "ETH",
                        "szi": "-10.0",
                        "entryPx": "3500",
                        "positionValue": "35000",
                        "unrealizedPnl": "-500",
                        "liquidationPx": "3800",
                        "marginUsed": "3500",
                        "leverage": {"type": "isolated", "value": 10},
                    }
                }
            ],
            "marginSummary": {"accountValue": "100000"},
            "crossMaintenanceMarginUsed": "1000",
        }
        mark_prices = {"ETH": 3550.0}
        rows = parse_whale_positions("0x" + "b" * 40, state, mark_prices)
        assert len(rows) == 1
        assert rows[0]["side"] == "short"
        assert rows[0]["size"] == 10.0  # absolute value

    def test_parse_cross_margin(self):
        state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "SOL",
                        "szi": "100",
                        "entryPx": "150",
                        "positionValue": "15000",
                        "unrealizedPnl": "200",
                        "liquidationPx": None,
                        "marginUsed": "750",
                        "leverage": {"type": "cross", "value": 20},
                    }
                }
            ],
            "marginSummary": {"accountValue": "50000"},
            "crossMaintenanceMarginUsed": "5000",
        }
        mark_prices = {"SOL": 152.0}
        rows = parse_whale_positions("0x" + "c" * 40, state, mark_prices)
        assert len(rows) == 1
        r = rows[0]
        assert r["margin_type"] == "cross"
        assert r["liquidation_price"] is None
        assert r["distance_pct"] == 90.0  # (50000-5000)/50000*100

    def test_parse_empty_positions(self):
        state = {
            "assetPositions": [],
            "marginSummary": {"accountValue": "10000"},
            "crossMaintenanceMarginUsed": "0",
        }
        rows = parse_whale_positions("0x" + "d" * 40, state, {})
        assert rows == []

    def test_parse_missing_mark_price_skips(self):
        """If mark price not available for asset, skip that position."""
        state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "UNKNOWN",
                        "szi": "100",
                        "entryPx": "1.0",
                        "positionValue": "100",
                        "unrealizedPnl": "0",
                        "liquidationPx": "0.5",
                        "marginUsed": "10",
                        "leverage": {"type": "isolated", "value": 10},
                    }
                }
            ],
            "marginSummary": {"accountValue": "10000"},
            "crossMaintenanceMarginUsed": "0",
        }
        mark_prices = {}  # UNKNOWN not in mark_prices
        rows = parse_whale_positions("0x" + "e" * 40, state, mark_prices)
        assert rows == []
```

- [ ] **Step 3: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_hl_whale_poller.py -v`
Expected: ImportError — functions don't exist yet

- [ ] **Step 4: Implement DB tables in db_hyperliquid.py**

Add two new `ensure_*` functions and update `ensure_all_hl_tables()`:

```python
def ensure_hl_tracked_wallets_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_tracked_wallets (
            wallet    TEXT PRIMARY KEY,
            label     TEXT NOT NULL DEFAULT '',
            source    TEXT NOT NULL DEFAULT 'manual',
            added_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

def ensure_hl_whale_positions_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_whale_positions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet             TEXT NOT NULL,
            asset              TEXT NOT NULL,
            side               TEXT NOT NULL CHECK (side IN ('long', 'short')),
            size               REAL NOT NULL,
            entry_price        REAL NOT NULL,
            mark_price         REAL NOT NULL,
            liquidation_price  REAL,
            margin_type        TEXT NOT NULL CHECK (margin_type IN ('isolated', 'cross')),
            leverage           REAL NOT NULL DEFAULT 1.0,
            margin_used        REAL NOT NULL DEFAULT 0,
            unrealized_pnl     REAL NOT NULL DEFAULT 0,
            position_value_usd REAL NOT NULL DEFAULT 0,
            distance_pct       REAL NOT NULL DEFAULT 0,
            account_value      REAL,
            cross_mm_used      REAL,
            captured_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(wallet, asset)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_whale_pos_distance
        ON hl_whale_positions(distance_pct ASC)
    """)
    conn.commit()
```

Update `ensure_all_hl_tables()` to call both new functions.

- [ ] **Step 5: Implement parsing functions in hl_whale_poller.py**

Create `hl_whale_poller.py` with pure functions:

```python
import re

_HL_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

def validate_hl_address(addr: str) -> bool:
    return bool(addr and _HL_ADDR_RE.match(addr))

def compute_distance_isolated(mark_price: float, liquidation_price: float) -> float:
    if mark_price <= 0:
        return 0.0
    return round(abs(mark_price - liquidation_price) / mark_price * 100, 4)

def compute_distance_cross(account_value: float, cross_mm_used: float) -> float:
    if account_value <= 0:
        return 0.0
    return round(max(0.0, (account_value - cross_mm_used) / account_value * 100), 4)

def parse_whale_positions(wallet: str, state: dict, mark_prices: dict) -> list:
    """Parse clearinghouseState response into hl_whale_positions rows."""
    # ... (extract from spec: parse assetPositions, compute distance, return list of dicts)
```

- [ ] **Step 6: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_hl_whale_poller.py -v`
Expected: All PASS

- [ ] **Step 7: Create migration, add settings, register feed adapter**

- `migrations/014_hl_whale_tables.py`
- `web_settings.py`: add `HL_WHALE_POLL_SECONDS=60`
- `feeds/hl_whale.py`: poll-mode FeedAdapter
- `feeds/registry.py`: register `hl_whale`

- [ ] **Step 8: Run full suite + commit**

```bash
python3 -m pytest tests/ -q --tb=line
git add db_hyperliquid.py hl_whale_poller.py feeds/hl_whale.py feeds/registry.py \
        migrations/014_hl_whale_tables.py web_settings.py tests/test_hl_whale_poller.py
git commit -m "feat(phase5a): HL whale position polling infrastructure

Poll clearinghouseState for tracked whale addresses every 60s.
Compute liquidation proximity (isolated + cross margin).
Store latest snapshots in hl_whale_positions (upserted per poll).
Manual seed list in hl_tracked_wallets.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
