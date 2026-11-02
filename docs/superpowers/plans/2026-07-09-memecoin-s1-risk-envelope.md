# Memecoin S1 Risk Envelope — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the memecoin real-money risk envelope — the fail-closed entry gate + pending-reservation ledger + latched daily-loss breaker — buildable and unit-testable now (balances injected), armed later.

**Architecture:** Config in `sniper/settings/memecoin.py`. Persistence (pending-reservation ledger, daily-loss latch, effective-exposure reads, `execution_mode` discriminator) in `sniper/persistence/db_memecoin.py`. Pure check + context builder in a new `sniper/services/memecoin_risk.py`, delegating the generic bankroll/concurrency/exposure checks to the existing pure `sniper/risk/policy.evaluate_entry_risk` and layering the memecoin-specific checks (one-position-per-coin, min-floor, size-invariant, SOL full-book exit reserve, latch) around it. No changes to `sniper/risk/policy.py` or `models.py` — S1 is additive.

**Tech Stack:** Python 3.13, SQLite (WAL), pytest. Existing risk dataclasses `RiskContext`/`VenueRiskLimits`/`RiskDecision` and pure evaluator `evaluate_entry_risk`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-09-memecoin-s1-risk-envelope-design.md` (authoritative). Map: `docs/superpowers/specs/2026-07-09-memecoin-real-money-launch-readiness.md`.
- **Fail-closed:** any ambiguity → reject. Never approve on error.
- **All caps are fixed USD** (no %-of-bankroll). Bankroll = live wallet USDC balance, injected — never a config knob.
- **Effective values everywhere:** concurrency/exposure/USDC/held-coins use `confirmed_real + pending_reserved`, not confirmed-only.
- Config lives in `sniper/settings/memecoin.py`, read via `os.getenv("MEMECOIN_...", "<default>")`. No thresholds hardcoded in logic.
- New tables/columns are added idempotently inside `ensure_memecoin_tables` (the `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN` migration idiom already used there).
- Tests: in-memory sqlite (`sqlite3.connect(":memory:")`, `row_factory = sqlite3.Row`), `m.ensure_memecoin_tables(c)`. Run with `python3 -m pytest <path> -q`.
- Only `execution_mode='real'` positions count toward the real-money latch/exposure; `'paper'` (default) never does.
- Daily boundary is the **real-UTC calendar day** string `now_real[:10]` (caller supplies it; do not call the clock inside pure functions).

---

### Task 1: Configuration knobs

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_risk_settings.py` (create)

**Interfaces:**
- Produces: module-level constants `MEMECOIN_REAL_ENABLED: bool`, `MEMECOIN_TRADE_SIZE_USD: float`, `MEMECOIN_MAX_CONCURRENT: int`, `MEMECOIN_MAX_TOTAL_EXPOSURE_USD: float`, `MEMECOIN_DAILY_LOSS_LIMIT_USD: float`, `MEMECOIN_MIN_TRADE_FLOOR_USD: float`, `MEMECOIN_MAX_ROUNDTRIP_COST_PCT: float`, `MEMECOIN_GAS_ENTRY_SOL: float`, `MEMECOIN_GAS_EXIT_SOL: float`, `MEMECOIN_GAS_RETRY_FACTOR: float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_risk_settings.py
import importlib


def test_risk_envelope_defaults():
    import sniper.settings.memecoin as s
    importlib.reload(s)
    assert s.MEMECOIN_REAL_ENABLED is False          # armed only by explicit opt-in
    assert s.MEMECOIN_TRADE_SIZE_USD == 10.0
    assert s.MEMECOIN_MAX_CONCURRENT == 6
    assert s.MEMECOIN_MAX_TOTAL_EXPOSURE_USD == 60.0
    assert s.MEMECOIN_DAILY_LOSS_LIMIT_USD == 30.0
    assert s.MEMECOIN_MIN_TRADE_FLOOR_USD == 5.0      # provisional; shadow-measured later
    assert s.MEMECOIN_MAX_ROUNDTRIP_COST_PCT == 3.0
    assert s.MEMECOIN_GAS_RETRY_FACTOR == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_risk_settings.py -q`
Expected: FAIL with `AttributeError: module 'sniper.settings.memecoin' has no attribute 'MEMECOIN_REAL_ENABLED'`

- [ ] **Step 3: Add the config block**

Append to `sniper/settings/memecoin.py`:

```python
# --- real-money risk envelope (S1) — fixed USD; bankroll is the wallet balance, not a knob ---
MEMECOIN_REAL_ENABLED = os.getenv("MEMECOIN_REAL_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
MEMECOIN_TRADE_SIZE_USD = float(os.getenv("MEMECOIN_TRADE_SIZE_USD", "10"))
MEMECOIN_MAX_CONCURRENT = int(os.getenv("MEMECOIN_MAX_CONCURRENT", "6"))
MEMECOIN_MAX_TOTAL_EXPOSURE_USD = float(os.getenv("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", "60"))
MEMECOIN_DAILY_LOSS_LIMIT_USD = float(os.getenv("MEMECOIN_DAILY_LOSS_LIMIT_USD", "30"))
# provisional floor: replaced by the shadow-phase measurement (smallest clip where round-trip
# cost <= MAX_ROUNDTRIP_COST_PCT of notional). Doubles as the launch-viability gate.
MEMECOIN_MIN_TRADE_FLOOR_USD = float(os.getenv("MEMECOIN_MIN_TRADE_FLOOR_USD", "5"))
MEMECOIN_MAX_ROUNDTRIP_COST_PCT = float(os.getenv("MEMECOIN_MAX_ROUNDTRIP_COST_PCT", "3"))
# SOL gas reserve (provisional placeholders; shadow measures true fees — over-reserve until then)
MEMECOIN_GAS_ENTRY_SOL = float(os.getenv("MEMECOIN_GAS_ENTRY_SOL", "0.002"))
MEMECOIN_GAS_EXIT_SOL = float(os.getenv("MEMECOIN_GAS_EXIT_SOL", "0.002"))
MEMECOIN_GAS_RETRY_FACTOR = float(os.getenv("MEMECOIN_GAS_RETRY_FACTOR", "2"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_risk_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/settings/memecoin.py tests/test_memecoin_risk_settings.py
git commit -m "feat(memecoin): S1 risk-envelope config knobs"
```

---

### Task 2: Pending-reservation ledger

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (table in `ensure_memecoin_tables`; helpers near the other memecoin helpers)
- Test: `tests/test_memecoin_pending_ledger.py` (create)

**Interfaces:**
- Produces:
  - `reserve_pending(conn, *, mint, source_user_id, strategy, notional, reserved_at) -> int | None` — INSERT a sig-less `status='reserved'` row; returns the row id, or `None` if the partial `UNIQUE(mint) WHERE status='reserved'` rejects it (coin already reserved).
  - `set_pending_sig(conn, pending_id, tx_sig, broadcast_at) -> None` — write-ahead the signature before broadcast.
  - `confirm_pending(conn, pending_id) -> None` — mark `status='confirmed'`.
  - `release_pending(conn, pending_id, reason) -> None` — mark `status='failed'` (frees the mint + the reservation).
  - `pending_totals(conn) -> tuple[int, float]` — `(count, notional)` over `status='reserved'`.
  - `pending_open_mints(conn) -> set[str]` — mints with a `status='reserved'` row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_pending_ledger.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_reserve_counts_and_dedups_by_mint():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z")
    assert pid is not None
    assert m.pending_totals(c) == (1, 10.0)
    assert m.pending_open_mints(c) == {"M1"}
    # same mint while first is still reserved -> rejected by the partial unique index
    assert m.reserve_pending(c, mint="M1", source_user_id="u2", strategy="control",
                             notional=10.0, reserved_at="t") is None
    assert m.pending_totals(c) == (1, 10.0)


def test_release_frees_mint_and_reservation():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="t")
    m.release_pending(c, pid, reason="tx_failed")
    assert m.pending_totals(c) == (0, 0.0)
    assert m.pending_open_mints(c) == set()
    # mint is free again after release
    assert m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                             notional=10.0, reserved_at="t") is not None


def test_confirm_clears_reservation_but_keeps_row():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="t")
    m.set_pending_sig(c, pid, "SIG123", "2026-07-09T00:00:01Z")
    m.confirm_pending(c, pid)
    assert m.pending_totals(c) == (0, 0.0)      # no longer 'reserved'
    row = c.execute("SELECT tx_sig, status FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()
    assert row["tx_sig"] == "SIG123" and row["status"] == "confirmed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_pending_ledger.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'reserve_pending'`

- [ ] **Step 3: Add the table + index in `ensure_memecoin_tables`**

Inside `ensure_memecoin_tables`, after the existing `CREATE TABLE IF NOT EXISTS memecoin_fills` block, add:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_pending_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT, source_user_id TEXT, strategy TEXT,
            reserved_notional_usd REAL,
            reserved_at TEXT, broadcast_at TEXT, tx_sig TEXT,
            status TEXT DEFAULT 'reserved'   -- reserved | confirmed | failed
        )""")
    # self-enforces one-position-per-coin even if the risk check races
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_pending_mint_uniq "
                 "ON memecoin_pending_entries(mint) WHERE status='reserved'")
```

- [ ] **Step 4: Add the helper functions**

Add near the other `memecoin_*` helpers in `db_memecoin.py`:

```python
def reserve_pending(conn, *, mint, source_user_id, strategy, notional, reserved_at):
    """INSERT a sig-less reserved row; returns id, or None if the mint is already reserved
    (partial UNIQUE(mint) WHERE status='reserved'). This is the atomic coin-dedup guard."""
    import sqlite3
    try:
        cur = conn.execute(
            "INSERT INTO memecoin_pending_entries(mint, source_user_id, strategy, "
            "reserved_notional_usd, reserved_at, status) VALUES (?,?,?,?,?, 'reserved')",
            (mint, source_user_id, strategy, float(notional), reserved_at))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def set_pending_sig(conn, pending_id, tx_sig, broadcast_at):
    """Write-ahead the signed tx signature BEFORE broadcast so a crashed reservation is
    reconcilable (has-sig -> resolve against chain; no-sig+old -> provably never broadcast)."""
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig=?, broadcast_at=? WHERE id=?",
                 (tx_sig, broadcast_at, pending_id))
    conn.commit()


def confirm_pending(conn, pending_id):
    conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending_id,))
    conn.commit()


def release_pending(conn, pending_id, reason):
    """Free a reservation on CONFIRMED failure only (never on timeout — that stays reserved,
    fail-closed). reason is recorded in status is coarse; keep the row for audit."""
    conn.execute("UPDATE memecoin_pending_entries SET status='failed' WHERE id=?", (pending_id,))
    conn.commit()


def pending_totals(conn):
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(reserved_notional_usd), 0.0) "
                     "FROM memecoin_pending_entries WHERE status='reserved'").fetchone()
    return (r[0], float(r[1]))


def pending_open_mints(conn):
    return {r[0] for r in conn.execute(
        "SELECT mint FROM memecoin_pending_entries WHERE status='reserved'")}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_pending_ledger.py -q`
Expected: PASS (3 tests)

- [ ] **Step 6: Regenerate the brain index (new table) and commit**

Run: `python3 scripts/brain_index.py generate && python3 scripts/validate_schema_refs.py`
Expected: index updated; "No schema violations found."

```bash
git add sniper/persistence/db_memecoin.py tests/test_memecoin_pending_ledger.py index/schema_registry.json
git commit -m "feat(memecoin): S1 pending-reservation ledger + coin-dedup unique index"
```

---

### Task 3: Daily-loss latch + real-money PnL discriminator

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (`execution_mode` column on `memecoin_positions`; trips table in `ensure_memecoin_tables`; helpers)
- Test: `tests/test_memecoin_daily_latch.py` (create)

**Interfaces:**
- Produces:
  - `daily_real_realized_pnl(conn, day) -> float` — sum `realized_pnl` over CLOSED, `execution_mode='real'` positions whose `closed_at` starts with `day` (real-UTC `YYYY-MM-DD`).
  - `check_and_latch_daily_loss(conn, day, limit_usd) -> bool` — if today's real realized PnL `<= -limit_usd`, insert a latch row for `day`; returns True if `day` is latched (now or already).
  - `is_daily_loss_tripped(conn, day) -> bool` — True iff a latch row exists for `day`.

- [ ] **Step 1: Write the failing test**

<!-- schema-validator: ignore-next-block -->
```python
# tests/test_memecoin_daily_latch.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _closed_real(c, pnl, closed_at, mode="real"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M"+str(pnl), ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z")
    c.execute("UPDATE memecoin_positions SET execution_mode=? WHERE id=?", (mode, pid))
    m.close_position(c, pid, closed_at, pnl, exit_reason="x")


def test_paper_pnl_never_counts_toward_real_latch():
    c = _c()
    _closed_real(c, -50.0, "2026-07-09T05:00:00Z", mode="paper")   # paper loss
    assert m.daily_real_realized_pnl(c, "2026-07-09") == 0.0
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is False


def test_latch_trips_and_stays_tripped_after_recovery():
    c = _c()
    _closed_real(c, -40.0, "2026-07-09T05:00:00Z")                 # real loss past -30
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is True
    _closed_real(c, +20.0, "2026-07-09T06:00:00Z")                 # later real win -> net -20
    # PnL recovered above the line, but the latch STAYS tripped (no un-trip / no flap)
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is True
    assert m.is_daily_loss_tripped(c, "2026-07-09") is True
    # a new day is clean
    assert m.is_daily_loss_tripped(c, "2026-07-10") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_daily_latch.py -q`
Expected: FAIL with `AttributeError: ... 'daily_real_realized_pnl'` (or missing `execution_mode` column)

- [ ] **Step 3: Add the `execution_mode` column + trips table in `ensure_memecoin_tables`**

In the `memecoin_positions` idempotent-migration column loop (the `for _col, _ddl in (...)` block), add the entry:

```python
        ("execution_mode", "TEXT DEFAULT 'paper'"),   # 'paper' | 'real' — real-money discriminator (S5 populates 'real')
```

After the `memecoin_pending_entries` block, add:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_daily_loss_trips (
            day TEXT PRIMARY KEY,          -- real-UTC YYYY-MM-DD
            tripped_at TEXT
        )""")
```

- [ ] **Step 4: Add the helpers**

<!-- schema-validator: ignore-next-block -->
```python
def daily_real_realized_pnl(conn, day):
    """Sum realized PnL of CLOSED real-money positions closed on `day` (real-UTC YYYY-MM-DD).
    Paper positions are excluded — a mixed ledger would let paper losses trip the real breaker."""
    r = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM memecoin_positions "
        "WHERE is_open=0 AND execution_mode='real' AND substr(closed_at,1,10)=?", (day,)).fetchone()
    return float(r[0])


def is_daily_loss_tripped(conn, day):
    return conn.execute("SELECT 1 FROM memecoin_daily_loss_trips WHERE day=?", (day,)).fetchone() is not None


def check_and_latch_daily_loss(conn, day, limit_usd):
    """Latched circuit breaker: trip once real realized PnL for `day` <= -limit_usd, and STAY
    tripped for the day even if a later fill revises PnL back up. Returns whether `day` is latched."""
    if is_daily_loss_tripped(conn, day):
        return True
    if daily_real_realized_pnl(conn, day) <= -abs(limit_usd):
        conn.execute("INSERT OR IGNORE INTO memecoin_daily_loss_trips(day, tripped_at) VALUES (?, ?)",
                     (day, day))
        conn.commit()
        return True
    return False
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_daily_latch.py -q`
Expected: PASS (2 tests)

- [ ] **Step 6: Regenerate index + commit**

```bash
python3 scripts/brain_index.py generate && python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin.py tests/test_memecoin_daily_latch.py index/schema_registry.json
git commit -m "feat(memecoin): S1 latched daily-loss breaker + execution_mode discriminator"
```

---

### Task 4: Effective real-money exposure snapshot

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_real_exposure.py` (create)

**Interfaces:**
- Produces: `real_open_snapshot(conn) -> dict` with keys `count: int`, `notional: float`, `mints: set[str]` over CONFIRMED real open positions (`is_open=1 AND execution_mode='real'`), using `entry_notional` for notional.

- [ ] **Step 1: Write the failing test**

<!-- schema-validator: ignore-next-block -->
```python
# tests/test_memecoin_real_exposure.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, mint, notional, mode="real"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=notional, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET execution_mode=? WHERE id=?", (mode, pid)); c.commit()


def test_real_open_snapshot_excludes_paper():
    c = _c()
    _open_real(c, "M1", 10.0)
    _open_real(c, "M2", 10.0)
    _open_real(c, "M3", 10.0, mode="paper")     # paper -> excluded
    snap = m.real_open_snapshot(c)
    assert snap["count"] == 2
    assert abs(snap["notional"] - 20.0) < 1e-9
    assert snap["mints"] == {"M1", "M2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_real_exposure.py -q`
Expected: FAIL with `AttributeError: ... 'real_open_snapshot'`

- [ ] **Step 3: Add the helper**

<!-- schema-validator: ignore-next-block -->
```python
def real_open_snapshot(conn):
    """Confirmed real-money open positions: {count, notional (sum entry_notional), mints set}."""
    rows = conn.execute(
        "SELECT mint, entry_notional FROM memecoin_positions "
        "WHERE is_open=1 AND execution_mode='real'").fetchall()
    return {"count": len(rows),
            "notional": float(sum((r[1] or 0.0) for r in rows)),
            "mints": {r[0] for r in rows}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_real_exposure.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_memecoin.py tests/test_memecoin_real_exposure.py
git commit -m "feat(memecoin): S1 confirmed-real open-exposure snapshot"
```

---

### Task 5: Risk context builder

**Files:**
- Create: `sniper/services/memecoin_risk.py`
- Test: `tests/test_memecoin_risk_context.py` (create)

**Interfaces:**
- Consumes: `real_open_snapshot`, `pending_totals`, `pending_open_mints`, `is_daily_loss_tripped` (Tasks 2–4); settings (Task 1).
- Produces:
  - `@dataclass(frozen=True) MemecoinRiskCtx` with fields: `usdc_balance: float`, `sol_balance: float`, `effective_concurrent: int`, `effective_exposure_usd: float`, `effective_pending_notional_usd: float`, `held_mints: frozenset[str]`, `daily_tripped: bool`, `real_enabled: bool`, `kill_tripped: bool`, and caps `trade_size_usd`, `max_concurrent`, `max_total_exposure_usd`, `min_trade_floor_usd`, `gas_entry_sol`, `gas_exit_sol`, `gas_retry_factor`.
  - `build_memecoin_risk_context(conn, *, usdc_balance, sol_balance, day, kill_tripped=False) -> MemecoinRiskCtx`.

- [ ] **Step 1: Write the failing test**

<!-- schema-validator: ignore-next-block -->
```python
# tests/test_memecoin_risk_context.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_risk import build_memecoin_risk_context


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_context_folds_confirmed_and_pending_into_effective():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M1", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET execution_mode='real' WHERE id=?", (pid,)); c.commit()
    m.reserve_pending(c, mint="M2", source_user_id="u", strategy="control",
                      notional=10.0, reserved_at="t")
    ctx = build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=1.0, day="2026-07-09")
    assert ctx.effective_concurrent == 2                 # 1 confirmed real + 1 pending
    assert abs(ctx.effective_exposure_usd - 20.0) < 1e-9
    assert ctx.held_mints == frozenset({"M1", "M2"})     # confirmed ∪ pending
    assert ctx.daily_tripped is False
    assert ctx.max_concurrent == 6 and ctx.trade_size_usd == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_risk_context.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.services.memecoin_risk'`

- [ ] **Step 3: Create the module + builder**

```python
# sniper/services/memecoin_risk.py
"""Memecoin real-money risk envelope (S1). Pure check over an injected context + a context
builder that folds confirmed-real and pending reservations into effective values. Delegates the
generic bankroll/concurrency/exposure checks to sniper.risk.policy.evaluate_entry_risk."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


@dataclass(frozen=True)
class MemecoinRiskCtx:
    usdc_balance: float
    sol_balance: float
    effective_concurrent: int
    effective_exposure_usd: float
    effective_pending_notional_usd: float
    held_mints: frozenset
    daily_tripped: bool
    real_enabled: bool
    kill_tripped: bool
    trade_size_usd: float
    max_concurrent: int
    max_total_exposure_usd: float
    min_trade_floor_usd: float
    gas_entry_sol: float
    gas_exit_sol: float
    gas_retry_factor: float


def build_memecoin_risk_context(conn, *, usdc_balance, sol_balance, day, kill_tripped=False):
    snap = m.real_open_snapshot(conn)
    p_count, p_notional = m.pending_totals(conn)
    held = frozenset(snap["mints"] | m.pending_open_mints(conn))
    return MemecoinRiskCtx(
        usdc_balance=float(usdc_balance), sol_balance=float(sol_balance),
        effective_concurrent=snap["count"] + p_count,
        effective_exposure_usd=snap["notional"] + p_notional,
        effective_pending_notional_usd=p_notional,
        held_mints=held,
        daily_tripped=m.is_daily_loss_tripped(conn, day),
        real_enabled=cfg.MEMECOIN_REAL_ENABLED, kill_tripped=bool(kill_tripped),
        trade_size_usd=cfg.MEMECOIN_TRADE_SIZE_USD,
        max_concurrent=cfg.MEMECOIN_MAX_CONCURRENT,
        max_total_exposure_usd=cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
        min_trade_floor_usd=cfg.MEMECOIN_MIN_TRADE_FLOOR_USD,
        gas_entry_sol=cfg.MEMECOIN_GAS_ENTRY_SOL, gas_exit_sol=cfg.MEMECOIN_GAS_EXIT_SOL,
        gas_retry_factor=cfg.MEMECOIN_GAS_RETRY_FACTOR)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_risk_context.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_risk.py tests/test_memecoin_risk_context.py
git commit -m "feat(memecoin): S1 risk context builder (effective confirmed+pending)"
```

---

### Task 6: Entry check — generic + memecoin-specific gates (no SOL yet)

**Files:**
- Modify: `sniper/services/memecoin_risk.py`
- Test: `tests/test_memecoin_entry_check.py` (create)

**Interfaces:**
- Consumes: `MemecoinRiskCtx` (Task 5); `sniper.risk.policy.evaluate_entry_risk`, `sniper.risk.models.RiskContext`, `RiskDecision`.
- Produces: `memecoin_entry_allowed(ctx: MemecoinRiskCtx, *, mint, trade_size) -> RiskDecision`. Order: real-enabled → kill → daily latch → size-invariant → min-floor → coin-dedup → (delegate concurrency/exposure/effective-USDC to `evaluate_entry_risk`). SOL reserve is added in Task 7.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_entry_check.py
from dataclasses import replace
from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed


def _ctx(**kw):
    base = dict(usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
               effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
               held_mints=frozenset(), daily_tripped=False, real_enabled=True, kill_tripped=False,
               trade_size_usd=10.0, max_concurrent=6, max_total_exposure_usd=60.0,
               min_trade_floor_usd=5.0, gas_entry_sol=0.002, gas_exit_sol=0.002, gas_retry_factor=2.0)
    base.update(kw); return MemecoinRiskCtx(**base)


def test_rejects_when_real_disabled():
    assert memecoin_entry_allowed(_ctx(real_enabled=False), mint="M", trade_size=10.0).allowed is False


def test_rejects_when_kill_tripped():
    assert memecoin_entry_allowed(_ctx(kill_tripped=True), mint="M", trade_size=10.0).allowed is False


def test_rejects_when_daily_latched():
    assert memecoin_entry_allowed(_ctx(daily_tripped=True), mint="M", trade_size=10.0).allowed is False


def test_rejects_size_not_equal_base():
    assert memecoin_entry_allowed(_ctx(), mint="M", trade_size=12.0).allowed is False


def test_rejects_below_min_floor():
    assert memecoin_entry_allowed(_ctx(trade_size_usd=4.0), mint="M", trade_size=4.0).allowed is False


def test_rejects_coin_already_held_or_reserved():
    assert memecoin_entry_allowed(_ctx(held_mints=frozenset({"M"})), mint="M", trade_size=10.0).allowed is False


def test_rejects_at_concurrency_cap():
    assert memecoin_entry_allowed(_ctx(effective_concurrent=6), mint="M", trade_size=10.0).allowed is False


def test_rejects_over_exposure_cap():
    # 5 held ($50) + this $10 = $60 ok; 6th would be $60+$10 blocked by concurrency first,
    # so test exposure directly with a raised concurrency cap
    ctx = _ctx(effective_concurrent=5, effective_exposure_usd=55.0, max_concurrent=99)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False   # 55+10 > 60


def test_rejects_when_effective_usdc_insufficient():
    # balance $12 but $10 already reserved pending -> effective $2 < $10
    ctx = _ctx(usdc_balance=12.0, effective_pending_notional_usd=10.0,
               effective_exposure_usd=10.0, effective_concurrent=1)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_approves_clean_entry():
    assert memecoin_entry_allowed(_ctx(), mint="M", trade_size=10.0).allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_entry_check.py -q`
Expected: FAIL with `ImportError: cannot import name 'memecoin_entry_allowed'`

- [ ] **Step 3: Implement the check**

Add to `sniper/services/memecoin_risk.py`:

```python
from sniper.risk.models import RiskContext, RiskDecision
from sniper.risk.policy import evaluate_entry_risk


def _reject(reason):
    return RiskDecision(allowed=False, approved_size_usdc=0.0, reason=reason, clamped=False)


def memecoin_entry_allowed(ctx, *, mint, trade_size):
    """Fail-closed entry gate (spec §4). First failure wins. SOL reserve is layered in Task 7."""
    if not ctx.real_enabled:
        return _reject("real trading disabled")
    if ctx.kill_tripped:
        return _reject("kill switch tripped")
    if ctx.daily_tripped:
        return _reject("daily loss limit reached (latched)")
    if float(trade_size) != ctx.trade_size_usd:
        return _reject("trade size must equal base size (invariant)")
    if trade_size < ctx.min_trade_floor_usd:
        return _reject("below min-trade floor (-EV at this clip)")
    if mint in ctx.held_mints:
        return _reject("one position per coin: already held or reserved")
    # delegate concurrency / total-exposure / effective-USDC to the shared evaluator, using
    # EFFECTIVE (confirmed+reserved) inputs. daily-loss + market checks are neutralized: the
    # latch above is the sole daily gate; there is no market concept for memecoins.
    rc = RiskContext(
        bankroll_usdc=ctx.usdc_balance - ctx.effective_pending_notional_usd,   # effective available USDC
        total_exposure_usdc=ctx.effective_exposure_usd,
        market_exposure_usdc=ctx.effective_exposure_usd,
        concurrent_positions=ctx.effective_concurrent,
        daily_realized_pnl_usdc=0.0,
        max_total_exposure_usdc=ctx.max_total_exposure_usd,
        max_market_exposure_usdc=ctx.max_total_exposure_usd,   # == total: market clamp never fires
        max_concurrent_positions=ctx.max_concurrent,
        daily_loss_limit_usdc=1e12,                            # inert; latch owns daily
        paper_only=not ctx.real_enabled, real_trading_enabled=ctx.real_enabled,
        venue="solana_memecoin")
    return evaluate_entry_risk(rc, float(trade_size), condition_id=mint, execution_mode="real")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_entry_check.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_risk.py tests/test_memecoin_entry_check.py
git commit -m "feat(memecoin): S1 entry gate — mode/kill/latch/size/floor/coin-dedup + delegated caps"
```

---

### Task 7: SOL full-book exit reserve (effective-N)

**Files:**
- Modify: `sniper/services/memecoin_risk.py` (insert SOL check before the delegate returns)
- Test: `tests/test_memecoin_sol_reserve.py` (create)

**Interfaces:**
- Modifies `memecoin_entry_allowed` behavior only; signature unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_sol_reserve.py
from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed


def _ctx(**kw):
    base = dict(usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
               effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
               held_mints=frozenset(), daily_tripped=False, real_enabled=True, kill_tripped=False,
               trade_size_usd=10.0, max_concurrent=6, max_total_exposure_usd=60.0,
               min_trade_floor_usd=5.0, gas_entry_sol=0.002, gas_exit_sol=0.002, gas_retry_factor=2.0)
    base.update(kw); return MemecoinRiskCtx(**base)


def test_blocks_when_sol_cannot_cover_full_book_exit():
    # required = entry 0.002 + (0+1)*exit 0.002*retry 2 = 0.006 ; balance 0.005 -> blocked
    ctx = _ctx(sol_balance=0.005)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_sol_reserve_uses_effective_N_not_confirmed_only():
    # confirmed-only N would pass, but a PENDING reservation pushes the post-entry book over.
    # effective_concurrent=2 (e.g. 0 confirmed + 2 pending). required = 0.002 + (2+1)*0.002*2 = 0.014
    ctx = _ctx(sol_balance=0.013, effective_concurrent=2, effective_exposure_usd=20.0,
               effective_pending_notional_usd=20.0)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_sol_reserve_passes_with_headroom():
    assert memecoin_entry_allowed(_ctx(sol_balance=1.0), mint="M", trade_size=10.0).allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_sol_reserve.py -q`
Expected: FAIL — `test_blocks_when_sol_cannot_cover_full_book_exit` returns allowed=True (no SOL check yet)

- [ ] **Step 3: Insert the SOL check**

In `memecoin_entry_allowed`, immediately **before** the `rc = RiskContext(...)` delegation block, add:

```python
    # SOL full-book exit reserve: after this entry there are (effective_concurrent + 1) open
    # positions; guarantee gas to exit ALL of them (with retry). effective N, not confirmed-only.
    required_sol = ctx.gas_entry_sol + (ctx.effective_concurrent + 1) * ctx.gas_exit_sol * ctx.gas_retry_factor
    if ctx.sol_balance < required_sol:
        return _reject("insufficient SOL for entry + full-book exit reserve")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_sol_reserve.py tests/test_memecoin_entry_check.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_risk.py tests/test_memecoin_sol_reserve.py
git commit -m "feat(memecoin): S1 SOL full-book exit reserve (effective-N)"
```

---

### Task 8: Atomic check-then-reserve (the armed entry path seam)

**Files:**
- Modify: `sniper/services/memecoin_risk.py`
- Test: `tests/test_memecoin_reserve_entry.py` (create)

**Interfaces:**
- Consumes: `build_memecoin_risk_context`, `memecoin_entry_allowed`, `m.reserve_pending`, `m.check_and_latch_daily_loss`.
- Produces: `reserve_entry(conn, *, mint, source_user_id, strategy, day, usdc_balance, sol_balance, now_iso, kill_tripped=False) -> tuple[RiskDecision, int | None]`. Latches the daily breaker first, builds the context, runs the check, and — only if allowed — reserves in one commit. Returns `(decision, pending_id)`; `pending_id` is None on rejection or a lost coin-dedup race.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_reserve_entry.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as r


def _c(monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_approved_entry_reserves_and_blocks_second_same_coin(monkeypatch):
    c = _c(monkeypatch)
    dec, pid = r.reserve_entry(c, mint="M1", source_user_id="u1", strategy="control",
                               day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t")
    assert dec.allowed is True and pid is not None
    assert m.pending_totals(c) == (1, 10.0)
    # a second signal for the SAME coin is rejected (one-position-per-coin, via held_mints)
    dec2, pid2 = r.reserve_entry(c, mint="M1", source_user_id="u2", strategy="control",
                                 day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t")
    assert dec2.allowed is False and pid2 is None
    assert m.pending_totals(c) == (1, 10.0)


def test_rejected_entry_reserves_nothing(monkeypatch):
    c = _c(monkeypatch)
    dec, pid = r.reserve_entry(c, mint="M1", source_user_id="u1", strategy="control",
                               day="2026-07-09", usdc_balance=1.0, sol_balance=1.0, now_iso="t")
    assert dec.allowed is False and pid is None          # insufficient USDC
    assert m.pending_totals(c) == (0, 0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_reserve_entry.py -q`
Expected: FAIL with `AttributeError: module 'sniper.services.memecoin_risk' has no attribute 'reserve_entry'`

- [ ] **Step 3: Implement `reserve_entry`**

```python
def reserve_entry(conn, *, mint, source_user_id, strategy, day, usdc_balance, sol_balance,
                  now_iso, kill_tripped=False):
    """Atomic armed-entry gate: latch the daily breaker, evaluate the envelope on effective
    values, and — only if allowed — reserve the coin (sig-less) in one commit. The reservation's
    partial UNIQUE(mint) is the final race guard; a lost race returns (reject, None). The caller
    (S2) then set_pending_sig(...) BEFORE broadcasting, and confirm/release on the outcome."""
    from sniper.settings import memecoin as _cfg
    check_and_latch = m.check_and_latch_daily_loss(conn, day, _cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD)
    ctx = build_memecoin_risk_context(conn, usdc_balance=usdc_balance, sol_balance=sol_balance,
                                      day=day, kill_tripped=kill_tripped)
    decision = memecoin_entry_allowed(ctx, mint=mint, trade_size=ctx.trade_size_usd)
    if not decision.allowed:
        return decision, None
    pid = m.reserve_pending(conn, mint=mint, source_user_id=source_user_id, strategy=strategy,
                            notional=ctx.trade_size_usd, reserved_at=now_iso)
    if pid is None:   # lost the coin-dedup race between check and INSERT
        return _reject("one position per coin: lost reservation race"), None
    return decision, pid
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_reserve_entry.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Full S1 suite + regression**

Run: `python3 -m pytest tests/ -q -k "memecoin_risk or pending_ledger or daily_latch or real_exposure or entry_check or sol_reserve or reserve_entry or risk_context or risk_settings"`
Expected: PASS (all S1 tests). Then a broad memecoin regression:
Run: `python3 -m pytest tests/ -q -k memecoin`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_risk.py tests/test_memecoin_reserve_entry.py
git commit -m "feat(memecoin): S1 atomic check-then-reserve armed-entry seam"
```

---

## Self-Review

**Spec coverage:**
- §2 shape (fixed USD, wallet=bankroll, per-coin=invariant): Tasks 1, 6 (size-invariant check), context uses injected USDC balance. ✓
- §3 config in `sniper/settings/memecoin.py`: Task 1. ✓
- §4 check order 1–11: Task 6 (1–9) + Task 7 (SOL, 10) + Task 8 (reserve, 11). ✓
- §5 pending ledger + write-ahead sig + partial unique + fail-closed release: Task 2 (`set_pending_sig`, release-on-failure-only, unique index). ✓
- §6 SOL effective-N reserve: Task 7. ✓
- §7 pin 1 latch (real-PnL-only, no un-trip, real-UTC reset): Task 3. Pin 2 floor: Task 1 (`MIN_TRADE_FLOOR_USD` + `MAX_ROUNDTRIP_COST_PCT`) + Task 6 min-floor check. ✓
- §8 policy-agnostic / real-enable flag: Task 6 (`real_enabled`), kill honored not owned. ✓
- §10 acceptance criteria 1–9: crit 1 (Task 6 real-disabled), 2 (Task 6 effective-USDC + Task 8 second-coin), 3 (Task 2 release/retain), 4 (Task 7 effective-N), 5 (Task 3 latch), 6 (Task 6 size-invariant), 7 (Task 6 min-floor), 8 (Task 6 effective values), 9 (Task 2 unique + Task 8 dedup). ✓

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `MemecoinRiskCtx` fields identical across Tasks 5–8; `memecoin_entry_allowed(ctx, *, mint, trade_size)` and `reserve_entry(...)` signatures stable; ledger helper names match between Task 2 and Tasks 5/8.

**Out of scope (spec §11), correctly absent:** real swap/sign/submit, `set_pending_sig` *call* timing (S2), real balance reads/RPC (S4 — balances injected), restart reconciliation of stale reservations (S5), kill-switch mechanism/mode-ladder/lock (S6). S1 provides the hooks (`set_pending_sig`, `execution_mode`, decidability rule) these consume.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-09-memecoin-s1-risk-envelope.md`.
