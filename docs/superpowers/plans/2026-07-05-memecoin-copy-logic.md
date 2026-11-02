# Memecoin Copy Logic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a chase guard on entry and SL/TP brackets on exit to the memecoin copy engine, with every close tagged by `exit_reason`, landing on top of the already-shipped partial-exit / manual-buy code.

**Architecture:** Entry gains a chase guard in `on_copy_event` (skip + record counterfactual). Exit gains a bracket evaluator run on a ~15s cadence from the runner loop, using a batched DexScreener price map, closing positions on −SL%/+TP% using **remaining** `open_qty` and the existing `_realized_from_exits`. `exit_reason` threads through all close paths.

**Tech Stack:** Python 3, stdlib `sqlite3`/`urllib`, existing `sniper.persistence.db_memecoin`, `sniper.services.memecoin_copy_trader`, `sniper.execution.solana_quote`, `sniper.services.memecoin_runner`, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-05-memecoin-copy-logic-design.md`. Values: chase guard **15%**, stop-loss **60%**, take-profit **500%**, **no** time cap, **no** position cap, fixed **$100** size.
- Branch `feat/memecoin-ui-integration`; land **on top of** the parallel build — reconcile, never rewrite `partial_exit`/`manual_buy`/`open_qty`/`_realized_from_exits`/`set_event_action`/`write_heartbeat`/`wallet_stats`.
- All DB access through `db_memecoin`; thresholds from `sniper/settings/memecoin.py` (env-loaded); `datetime.now(timezone.utc)`; guard `entry_price > 0`.
- Brackets fill **`open_qty`** (remaining), realize via **`_realized_from_exits`** — never recompute on entry qty.
- SL/TP are ~15s-cadence triggers; the `bracket_exit` fills at the **actual current price** (gap-through honest), not exactly ±threshold.
- Idempotent migrations (PRAGMA-guarded ALTER). After schema change: `python3 scripts/brain_index.py generate` + `python3 scripts/validate_schema_refs.py`.
- Focused tests: `python3 -m pytest tests/test_memecoin_*.py -q`.

---

### Task 1: Config knobs + schema migrations + fill-kind wiring

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/persistence/db_memecoin.py` (ensure_memecoin_tables migrations; EXIT_KINDS; close_position; new set_event_chase)
- Test: `tests/test_memecoin_copy_logic.py`

**Interfaces:**
- Produces:
  - Settings: `MEMECOIN_CHASE_MAX_PCT: float` (15), `MEMECOIN_STOP_LOSS_PCT: float` (60), `MEMECOIN_TAKE_PROFIT_PCT: float` (500).
  - `EXIT_KINDS` now includes `"bracket_exit"`.
  - `close_position(conn, position_id, closed_at, realized_pnl, exit_reason=None)`.
  - `set_event_chase(conn, fomo_event_id, wouldbe_price, chase_gap_pct) -> None`.
  - New columns: `memecoin_positions.exit_reason TEXT`, `memecoin_events.wouldbe_price REAL`, `memecoin_events.chase_gap_pct REAL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_migrations_and_exit_kinds():
    c = _conn()
    pos_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    ev_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_events)")}
    assert "exit_reason" in pos_cols
    assert {"wouldbe_price", "chase_gap_pct"} <= ev_cols
    assert "bracket_exit" in m.EXIT_KINDS          # brackets reduce open qty


def test_close_position_records_reason():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="M", ticker="T",
                          entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1.0, opened_at="t")
    m.close_position(c, pid, "t2", 10.0, exit_reason="stop_loss")
    assert m.list_closed_positions(c)[0]["exit_reason"] == "stop_loss"


def test_set_event_chase():
    c = _conn()
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "a", "mint": "M",
          "ticker": "T", "side": "buy", "their_price": 0.005, "their_market_cap": 1.0,
          "their_size_usd": 1.0, "is_first_buy": 1, "created_at": "t", "received_at": "t",
          "lag_s": 0.3}
    m.insert_event(c, ev)
    m.set_event_chase(c, "e1", wouldbe_price=0.0061, chase_gap_pct=22.0)
    row = c.execute("SELECT wouldbe_price, chase_gap_pct FROM memecoin_events WHERE fomo_event_id='e1'").fetchone()
    assert abs(row[0] - 0.0061) < 1e-9 and abs(row[1] - 22.0) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -q`
Expected: FAIL — `exit_reason`/columns missing, `bracket_exit` not in EXIT_KINDS, `set_event_chase` undefined.

- [ ] **Step 3: Write minimal implementation**

In `sniper/settings/memecoin.py` append:
```python
MEMECOIN_CHASE_MAX_PCT = float(os.getenv("MEMECOIN_CHASE_MAX_PCT", "15"))
MEMECOIN_STOP_LOSS_PCT = float(os.getenv("MEMECOIN_STOP_LOSS_PCT", "60"))
MEMECOIN_TAKE_PROFIT_PCT = float(os.getenv("MEMECOIN_TAKE_PROFIT_PCT", "500"))
```

In `sniper/persistence/db_memecoin.py`:
- Change `EXIT_KINDS = ("partial_exit", "mirror_exit", "manual_close")` → add `"bracket_exit"`:
```python
EXIT_KINDS = ("partial_exit", "mirror_exit", "manual_close", "bracket_exit")
```
- In `ensure_memecoin_tables`, after the existing `memecoin_events.action` migration block, add:
```python
    pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
    if "exit_reason" not in pos_cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN exit_reason TEXT")
    for _col in ("wouldbe_price", "chase_gap_pct"):
        if _col not in ev_cols:
            conn.execute(f"ALTER TABLE memecoin_events ADD COLUMN {_col} REAL")
```
  (`ev_cols` is already computed above for the `action` migration; reuse it. If it is out of scope, recompute: `ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_events)")}`.)
- Replace `close_position`:
```python
def close_position(conn, position_id, closed_at, realized_pnl, exit_reason=None):
    conn.execute(
        "UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=?, exit_reason=? WHERE id=?",
        (closed_at, realized_pnl, exit_reason, position_id))
    conn.commit()
```
- Add:
```python
def set_event_chase(conn, fomo_event_id, wouldbe_price, chase_gap_pct):
    """Record the chase-guard counterfactual so we can later judge if skipping was right."""
    conn.execute(
        "UPDATE memecoin_events SET wouldbe_price=?, chase_gap_pct=? WHERE fomo_event_id=?",
        (wouldbe_price, chase_gap_pct, fomo_event_id))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Regen index + commit**

```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/settings/memecoin.py sniper/persistence/db_memecoin.py tests/test_memecoin_copy_logic.py index/
git commit -m "feat(memecoin-copy): copy-logic config + schema migrations + bracket_exit kind"
```

---

### Task 2: Thread `exit_reason` + count `bracket_exit` in realized PnL

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py` (`_realized_from_exits`; `on_copy_event` mirror close; `partial_exit` close)
- Modify: `sniper/routes/api_memecoin.py` (manual close → `exit_reason="manual"`)
- Test: `tests/test_memecoin_copy_logic.py` (append)

**Interfaces:**
- Consumes: `close_position(..., exit_reason=)` (Task 1), `EXIT_KINDS` incl `bracket_exit`.
- Produces: `_realized_from_exits` counts `bracket_exit`; mirror close → `exit_reason="mirror"`; partial final close → `exit_reason="partial"`; manual close route → `exit_reason="manual"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py  (append)
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _ev(side, uid="u1", mint="M"):
    return CopyEvent(uid, "a", mint, "T", side, 0.005, 1e6, 1.0, True, "e", "t", "t", 0.3)


def test_mirror_close_tags_reason():
    c = _conn()
    eng.on_copy_event(c, _ev("buy"), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))
    eng.on_copy_event(c, _ev("sell"), 100.0, "t2", fill_fn=lambda *a, **k: (0.010, "jupiter"))
    assert m.list_closed_positions(c)[0]["exit_reason"] == "mirror"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py::test_mirror_close_tags_reason -q`
Expected: FAIL — `exit_reason` is None (mirror path doesn't set it yet).

- [ ] **Step 3: Write minimal implementation**

In `sniper/services/memecoin_copy_trader.py`:
- `_realized_from_exits` — add `bracket_exit` to the kind list:
```python
        "SELECT price, qty FROM memecoin_fills WHERE position_id=? AND kind IN "
        "('partial_exit','mirror_exit','manual_close','bracket_exit')", (position_id,)).fetchall()
```
- In `on_copy_event`, the mirror-exit close call — add `exit_reason="mirror"`:
```python
    m.close_position(conn, pos["id"], now_iso,
                     _realized_from_exits(conn, pos["id"], pos["entry_price"]),
                     exit_reason="mirror")
```
- In `partial_exit`, the final close call — add `exit_reason="partial"`:
```python
        m.close_position(conn, position_id, now_iso,
                         _realized_from_exits(conn, position_id, pos["entry_price"]),
                         exit_reason="partial")
```

In `sniper/routes/api_memecoin.py`, the `manual_close` route's `m.close_position(...)` call — add `exit_reason="manual"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py sniper/routes/api_memecoin.py tests/test_memecoin_copy_logic.py
git commit -m "feat(memecoin-copy): thread exit_reason through close paths + count bracket_exit"
```

---

### Task 3: Chase guard on entry (+ counterfactual)

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py` (`on_copy_event` buy branch)
- Test: `tests/test_memecoin_copy_logic.py` (append)

**Interfaces:**
- Consumes: `MEMECOIN_CHASE_MAX_PCT`, `m.set_event_chase`.
- Produces: `on_copy_event` returns `"skipped_chase"` when the quote is >CHASE_MAX_PCT above `their_price`; records `wouldbe_price`/`chase_gap_pct` on the event; opens otherwise. `their_price <= 0` disables the guard.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py  (append)
def _buy_event(their_price):
    return CopyEvent("u1", "a", "M", "T", "buy", their_price, 1e6, 1.0, True, "ev1", "t", "t", 0.3)


def test_chase_guard_skips_when_ran_up():
    c = _conn()
    m.insert_event(c, _buy_event(0.005).__dict__)
    # quote 0.006 is +20% over their 0.005 fill -> skip (default 15%)
    r = eng.on_copy_event(c, _buy_event(0.005), 100.0, "t", fill_fn=lambda *a, **k: (0.006, "dexscreener"))
    assert r == "skipped_chase"
    assert m.get_open_position(c, "u1", "M") is None
    row = c.execute("SELECT wouldbe_price, chase_gap_pct FROM memecoin_events WHERE fomo_event_id='ev1'").fetchone()
    assert abs(row[0] - 0.006) < 1e-9 and abs(row[1] - 20.0) < 0.5


def test_chase_guard_allows_within_threshold():
    c = _conn()
    m.insert_event(c, _buy_event(0.005).__dict__)
    r = eng.on_copy_event(c, _buy_event(0.005), 100.0, "t", fill_fn=lambda *a, **k: (0.0054, "dexscreener"))
    assert r == "opened"                      # +8% < 15%


def test_chase_guard_disabled_when_their_price_zero():
    c = _conn()
    ev = CopyEvent("u1", "a", "M", "T", "buy", 0.0, 1e6, 1.0, True, "ev2", "t", "t", 0.3)
    m.insert_event(c, ev.__dict__)
    assert eng.on_copy_event(c, ev, 100.0, "t", fill_fn=lambda *a, **k: (0.006, "x")) == "opened"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -k chase_guard -q`
Expected: FAIL — no chase guard; `skipped_chase` never returned.

- [ ] **Step 3: Write minimal implementation**

In `on_copy_event` buy branch, after computing `price, source = fill_fn(...)` and the `price <= 0` guard, before opening:
```python
        from sniper.settings.memecoin import MEMECOIN_CHASE_MAX_PCT
        tp = ev.their_price
        if tp and tp > 0:
            gap_pct = (price / tp - 1.0) * 100.0
            if gap_pct > MEMECOIN_CHASE_MAX_PCT:
                m.set_event_chase(conn, ev.fomo_event_id, price, gap_pct)
                return "skipped_chase"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -k chase_guard -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py tests/test_memecoin_copy_logic.py
git commit -m "feat(memecoin-copy): entry chase guard + skipped_chase counterfactual"
```

---

### Task 4: Batched DexScreener price map

**Files:**
- Modify: `sniper/execution/solana_quote.py` (add `dexscreener_stats_batch`)
- Modify: `sniper/services/memecoin_marks.py` (`mark_positions` accepts optional `price_map`)
- Test: `tests/test_memecoin_copy_logic.py` (append)

**Interfaces:**
- Produces:
  - `dexscreener_stats_batch(mints: list[str], http=None) -> dict[str, dict]` — `{mint: {"price","market_cap","liquidity","volume_24h"}}`, one request per ≤30 mints via `/latest/dex/tokens/{joined}`, keyed by the pair `baseToken.address`.
  - `mark_positions(conn, stats_fn=dexscreener_stats, price_map=None)` — if `price_map` given, use it instead of per-mint `stats_fn`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py  (append)
from sniper.execution import solana_quote as q


def test_dexscreener_batch_chunks_and_maps():
    calls = []
    def http(method, url, headers=None, body=None):
        calls.append(url)
        # echo a pair per mint present in the URL tail
        tail = url.rsplit("/", 1)[1]
        pairs = [{"baseToken": {"address": mt}, "priceUsd": "0.01",
                  "marketCap": "1000000", "liquidity": {"usd": "50000"},
                  "volume": {"h24": "12345"}} for mt in tail.split(",")]
        return 200, {"pairs": pairs}, None
    mints = [f"m{i}" for i in range(35)]           # >30 -> 2 requests
    out = q.dexscreener_stats_batch(mints, http=http)
    assert len(calls) == 2
    assert out["m0"]["price"] == 0.01 and out["m34"]["liquidity"] == 50000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py::test_dexscreener_batch_chunks_and_maps -q`
Expected: FAIL — `dexscreener_stats_batch` undefined.

- [ ] **Step 3: Write minimal implementation**

In `sniper/execution/solana_quote.py` add:
```python
def dexscreener_stats_batch(mints, http=None):
    http = http or _http
    out = {}
    uniq = list(dict.fromkeys(mints))
    for i in range(0, len(uniq), 30):
        chunk = uniq[i:i + 30]
        try:
            _s, j, _d = http("GET", DEXSCREENER + ",".join(chunk))
            for p in (j.get("pairs") or []):
                addr = (p.get("baseToken") or {}).get("address")
                if not addr or addr in out:
                    continue

                def g(*path, _p=p):
                    cur = _p
                    for k in path:
                        cur = cur.get(k) if isinstance(cur, dict) else None
                    return cur
                out[addr] = {
                    "price": float(p.get("priceUsd") or 0) or None,
                    "market_cap": float(p.get("marketCap") or 0) or None,
                    "liquidity": float(g("liquidity", "usd") or 0) or None,
                    "volume_24h": float(g("volume", "h24") or 0) or None,
                }
        except Exception:
            continue
    return out
```

In `sniper/services/memecoin_marks.py`, change `mark_positions` to accept `price_map=None` and prefer it:
```python
def mark_positions(conn, stats_fn=dexscreener_stats, price_map=None):
    out = []
    for pos in m.list_open_positions(conn):
        if not pos.get("entry_price") or pos["entry_price"] <= 0:
            continue
        st = (price_map or {}).get(pos["mint"]) if price_map is not None else stats_fn(pos["mint"])
        if not st or not st.get("price"):
            continue
        qty = m.open_qty(conn, pos["id"])
        out.append({
            "position_id": pos["id"], "mint": pos["mint"],
            "current_price": st["price"],
            "unrealized_pnl": (st["price"] - pos["entry_price"]) * qty,
            "liquidity": st.get("liquidity"), "volume_24h": st.get("volume_24h"),
        })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/solana_quote.py sniper/services/memecoin_marks.py tests/test_memecoin_copy_logic.py
git commit -m "feat(memecoin-copy): batched DexScreener price map + price_map marks"
```

---

### Task 5: Bracket evaluator (SL/TP)

**Files:**
- Create: `sniper/services/memecoin_brackets.py`
- Test: `tests/test_memecoin_copy_logic.py` (append)

**Interfaces:**
- Consumes: `db_memecoin` (`list_open_positions`, `open_qty`, `insert_fill`, `close_position`), `_realized_from_exits` (import from copy_trader), settings SL/TP.
- Produces: `evaluate_brackets(conn, price_map, now_iso, sl_pct=None, tp_pct=None) -> list[dict]` — for each open position with a price in `price_map`, if `pnl_pct <= -sl_pct` or `>= tp_pct`, sell **`open_qty`** at that price (`bracket_exit` fill), close with `_realized_from_exits` + `exit_reason`. Returns closed summaries `{position_id, exit_reason, price}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py  (append)
from sniper.services import memecoin_brackets as br


def _open(c, entry=0.005, qty=20000.0, uid="u1", mint="M"):
    pid = m.open_position(c, source_user_id=uid, source_handle="a", mint=mint, ticker="T",
                          entry_price=entry, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1.0, opened_at="t")
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=qty, notional=100.0,
                  fill_source="jupiter", fraction=1.0, created_at="t")
    return pid


def test_stop_loss_closes():
    c = _conn(); pid = _open(c)
    # -60%: 0.005 -> 0.002
    out = br.evaluate_brackets(c, {"M": {"price": 0.002}}, "t2", sl_pct=60, tp_pct=500)
    assert out and out[0]["exit_reason"] == "stop_loss"
    assert m.get_position(c, pid)["is_open"] == 0


def test_take_profit_closes():
    c = _conn(); pid = _open(c)
    out = br.evaluate_brackets(c, {"M": {"price": 0.030}}, "t2", sl_pct=60, tp_pct=500)  # +500%
    assert out[0]["exit_reason"] == "take_profit"


def test_no_trigger_stays_open():
    c = _conn(); pid = _open(c)
    assert br.evaluate_brackets(c, {"M": {"price": 0.004}}, "t2", sl_pct=60, tp_pct=500) == []
    assert m.get_position(c, pid)["is_open"] == 1


def test_bracket_uses_open_qty_after_partial():
    c = _conn(); pid = _open(c, entry=0.005, qty=20000.0)
    # sell half at 0.006 -> realized 0.5*(0.006-0.005)*20000/... use partial_exit helper
    eng.partial_exit(c, pid, 0.5, "t1", price_fn=lambda mint, http=None: {"price": 0.006})
    assert abs(m.open_qty(c, pid) - 10000.0) < 1e-6      # half remains
    out = br.evaluate_brackets(c, {"M": {"price": 0.002}}, "t3", sl_pct=60, tp_pct=500)
    assert out[0]["exit_reason"] == "stop_loss"
    # realized = partial slice + bracket slice on REMAINING 10000 (not entry 20000)
    realized = m.get_position(c, pid)["realized_pnl"]
    # partial: (0.006-0.005)*10000 = +10 ; bracket: (0.002-0.005)*10000 = -30 ; total = -20
    assert abs(realized - (-20.0)) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -k "stop_loss or take_profit or no_trigger or open_qty" -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/memecoin_brackets.py
"""SL/TP bracket evaluator for open memecoin positions (paper).

Cadence-checked (~15s from the runner). Fills the REMAINING open_qty at the
current price (gap-through honest), realizes via _realized_from_exits, tags exit_reason.
"""
from __future__ import annotations

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import _realized_from_exits


def evaluate_brackets(conn, price_map, now_iso, sl_pct=None, tp_pct=None):
    if sl_pct is None or tp_pct is None:
        from sniper.settings.memecoin import MEMECOIN_STOP_LOSS_PCT, MEMECOIN_TAKE_PROFIT_PCT
        sl_pct = MEMECOIN_STOP_LOSS_PCT if sl_pct is None else sl_pct
        tp_pct = MEMECOIN_TAKE_PROFIT_PCT if tp_pct is None else tp_pct
    closed = []
    for pos in m.list_open_positions(conn):
        entry = pos.get("entry_price")
        if not entry or entry <= 0:
            continue
        st = (price_map or {}).get(pos["mint"])
        price = st and st.get("price")
        if not price or price <= 0:
            continue
        pnl_pct = (price / entry - 1.0) * 100.0
        reason = ("stop_loss" if pnl_pct <= -sl_pct
                  else "take_profit" if pnl_pct >= tp_pct else None)
        if not reason:
            continue
        qty = m.open_qty(conn, pos["id"])         # remaining after any partials
        if qty <= 0:
            continue
        m.insert_fill(conn, position_id=pos["id"], kind="bracket_exit", price=price,
                      qty=qty, notional=price * qty, fill_source="dexscreener",
                      fraction=1.0, created_at=now_iso)
        m.close_position(conn, pos["id"], now_iso,
                         _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
        closed.append({"position_id": pos["id"], "exit_reason": reason, "price": price})
    return closed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_brackets.py tests/test_memecoin_copy_logic.py
git commit -m "feat(memecoin-copy): SL/TP bracket evaluator (open_qty, gap-through honest)"
```

---

### Task 6: Runner integration — ~15s bracket pass

**Files:**
- Modify: `sniper/services/memecoin_runner.py` (add `bracket_pass`)
- Modify: `scripts/memecoin_runner_loop.py` (call `bracket_pass` every ~15s)
- Test: `tests/test_memecoin_copy_logic.py` (append)

**Interfaces:**
- Consumes: `dexscreener_stats_batch`, `evaluate_brackets`, `db_memecoin.list_open_positions`.
- Produces: `bracket_pass(conn, price_fn=dexscreener_stats_batch, now_fn=None) -> list[dict]` — gathers open mints, one batched fetch, runs `evaluate_brackets`. The loop calls it on a ~15s timer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_logic.py  (append)
from sniper.services import memecoin_runner as run


def test_bracket_pass_closes_via_batched_prices():
    c = _conn(); pid = _open(c, entry=0.005)
    def price_fn(mints, http=None):
        return {mt: {"price": 0.002} for mt in mints}     # all -60%
    out = run.bracket_pass(c, price_fn=price_fn, now_fn=lambda: "t2")
    assert out and out[0]["exit_reason"] == "stop_loss"
    assert m.get_position(c, pid)["is_open"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_logic.py::test_bracket_pass_closes_via_batched_prices -q`
Expected: FAIL — `bracket_pass` undefined.

- [ ] **Step 3: Write minimal implementation**

In `sniper/services/memecoin_runner.py` add:
```python
def bracket_pass(conn, price_fn=None, now_fn=None):
    from sniper.execution.solana_quote import dexscreener_stats_batch
    from sniper.services.memecoin_brackets import evaluate_brackets
    price_fn = price_fn or dexscreener_stats_batch
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    mints = list({p["mint"] for p in m.list_open_positions(conn)})
    if not mints:
        return []
    price_map = price_fn(mints)
    return evaluate_brackets(conn, price_map, now_fn())
```

In `scripts/memecoin_runner_loop.py`, in `main()` before the loop add `last_bracket = 0.0`, import `bracket_pass`, and inside the loop after `run_once(...)`:
```python
            if cycle - last_bracket > 15:
                from sniper.services.memecoin_runner import bracket_pass
                bracket_pass(conn)
                last_bracket = cycle
```
(Guard it inside the existing `try/except`; a bracket-pass error must not kill the loop.)

- [ ] **Step 4: Run test to verify it passes + focused suite**

Run:
```bash
python3 -m pytest tests/test_memecoin_copy_logic.py -q
python3 -m pytest tests/test_memecoin_*.py -q
python3 -c "import sniper.web.server"
```
Expected: all PASS; web import clean.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_runner.py scripts/memecoin_runner_loop.py tests/test_memecoin_copy_logic.py
git commit -m "feat(memecoin-copy): runner ~15s bracket pass (batched price map)"
```

---

## Self-Review

**Spec coverage:**
- §1 Entry chase guard + counterfactual → Task 3 (+ Task 1 columns/`set_event_chase`). ✓
- §2 Exit brackets (open_qty, _realized_from_exits, exit_reason, SL soft trigger) → Tasks 2, 5. ✓
- §3 ~15s batched cadence → Tasks 4, 6. ✓
- §4 Config knobs → Task 1. ✓
- §5 Schema migrations (`exit_reason`, `wouldbe_price`, `chase_gap_pct`), close_position param → Task 1. ✓
- §6 Integration (reconcile, don't rewrite; `bracket_exit` into realized + EXIT_KINDS; mark_positions price_map) → Tasks 1, 2, 4. ✓
- §7 Tests (chase, re-entry already covered pre-existing, SL/TP, no-trigger, open_qty-after-partial, batched chunking, mirror/manual reasons) → Tasks 2–6. ✓

**Placeholder scan:** none — every step has concrete code + exact commands.

**Type consistency:** `close_position(conn, position_id, closed_at, realized_pnl, exit_reason=None)` used identically in Tasks 1/2/5; `EXIT_KINDS` incl `bracket_exit` (Task 1) matched by `_realized_from_exits` kind list (Task 2) and `open_qty` (existing); `evaluate_brackets(conn, price_map, now_iso, sl_pct, tp_pct)` consistent Tasks 5/6; `dexscreener_stats_batch(mints, http)` consistent Tasks 4/6. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-05-memecoin-copy-logic.md`.
