# Memecoin Copy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run three parallel exit-strategy arms (`control`, `momentum_a`, `alpha_b`) on the same curated-buy entry stream, tagged and compared, so live paper PnL decides which exit beats the exit-liquidity failure mode.

**Architecture:** Entry stays shared (flat $100, all curated large buys); each event fans out to one paper position per enabled arm, tagged with a `strategy` column (reusing the `signal_source` attribution pattern). A pure momentum state machine drives Arm A's exit inside the existing ~15s sweep; Arm B mirrors the trader's sell with catastrophe rails; control is unchanged. Per-close instrumentation (`time_to_peak_s`) yields a free exit-liquidity meter.

**Tech Stack:** Python 3.13, stdlib, SQLite (WAL), existing `db_memecoin`/`memecoin_copy_trader`/`memecoin_brackets`/`memecoin_runner`, Flask blueprint, vanilla JS `memecoin` controller.

## Global Constraints

- **Exit is the ONLY v1 variable.** Entry + sizing shared across arms (flat $100, all curated). Per-thesis selection/sizing is v2 — out of scope. (spec §2, §10)
- **Arms:** `control` (SL −60% / TP +500% / mirror), `momentum_a` (rollover fast exit), `alpha_b` (mirror + wide catastrophe rails, NO take-profit). (spec §2, §4, §5)
- **Dedup key is per (source_user_id, mint, strategy)** — three arms hold independent positions on one (trader, token). (spec §3)
- **Arm A ignores the trader's sell** (pure momentum); mirror-on-sell applies to `control` + `alpha_b` only. (spec §4, §5)
- **Exit constants are launch guesses, recalibrated later from instrumentation** — never claimed correct. (spec §7)
- All arms paper; exits use the honest modeled-slippage fills already shipped. DB access via `db_memecoin` only. `datetime.now(timezone.utc)`, guard divisions. Regenerate brain index + run `validate_schema_refs.py` after schema changes.

---

### Task 1: Momentum state machine (pure) + settings

**Files:**
- Create: `sniper/services/memecoin_momentum.py`
- Modify: `sniper/settings/memecoin.py` (append arm constants)
- Test: `tests/test_memecoin_momentum.py`

**Interfaces:**
- Produces: `momentum_decision(*, entry_price, peak_price, no_new_high_sweeps, price, held_s, rollover_sweeps, trail_pct, stop_pct, max_hold_s) -> dict` with keys `peak_price`, `no_new_high_sweeps`, `exit_reason` (`a_stop|a_trailing|a_rollover|a_timecap|None`).
- Settings: `MEMECOIN_A_ROLLOVER_SWEEPS`, `MEMECOIN_A_TRAIL_PCT`, `MEMECOIN_A_STOP_PCT`, `MEMECOIN_A_MAX_HOLD_S`, `MEMECOIN_B_STOP_PCT`, `MEMECOIN_B_MAX_HOLD_S`, `MEMECOIN_ARMS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_momentum.py
from sniper.services.memecoin_momentum import momentum_decision

DEF = dict(rollover_sweeps=2, trail_pct=15.0, stop_pct=30.0, max_hold_s=900)


def _step(entry, peak, nnh, price, held_s=10):
    return momentum_decision(entry_price=entry, peak_price=peak, no_new_high_sweeps=nnh,
                             price=price, held_s=held_s, **DEF)


def test_new_high_resets_counter_no_exit():
    r = _step(1.0, 1.0, 1, 1.2)          # new high
    assert r["peak_price"] == 1.2 and r["no_new_high_sweeps"] == 0 and r["exit_reason"] is None


def test_rollover_after_n_stalled_sweeps():
    r1 = _step(1.0, 1.2, 0, 1.15)        # no new high -> nnh 1
    assert r1["no_new_high_sweeps"] == 1 and r1["exit_reason"] is None
    r2 = _step(1.0, 1.2, 1, 1.15)        # nnh 2 >= rollover_sweeps -> exit
    assert r2["exit_reason"] == "a_rollover"


def test_trailing_before_rollover_on_sharp_reversal():
    # 20% below peak (>15% trail) but only 1 stalled sweep -> trailing wins
    r = _step(1.0, 1.2, 0, 1.2 * 0.80)
    assert r["exit_reason"] == "a_trailing"


def test_hard_stop_has_priority():
    # 35% below entry (>30% stop) -> stop even though it's also below peak
    r = _step(1.0, 1.2, 0, 0.65)
    assert r["exit_reason"] == "a_stop"


def test_time_cap_when_price_flat():
    # flat price (== peak, no new high), under all price triggers, but held past cap
    r = _step(1.0, 1.0, 0, 1.0, held_s=1000)
    assert r["exit_reason"] == "a_timecap"


def test_no_exit_while_pumping_under_cap():
    r = _step(1.0, 1.5, 0, 1.6, held_s=100)   # new high, young
    assert r["exit_reason"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_momentum.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/memecoin_momentum.py
"""Pure momentum-rollover exit decision for Arm A (fast herd-pump ride).

No DB, no clock — the runner supplies price/held_s and persists the returned peak state.
Exit priority: hard stop (catastrophe) -> trailing (sharp reversal) -> rollover (stall)
-> time cap. A new-high sweep never exits (momentum still up)."""
from __future__ import annotations


def momentum_decision(*, entry_price, peak_price, no_new_high_sweeps, price, held_s,
                      rollover_sweeps, trail_pct, stop_pct, max_hold_s):
    if price > peak_price:
        peak_price, no_new_high_sweeps = price, 0
    else:
        no_new_high_sweeps += 1
    reason = None
    if entry_price > 0 and price <= entry_price * (1 - stop_pct / 100.0):
        reason = "a_stop"
    elif peak_price > 0 and price <= peak_price * (1 - trail_pct / 100.0):
        reason = "a_trailing"
    elif no_new_high_sweeps >= rollover_sweeps:
        reason = "a_rollover"
    elif held_s > max_hold_s:
        reason = "a_timecap"
    return {"peak_price": peak_price, "no_new_high_sweeps": no_new_high_sweeps,
            "exit_reason": reason}
```

Append to `sniper/settings/memecoin.py`:

```python
# --- copy-hardening: parallel exit-strategy arms (2026-07-07) ---
# Constants are LAUNCH GUESSES, recalibrated from Arm A's time-to-peak instrumentation.
MEMECOIN_A_ROLLOVER_SWEEPS = int(os.getenv("MEMECOIN_A_ROLLOVER_SWEEPS", "2"))
MEMECOIN_A_TRAIL_PCT = float(os.getenv("MEMECOIN_A_TRAIL_PCT", "15"))
MEMECOIN_A_STOP_PCT = float(os.getenv("MEMECOIN_A_STOP_PCT", "30"))
MEMECOIN_A_MAX_HOLD_S = float(os.getenv("MEMECOIN_A_MAX_HOLD_S", "900"))       # 15 min
MEMECOIN_B_STOP_PCT = float(os.getenv("MEMECOIN_B_STOP_PCT", "50"))
MEMECOIN_B_MAX_HOLD_S = float(os.getenv("MEMECOIN_B_MAX_HOLD_S", "43200"))     # 12 h
# enabled arms (kill switch per arm); order-independent.
MEMECOIN_ARMS = [a.strip() for a in os.getenv(
    "MEMECOIN_ARMS", "control,momentum_a,alpha_b").split(",") if a.strip()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_momentum.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_momentum.py sniper/settings/memecoin.py tests/test_memecoin_momentum.py
git commit -m "feat(memecoin): pure momentum-rollover exit decision + arm settings"
```

---

### Task 2: Schema + strategy-aware position CRUD

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (migration; `open_position`, `get_open_position`, add `update_position_peak`, `open_positions_by_strategy`)
- Test: `tests/test_memecoin_strategy_db.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `open_position(conn, *, ..., strategy="control")` — adds `strategy` (existing kwargs unchanged).
  - `get_open_position(conn, source_user_id, mint, strategy="control")` — dedup now per (uid, mint, strategy).
  - `update_position_peak(conn, position_id, peak_price, peak_ts, no_new_high_sweeps) -> None`.
  - `finalize_close` (existing) unchanged; new `set_position_instrumentation(conn, position_id, time_to_peak_s, max_gain_pct) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_strategy_db.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, strat):
    return m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                           entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                           entry_lag_s=0.1, their_size_usd=0.0, opened_at="t0", strategy=strat)


def test_three_arms_hold_independent_positions_on_same_token():
    c = _conn()
    ids = [_open(c, s) for s in ("control", "momentum_a", "alpha_b")]
    assert len(set(ids)) == 3
    # dedup is per (uid, mint, strategy)
    assert m.get_open_position(c, "u1", "M", "momentum_a")["id"] == ids[1]
    assert m.get_open_position(c, "u1", "M", "control")["id"] == ids[0]
    # default strategy is 'control' (back-compat)
    assert m.get_open_position(c, "u1", "M")["strategy"] == "control"


def test_update_peak_and_instrumentation():
    c = _conn(); pid = _open(c, "momentum_a")
    m.update_position_peak(c, pid, 1.8, "t5", 0)
    p = m.get_position(c, pid)
    assert p["peak_price"] == 1.8 and p["no_new_high_sweeps"] == 0
    m.set_position_instrumentation(c, pid, time_to_peak_s=42.0, max_gain_pct=80.0)
    p = m.get_position(c, pid)
    assert p["time_to_peak_s"] == 42.0 and p["max_gain_pct"] == 80.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_strategy_db.py -q`
Expected: FAIL — `open_position() got an unexpected keyword argument 'strategy'`.

- [ ] **Step 3: Write minimal implementation**

In `ensure_memecoin_tables`, alongside the existing `signal_source` migration block, add:

```python
    for _col, _ddl in (("strategy", "TEXT DEFAULT 'control'"),
                       ("peak_price", "REAL"), ("peak_ts", "TEXT"),
                       ("no_new_high_sweeps", "INTEGER DEFAULT 0"),
                       ("time_to_peak_s", "REAL"), ("max_gain_pct", "REAL")):
        if _col not in pos_cols:
            conn.execute(f"ALTER TABLE memecoin_positions ADD COLUMN {_col} {_ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_strat "
                 "ON memecoin_positions(source_user_id, mint, strategy, is_open)")
```

Update `open_position` signature + INSERT to include `strategy`:

```python
def open_position(conn, *, source_user_id, source_handle, mint, ticker, entry_price,
                  entry_notional, entry_fill_source, entry_lag_s, their_size_usd,
                  entry_liquidity=None, entry_token_age_s=None, opened_at,
                  signal_source="global_feed", strategy="control"):
    cur = conn.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, mint, ticker, "
        "entry_price, entry_notional, entry_fill_source, entry_lag_s, their_size_usd, "
        "entry_liquidity, entry_token_age_s, is_open, opened_at, signal_source, strategy, "
        "peak_price, peak_ts, no_new_high_sweeps) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,0)",
        (source_user_id, source_handle, mint, ticker, entry_price, entry_notional,
         entry_fill_source, entry_lag_s, their_size_usd, entry_liquidity,
         entry_token_age_s, opened_at, signal_source, strategy, entry_price, opened_at))
    conn.commit()
    return cur.lastrowid
```

(peak seeded to entry_price / opened_at so the first sweep compares against entry.)

Update `get_open_position` to filter by strategy:

```python
def get_open_position(conn, source_user_id, mint, strategy="control"):
    r = conn.execute(
        "SELECT * FROM memecoin_positions WHERE source_user_id=? AND mint=? "
        "AND strategy=? AND is_open=1", (source_user_id, mint, strategy)).fetchone()
    return dict(r) if r else None
```

Add helpers:

```python
def update_position_peak(conn, position_id, peak_price, peak_ts, no_new_high_sweeps):
    conn.execute("UPDATE memecoin_positions SET peak_price=?, peak_ts=?, no_new_high_sweeps=? "
                 "WHERE id=?", (peak_price, peak_ts, no_new_high_sweeps, position_id))
    conn.commit()


def set_position_instrumentation(conn, position_id, *, time_to_peak_s, max_gain_pct):
    conn.execute("UPDATE memecoin_positions SET time_to_peak_s=?, max_gain_pct=? WHERE id=?",
                 (time_to_peak_s, max_gain_pct, position_id))
    conn.commit()


def open_positions_by_strategy(conn, strategy):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE is_open=1 AND strategy=?", (strategy,))]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_strategy_db.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Regenerate brain index + commit**

```bash
python3 scripts/brain_index.py generate
git add sniper/persistence/db_memecoin.py tests/test_memecoin_strategy_db.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin): strategy column + per-(uid,mint,strategy) dedup + peak/instrumentation helpers"
```

---

### Task 3: Entry fan-out + mirror-dispatch by strategy

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py` (`on_copy_event` gains `strategy`; sell path mirrors only mirroring arms), `sniper/services/memecoin_runner.py` (`run_once` loops enabled arms)
- Test: `tests/test_memecoin_fanout.py`

**Interfaces:**
- Consumes: `open_position(strategy=)`, `get_open_position(...,strategy)` (Task 2); `MEMECOIN_ARMS` (Task 1).
- Produces: `on_copy_event(conn, ev, size_usd, now_iso, fill_fn=honest_fill, stats_fn=None, strategy="control")`. `momentum_a` sells → `"ignored"` (A ignores trader sells). `run_once` opens/handles one position per arm in `MEMECOIN_ARMS`.
- Module constant `MIRROR_ARMS = ("control", "alpha_b")` in `memecoin_copy_trader`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_fanout.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


class _Ev:
    def __init__(self, side):
        self.side = side; self.source_user_id = "u1"; self.source_handle = "alice"
        self.mint = "MINT"; self.ticker = "PEPE"; self.their_price = 0.005
        self.their_market_cap = 1e6; self.their_size_usd = 3000.0; self.is_first_buy = True
        self.fomo_event_id = f"e-{side}"; self.created_at = "t"; self.received_at = "t"
        self.lag_s = 0.1; self.signal_source = "global_feed"


def test_buy_opens_one_position_per_arm():
    c = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "b1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-07T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}}]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 07 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.005, "jupiter"))
    strats = {r[0] for r in c.execute("SELECT strategy FROM memecoin_positions WHERE is_open=1")}
    assert strats == {"control", "momentum_a", "alpha_b"}


def test_momentum_a_ignores_trader_sell_but_control_and_b_mirror():
    c = _conn()
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev("buy"), 100.0, "t0",
                         fill_fn=lambda *a, **k: (0.005, "jupiter"), strategy=s)
    # a trader sell
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev("sell"), 100.0, "t1",
                         fill_fn=lambda *a, **k: (0.006, "jupiter"), strategy=s)
    open_now = {r[0] for r in c.execute("SELECT strategy FROM memecoin_positions WHERE is_open=1")}
    assert open_now == {"momentum_a"}      # A still open; control + alpha_b mirror-closed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_fanout.py -q`
Expected: FAIL — `on_copy_event() got an unexpected keyword argument 'strategy'`.

- [ ] **Step 3: Write minimal implementation**

In `memecoin_copy_trader.py`, add at module level: `MIRROR_ARMS = ("control", "alpha_b")`.
Change the signature and thread `strategy` through the buy path (dedup + open); gate the sell path:

```python
def on_copy_event(conn, ev, size_usd, now_iso, fill_fn=honest_fill, stats_fn=None,
                  strategy="control"):
    if ev.side == "buy":
        if m.get_open_position(conn, ev.source_user_id, ev.mint, strategy):
            return "skipped_dup"
        price, source = fill_fn(ev.mint, size_usd, "buy", ev.their_price)
        if not price or price <= 0:
            return "ignored"
        from sniper.settings.memecoin import MEMECOIN_CHASE_MAX_PCT
        tp = ev.their_price
        if tp and tp > 0:
            gap_pct = (price / tp - 1.0) * 100.0
            if gap_pct > MEMECOIN_CHASE_MAX_PCT:
                m.set_event_chase(conn, ev.fomo_event_id, price, gap_pct)
                return "skipped_chase"
        entry_liq = None
        if stats_fn is not None:
            try:
                _st = stats_fn(ev.mint)
                entry_liq = _st.get("liquidity") if _st else None
            except Exception:
                entry_liq = None
        qty = size_usd / price
        pid = m.open_position(
            conn, source_user_id=ev.source_user_id, source_handle=ev.source_handle,
            mint=ev.mint, ticker=ev.ticker, entry_price=price, entry_notional=size_usd,
            entry_fill_source=source, entry_lag_s=ev.lag_s, their_size_usd=ev.their_size_usd,
            entry_liquidity=entry_liq, opened_at=now_iso,
            signal_source=getattr(ev, "signal_source", "global_feed"), strategy=strategy)
        m.insert_fill(conn, position_id=pid, kind="entry", price=price, qty=qty,
                      notional=size_usd, fill_source=source, fraction=1.0, created_at=now_iso)
        return "opened"

    # ev.side == "sell": only the MIRRORING arms exit on the trader's sell; momentum_a ignores it.
    if strategy not in MIRROR_ARMS:
        return "ignored"
    pos = m.get_open_position(conn, ev.source_user_id, ev.mint, strategy)
    if not pos:
        return "ignored"
    exit_price, source = fill_fn(ev.mint, pos["entry_notional"], "sell", ev.their_price)
    if not exit_price or exit_price <= 0:
        return "ignored"
    if not m.claim_close(conn, pos["id"]):
        return "ignored"
    qty = _qty_of(conn, pos["id"])
    m.insert_fill(conn, position_id=pos["id"], kind="mirror_exit", price=exit_price,
                  qty=qty, notional=exit_price * qty, fill_source=source,
                  fraction=1.0, created_at=now_iso)
    m.finalize_close(conn, pos["id"], now_iso,
                     _realized_from_exits(conn, pos["id"], pos["entry_price"]),
                     exit_reason="b_mirror" if strategy == "alpha_b" else "mirror")
    return "exit"
```

In `memecoin_runner.py` `run_once`, replace the single `on_copy_event` call with a per-arm loop:

```python
        from sniper.settings.memecoin import MEMECOIN_ARMS
        ev_size = conviction_size(ev.their_size_usd, size_usd) if ev.side == "buy" else size_usd
        try:
            r = None
            for _strat in MEMECOIN_ARMS:
                r = on_copy_event(conn, ev, ev_size, now_fn(), fill_fn=fill_fn,
                                  stats_fn=stats_fn, strategy=_strat)
        except Exception:
            m.delete_event(conn, ev.fomo_event_id)
            continue
        processed += 1
        m.set_event_action(conn, ev.fomo_event_id, r)   # last arm's outcome (control ordering)
        opened += (r == "opened")
        exits += (r == "exit")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_fanout.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py sniper/services/memecoin_runner.py tests/test_memecoin_fanout.py
git commit -m "feat(memecoin): entry fan-out to enabled arms + mirror-dispatch (A ignores trader sells)"
```

---

### Task 4: Strategy-aware exit sweep (momentum + rails) + instrumentation

**Files:**
- Create: `sniper/services/memecoin_exits.py` (the per-strategy sweep dispatcher)
- Modify: `sniper/services/memecoin_runner.py` (`bracket_pass` calls the dispatcher), `sniper/services/memecoin_brackets.py` (export the control SL/TP as a reusable helper)
- Test: `tests/test_memecoin_exits.py`

**Interfaces:**
- Consumes: `momentum_decision` (Task 1); `open_positions_by_strategy`, `update_position_peak`, `set_position_instrumentation`, `claim_close`, `finalize_close`, `open_qty`, `insert_fill` (Task 2 + existing); `modeled_fill`, `norm_mint` (`solana_quote`).
- Produces: `sweep_exits(conn, price_map, now_iso, *, cfg=None) -> list[dict]` — runs every arm's time/price exit, returns closed rows. `control` uses the existing SL/TP; `momentum_a` uses `momentum_decision` + peak persistence + `time_to_peak_s`/`max_gain_pct` at close; `alpha_b` uses wide stop + time cap.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_exits.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_exits import sweep_exits


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, strat, entry=1.0):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=entry, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=100.0 / entry,
                  notional=100.0, fill_source="j", fraction=1.0, created_at="2026-07-07T10:00:00+00:00")
    return pid


def _pm(price, liq=1e15):
    return {"M": {"price": price, "liquidity": liq}}


def test_momentum_a_rides_new_high_then_exits_on_rollover():
    c = _conn(); pid = _open(c, "momentum_a")
    # sweep 1: new high 1.5 -> stays open, peak updated
    assert sweep_exits(c, _pm(1.5), "2026-07-07T10:00:15+00:00") == []
    assert m.get_position(c, pid)["peak_price"] == 1.5
    # sweep 2: 1.4 (stall 1), sweep 3: 1.4 (stall 2 >= rollover) -> exit a_rollover
    sweep_exits(c, _pm(1.4), "2026-07-07T10:00:30+00:00")
    out = sweep_exits(c, _pm(1.4), "2026-07-07T10:00:45+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "a_rollover"
    assert p["max_gain_pct"] is not None and abs(p["max_gain_pct"] - 50.0) < 1e-6   # peak 1.5 vs entry 1.0
    assert p["time_to_peak_s"] is not None                                          # peaked at sweep 1


def test_alpha_b_ignores_momentum_holds_until_wide_stop():
    c = _conn(); pid = _open(c, "alpha_b")
    # a stall that would trip momentum does NOT close alpha_b
    for t in ("10:00:15", "10:00:30", "10:00:45"):
        sweep_exits(c, _pm(0.9), f"2026-07-07T{t}+00:00")
    assert m.get_position(c, pid)["is_open"] == 1
    # only a >50% drop (wide stop) closes it
    out = sweep_exits(c, _pm(0.45), "2026-07-07T10:01:00+00:00")
    assert m.get_position(c, pid)["exit_reason"] == "b_stop"


def test_control_uses_sl_tp():
    c = _conn(); pid = _open(c, "control")
    sweep_exits(c, _pm(0.35), "2026-07-07T10:00:15+00:00")   # -65% <= -60% SL
    assert m.get_position(c, pid)["exit_reason"] == "stop_loss"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_exits.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

First, in `sniper/services/memecoin_brackets.py`, factor the control SL/TP trigger into a reusable helper (keep `evaluate_brackets` working by delegating):

```python
def control_exit_reason(pnl_pct, sl_pct, tp_pct):
    return ("stop_loss" if pnl_pct <= -sl_pct
            else "take_profit" if pnl_pct >= tp_pct else None)
```

Then create the dispatcher:

```python
# sniper/services/memecoin_exits.py
"""Per-strategy exit sweep. One batched price_map (from the runner's ~15s bracket pass) drives
every arm's time/price exits: control=SL/TP, momentum_a=rollover machine, alpha_b=wide rails.
Mirror-on-sell is handled in the copy engine (control+alpha_b); momentum_a exits ONLY here."""
from __future__ import annotations

from datetime import datetime

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import _realized_from_exits
from sniper.services.memecoin_momentum import momentum_decision
from sniper.services.memecoin_brackets import control_exit_reason
from sniper.execution.solana_quote import norm_mint, modeled_fill


def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _held_s(opened_at, now_iso):
    a, b = _parse(opened_at), _parse(now_iso)
    return (b - a).total_seconds() if (a and b) else 0.0


def _close(conn, pos, mid, now_iso, reason):
    """Claim + modeled-slippage exit fill + finalize, with instrumentation."""
    if not m.claim_close(conn, pos["id"]):
        return None
    qty = m.open_qty(conn, pos["id"])
    if qty <= 0:
        return None
    price = modeled_fill(mid, mid * qty, None, "sell")   # liquidity applied by caller's map below
    m.insert_fill(conn, position_id=pos["id"], kind="bracket_exit", price=price, qty=qty,
                  notional=price * qty, fill_source="dexscreener", fraction=1.0, created_at=now_iso)
    entry = pos["entry_price"] or 0.0
    peak = pos.get("peak_price") or entry
    tpk = _held_s(pos["opened_at"], pos.get("peak_ts") or pos["opened_at"])
    max_gain = ((peak / entry - 1.0) * 100.0) if entry > 0 else None
    m.set_position_instrumentation(conn, pos["id"], time_to_peak_s=tpk, max_gain_pct=max_gain)
    m.finalize_close(conn, pos["id"], now_iso,
                     _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
    return {"position_id": pos["id"], "strategy": pos["strategy"], "exit_reason": reason}


def sweep_exits(conn, price_map, now_iso, *, cfg=None):
    from sniper.settings import memecoin as S
    closed = []
    price_map = price_map or {}

    def _mid_liq(pos):
        st = price_map.get(norm_mint(pos["mint"])) or {}
        return st.get("price"), st.get("liquidity")

    # control: SL/TP on mid
    for pos in m.open_positions_by_strategy(conn, "control"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            continue
        pnl = (mid / pos["entry_price"] - 1.0) * 100.0
        reason = control_exit_reason(pnl, S.MEMECOIN_STOP_LOSS_PCT, S.MEMECOIN_TAKE_PROFIT_PCT)
        if reason:
            r = _close(conn, pos, mid, now_iso, reason)
            if r:
                closed.append(r)

    # momentum_a: rollover machine + peak persistence
    for pos in m.open_positions_by_strategy(conn, "momentum_a"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            continue
        d = momentum_decision(
            entry_price=pos["entry_price"], peak_price=pos.get("peak_price") or pos["entry_price"],
            no_new_high_sweeps=pos.get("no_new_high_sweeps") or 0, price=mid,
            held_s=_held_s(pos["opened_at"], now_iso),
            rollover_sweeps=S.MEMECOIN_A_ROLLOVER_SWEEPS, trail_pct=S.MEMECOIN_A_TRAIL_PCT,
            stop_pct=S.MEMECOIN_A_STOP_PCT, max_hold_s=S.MEMECOIN_A_MAX_HOLD_S)
        # persist peak (peak_ts advances only on a new high)
        new_peak_ts = now_iso if d["peak_price"] > (pos.get("peak_price") or 0) else pos.get("peak_ts")
        m.update_position_peak(conn, pos["id"], d["peak_price"], new_peak_ts, d["no_new_high_sweeps"])
        if d["exit_reason"]:
            pos2 = m.get_position(conn, pos["id"])   # re-read with updated peak for instrumentation
            r = _close(conn, pos2, mid, now_iso, d["exit_reason"])
            if r:
                closed.append(r)

    # alpha_b: wide catastrophe stop + long time cap (NO TP; mirror handled in copy engine)
    for pos in m.open_positions_by_strategy(conn, "alpha_b"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            continue
        pnl = (mid / pos["entry_price"] - 1.0) * 100.0
        reason = ("b_stop" if pnl <= -S.MEMECOIN_B_STOP_PCT
                  else "b_timecap" if _held_s(pos["opened_at"], now_iso) > S.MEMECOIN_B_MAX_HOLD_S
                  else None)
        if reason:
            r = _close(conn, pos, mid, now_iso, reason)
            if r:
                closed.append(r)
    return closed
```

Then in `memecoin_runner.py`, point `bracket_pass` at the dispatcher (replace the `evaluate_brackets` call):

```python
def bracket_pass(conn, price_fn=None, now_fn=None):
    from sniper.execution.solana_quote import dexscreener_stats_batch
    from sniper.services.memecoin_exits import sweep_exits
    price_fn = price_fn or dexscreener_stats_batch
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    mints = list({p["mint"] for p in m.list_open_positions(conn)})
    if not mints:
        return []
    return sweep_exits(conn, price_fn(mints), now_fn())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_exits.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_exits.py sniper/services/memecoin_brackets.py sniper/services/memecoin_runner.py tests/test_memecoin_exits.py
git commit -m "feat(memecoin): strategy-aware exit sweep (momentum_a rollover, alpha_b rails, control SL/TP) + instrumentation"
```

---

### Task 5: Strategy comparison — summary, exit-liquidity meter, API, UI

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (`strategy_summary`), `sniper/routes/api_memecoin.py` (`GET /api/memecoin/strategies`), `static/app.js` (Closed-tab arm panel + `strategy` on Open positions)
- Test: `tests/test_memecoin_strategy_summary.py`

**Interfaces:**
- Consumes: positions with `strategy`, `time_to_peak_s` (Tasks 2/4).
- Produces: `strategy_summary(conn) -> list[dict]` per arm: `strategy, opened, closed, wins, realized_pnl, win_rate, median_hold_s, exit_liquidity_rate` (fraction of closes whose `time_to_peak_s <= 15`, i.e. peaked within one sweep of entry).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_strategy_summary.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _closed(c, strat, pnl, ttp):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat)
    m.set_position_instrumentation(c, pid, time_to_peak_s=ttp, max_gain_pct=10.0)
    m.finalize_close(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason="x")


def test_strategy_summary_and_exit_liquidity_rate():
    c = _conn()
    _closed(c, "momentum_a", 5.0, 40.0)    # peaked later -> not exit-liq
    _closed(c, "momentum_a", -3.0, 0.0)    # peaked at entry -> exit-liq
    _closed(c, "control", -8.0, 2.0)       # peaked within a sweep -> exit-liq
    s = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert s["momentum_a"]["closed"] == 2 and abs(s["momentum_a"]["realized_pnl"] - 2.0) < 1e-6
    assert s["momentum_a"]["win_rate"] == 0.5
    assert abs(s["momentum_a"]["exit_liquidity_rate"] - 0.5) < 1e-6   # 1 of 2 peaked <=15s
    assert s["control"]["exit_liquidity_rate"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_strategy_summary.py -q`
Expected: FAIL — `module has no attribute 'strategy_summary'`.

- [ ] **Step 3: Write minimal implementation**

Add to `db_memecoin.py`:

```python
def strategy_summary(conn):
    """Copy performance by execution arm — the A/B/control read. exit_liquidity_rate =
    fraction of closes whose peak was within one sweep (~15s) of entry (we were the bag)."""
    import statistics as _st
    out = {}
    for r in conn.execute("SELECT * FROM memecoin_positions"):
        p = dict(r)
        a = out.setdefault(p.get("strategy") or "control",
                           {"strategy": p.get("strategy") or "control", "opened": 0, "closed": 0,
                            "wins": 0, "realized_pnl": 0.0, "_holds": [], "_peaked_at_entry": 0,
                            "_ttp_n": 0})
        a["opened"] += 1
        if not p["is_open"]:
            a["closed"] += 1
            rp = p.get("realized_pnl") or 0.0
            a["realized_pnl"] += rp
            if rp > 0:
                a["wins"] += 1
            ttp = p.get("time_to_peak_s")
            if ttp is not None:
                a["_ttp_n"] += 1
                if ttp <= 15.0:
                    a["_peaked_at_entry"] += 1
            try:
                from datetime import datetime as _dt
                h = (_dt.fromisoformat(p["closed_at"]) - _dt.fromisoformat(p["opened_at"])).total_seconds()
                if h >= 0:
                    a["_holds"].append(h)
            except (ValueError, TypeError, KeyError):
                pass
    res = []
    for a in out.values():
        res.append({
            "strategy": a["strategy"], "opened": a["opened"], "closed": a["closed"],
            "realized_pnl": round(a["realized_pnl"], 2),
            "win_rate": (a["wins"] / a["closed"]) if a["closed"] else None,
            "median_hold_s": _st.median(a["_holds"]) if a["_holds"] else None,
            "exit_liquidity_rate": (a["_peaked_at_entry"] / a["_ttp_n"]) if a["_ttp_n"] else None,
        })
    return res
```

Add to `api_memecoin.py` (inside the factory, next to `signal_sources`):

```python
    @bp.get("/api/memecoin/strategies")
    def strategies():
        return jsonify({"rows": m.strategy_summary(conn)})
```

In `static/app.js` `_loadClosed`, fetch `/api/memecoin/strategies` alongside the others and render an arm panel (PnL/WR/exit-liq per arm) above the source A/B line; add the `strategy` value to each Open-position row in `_loadOpen`. (Follows the existing `srcLine` render pattern — one `<div>` per arm with `strategy`, `realized_pnl` colored via `_col`, `win_rate` %, and `exit_liquidity_rate` % labeled "exit-liq".)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_strategy_summary.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
node --check static/app.js
git add sniper/persistence/db_memecoin.py sniper/routes/api_memecoin.py static/app.js tests/test_memecoin_strategy_summary.py
git commit -m "feat(memecoin): strategy_summary + exit-liquidity meter + /strategies endpoint + Closed-tab arm panel"
```

---

### Task 6: Integration smoke + finalize

**Files:**
- Test: `tests/test_memecoin_hardening_integration.py`

- [ ] **Step 1: Write an end-to-end test (buy fan-out → momentum sweep exit → summary)**

```python
# tests/test_memecoin_hardening_integration.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services.memecoin_exits import sweep_exits


class _Ev:
    side = "buy"; source_user_id = "u1"; source_handle = "alice"; mint = "M"; ticker = "T"
    their_price = 1.0; their_market_cap = 1e6; their_size_usd = 3000.0; is_first_buy = True
    fomo_event_id = "e1"; created_at = "t"; received_at = "t"; lag_s = 0.1; signal_source = "global_feed"


def test_full_arms_pipeline():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice")
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev(), 100.0, "2026-07-07T10:00:00+00:00",
                         fill_fn=lambda *a, **k: (1.0, "jupiter"), strategy=s)
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0] == 3
    pm = {"M": {"price": 1.5, "liquidity": 1e15}}
    sweep_exits(c, pm, "2026-07-07T10:00:15+00:00")                 # new high
    sweep_exits(c, {"M": {"price": 1.3, "liquidity": 1e15}}, "2026-07-07T10:00:30+00:00")
    sweep_exits(c, {"M": {"price": 1.3, "liquidity": 1e15}}, "2026-07-07T10:00:45+00:00")
    # momentum_a should have rolled over and closed; control/alpha_b still open (no SL/rails hit)
    a = m.get_open_position(c, "u1", "M", "momentum_a")
    assert a is None
    assert m.get_open_position(c, "u1", "M", "control") is not None
    summ = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert summ["momentum_a"]["closed"] == 1
```

- [ ] **Step 2: Run the full new + existing memecoin suite**

Run:
```bash
python3 -m pytest tests/ -q -k "memecoin or fomo"
```
Expected: all PASS (existing suite unaffected — `on_copy_event`/`get_open_position` defaults keep back-compat).

- [ ] **Step 3: Brain index + schema validator**

Run:
```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
```
Expected: validator clean; index fresh (commit if changed).

- [ ] **Step 4: Commit**

```bash
git add tests/test_memecoin_hardening_integration.py index/schema_registry.json index/dependency_index.json
git commit -m "test(memecoin): arms pipeline integration smoke + finalize"
```

---

## Self-Review

**Spec coverage:** §2 arms → Tasks 1/3/4. §3 data model + per-(uid,mint,strategy) dedup → Task 2. §4 momentum machine + instrumentation → Tasks 1/4. §5 alpha_b rails/no-TP → Tasks 3/4. §6 exit-liquidity meter → Tasks 4/5. §7 settings → Task 1. §8 surfaces → Task 5. §9 testing → every task + Task 6. §10 v2 deferrals → not implemented (correct). §11 honesty (modeled-slippage exits, pre-registered read) → `_close` uses `modeled_fill`; comparison is descriptive. ✓

**Placeholder scan:** none. The only prose-described step is the `_loadClosed`/`_loadOpen` JS render (Task 5 step 3), which points at the concrete existing `srcLine` pattern and exact fields — no TODOs.

**Type consistency:** `strategy` kwarg default `"control"` consistent across `open_position`/`get_open_position`/`on_copy_event` (Tasks 2/3). `momentum_decision` keyword signature identical in Tasks 1 and 4. `sweep_exits(conn, price_map, now_iso)` consistent Tasks 4/6. `MIRROR_ARMS`/`MEMECOIN_ARMS` names stable. `exit_reason` vocabulary (`a_rollover|a_trailing|a_stop|a_timecap|b_mirror|b_stop|b_timecap|stop_loss|take_profit|mirror`) consistent across copy engine + sweep.
