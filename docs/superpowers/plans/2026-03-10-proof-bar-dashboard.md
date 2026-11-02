# Proof Bar Dashboard Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent proof strip and dedicated Proof Bar tab showing real-time go-live readiness against the 5 proof bar criteria.

**Architecture:** New `proof_bar_status(conn)` function in `db.py` computes all metrics from `paper_trades`. New `/api/proof-bar` endpoint in `routes/api_core.py` serves the data. Frontend adds a proof strip below the header (persistent across tabs) and a 9th "Proof Bar" tab with blockers, criteria cards, health, pace, quality trend, and monthly breakdown.

**Tech Stack:** Python/SQLite (db.py), Flask (routes/api_core.py), vanilla JS + Canvas (app.js), HTML (index.html), CSS (styles.css)

**Spec:** `docs/superpowers/specs/2026-03-10-proof-bar-dashboard-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `web_settings.py` | Modify (line ~124) | Add 5 PROOF_* env constants |
| `db.py` | Modify (append after line ~9308) | Add `proof_bar_status(conn)` + helpers |
| `routes/api_core.py` | Modify (before `return bp`, line ~1387) | Add `GET /api/proof-bar` endpoint |
| `mcp_tools/read_tools.py` | Modify (append in register function) | Add `get_proof_bar_status` MCP tool |
| `templates/index.html` | Modify (lines 31-32, 633) | Add proof strip + proof-bar nav item + tab panel |
| `static/app.js` | Modify (line 10, ~3853, ~3865) | Add `proofBar` module + strip renderer + charts |
| `static/styles.css` | Modify (append) | Add proof strip + proof tab styles |
| `tests/test_proof_bar.py` | Create | All proof bar tests |

---

## Chunk 1: Backend (Tasks 1-4)

### Task 1: Configuration Constants

**Files:**
- Modify: `web_settings.py:124` (after PAPER_TAKE_PROFIT_PCT line)

- [ ] **Step 1: Add PROOF_* constants to web_settings.py**

Open `web_settings.py` and add the following block after line 124 (`PAPER_TAKE_PROFIT_PCT`):

```python
# === PROOF BAR CONFIG ===
PROOF_MIN_TRADES = int(os.getenv("PROOF_MIN_TRADES", "150"))
PROOF_MIN_INSIDER = int(os.getenv("PROOF_MIN_INSIDER", "60"))
PROOF_MIN_WIN_RATE = float(os.getenv("PROOF_MIN_WIN_RATE", "60.0"))
PROOF_SLIPPAGE_PCT = float(os.getenv("PROOF_SLIPPAGE_PCT", "0.025"))
PROOF_MIN_MONTHS = int(os.getenv("PROOF_MIN_MONTHS", "2"))
```

- [ ] **Step 2: Verify import works**

Run: `python3 -c "from web_settings import PROOF_MIN_TRADES, PROOF_SLIPPAGE_PCT; print(PROOF_MIN_TRADES, PROOF_SLIPPAGE_PCT)"`
Expected: `150 0.025`

- [ ] **Step 3: Commit**

```bash
git add web_settings.py
git commit -m "feat(proof-bar): add PROOF_* config constants to web_settings"
```

---

### Task 2: DB Helper — proof_bar_status()

**Files:**
- Modify: `db.py` (append after `paper_trade_gate_status` block, ~line 9308)
- Create: `tests/test_proof_bar.py`

This is the core computation. It queries `paper_trades` for qualifying closed trades, computes all 5 criteria, and returns the full status dict.

- [ ] **Step 1: Write the test file with core test cases**

Create `tests/test_proof_bar.py`:

```python
"""Tests for proof_bar_status() and related helpers."""
import unittest
import time
from datetime import datetime, timezone, timedelta

from db import get_connection, init_db, proof_bar_status


def _seed_paper_trades(conn, trades):
    """Seed paper_trades with list of trade tuples.

    Each tuple: (wallet, condition_id, outcome, direction, entry_price,
                 exit_price, size_usdc, pnl_usdc, roi_percent,
                 signal_insider_score, status, opened_at, closed_at)
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT,
            outcome TEXT, direction TEXT, entry_price REAL, exit_price REAL,
            size_usdc REAL, pnl_usdc REAL, roi_percent REAL,
            signal_insider_score REAL, signal_speculator_score REAL,
            status TEXT, opened_at TEXT, closed_at TEXT,
            market_title TEXT, shares REAL, position_value_at_entry REAL,
            stop_loss_price REAL, take_profit_price REAL,
            signal_type TEXT DEFAULT 'unknown', exit_classification TEXT,
            stale_flagged_at TEXT, exit_reason TEXT,
            wallet_win_rate REAL, current_price REAL,
            alert_sent_entry INTEGER DEFAULT 0, alert_sent_exit INTEGER DEFAULT 0,
            position_signal_id INTEGER
        );
    """)
    conn.executemany(
        """INSERT INTO paper_trades
           (wallet, condition_id, outcome, direction, entry_price, exit_price,
            size_usdc, pnl_usdc, roi_percent, signal_insider_score,
            status, opened_at, closed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        trades,
    )
    conn.commit()


class TestProofBarStatus(unittest.TestCase):
    """Test proof_bar_status() computation."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_db_returns_zero_state(self):
        """No paper_trades → all zeros, not_ready."""
        result = proof_bar_status(self.conn)
        self.assertEqual(result["overall"], "not_ready")
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 0)
        self.assertEqual(result["criteria"]["win_rate"]["value"], 0.0)
        self.assertEqual(result["criteria"]["slip_pnl"]["value"], 0.0)
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 0)

    def test_insider_score_filter(self):
        """Only trades with signal_insider_score >= 60 count."""
        trades = [
            # insider=80 → qualifies
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # insider=50 → excluded
            ("0xb", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 50,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)

    def test_status_filter_excludes_stale_and_source_exited(self):
        """Only resolution_win/loss, early_exit, manual count."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 0.5, 25.0, 5.0, 20.0, 80,
             "source_exited", "2026-03-01", "2026-03-05"),
            ("0xa", "c3", "yes", "buy", 0.40, 0.5, 25.0, 5.0, 20.0, 80,
             "stale", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)

    def test_win_rate_uses_entry_price_filter(self):
        """WR denominator only includes entry_price < 0.80 (ADR-002)."""
        trades = [
            # entry=0.40 < 0.80, win → counts in WR
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # entry=0.85 >= 0.80, win → excluded from WR
            ("0xa", "c2", "yes", "buy", 0.85, 1.0, 25.0, 4.0, 16.0, 80,
             "resolution_win", "2026-03-01", "2026-03-06"),
            # entry=0.50 < 0.80, loss → counts in WR
            ("0xa", "c3", "yes", "buy", 0.50, 0.0, 25.0, -25.0, -100.0, 80,
             "resolution_loss", "2026-03-01", "2026-03-07"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # 3 trades count toward trade count
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 3)
        # WR: 1 win / 2 qualifying = 50%
        self.assertAlmostEqual(result["criteria"]["win_rate"]["value"], 50.0, places=1)

    def test_slippage_calculation_one_sided(self):
        """Slip PnL = sum(roi/100 * size - size * 0.025) across all qualifying."""
        trades = [
            # roi=60%, size=25 → raw_pnl=15, slip=0.625, net=14.375
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # raw_pnl = 60/100 * 25 = 15.0; slippage = 25 * 0.025 = 0.625
        # slip_pnl = 15.0 - 0.625 = 14.375
        self.assertAlmostEqual(result["criteria"]["slip_pnl"]["value"], 14.375, places=2)

    def test_null_roi_excluded_from_pnl(self):
        """Trades with NULL roi_percent are excluded from P&L but counted in trades."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # NULL roi_percent
            ("0xa", "c2", "yes", "buy", 0.40, None, 25.0, None, None, 80,
             "manual", "2026-03-01", "2026-03-06"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 2)
        # Only the first trade contributes to P&L
        self.assertAlmostEqual(result["criteria"]["slip_pnl"]["value"], 14.375, places=2)
        self.assertEqual(result["data_gaps"]["null_roi"], 1)

    def test_monthly_grouping_complete_vs_incomplete(self):
        """Complete months count toward criterion 4; in-progress months don't."""
        # Use a date far in the past so both months are "complete"
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-01-01", "2025-01-15"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-02-01", "2025-02-15"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        monthly = result["monthly"]
        self.assertEqual(len(monthly), 2)
        self.assertTrue(all(m["complete"] for m in monthly))
        # Both months profitable → criterion value = 2
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 2)

    def test_losing_month_blocks_criterion(self):
        """If ANY complete month is unprofitable, criterion 4 = 0 (fail)."""
        trades = [
            # 2025-01: profitable
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-01-01", "2025-01-15"),
            # 2025-02: unprofitable (loss > slippage-adjusted gain)
            ("0xa", "c2", "yes", "buy", 0.40, 0.0, 100.0, -100.0, -100.0, 80,
             "resolution_loss", "2025-02-01", "2025-02-15"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # 2 complete months but 1 is losing → value should be 0
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 0)
        self.assertEqual(result["criteria"]["no_losing_month"]["status"], "fail")

    def test_overall_ready_when_all_pass(self):
        """Overall = ready only when all 5 criteria pass."""
        # Seed 150+ winning trades across 2+ past months
        trades = []
        for i in range(160):
            month = "01" if i < 80 else "02"
            trades.append(
                ("0xa", f"c{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
                 "resolution_win", f"2025-{month}-01", f"2025-{month}-15")
            )
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["status"], "pass")
        self.assertEqual(result["criteria"]["win_rate"]["status"], "pass")
        self.assertEqual(result["criteria"]["slip_pnl"]["status"], "pass")
        self.assertEqual(result["criteria"]["no_losing_month"]["status"], "pass")
        self.assertEqual(result["overall"], "ready")

    def test_warn_threshold_win_rate(self):
        """WR within 2% of target → warn (not fail)."""
        # 59% WR: 59 wins, 41 losses (all qualifying entry_price < 0.80)
        trades = []
        for i in range(59):
            trades.append(("0xa", f"w{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
                           "resolution_win", "2026-03-01", "2026-03-05"))
        for i in range(41):
            trades.append(("0xa", f"l{i}", "yes", "buy", 0.40, 0.0, 25.0, -25.0, -100.0, 80,
                           "resolution_loss", "2026-03-01", "2026-03-05"))
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["win_rate"]["status"], "warn")

    def test_pace_calculations(self):
        """Pace section shows 7d rate and trades remaining."""
        trades = [
            ("0xa", f"c{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01",
             (datetime(2026, 3, 10, tzinfo=timezone.utc) - timedelta(days=i % 7)).isoformat())
            for i in range(10)
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertIn("pace", result)
        self.assertEqual(result["pace"]["trades_to_go"], 140)
        self.assertGreater(result["pace"]["seven_day_rate"], 0)

    def test_entry_price_zero_excluded(self):
        """Trades with entry_price <= 0 are excluded."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.0, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)
        self.assertEqual(result["data_gaps"]["zero_entry_price"], 1)

    def test_chart_data_daily_granularity(self):
        """Chart data has one point per day."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-07"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        cum_trades = result["charts"]["cumulative_trades"]
        self.assertGreater(len(cum_trades), 0)
        # Each point has date and count keys
        self.assertIn("date", cum_trades[0])
        self.assertIn("count", cum_trades[0])

    def test_win_is_pnl_positive(self):
        """A 'win' is defined as pnl_usdc > 0."""
        trades = [
            # pnl > 0 → win
            ("0xa", "c1", "yes", "buy", 0.40, 0.60, 25.0, 5.0, 20.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
            # pnl < 0 → loss
            ("0xa", "c2", "yes", "buy", 0.40, 0.30, 25.0, -2.5, -10.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
            # pnl = 0 → not a win
            ("0xa", "c3", "yes", "buy", 0.40, 0.40, 25.0, 0.0, 0.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # WR: 1 win / 3 qualifying (all entry < 0.80) = 33.3%
        self.assertAlmostEqual(result["criteria"]["win_rate"]["value"], 33.3, places=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_proof_bar.py -v`
Expected: FAIL — `ImportError: cannot import name 'proof_bar_status' from 'db'`

- [ ] **Step 3: Implement proof_bar_status() in db.py**

Add the following after the `paper_trade_gate_status` block (after line ~9308 in `db.py`):

```python
# ---------------------------------------------------------------------------
# Proof Bar — go-live readiness dashboard
# ---------------------------------------------------------------------------

def proof_bar_status(conn: sqlite3.Connection) -> dict:
    """Compute all 5 proof bar criteria and supporting data.

    Returns a dict with: criteria, overall, pace, monthly, health_details,
    charts, data_gaps.  See spec for full schema.
    """
    from web_settings import (
        PROOF_MIN_TRADES, PROOF_MIN_INSIDER, PROOF_MIN_WIN_RATE,
        PROOF_SLIPPAGE_PCT, PROOF_MIN_MONTHS,
    )
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    # --- Qualifying statuses (clean resolution paths only) ---
    PROOF_STATUSES = ("resolution_win", "resolution_loss", "early_exit", "manual")
    placeholders = ",".join("?" for _ in PROOF_STATUSES)

    # --- Fetch all qualifying closed trades ---
    rows = conn.execute(
        f"""
        SELECT entry_price, exit_price, size_usdc, pnl_usdc, roi_percent,
               signal_insider_score, status, closed_at
        FROM paper_trades
        WHERE signal_insider_score >= ?
          AND status IN ({placeholders})
        """,
        (PROOF_MIN_INSIDER, *PROOF_STATUSES),
    ).fetchall()

    # --- Filter and compute ---
    trades = []
    data_gaps = {"null_roi": 0, "zero_entry_price": 0}

    for r in rows:
        entry_price = r["entry_price"] if r["entry_price"] is not None else 0
        if entry_price <= 0:
            data_gaps["zero_entry_price"] += 1
            continue
        trades.append(dict(r))

    trade_count = len(trades)

    # Win rate (ADR-002: entry_price < 0.80 only)
    wr_trades = [t for t in trades if t["entry_price"] < 0.80]
    wr_wins = sum(1 for t in wr_trades if (t["pnl_usdc"] or 0) > 0)
    wr_denom = len(wr_trades)
    win_rate = (wr_wins / wr_denom * 100) if wr_denom > 0 else 0.0

    # Slippage-adjusted P&L (one-sided, entry only)
    slip_pnl = 0.0
    for t in trades:
        roi = t["roi_percent"]
        size = t["size_usdc"] or 0
        if roi is None:
            data_gaps["null_roi"] += 1
            continue
        raw_pnl = (roi / 100) * size
        slippage = size * PROOF_SLIPPAGE_PCT
        slip_pnl += raw_pnl - slippage

    slip_pnl = round(slip_pnl, 2)

    # Monthly breakdown
    month_buckets: dict[str, list] = {}
    for t in trades:
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        month_key = str(closed_at)[:7]  # "YYYY-MM"
        month_buckets.setdefault(month_key, []).append(t)

    monthly = []
    complete_months = 0
    losing_months = 0
    for month_key in sorted(month_buckets.keys()):
        bucket = month_buckets[month_key]
        m_trades = len(bucket)
        m_wr_trades = [t for t in bucket if t["entry_price"] < 0.80]
        m_wr_wins = sum(1 for t in m_wr_trades if (t["pnl_usdc"] or 0) > 0)
        m_wr = (m_wr_wins / len(m_wr_trades) * 100) if m_wr_trades else 0
        m_pnl_raw = sum(t["pnl_usdc"] or 0 for t in bucket)
        m_pnl_slip = sum(
            ((t["roi_percent"] or 0) / 100) * (t["size_usdc"] or 0)
            - (t["size_usdc"] or 0) * PROOF_SLIPPAGE_PCT
            for t in bucket if t["roi_percent"] is not None
        )
        # Wins/losses for display (all trades, not just WR-qualifying)
        m_wins = sum(1 for t in bucket if (t["pnl_usdc"] or 0) > 0)
        m_losses = m_trades - m_wins

        # Month is complete if we are past its last day
        try:
            year, mon = int(month_key[:4]), int(month_key[5:7])
            if mon == 12:
                first_of_next = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                first_of_next = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
            complete = now >= first_of_next
        except (ValueError, IndexError):
            complete = False

        profitable = m_pnl_slip > 0
        if complete:
            complete_months += 1
            if not profitable:
                losing_months += 1

        monthly.append({
            "month": month_key,
            "trades": m_trades,
            "wins": m_wins,
            "losses": m_losses,
            "win_rate": round(m_wr, 1),
            "pnl_raw": round(m_pnl_raw, 2),
            "pnl_slip": round(m_pnl_slip, 2),
            "complete": complete,
            "profitable": profitable,
        })

    # Criterion 4: need PROOF_MIN_MONTHS complete months with ZERO losing
    no_losing_month_value = complete_months if losing_months == 0 else 0

    # --- Status per criterion ---
    def _status(value, target, warn_fn):
        if value >= target:
            return "pass"
        if warn_fn(value, target):
            return "warn"
        return "fail"

    criteria = {
        "closed_trades": {
            "value": trade_count,
            "target": PROOF_MIN_TRADES,
            "status": _status(trade_count, PROOF_MIN_TRADES,
                              lambda v, t: v >= t * 0.9),
        },
        "win_rate": {
            "value": round(win_rate, 1),
            "target": PROOF_MIN_WIN_RATE,
            "status": _status(win_rate, PROOF_MIN_WIN_RATE,
                              lambda v, t: v >= t - 2.0),
        },
        "slip_pnl": {
            "value": slip_pnl,
            "target": 0.0,
            "status": _status(slip_pnl, 0.01,  # must be strictly > 0
                              lambda v, t: 0 < v < 50),
        },
        "no_losing_month": {
            "value": no_losing_month_value,
            "target": PROOF_MIN_MONTHS,
            "status": _status(no_losing_month_value, PROOF_MIN_MONTHS,
                              lambda v, t: v == t - 1),
        },
        "health": {
            "value": "pass",
            "target": "pass",
            "status": "pass",  # computed separately via health_check
        },
    }

    overall = "ready" if all(
        c["status"] == "pass" for c in criteria.values()
    ) else "not_ready"

    # --- Pace ---
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    recent_trades = [t for t in trades if (t.get("closed_at") or "") >= seven_days_ago]
    seven_day_rate = round(len(recent_trades) / 7, 1) if recent_trades else 0
    trades_to_go = max(0, PROOF_MIN_TRADES - trade_count)

    eta_date = None
    if seven_day_rate > 0 and trades_to_go > 0:
        days_remaining = trades_to_go / seven_day_rate
        eta_date = (now + timedelta(days=days_remaining)).strftime("%Y-%m-%d")

    # Open exposure
    open_row = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_usdc), 0) AS total "
        "FROM paper_trades WHERE status = 'open'"
    ).fetchone()
    open_positions = int(open_row["cnt"] or 0)
    open_exposure = round(float(open_row["total"] or 0), 2)

    pace = {
        "seven_day_rate": seven_day_rate,
        "trades_to_go": trades_to_go,
        "eta_date": eta_date,
        "cumulative_equity": slip_pnl,
        "open_exposure": open_exposure,
        "open_positions": open_positions,
    }

    # --- Chart data (daily granularity) ---
    # Cumulative trades by closed_at date
    sorted_trades = sorted(trades, key=lambda t: t.get("closed_at") or "")
    cum_trades_chart = []
    cum_count = 0
    last_date = None
    for t in sorted_trades:
        d = str(t.get("closed_at") or "")[:10]
        if not d:
            continue
        cum_count += 1
        if d != last_date:
            cum_trades_chart.append({"date": d, "count": cum_count})
            last_date = d
        else:
            cum_trades_chart[-1]["count"] = cum_count

    # 30-day equity curve (daily cumulative slip-adj P&L)
    equity_chart = []
    cum_eq = 0.0
    last_eq_date = None
    for t in sorted_trades:
        roi = t["roi_percent"]
        if roi is None:
            continue
        size = t["size_usdc"] or 0
        pnl = (roi / 100) * size - size * PROOF_SLIPPAGE_PCT
        cum_eq += pnl
        d = str(t.get("closed_at") or "")[:10]
        if not d:
            continue
        if d != last_eq_date:
            equity_chart.append({"date": d, "equity": round(cum_eq, 2)})
            last_eq_date = d
        else:
            equity_chart[-1]["equity"] = round(cum_eq, 2)

    # --- Health (point-in-time, populated by caller or API layer) ---
    health_details = {}  # populated by API endpoint using health_check module

    return {
        "criteria": criteria,
        "overall": overall,
        "pace": pace,
        "monthly": monthly,
        "health_details": health_details,
        "charts": {
            "cumulative_trades": cum_trades_chart,
            "equity_curve_30d": equity_chart,
        },
        "data_gaps": data_gaps,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_proof_bar.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: All existing tests pass + new proof bar tests pass

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_proof_bar.py
git commit -m "feat(proof-bar): add proof_bar_status() DB helper with tests"
```

---

### Task 3: API Endpoint

**Files:**
- Modify: `routes/api_core.py` (before `return bp`, line ~1387)
- Modify: `tests/test_proof_bar.py` (add API test)

- [ ] **Step 1: Add test for API endpoint**

Append to `tests/test_proof_bar.py`:

```python
class TestProofBarAPI(unittest.TestCase):
    """Test /api/proof-bar endpoint returns valid data."""

    def test_endpoint_returns_criteria(self):
        """Smoke test: endpoint returns dict with required keys."""
        # This is a lightweight integration test — we test the DB function
        # directly since Flask app setup is complex. The real integration
        # test is manual: GET /api/proof-bar on the running server.
        conn = get_connection(":memory:")
        init_db(conn)
        result = proof_bar_status(conn)
        conn.close()
        self.assertIn("criteria", result)
        self.assertIn("overall", result)
        self.assertIn("pace", result)
        self.assertIn("monthly", result)
        self.assertIn("charts", result)
        self.assertIn("data_gaps", result)
        self.assertIn("closed_trades", result["criteria"])
        self.assertIn("win_rate", result["criteria"])
        self.assertIn("slip_pnl", result["criteria"])
        self.assertIn("no_losing_month", result["criteria"])
        self.assertIn("health", result["criteria"])
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/test_proof_bar.py::TestProofBarAPI -v`
Expected: PASS

- [ ] **Step 3: Add endpoint to routes/api_core.py**

Add the following before the `return bp` line (line ~1387) in `routes/api_core.py`:

```python
    @bp.get("/api/proof-bar")
    def api_proof_bar():
        from db import proof_bar_status
        from health_check import get_health_summary

        result = proof_bar_status(ctx["conn"])

        # Populate health details from health_check module + direct queries
        try:
            health = get_health_summary()
            orphan_check = {c["name"]: c for c in health.get("checks", [])}.get("orphaned_paper_trades", {})

            # Stream freshness: check last_trade_ts from health summary
            last_ts = health.get("last_trade_ts")
            stream_ok = last_ts is not None  # has recent trades
            stream_detail = f"Last trade ts: {last_ts}" if last_ts else "No trades recorded"

            # Paper trader: use orphan check from health_check
            orphan_ok = orphan_check.get("passed", True)
            orphan_detail = orphan_check.get("detail", "")

            # Paper summary from health
            ps = health.get("paper_summary", {})

            result["health_details"] = {
                "stream": {"status": "ok" if stream_ok else "fail", "detail": stream_detail, "proof_required": True},
                "analyzer": {"status": "ok", "detail": f"{health.get('table_counts', {}).get('user_analysis', 0)} wallets scored", "proof_required": True},
                "paper_trader": {"status": "ok" if orphan_ok else "fail", "detail": orphan_detail or f"{ps.get('closed_count', 0)} closed trades", "proof_required": True},
                "data_integrity": {"status": "ok" if orphan_ok else "fail", "detail": f"Orphans: {0 if orphan_ok else 'found'}", "proof_required": True},
                "db_size": {"status": "ok", "detail": f"{health.get('db_size_mb', 0)} MB, WAL mode", "proof_required": False},
                "alerter": {"status": "warn", "detail": "Check Telegram config", "proof_required": False},
            }

            # Update health criterion based on proof-required checks only
            proof_required_ok = all(
                v["status"] == "ok"
                for v in result["health_details"].values()
                if v.get("proof_required")
            )
            result["criteria"]["health"]["value"] = "pass" if proof_required_ok else "fail"
            result["criteria"]["health"]["status"] = "pass" if proof_required_ok else "fail"

            # Check if any advisory is degraded → warn (but not fail)
            if proof_required_ok:
                any_advisory_warn = any(
                    v["status"] != "ok"
                    for v in result["health_details"].values()
                    if not v.get("proof_required")
                )
                if any_advisory_warn:
                    result["criteria"]["health"]["status"] = "warn"

        except Exception:
            result["health_details"] = {}
            result["criteria"]["health"]["value"] = "unknown"
            result["criteria"]["health"]["status"] = "warn"

        # Recompute overall after health
        result["overall"] = "ready" if all(
            c["status"] == "pass" for c in result["criteria"].values()
        ) else "not_ready"

        return jsonify({
            **result,
            "updated_at": _updated_at(),
        })
```

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add routes/api_core.py tests/test_proof_bar.py
git commit -m "feat(proof-bar): add GET /api/proof-bar endpoint"
```

---

### Task 4: MCP Tool

**Files:**
- Modify: `mcp_tools/read_tools.py` (add internal function + register tool)
- Modify: `tests/test_proof_bar.py` (add MCP tool test)

- [ ] **Step 1: Add MCP tool test**

Append to `tests/test_proof_bar.py`:

```python
class TestProofBarMCPTool(unittest.TestCase):
    """Test get_proof_bar_status MCP tool."""

    def test_returns_envelope(self):
        from mcp_tools.read_tools import proof_bar_mcp
        conn = get_connection(":memory:")
        init_db(conn)
        result = proof_bar_mcp(conn=conn)
        conn.close()
        self.assertIn("data", result)
        self.assertIn("data_source", result)
        self.assertEqual(result["data_source"], "proof_bar")
        self.assertIn("criteria", result["data"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_proof_bar.py::TestProofBarMCPTool -v`
Expected: FAIL — `ImportError: cannot import name 'proof_bar_mcp'`

- [ ] **Step 3: Add internal function to mcp_tools/read_tools.py**

Add before the `register_read_tools` function:

```python
def proof_bar_mcp(*, conn=None, db_path: str | None = None) -> dict:
    """Internal implementation for get_proof_bar_status tool."""
    from db import proof_bar_status
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        data = proof_bar_status(conn)
        return make_envelope(data, data_source="proof_bar", last_write_ts=_db_file_mtime(db_path))
    finally:
        if close_conn:
            conn.close()
```

- [ ] **Step 4: Register tool in register_read_tools()**

Add inside `register_read_tools()` (after the last `@mcp_server.tool()` block):

```python
    @mcp_server.tool()
    def get_proof_bar_status() -> dict:
        """Get proof bar go-live readiness: 5 criteria status, pace, monthly breakdown, and health. Used to check if system is ready for real-money deployment."""
        return proof_bar_mcp()
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_proof_bar.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add mcp_tools/read_tools.py tests/test_proof_bar.py
git commit -m "feat(proof-bar): add get_proof_bar_status MCP tool"
```

---

## Chunk 2: Frontend (Tasks 5-7)

### Task 5: HTML Structure — Proof Strip + Tab

**Files:**
- Modify: `templates/index.html` (3 insertion points)

- [ ] **Step 1: Add proof strip HTML after the header**

In `templates/index.html`, after the closing `</header>` tag (line ~36), add:

```html
            <!-- PROOF STRIP (persistent across all tabs) -->
            <div class="proof-strip" id="proof-strip">
                <span class="strip-label">PROOF</span>
                <a class="strip-chip fail" id="strip-trades" data-proof="closed-trades">
                    <span class="strip-icon"></span>
                    <span class="strip-metric">Trades</span>
                    <span class="strip-value">--</span>
                    <span class="strip-target">/ 150</span>
                </a>
                <span class="strip-divider"></span>
                <a class="strip-chip fail" id="strip-wr" data-proof="win-rate">
                    <span class="strip-icon"></span>
                    <span class="strip-metric">WR</span>
                    <span class="strip-value">--%</span>
                    <span class="strip-target">/ 60%</span>
                </a>
                <span class="strip-divider"></span>
                <a class="strip-chip pass" id="strip-pnl" data-proof="slip-pnl">
                    <span class="strip-icon"></span>
                    <span class="strip-metric">Slip PnL</span>
                    <span class="strip-value">--</span>
                </a>
                <span class="strip-divider"></span>
                <a class="strip-chip fail" id="strip-months" data-proof="no-losing-month">
                    <span class="strip-icon"></span>
                    <span class="strip-metric">Months</span>
                    <span class="strip-value">--</span>
                    <span class="strip-target">/ 2</span>
                </a>
                <span class="strip-divider"></span>
                <a class="strip-chip pass" id="strip-health" data-proof="health">
                    <span class="strip-icon"></span>
                    <span class="strip-metric">Health</span>
                    <span class="strip-value">--</span>
                </a>
                <span class="strip-badge not-ready" id="strip-badge">NOT READY</span>
            </div>
```

- [ ] **Step 2: Add Proof Bar nav item**

In the `<nav class="topnav">` section (after the Research nav item, line ~31), add:

```html
                    <a href="#" class="nav-item" data-tab="proof-bar"><span class="nav-label">Proof Bar</span></a>
```

- [ ] **Step 3: Add Proof Bar tab panel**

After the Research tab's closing `</section>` (after line ~730, before the closing `</div>` of `content-area`), add:

```html
                <!-- PROOF BAR TAB -->
                <section id="tab-proof-bar" class="tab-panel">
                    <div class="panel-header">
                        <div>
                            <h1 class="panel-title">Proof Bar &mdash; Go-Live Readiness</h1>
                            <p class="panel-subtitle proof-definition">Paper trades &bull; insider &ge; 60 &bull; slippage haircut 2.5% &bull; full calendar months only</p>
                        </div>
                    </div>

                    <!-- Blockers -->
                    <div id="proof-blockers" class="proof-blockers" hidden></div>

                    <!-- Criteria cards -->
                    <div id="proof-criteria-grid" class="proof-criteria-grid"></div>

                    <!-- Health panel -->
                    <div id="proof-health" class="proof-section">
                        <h3 class="section-heading">System Health</h3>
                        <div id="proof-health-grid" class="proof-health-grid"></div>
                    </div>

                    <!-- Pace & Accumulation -->
                    <div class="proof-section">
                        <h3 class="section-heading">Pace &amp; Accumulation</h3>
                        <div id="proof-pace-grid" class="proof-pace-grid"></div>
                        <div class="proof-chart-wrap">
                            <canvas id="proof-cum-trades-chart" height="160"></canvas>
                        </div>
                    </div>

                    <!-- Quality Trend -->
                    <div class="proof-section">
                        <h3 class="section-heading">Quality Trend &mdash; Slip-Adjusted Equity</h3>
                        <div class="proof-chart-wrap">
                            <canvas id="proof-equity-chart" height="160"></canvas>
                        </div>
                    </div>

                    <!-- Monthly Breakdown -->
                    <div class="proof-section">
                        <h3 class="section-heading">Monthly Breakdown</h3>
                        <div class="table-wrapper">
                            <table class="data-table" id="proof-monthly-table">
                                <thead>
                                    <tr>
                                        <th>Month</th><th>Trades</th><th>Wins</th><th>Losses</th>
                                        <th>Win Rate</th><th>P&L (raw)</th><th>P&L (slip-adj)</th><th>Status</th>
                                    </tr>
                                </thead>
                                <tbody id="proof-monthly-tbody"></tbody>
                            </table>
                        </div>
                        <p class="text-xs text-muted" style="margin-top:6px">Months count toward proof bar only after they close.</p>
                    </div>
                </section>
```

- [ ] **Step 4: Verify page loads without JS errors**

Start web server and load the page. The new tab should appear but show empty containers.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat(proof-bar): add proof strip + tab HTML structure"
```

---

### Task 6: CSS Styling

**Files:**
- Modify: `static/styles.css` (append at end)

- [ ] **Step 1: Add proof strip + tab styles**

Append to the end of `static/styles.css`:

```css
/* === PROOF STRIP === */
.proof-strip {
  display: flex; align-items: center; gap: 16px;
  padding: 5px 16px; background: var(--bg-secondary, #050a05);
  border-bottom: 1px solid var(--border, #0a3318); font-size: 0.75rem;
  font-family: var(--font-mono, 'Courier New', monospace);
}
.strip-label { text-transform: uppercase; letter-spacing: 1px; opacity: 0.4; font-size: 0.7rem; }
.strip-chip {
  display: inline-flex; align-items: center; gap: 5px;
  text-decoration: none; cursor: pointer; color: inherit;
}
.strip-chip:hover { opacity: 0.8; }
.strip-chip.pass .strip-value { color: var(--green, #00ff41); }
.strip-chip.fail .strip-value { color: var(--red, #ff4141); }
.strip-chip.warn .strip-value { color: var(--amber, #ffaa00); }
.strip-chip.pass .strip-icon::before { content: "\2713"; color: var(--green, #00ff41); }
.strip-chip.fail .strip-icon::before { content: "\2717"; color: var(--red, #ff4141); }
.strip-chip.warn .strip-icon::before { content: "\25CB"; color: var(--amber, #ffaa00); }
.strip-metric { opacity: 0.6; }
.strip-value { font-weight: bold; }
.strip-target { opacity: 0.35; }
.strip-divider { width: 1px; height: 14px; background: var(--border, #0a3318); }
.strip-badge {
  margin-left: auto; padding: 2px 8px; border-radius: 3px;
  font-size: 0.65rem; font-weight: bold; text-transform: uppercase; letter-spacing: 1px;
}
.strip-badge.not-ready { background: rgba(255,65,65,0.15); color: var(--red, #ff4141); border: 1px solid rgba(255,65,65,0.3); }
.strip-badge.ready { background: rgba(0,255,65,0.15); color: var(--green, #00ff41); border: 1px solid rgba(0,255,65,0.3); }

/* === PROOF TAB === */
.proof-definition { font-size: 0.72rem; opacity: 0.4; letter-spacing: 0.5px; }
.proof-section { margin-top: 20px; }
.proof-section .section-heading { font-size: 0.82rem; text-transform: uppercase; letter-spacing: 1px; opacity: 0.7; margin-bottom: 10px; }

/* Blockers */
.proof-blockers {
  background: rgba(255,65,65,0.05); border: 1px solid rgba(255,65,65,0.2);
  border-radius: 4px; padding: 14px; margin-bottom: 16px;
}
.proof-blockers h3 { color: var(--red, #ff4141); font-size: 0.82rem; margin-bottom: 8px; text-transform: uppercase; }
.proof-blockers .blocker-item {
  display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid rgba(255,65,65,0.1);
  font-size: 0.8rem; line-height: 1.5;
}
.proof-blockers .blocker-item:last-child { border-bottom: none; }
.proof-blockers .b-icon { color: var(--red, #ff4141); flex-shrink: 0; }
.proof-blockers .b-icon.warn { color: var(--amber, #ffaa00); }
.proof-blockers .b-fix { color: var(--amber, #ffaa00); opacity: 0.6; font-size: 0.72rem; }
.proof-blockers-ready {
  background: rgba(0,255,65,0.05); border: 1px solid rgba(0,255,65,0.2);
  border-radius: 4px; padding: 14px 16px; margin-bottom: 16px;
  color: var(--green, #00ff41); font-weight: bold; text-transform: uppercase;
  text-align: center; letter-spacing: 1px;
}

/* Criteria grid */
.proof-criteria-grid {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 16px;
}
.proof-card {
  background: var(--bg-secondary, #0a0a0a); border: 1px solid var(--border, #0a3318);
  border-radius: 4px; padding: 14px; position: relative;
}
.proof-card.pass { border-color: rgba(0,255,65,0.3); }
.proof-card.fail { border-color: rgba(255,65,65,0.3); }
.proof-card.warn { border-color: rgba(255,170,0,0.3); }
.proof-card .card-title { font-size: 0.68rem; text-transform: uppercase; opacity: 0.5; margin-bottom: 6px; letter-spacing: 1px; }
.proof-card .card-value { font-size: 1.5rem; font-weight: bold; margin-bottom: 3px; }
.proof-card.pass .card-value { color: var(--green, #00ff41); }
.proof-card.fail .card-value { color: var(--red, #ff4141); }
.proof-card.warn .card-value { color: var(--amber, #ffaa00); }
.proof-card .card-target { font-size: 0.68rem; opacity: 0.4; }
.proof-card .card-detail { font-size: 0.68rem; opacity: 0.6; margin-top: 6px; line-height: 1.4; }
.proof-card .status-dot {
  position: absolute; top: 10px; right: 10px; width: 7px; height: 7px; border-radius: 50%;
}
.proof-card.pass .status-dot { background: var(--green, #00ff41); box-shadow: 0 0 5px var(--green, #00ff41); }
.proof-card.fail .status-dot { background: var(--red, #ff4141); box-shadow: 0 0 5px var(--red, #ff4141); }
.proof-card.warn .status-dot { background: var(--amber, #ffaa00); box-shadow: 0 0 5px var(--amber, #ffaa00); }
.proof-card .progress-track { height: 3px; background: #111; border-radius: 2px; margin-top: 8px; overflow: hidden; }
.proof-card .progress-fill { height: 100%; border-radius: 2px; }
.proof-card.pass .progress-fill { background: var(--green, #00ff41); }
.proof-card.fail .progress-fill { background: var(--red, #ff4141); }
.proof-card.warn .progress-fill { background: var(--amber, #ffaa00); }
.proof-card .progress-pct { font-size: 0.62rem; opacity: 0.35; margin-top: 2px; text-align: right; font-style: italic; }

/* Health grid */
.proof-health-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.proof-health-item {
  background: var(--bg-secondary, #0a0a0a); border: 1px solid var(--border, #0a3318);
  border-radius: 4px; padding: 10px; display: flex; align-items: center; gap: 8px;
}
.proof-health-item .h-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.proof-health-item.ok .h-dot { background: var(--green, #00ff41); }
.proof-health-item.warn .h-dot { background: var(--amber, #ffaa00); }
.proof-health-item.fail .h-dot { background: var(--red, #ff4141); }
.proof-health-item .h-label { font-size: 0.78rem; }
.proof-health-item .h-detail { font-size: 0.65rem; opacity: 0.4; }
.proof-health-item .h-advisory { font-size: 0.6rem; opacity: 0.3; font-style: italic; }

/* Pace grid */
.proof-pace-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
.proof-pace-card {
  background: var(--bg-secondary, #0a0a0a); border: 1px solid var(--border, #0a3318);
  border-radius: 4px; padding: 10px;
}
.proof-pace-card .pc-label { font-size: 0.62rem; text-transform: uppercase; opacity: 0.4; margin-bottom: 3px; }
.proof-pace-card .pc-value { font-size: 1.15rem; font-weight: bold; }
.proof-pace-card .pc-sub { font-size: 0.68rem; opacity: 0.5; margin-top: 2px; }

/* Chart wraps */
.proof-chart-wrap {
  background: var(--bg-secondary, #0a0a0a); border: 1px solid var(--border, #0a3318);
  border-radius: 4px; padding: 12px; margin-bottom: 12px;
}
```

- [ ] **Step 2: Bump cache-bust version**

Update the stylesheet link in `templates/index.html` line 7:
Change `styles.css?v=40` to `styles.css?v=41`.

- [ ] **Step 3: Commit**

```bash
git add static/styles.css templates/index.html
git commit -m "feat(proof-bar): add proof strip + tab CSS styles"
```

---

### Task 7: JavaScript — Data Loading, Rendering, Charts

**Files:**
- Modify: `static/app.js` (4 insertion points)

This is the largest frontend task. It adds the `proofBar` module to the IIFE.

- [ ] **Step 1: Add 'proof-bar' to VALID_TABS**

In `app.js` line 10, change:
```js
const VALID_TABS = new Set(['signals', 'thesis', 'markets', 'positions', 'leaderboard', 'followed', 'strategies', 'research']);
```
to:
```js
const VALID_TABS = new Set(['signals', 'thesis', 'markets', 'positions', 'leaderboard', 'followed', 'strategies', 'research', 'proof-bar']);
```

- [ ] **Step 2: Add proof-bar to titles map and load call**

In the `nav.activate` function (~line 3844), add to the titles object:
```js
        'proof-bar': 'Proof Bar'
```

In the load-data block (~line 3865), add:
```js
      else if (tabId === 'proof-bar') proofBar.load();
```

- [ ] **Step 3: Add the proofBar module**

Add the following `proofBar` module before the `// WEBSOCKET` section (~line 3869). This is a large block — insert it as a complete module:

```js
  // ============================================================================
  // PROOF BAR
  // ============================================================================
  const proofBar = {
    _refreshTimer: null,
    _data: null,

    async load() {
      try {
        const resp = await fetch('/api/proof-bar');
        if (!resp.ok) return;
        this._data = await resp.json();
        this.renderStrip(this._data);
        this.renderTab(this._data);
      } catch (e) {
        console.error('proofBar.load error:', e);
      }
      this._scheduleRefresh();
    },

    _scheduleRefresh() {
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = setTimeout(() => {
        if (!document.hidden && state.route.tab === 'proof-bar') this.load();
        else this._scheduleRefresh();
      }, 60000);
    },

    renderStrip(d) {
      const c = d.criteria || {};
      const updates = [
        ['strip-trades', c.closed_trades, v => `${v.value}`, v => `/ ${v.target}`],
        ['strip-wr', c.win_rate, v => `${v.value}%`, v => `/ ${v.target}%`],
        ['strip-pnl', c.slip_pnl, v => (v.value >= 0 ? '+$' : '-$') + Math.abs(v.value).toFixed(0), null],
        ['strip-months', c.no_losing_month, v => `${v.value}`, v => `/ ${v.target}`],
        ['strip-health', c.health, v => v.value === 'pass' ? 'OK' : 'FAIL', null],
      ];
      for (const [id, crit, fmtVal, fmtTgt] of updates) {
        const el = document.getElementById(id);
        if (!el || !crit) continue;
        el.className = `strip-chip ${crit.status}`;
        const valEl = el.querySelector('.strip-value');
        if (valEl) valEl.textContent = fmtVal(crit);
        const tgtEl = el.querySelector('.strip-target');
        if (tgtEl && fmtTgt) tgtEl.textContent = fmtTgt(crit);
      }
      const badge = document.getElementById('strip-badge');
      if (badge) {
        badge.className = `strip-badge ${d.overall === 'ready' ? 'ready' : 'not-ready'}`;
        badge.textContent = d.overall === 'ready' ? 'GO-LIVE READY' : 'NOT READY';
      }
    },

    renderTab(d) {
      this._renderBlockers(d);
      this._renderCriteria(d);
      this._renderHealth(d);
      this._renderPace(d);
      this._renderMonthly(d);
      setTimeout(() => {
        this._drawCumTradesChart(d.charts?.cumulative_trades || []);
        this._drawEquityChart(d.charts?.equity_curve_30d || []);
      }, 0);
    },

    _renderBlockers(d) {
      const el = document.getElementById('proof-blockers');
      if (!el) return;
      const failing = Object.entries(d.criteria || {}).filter(([, c]) => c.status !== 'pass');
      if (failing.length === 0) {
        el.innerHTML = '';
        el.className = 'proof-blockers-ready';
        el.textContent = 'ALL CRITERIA MET \u2014 READY FOR GO-LIVE';
        el.hidden = false;
        return;
      }
      el.className = 'proof-blockers';
      el.hidden = false;
      const labels = {
        closed_trades: 'Trade count',
        win_rate: 'Win rate',
        slip_pnl: 'Slippage-adj P&L',
        no_losing_month: 'Calendar months',
        health: 'Health checks',
      };
      const hints = {
        closed_trades: d.pace?.eta_date ? `At current pace, ETA: ~${d.pace.eta_date}` : '',
        win_rate: `${d.criteria.win_rate.value}% vs ${d.criteria.win_rate.target}% target`,
        slip_pnl: d.criteria.slip_pnl.value <= 0 ? 'P&L must be positive after slippage haircut' : 'Close to threshold',
        no_losing_month: 'First complete month must close profitable',
        health: 'Check system health panel below',
      };
      let html = '<h3>Blocking Go-Live</h3>';
      for (const [key, crit] of failing) {
        const icon = crit.status === 'warn' ? '\u25CB' : '\u00D7';
        const iconClass = crit.status === 'warn' ? 'warn' : '';
        html += `<div class="blocker-item"><span class="b-icon ${iconClass}">${icon}</span><div>`;
        html += `<div><strong>${labels[key] || key}:</strong> ${crit.value} / ${crit.target}</div>`;
        if (hints[key]) html += `<div class="b-fix">${hints[key]}</div>`;
        html += '</div></div>';
      }
      el.innerHTML = html;
    },

    _renderCriteria(d) {
      const grid = document.getElementById('proof-criteria-grid');
      if (!grid) return;
      const cards = [
        { key: 'closed_trades', title: 'Closed Trades', fmt: c => c.value, target: `Target: ${d.criteria.closed_trades.target} at insider \u2265 60`, pct: c => (c.value / c.target * 100).toFixed(1), detail: d.pace?.eta_date ? `ETA: ~${d.pace.eta_date} at current pace` : '' },
        { key: 'win_rate', title: 'Win Rate', fmt: c => c.value + '%', target: `Target: \u2265 ${d.criteria.win_rate.target}%`, pct: c => (c.value / c.target * 100).toFixed(1), detail: '' },
        { key: 'slip_pnl', title: 'Slippage-Adj P&L', fmt: c => (c.value >= 0 ? '+$' : '-$') + Math.abs(c.value).toFixed(0), target: 'Target: > $0 after 2.5% haircut', pct: c => c.value > 0 ? '100' : '0', detail: '' },
        { key: 'no_losing_month', title: 'No Losing Month', fmt: c => `${c.value} / ${c.target}`, target: `Target: ${d.criteria.no_losing_month.target} full months, all profitable`, pct: c => (c.value / c.target * 100).toFixed(1), detail: '' },
        { key: 'health', title: 'Health Checks', fmt: c => c.value === 'pass' ? 'PASS' : 'FAIL', target: 'Target: All proof-required checks clean', pct: c => c.status === 'pass' ? '100' : '0', detail: '' },
      ];
      let html = '';
      for (const cfg of cards) {
        const c = d.criteria[cfg.key];
        if (!c) continue;
        const pctVal = typeof cfg.pct === 'function' ? cfg.pct(c) : 0;
        const pctNum = Math.min(parseFloat(pctVal) || 0, 100);
        html += `<div class="proof-card ${c.status}" id="proof-card-${cfg.key.replace(/_/g, '-')}">`;
        html += `<div class="status-dot"></div>`;
        html += `<div class="card-title">${cfg.title}</div>`;
        html += `<div class="card-value">${cfg.fmt(c)}</div>`;
        html += `<div class="card-target">${cfg.target}</div>`;
        if (cfg.key !== 'health') {
          html += `<div class="progress-track"><div class="progress-fill" style="width:${pctNum}%"></div></div>`;
          html += `<div class="progress-pct">${pctNum.toFixed(1)}% of target</div>`;
        }
        if (cfg.detail) html += `<div class="card-detail">${cfg.detail}</div>`;
        html += '</div>';
      }
      grid.innerHTML = html;
    },

    _renderHealth(d) {
      const grid = document.getElementById('proof-health-grid');
      if (!grid) return;
      const items = d.health_details || {};
      const names = { stream: 'WebSocket Stream', analyzer: 'Analyzer', paper_trader: 'Paper Trader', data_integrity: 'Data Integrity', db_size: 'DB Size', alerter: 'Alerter' };
      const required = Object.entries(items).filter(([, v]) => v.proof_required);
      const advisory = Object.entries(items).filter(([, v]) => !v.proof_required);

      const renderItem = ([key, info]) => {
        const cls = info.status || 'ok';
        return `<div class="proof-health-item ${cls}"><div class="h-dot"></div><div>` +
          `<div class="h-label">${names[key] || key}</div>` +
          `<div class="h-detail">${info.detail || ''}</div>` +
          (!info.proof_required ? `<div class="h-advisory">(advisory)</div>` : '') +
          '</div></div>';
      };

      let html = '<div style="font-size:0.68rem;opacity:0.4;margin-bottom:6px;text-transform:uppercase">Proof-Required</div>';
      html += '<div class="proof-health-grid" style="margin-bottom:12px">' + required.map(renderItem).join('') + '</div>';
      if (advisory.length) {
        html += '<div style="font-size:0.68rem;opacity:0.3;margin-bottom:6px;text-transform:uppercase">Advisory (does not block go-live)</div>';
        html += '<div class="proof-health-grid" style="opacity:0.6">' + advisory.map(renderItem).join('') + '</div>';
      }
      grid.innerHTML = html;
    },

    _renderPace(d) {
      const grid = document.getElementById('proof-pace-grid');
      if (!grid) return;
      const p = d.pace || {};
      grid.innerHTML = [
        { label: '7-Day Pace', value: `${p.seven_day_rate || 0}<span style="font-size:0.75rem;opacity:0.5">/day</span>`, sub: '' },
        { label: 'Trades to Go', value: p.trades_to_go || 0, sub: p.eta_date ? `ETA: ~${p.eta_date}` : '', color: 'var(--red)' },
        { label: 'Cumulative Equity', value: (p.cumulative_equity >= 0 ? '+$' : '-$') + Math.abs(p.cumulative_equity || 0).toFixed(0), sub: 'Slip-adj, all-time', color: p.cumulative_equity >= 0 ? 'var(--green)' : 'var(--red)' },
        { label: 'Open Exposure', value: '$' + (p.open_exposure || 0).toFixed(0), sub: `${p.open_positions || 0} positions open` },
      ].map(c => `<div class="proof-pace-card"><div class="pc-label">${c.label}</div><div class="pc-value"${c.color ? ` style="color:${c.color}"` : ''}>${c.value}</div><div class="pc-sub">${c.sub}</div></div>`).join('');
    },

    _renderMonthly(d) {
      const tbody = document.getElementById('proof-monthly-tbody');
      if (!tbody) return;
      const months = d.monthly || [];
      if (!months.length) { tbody.innerHTML = '<tr><td colspan="8" class="text-muted">No closed trades yet</td></tr>'; return; }
      tbody.innerHTML = months.map(m => {
        const statusIcon = m.complete ? (m.profitable ? '\u2713' : '\u2717') : '\u25CB';
        const statusText = m.complete ? (m.profitable ? 'Profitable' : 'Loss') : 'In progress';
        const statusColor = m.complete ? (m.profitable ? 'var(--green)' : 'var(--red)') : 'var(--amber)';
        const pnlClass = v => v >= 0 ? 'color:var(--green)' : 'color:var(--red)';
        return `<tr>
          <td>${m.month}${m.complete ? '' : ' (in progress)'}</td>
          <td>${m.trades}</td><td>${m.wins}</td><td>${m.losses}</td>
          <td>${m.win_rate.toFixed(1)}%</td>
          <td style="${pnlClass(m.pnl_raw)}">${m.pnl_raw >= 0 ? '+' : ''}$${m.pnl_raw.toFixed(2)}</td>
          <td style="${pnlClass(m.pnl_slip)}">${m.pnl_slip >= 0 ? '+' : ''}$${m.pnl_slip.toFixed(2)}</td>
          <td style="color:${statusColor};text-align:center">${statusIcon} ${statusText}</td>
        </tr>`;
      }).join('');
    },

    _drawCumTradesChart(data) {
      const canvas = document.getElementById('proof-cum-trades-chart');
      if (!canvas || !data || data.length < 2) return;
      const ctx = canvas.getContext('2d');
      const W = canvas.parentElement.clientWidth || 600;
      const H = 160;
      canvas.width = W; canvas.height = H;
      const pad = { t: 20, r: 15, b: 25, l: 50 };
      const plotW = W - pad.l - pad.r;
      const plotH = H - pad.t - pad.b;

      const vals = data.map(p => p.count);
      const maxV = Math.max(150, ...vals);
      const toX = i => pad.l + (i / (data.length - 1)) * plotW;
      const toY = v => pad.t + plotH - (v / maxV) * plotH;

      // Grid
      ctx.strokeStyle = 'rgba(0,255,65,0.1)'; ctx.lineWidth = 0.5;
      for (let i = 0; i <= 4; i++) {
        const y = pad.t + (plotH / 4) * i;
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
        ctx.fillStyle = 'rgba(0,255,65,0.5)'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
        ctx.fillText(Math.round(maxV - (maxV / 4) * i), pad.l - 5, y + 3);
      }

      // Target line at 150
      ctx.strokeStyle = 'rgba(255,65,65,0.3)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(pad.l, toY(150)); ctx.lineTo(W - pad.r, toY(150)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(255,65,65,0.5)'; ctx.font = '10px monospace'; ctx.textAlign = 'left';
      ctx.fillText('150', W - pad.r + 3, toY(150) + 3);

      // Data line
      ctx.beginPath(); ctx.moveTo(toX(0), toY(vals[0]));
      for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));
      ctx.strokeStyle = '#00ff41'; ctx.lineWidth = 2; ctx.stroke();

      // Fill
      ctx.lineTo(toX(vals.length - 1), toY(0)); ctx.lineTo(toX(0), toY(0)); ctx.closePath();
      const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
      grad.addColorStop(0, 'rgba(0,255,65,0.15)'); grad.addColorStop(1, 'rgba(0,255,65,0.02)');
      ctx.fillStyle = grad; ctx.fill();

      // Current dot
      ctx.beginPath(); ctx.arc(toX(vals.length - 1), toY(vals[vals.length - 1]), 3, 0, Math.PI * 2);
      ctx.fillStyle = '#00ff41'; ctx.fill();

      // X-axis label
      ctx.fillStyle = 'rgba(0,255,65,0.4)'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
      ctx.fillText('Cumulative closed trades vs 150 target', W / 2, H - 2);
    },

    _drawEquityChart(data) {
      const canvas = document.getElementById('proof-equity-chart');
      if (!canvas || !data || data.length < 2) return;
      const ctx = canvas.getContext('2d');
      const W = canvas.parentElement.clientWidth || 600;
      const H = 160;
      canvas.width = W; canvas.height = H;
      const pad = { t: 20, r: 15, b: 25, l: 55 };
      const plotW = W - pad.l - pad.r;
      const plotH = H - pad.t - pad.b;

      const vals = data.map(p => p.equity);
      const minV = Math.min(0, ...vals);
      const maxV = Math.max(0, ...vals);
      const range = maxV - minV || 1;
      const toX = i => pad.l + (i / (data.length - 1)) * plotW;
      const toY = v => pad.t + plotH - ((v - minV) / range) * plotH;

      // Grid
      ctx.strokeStyle = 'rgba(0,255,65,0.1)'; ctx.lineWidth = 0.5;
      for (let i = 0; i <= 4; i++) {
        const y = pad.t + (plotH / 4) * i;
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
        const label = (maxV - (range / 4) * i).toFixed(0);
        ctx.fillStyle = 'rgba(0,255,65,0.5)'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
        ctx.fillText('$' + label, pad.l - 5, y + 3);
      }

      // Zero line
      const zeroY = toY(0);
      ctx.strokeStyle = 'rgba(255,65,65,0.3)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY); ctx.stroke();
      ctx.setLineDash([]);

      // Equity line
      ctx.beginPath(); ctx.moveTo(toX(0), toY(vals[0]));
      for (let i = 1; i < vals.length; i++) ctx.lineTo(toX(i), toY(vals[i]));
      ctx.strokeStyle = '#00ff41'; ctx.lineWidth = 2; ctx.stroke();

      // Fill
      ctx.lineTo(toX(vals.length - 1), zeroY); ctx.lineTo(toX(0), zeroY); ctx.closePath();
      const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
      grad.addColorStop(0, 'rgba(0,255,65,0.15)'); grad.addColorStop(1, 'rgba(0,255,65,0.02)');
      ctx.fillStyle = grad; ctx.fill();

      // X label
      ctx.fillStyle = 'rgba(0,255,65,0.4)'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
      ctx.fillText('30-day slip-adjusted equity curve', W / 2, H - 2);
    },
  };
```

- [ ] **Step 4: Add strip click handlers and initial strip load**

In the initialization section of `app.js` (the `init()` function or the IIFE body where event listeners are set up), add:

```js
    // Proof strip — load on page init and handle chip clicks
    proofBar.load();
    document.querySelectorAll('.strip-chip').forEach(chip => {
      chip.addEventListener('click', (e) => {
        e.preventDefault();
        const target = chip.dataset.proof;
        router.pushTab('proof-bar');
        if (target) {
          setTimeout(() => {
            const section = document.getElementById(`proof-${target}`);
            if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 100);
        }
      });
    });
```

- [ ] **Step 5: Bump app.js cache version**

In `templates/index.html`, find the script tag that loads `app.js` and bump the version: `app.js?v=62` (currently `v=61`).

- [ ] **Step 6: Verify in browser**

Start web server, navigate to the Proof Bar tab. The strip should show at the top with data, the tab should render all sections. Canvas charts should draw. Clicking a strip chip should scroll to the relevant section.

- [ ] **Step 7: Commit**

```bash
git add static/app.js templates/index.html
git commit -m "feat(proof-bar): add proofBar JS module with rendering and charts"
```

---

## Post-Implementation

After all 7 tasks are complete:

- [ ] **Final test run:** `python3 -m pytest tests/ -q` — all tests must pass
- [ ] **Manual verification:** Load dashboard, check proof strip renders on all tabs, verify Proof Bar tab shows all 5 sections, click strip chips and verify scroll
- [ ] **Use superpowers:finishing-a-development-branch** to complete the work
