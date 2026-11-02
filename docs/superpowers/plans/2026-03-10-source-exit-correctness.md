# Source Exit Correctness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix paper trader to only close trades when the source wallet actually exits, not when the analyzer disqualifies the signal.

**Architecture:** Remove `is_open=0` as a close trigger; replace with disqualification annotation. Add per-cycle wallet position cache for data-api lookups. Verify remaining position size before closing on sell signals. One-off repair script to reopen 8 falsely closed trades.

**Tech Stack:** Python 3, SQLite, requests (Polymarket data-api/CLOB), unittest

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `db.py` | Schema evolution — add disqualification columns | Modify (fn def ~1498, call ~1390) |
| `paper_trader.py` | Core fix — exit logic, sell signals, cache, helper | Modify (lines 66–77, 200–212, 834–970, 1075–1127, 1224–1238) |
| `tests/test_source_exit_correctness.py` | All new tests | Create |
| `scripts/repair_false_source_exits.py` | One-off repair script | Create |
| `routes/api_misc.py` | Add disqualification fields to paper trades API | Modify |
| `static/app.js` | Render disqualification badge on open paper trades | Modify |

---

## Chunk 1: Schema + Disqualification Helper

### Task 1: Add disqualification columns to paper_trades schema

**Files:**
- Modify: `db.py:1388-1391` (after `ensure_paper_trades_slippage_columns` call)
- Modify: `paper_trader.py:200-212` (in `_init_tables`, legacy column list)

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_exit_correctness.py`:

```python
"""Tests for source exit correctness: disqualification tracking, exit verification."""
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from db import get_connection, init_db


def _create_paper_trades_table(conn):
    """Create paper_trades table matching paper_trader._init_tables schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            market_title TEXT,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL,
            exit_price REAL,
            size_usdc REAL NOT NULL,
            shares REAL,
            position_value_at_entry REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            signal_type TEXT DEFAULT 'unknown',
            exit_classification TEXT,
            stale_flagged_at TIMESTAMP,
            status TEXT DEFAULT 'open',
            pnl_usdc REAL,
            roi_percent REAL,
            exit_reason TEXT,
            signal_insider_score REAL,
            signal_speculator_score REAL,
            wallet_win_rate REAL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            alert_sent_entry INTEGER DEFAULT 0,
            alert_sent_exit INTEGER DEFAULT 0,
            position_signal_id INTEGER,
            signal_disqualified_at TEXT,
            signal_disqualification_reason TEXT
        );
    """)


class TestDisqualificationColumns(unittest.TestCase):
    """Verify schema evolution adds disqualification columns."""

    def test_columns_exist_after_init(self):
        conn = get_connection(":memory:")
        init_db(conn)
        _create_paper_trades_table(conn)
        # Import triggers _ensure_column calls
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        self.assertIn("signal_disqualified_at", cols)
        self.assertIn("signal_disqualification_reason", cols)
        conn.close()

    def test_columns_default_null(self):
        conn = get_connection(":memory:")
        init_db(conn)
        _create_paper_trades_table(conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(conn)
        conn.execute(
            "INSERT INTO paper_trades (wallet, condition_id, outcome, direction, entry_price, size_usdc) "
            "VALUES ('0xabc', 'cond1', 'Yes', 'buy', 0.5, 25.0)"
        )
        row = conn.execute("SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades").fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestDisqualificationColumns -v`
Expected: FAIL — `ensure_paper_trades_disqualification_columns` does not exist yet

- [ ] **Step 3: Implement schema evolution in db.py**

Add after `ensure_paper_trades_slippage_columns` definition (after line 1498 in `db.py`):

```python
def ensure_paper_trades_disqualification_columns(conn: sqlite3.Connection) -> None:
    """Ensure disqualification tracking columns exist on paper_trades."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "paper_trades", "signal_disqualified_at", "TEXT DEFAULT NULL")
        _ensure_column(conn, "paper_trades", "signal_disqualification_reason", "TEXT DEFAULT NULL")
        conn.commit()
```

Add call in `init_db()` after `ensure_paper_trades_slippage_columns(conn)` at line 1390:

```python
    ensure_paper_trades_slippage_columns(conn)
    ensure_paper_trades_disqualification_columns(conn)
```

Update import in `paper_trader.py` line 19:

```python
from db import ensure_paper_trades_slippage_columns, ensure_paper_trades_disqualification_columns, get_connection, init_db
```

Add to `_init_tables()` legacy column list (after line 211 in `paper_trader.py`):

```python
            ("signal_disqualified_at", "TEXT DEFAULT NULL"),
            ("signal_disqualification_reason", "TEXT DEFAULT NULL"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestDisqualificationColumns -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run full suite to verify no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add db.py paper_trader.py tests/test_source_exit_correctness.py
git commit -m "feat: add signal_disqualified_at/reason columns to paper_trades schema"
```

---

### Task 2: Implement `_maybe_mark_disqualified()` helper

**Files:**
- Modify: `paper_trader.py` (add method to `PaperTrader` class, after `_close_position` around line 1073)
- Test: `tests/test_source_exit_correctness.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_source_exit_correctness.py`:

```python
class TestMaybeMarkDisqualified(unittest.TestCase):
    """Test _maybe_mark_disqualified() append-safe behavior."""

    def setUp(self):
        # Create a PaperTrader without full init (avoid DB/network)
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        # Seed one open trade
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status) VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.5, 25.0, 'open')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_first_disqualification_sets_both_fields(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        row = self.conn.execute(
            "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "signal_is_open_0")

    def test_preserves_first_timestamp_on_repeat(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        first_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        # Call again with same reason — should be no-op
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        second_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        self.assertEqual(first_ts, second_ts)

    def test_updates_reason_when_changed(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        first_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        self.trader._maybe_mark_disqualified(1, "price_above_0.80")
        row = self.conn.execute(
            "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        # Timestamp preserved, reason updated
        self.assertEqual(row[0], first_ts)
        self.assertEqual(row[1], "price_above_0.80")

    def test_same_reason_is_noop(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        # Should not raise or change anything
        row = self.conn.execute(
            "SELECT signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        self.assertEqual(row[0], "signal_is_open_0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestMaybeMarkDisqualified -v`
Expected: FAIL — `_maybe_mark_disqualified` does not exist

- [ ] **Step 3: Implement `_maybe_mark_disqualified()`**

Add to `PaperTrader` class in `paper_trader.py`, after `_close_position` (after line 1073):

```python
    def _maybe_mark_disqualified(self, trade_id: int, reason: str):
        """Record signal disqualification without closing the trade.

        Append-safe: preserves the original disqualified_at timestamp.
        Updates reason only when it actually changes.
        """
        existing = self.conn.execute(
            "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = ?",
            (trade_id,)
        ).fetchone()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if existing and existing["signal_disqualified_at"]:
            # Already disqualified — update reason only if it changed
            if existing["signal_disqualification_reason"] == reason:
                return
            self.conn.execute(
                "UPDATE paper_trades SET signal_disqualification_reason = ? WHERE trade_id = ?",
                (reason, trade_id),
            )
            self.conn.commit()
            logger.info(f"Signal disqualification reason updated for trade {trade_id}: {reason}")
            return
        self.conn.execute(
            "UPDATE paper_trades SET signal_disqualified_at = ?, signal_disqualification_reason = ? WHERE trade_id = ?",
            (now, reason, trade_id),
        )
        self.conn.commit()
        logger.info(f"Signal disqualified for trade {trade_id}: {reason}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestMaybeMarkDisqualified -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run full suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add paper_trader.py tests/test_source_exit_correctness.py
git commit -m "feat: add _maybe_mark_disqualified() append-safe helper"
```

---

## Chunk 2: Per-Cycle Cache + Core Exit Logic Fix

### Task 3: Add per-cycle wallet position cache

**Files:**
- Modify: `paper_trader.py` (add `_fetch_wallet_positions()` method, modify `run_once()`)
- Test: `tests/test_source_exit_correctness.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_source_exit_correctness.py`:

```python
class TestWalletPositionCache(unittest.TestCase):
    """Test per-cycle wallet position cache."""

    def setUp(self):
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.trader._wallet_positions_cache = {}

    @patch("paper_trader.requests.get")
    def test_cache_hit_avoids_second_api_call(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"market": "cond1", "size": 100.0, "curPrice": 0.5}]
        mock_get.return_value = mock_resp

        # First call — hits API
        result1 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(len(result1), 1)
        self.assertEqual(mock_get.call_count, 1)

        # Second call — hits cache, no new API call
        result2 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(len(result2), 1)
        self.assertEqual(mock_get.call_count, 1)  # Still 1

    @patch("paper_trader.requests.get")
    def test_different_wallet_makes_new_call(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        self.trader._fetch_wallet_positions("0xabc")
        self.trader._fetch_wallet_positions("0xdef")
        self.assertEqual(mock_get.call_count, 2)

    @patch("paper_trader.requests.get")
    def test_cache_reset_between_cycles(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"market": "cond1", "size": 50.0}]
        mock_get.return_value = mock_resp

        self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(mock_get.call_count, 1)

        # Reset cache (simulates start of new run_once cycle)
        self.trader._wallet_positions_cache = {}

        self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(mock_get.call_count, 2)

    @patch("paper_trader.requests.get")
    def test_api_failure_caches_empty_list(self, mock_get):
        mock_get.side_effect = Exception("network error")

        result = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(result, [])
        # Cached — second call doesn't retry
        result2 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(result2, [])
        self.assertEqual(mock_get.call_count, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestWalletPositionCache -v`
Expected: FAIL — `_fetch_wallet_positions` does not exist, `_wallet_positions_cache` not initialized

- [ ] **Step 3: Implement cache**

Add `_wallet_positions_cache` initialization in `__init__` (after line 72 in `paper_trader.py`):

```python
        self._wallet_positions_cache = {}
```

Add `_fetch_wallet_positions()` method to `PaperTrader` class (after the new `_maybe_mark_disqualified`, around line 1090):

```python
    def _fetch_wallet_positions(self, wallet: str) -> list:
        """Fetch wallet positions from data-api, cached per monitoring cycle."""
        if wallet in self._wallet_positions_cache:
            return self._wallet_positions_cache[wallet]
        try:
            r = requests.get(
                'https://data-api.polymarket.com/positions',
                params={'user': wallet, 'sizeThreshold': 0, 'limit': 500},
                timeout=8,
            )
            positions = r.json() if r.ok else []
        except Exception:
            positions = []
        self._wallet_positions_cache[wallet] = positions
        return positions
```

Add cache reset at the start of `run_once()` (line 1231, after the log line):

```python
        self._wallet_positions_cache = {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestWalletPositionCache -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add paper_trader.py tests/test_source_exit_correctness.py
git commit -m "feat: add per-cycle wallet position cache for data-api lookups"
```

---

### Task 4: Fix `_check_position_exit()` — `is_open=0` no longer closes

**Files:**
- Modify: `paper_trader.py:834-970` (`_check_position_exit`)
- Test: `tests/test_source_exit_correctness.py`

This is the core fix. Four changes to `_check_position_exit()`:

1. Line 876-877: `is_open=0` → disqualification annotation instead of `position_closed_by_source = True`
2. Lines 901-918: Replace inline data-api call with `_fetch_wallet_positions()` cache. **Important:** The old code only ran data-api when `current_price is None` (CLOB failed). The new code ALWAYS runs data-api to verify source position size, even when CLOB returned a price. This is intentional — we need position verification regardless of price availability.
3. Line 939: Simplify close condition to just `if position_closed_by_source:` — this flag is now only set by confirmed exits (CLOB closed/archived, data-api size < 0.01, live signals size < 0.01). The old `source_size` OR branch is absorbed into earlier flag assignments.
4. Line 857: Change live_signals size default from `1` to `None` and add explicit size check after the loop

- [ ] **Step 1: Write failing tests**

Append to `tests/test_source_exit_correctness.py`:

```python
class TestCheckPositionExit(unittest.TestCase):
    """Test _check_position_exit() source exit correctness."""

    def setUp(self):
        import paper_trader as pt_mod
        self.pt_mod = pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        self.trader._wallet_positions_cache = {}

        # Create position_signals table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS position_signals (
                rowid INTEGER PRIMARY KEY,
                wallet TEXT, condition_id TEXT, outcome TEXT,
                is_open INTEGER DEFAULT 1,
                entry_price REAL, current_price REAL,
                position_insider_score REAL, position_speculator_score REAL,
                position_classification TEXT,
                entry_anchor_ts REAL DEFAULT 0
            )
        """)

        # Seed one open paper trade
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status, opened_at, position_signal_id) "
            "VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.50, 25.0, 'open', '2026-03-01 00:00:00', 100)"
        )
        # Seed matching position signal
        self.conn.execute(
            "INSERT INTO position_signals (rowid, wallet, condition_id, outcome, is_open, "
            "entry_price, current_price, position_insider_score) "
            "VALUES (100, '0xabc', 'cond1', 'Yes', 1, 0.50, 0.55, 70.0)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _get_trade(self):
        return self.conn.execute("SELECT * FROM paper_trades WHERE trade_id = 1").fetchone()

    @patch("paper_trader.requests.get")
    def test_is_open_zero_does_not_close(self, mock_get):
        """is_open=0 should NOT close the trade — only annotate."""
        # Set signal to is_open=0 (analyzer reclassified)
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()

        # CLOB returns valid price (market still active)
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.60}],
            "closed": False, "archived": False,
        }
        # Data-api returns wallet still holds position
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.60}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])  # empty live_signals forces DB+API path

        trade = self._get_trade()
        self.assertEqual(trade["status"], "open")  # NOT closed
        self.assertIsNotNone(trade["signal_disqualified_at"])
        self.assertEqual(trade["signal_disqualification_reason"], "signal_is_open_0")

    @patch("paper_trader.requests.get")
    def test_confirmed_full_exit_closes(self, mock_get):
        """Data-api confirms size < 0.01 → should close with source_exited."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()

        # CLOB returns valid price
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.65}],
            "closed": False, "archived": False,
        }
        # Data-api confirms wallet has exited (size near zero)
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 0.001, "curPrice": 0.65}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])

        trade = self._get_trade()
        self.assertEqual(trade["status"], "early_exit")
        self.assertEqual(trade["exit_reason"], "source_exited")

    @patch("paper_trader.requests.get")
    def test_market_closed_closes_trade(self, mock_get):
        """CLOB reports market closed → should close trade."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()

        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.90}],
            "closed": True, "archived": False,
        }
        mock_get.return_value = mock_clob_resp

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])

        trade = self._get_trade()
        # Should be closed (market closed is a valid close reason)
        self.assertNotEqual(trade["status"], "open")

    @patch("paper_trader.requests.get")
    def test_data_api_failure_keeps_trade_open_when_disqualified(self, mock_get):
        """If is_open=0 and data-api fails, trade stays open (err on side of keeping)."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()

        # CLOB returns valid price but market not closed
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.60}],
            "closed": False, "archived": False,
        }
        # Data-api fails
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = []  # Empty — wallet not found
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])

        trade = self._get_trade()
        self.assertEqual(trade["status"], "open")  # Kept open
        self.assertIsNotNone(trade["signal_disqualified_at"])

    def test_live_signals_zero_size_closes(self):
        """Position in live_signals with size < 0.01 → confirmed exit."""
        live_signals = [{
            "wallet": "0xabc", "condition_id": "cond1", "outcome": "Yes",
            "current_price": 0.65, "size": 0.005,
        }]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, live_signals)

        trade = self._get_trade()
        self.assertNotEqual(trade["status"], "open")
        self.assertEqual(trade["exit_reason"], "source_exited")

    @patch("paper_trader.requests.get")
    @patch("paper_trader.PaperTrader._source_has_recent_activity", return_value=False)
    def test_stale_timeout_still_works_when_disqualified(self, mock_activity, mock_get):
        """Disqualified trade that ages past STALE_DAYS should still be closed by stale logic."""
        # Set old opened_at and is_open=0
        self.conn.execute(
            "UPDATE paper_trades SET opened_at = '2026-01-01 00:00:00' WHERE trade_id = 1"
        )
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()

        # CLOB returns valid price, market not closed
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.55}],
            "closed": False, "archived": False,
        }
        # Data-api: source still holds (so source_exited won't fire)
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.55}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])

        trade = self._get_trade()
        self.assertEqual(trade["status"], "stale")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestCheckPositionExit -v`
Expected: FAIL — `test_is_open_zero_does_not_close` will show status changed to `early_exit` (current broken behavior)

- [ ] **Step 3: Apply the fix to `_check_position_exit()`**

In `paper_trader.py`, modify `_check_position_exit()` (lines 834–970). Three changes:

**Change 1 — Line 876-877:** Replace `position_closed_by_source = True` with disqualification annotation:

```python
# BEFORE (line 876-877):
                    if signal_row is not None and int(signal_row["is_open"] or 0) == 0:
                        position_closed_by_source = True

# AFTER:
                    if signal_row is not None and int(signal_row["is_open"] or 0) == 0:
                        self._maybe_mark_disqualified(trade_id, "signal_is_open_0")
```

**Change 2 — Lines 901-918:** Replace inline data-api call with cached version. **Intentionally removes the `if current_price is None:` guard** so we always verify the source wallet's actual position size, even when CLOB returned a price. This is the key behavioral change — we now check source exit status on every cycle, not just when CLOB fails:

```python
# BEFORE (lines 901-918):
                # CLOB fallback: check wallet's current positions on data-api
                # (only useful if CLOB lookup failed entirely)
                if current_price is None:
                    try:
                        r = requests.get(
                            'https://data-api.polymarket.com/positions',
                            params={'user': wallet, 'sizeThreshold': 0, 'limit': 500},
                            timeout=8,
                        )
                        items = r.json() if r.ok else []
                        for p in items:
                            if str(p.get('market', '') or p.get('conditionId', '')).lower() == condition_id.lower():
                                current_price = float(p.get('curPrice') or p.get('currentPrice') or 0)
                                if float(p.get('size', 1)) < 0.01:
                                    position_closed_by_source = True
                                break
                    except Exception:
                        pass

# AFTER:
                # Check wallet's actual position via data-api (cached per cycle).
                # This serves two purposes:
                # 1. Get current_price if CLOB lookup failed
                # 2. Verify whether source actually exited (size < 0.01)
                wallet_positions = self._fetch_wallet_positions(wallet)
                source_size = None
                for p in wallet_positions:
                    cid = str(p.get('market', '') or p.get('conditionId', '')).lower()
                    if cid == condition_id.lower():
                        if current_price is None:
                            current_price = float(p.get('curPrice') or p.get('currentPrice') or 0)
                        source_size = float(p.get('size', 0))
                        if source_size < 0.01:
                            position_closed_by_source = True
                        break
```

**Change 3 — Line 939:** Simplify to just `if position_closed_by_source:` because all size-check paths now set this flag directly (the old `source_size` OR branch is absorbed into earlier assignments):

```python
# BEFORE (line 939):
            if position_closed_by_source or (source_size is not None and float(source_size) < 0.01):

# AFTER:
            if position_closed_by_source:
```

`position_closed_by_source` is now set True in exactly three places:
- CLOB `closed`/`archived` (line 897) — real market event
- Data-api `size < 0.01` (Change 2 above) — confirmed full exit via data-api
- Live signals `size < 0.01` (Change 4 below) — confirmed full exit via pre-fetched signals

The `source_size` check from the live signals list (line 857) also needs updating. Change line 857:

```python
# BEFORE:
                        source_size = item.get('size', 1)

# AFTER:
                        source_size = item.get('size')
```

And add after the live_signals loop (after line 858), before the `if current_price is None:` block:

```python
            # If found in live signals with near-zero size, source has exited
            if source_size is not None and float(source_size) < 0.01:
                position_closed_by_source = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestCheckPositionExit -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run full suite to verify no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add paper_trader.py tests/test_source_exit_correctness.py
git commit -m "fix: is_open=0 no longer closes paper trades — annotates as disqualified instead"
```

---

## Chunk 3: Sell Signal Fix + Repair Script

### Task 5: Fix `check_sell_signals()` — partial sells no longer close

**Files:**
- Modify: `paper_trader.py:1075-1127` (`check_sell_signals`)
- Test: `tests/test_source_exit_correctness.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_source_exit_correctness.py`:

```python
class TestCheckSellSignals(unittest.TestCase):
    """Test check_sell_signals() partial vs full exit."""

    def setUp(self):
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        self.trader._wallet_positions_cache = {}

        # Create paper_sell_signals table
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_sell_signals (
                wallet TEXT, condition_id TEXT, asset_id TEXT,
                price REAL, size REAL, timestamp TEXT,
                processed INTEGER DEFAULT 0
            );
        """)

        # Seed open paper trade
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status, opened_at, market_title) "
            "VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.50, 25.0, 'open', '2026-03-01', 'Test Market')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @patch("paper_trader.requests.get")
    def test_partial_sell_does_not_close(self, mock_get):
        """Sell signal where source still holds significant position → trade stays open."""
        # Insert unprocessed sell signal
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.55, 10.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        # Data-api: source still holds 490 shares (only sold 10)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {"market": "cond1", "size": 490.0, "curPrice": 0.55}
        ]
        mock_get.return_value = mock_resp

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertEqual(trade[0], "open")

        # Sell signal should still be marked processed
        sell = self.conn.execute("SELECT processed FROM paper_sell_signals").fetchone()
        self.assertEqual(sell[0], 1)

    @patch("paper_trader.requests.get")
    def test_full_sell_closes_trade(self, mock_get):
        """Sell signal where source has fully exited → trade closes."""
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.60, 500.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        # Data-api: source position is gone
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = []  # No positions found
        mock_get.return_value = mock_resp

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status, exit_reason FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertNotEqual(trade[0], "open")
        self.assertEqual(trade[1], "source_exited")

    @patch("paper_trader.requests.get")
    def test_api_failure_does_not_close(self, mock_get):
        """If data-api fails, don't close — err on side of keeping open."""
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.55, 10.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        mock_get.side_effect = Exception("network error")

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertEqual(trade[0], "open")  # Kept open on API failure
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestCheckSellSignals -v`
Expected: FAIL — current code closes immediately without checking remaining size

- [ ] **Step 3: Implement the fix**

Replace `check_sell_signals()` in `paper_trader.py` (lines 1075-1127):

```python
    def check_sell_signals(self):
        """Process sell signals from stream_low_buys.py paper_sell_signals table.

        Only closes the paper trade if the source wallet has fully exited
        (remaining position size < 0.01 via data-api). Partial sells are
        logged but do not trigger closure.
        """
        try:
            cursor = self.conn.execute(
                '''SELECT * FROM paper_sell_signals WHERE processed = 0'''
            )
            sell_signals = cursor.fetchall()
        except Exception as e:
            logger.debug(f"paper_sell_signals table not available: {e}")
            return

        for sell in sell_signals:
            wallet = sell['wallet']
            condition_id = sell['condition_id']
            sell_price = float(sell['price'] or 0)

            # Find matching open paper trade
            cursor2 = self.conn.execute(
                '''SELECT trade_id FROM paper_trades
                   WHERE wallet = ? AND condition_id = ? AND status = 'open'
                   LIMIT 1''',
                (wallet, condition_id)
            )
            match = cursor2.fetchone()
            if not match:
                self.conn.execute(
                    'UPDATE paper_sell_signals SET processed = 1 WHERE wallet = ? AND condition_id = ?',
                    (wallet, condition_id)
                )
                self.conn.commit()
                continue

            trade_id = match['trade_id']

            # Check remaining position size via data-api (cached per cycle)
            wallet_positions = self._fetch_wallet_positions(wallet)
            remaining_size = 0.0
            for p in wallet_positions:
                cid = str(p.get('market', '') or p.get('conditionId', '')).lower()
                if cid == condition_id.lower():
                    remaining_size = float(p.get('size', 0))
                    break

            # Mark sell signal processed regardless of outcome
            self.conn.execute(
                'UPDATE paper_sell_signals SET processed = 1 WHERE wallet = ? AND condition_id = ?',
                (wallet, condition_id)
            )
            self.conn.commit()

            if remaining_size >= 0.01:
                # Partial sell — source still holds. Keep paper trade open.
                logger.info(
                    f"Partial sell detected for {wallet[:8]}... "
                    f"(cond={condition_id[:8]}...) — remaining size={remaining_size:.2f}, keeping open"
                )
                continue

            # Full exit confirmed — close the trade
            if sell_price >= RESOLUTION_WIN_THRESHOLD:
                exit_cls = 'resolution_win'
            elif sell_price <= RESOLUTION_LOSS_THRESHOLD:
                exit_cls = 'resolution_loss'
            else:
                exit_cls = 'early_exit'

            self._close_position(trade_id, sell_price, 'source_exited', exit_cls)
            logger.info(f"✅ Source fully exited for {wallet[:8]}... (cond={condition_id[:8]}...) → {exit_cls}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_source_exit_correctness.py::TestCheckSellSignals -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run full suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add paper_trader.py tests/test_source_exit_correctness.py
git commit -m "fix: partial sells no longer close paper trades — verify remaining size first"
```

---

### Task 6: Create one-off repair script

**Files:**
- Create: `scripts/repair_false_source_exits.py`

- [ ] **Step 1: Write the repair script**

```python
#!/usr/bin/env python3
"""
One-off repair script: reopen paper trades falsely closed as 'source_exited'
when the source wallet never actually exited.

Default mode: dry-run (prints what would change).
Use --apply to actually mutate the database.

Criteria for reopening:
1. exit_reason = 'source_exited' (status is typically 'early_exit')
2. No matching sell signal in paper_sell_signals
3. Source wallet still holds position (data-api size > 0.01)
4. Market not resolved/archived (CLOB check)
5. No duplicate open trade for same (wallet, condition_id, outcome)
"""
import argparse
import sqlite3
import sys
import os
import requests
from datetime import datetime, timezone

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")


def main():
    parser = argparse.ArgumentParser(description="Repair falsely closed source_exited paper trades")
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB (default: dry-run)")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Ensure disqualification columns exist
    from db import ensure_paper_trades_disqualification_columns
    try:
        ensure_paper_trades_disqualification_columns(conn)
    except Exception:
        # Columns might already exist or table might not exist
        pass

    # Find candidates: closed trades with exit_reason='source_exited'
    candidates = conn.execute("""
        SELECT trade_id, wallet, condition_id, outcome, status, exit_reason,
               entry_price, exit_price, pnl_usdc, opened_at, closed_at
        FROM paper_trades
        WHERE exit_reason = 'source_exited'
        ORDER BY closed_at DESC
    """).fetchall()

    if not candidates:
        print("No source_exited trades found. Nothing to repair.")
        return

    print(f"Found {len(candidates)} trade(s) with exit_reason='source_exited'\n")
    print(f"{'Mode':>8}: {'DRY-RUN' if not args.apply else 'APPLY'}\n")

    reopened = []
    skipped = []

    for trade in candidates:
        trade_id = trade["trade_id"]
        wallet = trade["wallet"]
        condition_id = trade["condition_id"]
        outcome = trade["outcome"]
        prefix = f"  Trade #{trade_id} ({wallet[:10]}... / {condition_id[:16]}...)"

        # Check 1: No matching sell signal
        sell = conn.execute(
            "SELECT 1 FROM paper_sell_signals WHERE wallet = ? AND condition_id = ? LIMIT 1",
            (wallet, condition_id)
        ).fetchone()
        if sell:
            skipped.append((trade_id, "has matching sell signal"))
            print(f"{prefix}: SKIP — has matching sell signal")
            continue

        # Check 2: Source wallet still holds position
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": wallet, "sizeThreshold": 0, "limit": 500},
                timeout=10,
            )
            positions = r.json() if r.ok else []
        except Exception as e:
            skipped.append((trade_id, f"data-api error: {e}"))
            print(f"{prefix}: SKIP — data-api error: {e}")
            continue

        source_size = 0.0
        for p in positions:
            cid = str(p.get("market", "") or p.get("conditionId", "")).lower()
            if cid == condition_id.lower():
                source_size = float(p.get("size", 0))
                break

        if source_size < 0.01:
            skipped.append((trade_id, f"source has exited (size={source_size:.4f})"))
            print(f"{prefix}: SKIP — source has actually exited (size={source_size:.4f})")
            continue

        # Check 3: Market not resolved/archived
        try:
            r = requests.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=8,
            )
            if r.ok:
                m = r.json()
                if m.get("closed") or m.get("archived"):
                    skipped.append((trade_id, "market closed/archived"))
                    print(f"{prefix}: SKIP — market closed/archived")
                    continue
        except Exception:
            pass  # If CLOB fails, proceed with reopen (conservative)

        # Check 4: No duplicate open trade
        dup = conn.execute(
            "SELECT 1 FROM paper_trades WHERE wallet = ? AND condition_id = ? AND outcome = ? AND status = 'open' LIMIT 1",
            (wallet, condition_id, outcome)
        ).fetchone()
        if dup:
            skipped.append((trade_id, "duplicate open trade exists"))
            print(f"{prefix}: SKIP — duplicate open trade exists")
            continue

        # All checks passed — this trade was falsely closed
        print(f"{prefix}: REOPEN — source still holds {source_size:.2f} shares")
        reopened.append(trade_id)

        if args.apply:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                UPDATE paper_trades SET
                    status = 'open',
                    exit_price = NULL,
                    exit_reason = NULL,
                    exit_classification = NULL,
                    closed_at = NULL,
                    pnl_usdc = NULL,
                    roi_percent = NULL,
                    signal_disqualified_at = ?,
                    signal_disqualification_reason = ?
                WHERE trade_id = ?
            """, (now, "reopened: false source_exited (analyzer reclassification)", trade_id))
            conn.commit()

    print(f"\n{'='*60}")
    print(f"Summary: {len(reopened)} to reopen, {len(skipped)} skipped")
    if reopened:
        print(f"Trade IDs to reopen: {reopened}")
    if skipped:
        print("Skipped:")
        for tid, reason in skipped:
            print(f"  #{tid}: {reason}")
    if reopened and not args.apply:
        print(f"\nRe-run with --apply to execute: python3 scripts/repair_false_source_exits.py --apply")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify script runs in dry-run mode locally**

Run: `python3 scripts/repair_false_source_exits.py --db sniper_tracker.db`
Expected: Either "No source_exited trades found" (local DB) or a list of candidates with SKIP/REOPEN status. No DB mutations.

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass (script has no test dependencies)

- [ ] **Step 4: Commit**

```bash
git add scripts/repair_false_source_exits.py
git commit -m "feat: add one-off repair script for falsely closed source_exited trades"
```

---

### Task 7: UI — disqualification badge on paper trades

**Files:**
- Modify: `routes/api_misc.py:188-196` (add columns to SELECT)
- Modify: `static/app.js` (render badge)

- [ ] **Step 1: Add disqualification columns to paper trades API query**

In `routes/api_misc.py`, update the SELECT in `api_paper_trades()` (line 188-196) to include the two new columns:

```python
# BEFORE:
        rows = conn.execute("""
            SELECT trade_id, wallet, condition_id, outcome, market_title, direction,
                   entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                   signal_type, exit_classification, exit_reason,
                   signal_insider_score, wallet_win_rate, signal_entry_price, slippage_pct,
                   opened_at, closed_at, status, position_signal_id
            FROM paper_trades
            ORDER BY opened_at DESC
        """).fetchall()

# AFTER:
        rows = conn.execute("""
            SELECT trade_id, wallet, condition_id, outcome, market_title, direction,
                   entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                   signal_type, exit_classification, exit_reason,
                   signal_insider_score, wallet_win_rate, signal_entry_price, slippage_pct,
                   opened_at, closed_at, status, position_signal_id,
                   signal_disqualified_at, signal_disqualification_reason
            FROM paper_trades
            ORDER BY opened_at DESC
        """).fetchall()
```

- [ ] **Step 2: Add disqualification badge rendering in app.js**

In `static/app.js`, find where paper trade rows are rendered (search for `status === 'open'` or the paper trades table rendering). Add a badge after the status cell for open trades that have `signal_disqualified_at`:

```javascript
// In the paper trade row rendering, after the status badge:
if (trade.status === 'open' && trade.signal_disqualified_at) {
    statusHtml += ' <span class="badge badge-warn" title="Signal disqualified: ' +
        (trade.signal_disqualification_reason || 'unknown') +
        ' at ' + trade.signal_disqualified_at +
        '">disqualified</span>';
}
```

The exact insertion point depends on how the paper trades table is rendered. Search for the paper trades rendering code and add the badge inline.

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add routes/api_misc.py static/app.js
git commit -m "feat: show disqualification badge on open paper trades in UI"
```

---

### Task 8: Final integration verification

**Files:**
- No new files — verification only

- [ ] **Step 1: Run all new tests**

Run: `python3 -m pytest tests/test_source_exit_correctness.py -v`
Expected: All 19 tests pass:
- `TestDisqualificationColumns` (2)
- `TestMaybeMarkDisqualified` (4)
- `TestWalletPositionCache` (4)
- `TestCheckPositionExit` (6)
- `TestCheckSellSignals` (3)

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests pass (baseline + new tests)

- [ ] **Step 3: Verify no import issues**

Run: `python3 -c "from paper_trader import PaperTrader; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Final commit if any loose ends**

```bash
git status
# If anything unstaged, add and commit
```
