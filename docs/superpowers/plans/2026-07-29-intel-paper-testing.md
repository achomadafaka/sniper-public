# Intel Paper Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every intel candidate that resolves to a real Polymarket market or Hyperliquid instrument opens a paper position, marked on a schedule and rendered with a live-enough price, its thesis, and two display-only signals.

**Architecture:** One table pair (`intel_paper_positions` + `intel_paper_marks`) holds both venues. Opening is one Gamma call that returns metadata *and* both CLOB token ids. Marking is a `*/5` cron making ~3 HTTP calls total regardless of book size; the UI reads only SQLite. Nothing gates entry — the reviewer verdict and the holder panel are recorded as tags so they can be measured later.

**Tech Stack:** Python 3.13, SQLite (WAL), Polymarket Gamma + CLOB REST, Hyperliquid `/info` REST, Flask blueprints, vanilla JS, cron, pytest.

**Spec:** `docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md`

## Global Constraints

- **Paper only.** No order placement, no keys, no funded route. Hyperliquid is display + paper ledger only.
- **Nothing gates entry.** `reviewer_verdict` and `holder_snapshot` are recorded, never enforced. Gating a signal before measuring it is the failure mode in `finding-crypto5m-floor-endogeneity-2026-07-07`.
- **No external HTTP in a Flask request path.** The UI reads SQLite. The only exception is the explicit manual-refresh endpoint in Task 7.
- **Marks must be O(1) HTTP calls, not O(positions).** `clob POST /prices` takes all token ids at once; HL `allMids` returns every mid per call.
- **Identity comes from `condition_id`.** An idea that cannot be pinned to exactly one `condition_id` is `no_instrument` — never a guess. There are no non-binary markets; an event holds N binary markets.
- **ADR-003 (outcome-native prices):** a `buy_no` position stores the NO token price. Never mix YES and NO in one PnL calculation.
- **Timestamps:** `datetime.now(timezone.utc)` only. `utcnow()` is banned and CI-guarded by `tests/test_clock_naive_datetimes.py`.
- **Schema discipline:** `python3 scripts/brain_index.py schema <table>` before naming any column; `python3 scripts/brain_index.py generate` after schema changes; `python3 scripts/validate_schema_refs.py` before every commit.
- Verified live 2026-07-29: `trade_ideas` has 32 columns including `token_id`, `event_ts`, `price_at_event`, `price_at_decision`, `reject_reason`, `adversarial_approved`, `adversarial_score`.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `sniper/persistence/intel_paper.py` | Schema + all reads/writes for both new tables. Sole SQL owner. | Create |
| `sniper/markets/pm_resolver.py` | Add `fetch_market(condition_id)` — one-shot Gamma lookup returning metadata + both token ids | Modify |
| `sniper/markets/hl_universe.py` | Resolve an HL symbol against live `meta` + `perpDexs`; fetch `allMids` | Create |
| `sniper/services/intel_paper_open.py` | Route a candidate to a venue and open a position | Create |
| `sniper/services/intel_paper_mark.py` | One mark cycle across both venues | Create |
| `sniper/services/intel_holder_panel.py` | Display-only holder lookup + H5 filter | Create |
| `scripts/intel_paper_mark_job.py` | Cron entrypoint wrapping the mark cycle | Create |
| `sniper/routes/api_intel_paper.py` | `/api/intel-paper/*` read endpoints + manual refresh | Create |
| `sniper/web/server.py` | Register the new blueprint | Modify |
| `static/app.js` | Render PM rows in the Paper Trades sub-tab; new Paper fold in Geopolitics | Modify |
| `templates/index.html` | Paper fold markup inside `#intel-geo-view` | Modify |
| `tests/test_intel_paper.py` | All tests for the above | Create |

`intel_paper.py` is a new persistence module rather than an addition to `db.py` because `db.py` is already ~9,200 lines across 9 modules; a self-contained surface is easier to hold in context and to test.

---

## Task 1: Schema and persistence layer

**Files:**
- Create: `sniper/persistence/intel_paper.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `ensure_schema(conn) -> None`
  - `open_position(conn, **fields) -> int | None` — returns `position_id`, or `None` if a position for that `idea_id` already exists
  - `record_mark(conn, position_id: int, ts: str, price: float, pnl_pct: float) -> None`
  - `open_positions(conn, venue: str | None = None) -> list[dict]` — each dict carries the position plus `last_mark_price`, `last_mark_ts`, `last_pnl_pct` (all `None` when unmarked)
  - `close_position(conn, position_id: int, price: float, ts: str, reason: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.persistence import intel_paper


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    intel_paper.ensure_schema(c)
    return c


def _open(conn, **over):
    fields = dict(
        idea_id=1, venue="pm",
        instrument="0xabc", token_id="111",
        event_title="US x Iran Ceasefire", outcome_label="July 31",
        direction="buy_no", entry_price=0.635,
        entry_ts="2026-07-29T11:08:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None,
    )
    fields.update(over)
    return intel_paper.open_position(conn, **fields)


def test_open_and_read_back(conn):
    pid = _open(conn)
    assert pid is not None
    rows = intel_paper.open_positions(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["venue"] == "pm"
    assert r["outcome_label"] == "July 31"
    assert r["status"] == "open"
    assert r["last_mark_price"] is None, "an unmarked position must not fake a price"


def test_one_position_per_idea(conn):
    assert _open(conn) is not None
    assert _open(conn) is None, "a second open for the same idea_id must be refused"


def test_marks_attach_and_latest_wins(conn):
    pid = _open(conn)
    intel_paper.record_mark(conn, pid, "2026-07-29T12:00:00+00:00", 0.60, -5.51)
    intel_paper.record_mark(conn, pid, "2026-07-29T13:00:00+00:00", 0.58, -8.66)
    r = intel_paper.open_positions(conn)[0]
    assert r["last_mark_price"] == 0.58
    assert r["last_mark_ts"] == "2026-07-29T13:00:00+00:00"
    assert r["last_pnl_pct"] == -8.66


def test_venue_filter_and_close(conn):
    pm = _open(conn)
    _open(conn, idea_id=2, venue="hl", instrument="flx:OIL", token_id=None,
          direction="long", entry_price=62.4, event_title=None, outcome_label=None)
    assert len(intel_paper.open_positions(conn, venue="pm")) == 1
    assert len(intel_paper.open_positions(conn, venue="hl")) == 1

    intel_paper.close_position(conn, pm, 0.71, "2026-07-31T00:00:00+00:00", "resolved")
    assert len(intel_paper.open_positions(conn, venue="pm")) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.persistence.intel_paper'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/persistence/intel_paper.py
"""Paper book for intel-driven candidates — both venues, one table pair.

Deliberately NOT strategy_positions (condition_id NOT NULL cannot represent a
perp without fabricating a market identity) and NOT hl_paper_positions (welded to
the alt-short study's arm/obs schema). See the design doc.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS intel_paper_positions (
    position_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id          INTEGER NOT NULL,
    venue            TEXT    NOT NULL,
    instrument       TEXT    NOT NULL,
    token_id         TEXT,
    event_title      TEXT,
    outcome_label    TEXT,
    direction        TEXT    NOT NULL,
    entry_price      REAL    NOT NULL,
    entry_ts         TEXT    NOT NULL,
    size_usdc        REAL    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open',
    close_price      REAL,
    close_ts         TEXT,
    close_reason     TEXT,
    reviewer_verdict TEXT,
    reviewer_score   INTEGER,
    holder_snapshot  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_paper_idea ON intel_paper_positions(idea_id);
CREATE INDEX IF NOT EXISTS idx_intel_paper_open ON intel_paper_positions(status, venue);

CREATE TABLE IF NOT EXISTS intel_paper_marks (
    position_id INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    price       REAL    NOT NULL,
    pnl_pct     REAL    NOT NULL,
    PRIMARY KEY (position_id, ts)
);
"""

_FIELDS = ("idea_id", "venue", "instrument", "token_id", "event_title", "outcome_label",
           "direction", "entry_price", "entry_ts", "size_usdc", "reviewer_verdict",
           "reviewer_score", "holder_snapshot")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_POSITIONS_DDL)
    conn.commit()


def open_position(conn: sqlite3.Connection, **fields: Any) -> Optional[int]:
    """Insert a position. Returns position_id, or None if this idea already has one."""
    ensure_schema(conn)
    cols = ", ".join(_FIELDS)
    marks = ", ".join("?" for _ in _FIELDS)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO intel_paper_positions ({cols}) VALUES ({marks})",
        tuple(fields.get(f) for f in _FIELDS),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def record_mark(conn: sqlite3.Connection, position_id: int, ts: str,
                price: float, pnl_pct: float) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO intel_paper_marks (position_id, ts, price, pnl_pct) "
        "VALUES (?,?,?,?)",
        (position_id, ts, float(price), float(pnl_pct)),
    )
    conn.commit()


def open_positions(conn: sqlite3.Connection, venue: Optional[str] = None) -> list[dict]:
    """Open positions with their most recent mark (None when never marked)."""
    ensure_schema(conn)
    sql = """
        SELECT p.*, m.price AS last_mark_price, m.ts AS last_mark_ts,
               m.pnl_pct AS last_pnl_pct
        FROM intel_paper_positions p
        LEFT JOIN intel_paper_marks m
          ON m.position_id = p.position_id
         AND m.ts = (SELECT MAX(ts) FROM intel_paper_marks WHERE position_id = p.position_id)
        WHERE p.status = 'open'
    """
    params: tuple = ()
    if venue:
        sql += " AND p.venue = ?"
        params = (venue,)
    sql += " ORDER BY p.entry_ts DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def close_position(conn: sqlite3.Connection, position_id: int, price: float,
                   ts: str, reason: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE intel_paper_positions SET status='closed', close_price=?, close_ts=?, "
        "close_reason=? WHERE position_id=?",
        (float(price), ts, reason, position_id),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Regenerate index and validate**

```bash
python3 scripts/brain_index.py generate
python3 scripts/brain_index.py schema intel_paper_positions
python3 scripts/validate_schema_refs.py
```
Expected: both tables listed; validator exits clean.

- [ ] **Step 6: Commit**

```bash
git add sniper/persistence/intel_paper.py tests/test_intel_paper.py index/
git commit -m "feat(intel-paper): position + mark tables for both venues"
```

---

## Task 2: One-shot Polymarket market lookup

**Files:**
- Modify: `sniper/markets/pm_resolver.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: nothing
- Produces: `fetch_market(condition_id: str, timeout: float = 10.0) -> dict | None` returning
  `{"condition_id", "question", "event_title", "outcome_label", "token_ids": {"YES": str, "NO": str},
    "prices": {"YES": float, "NO": float}, "best_bid", "best_ask", "end_date_iso",
    "neg_risk": bool, "closed": bool}` — or `None` when the market cannot be resolved.

This supersedes the CLOB round-trip in `resolve_token` for the open path. Leave
`resolve_token` in place; `intel_gate_read.py` and existing tests use it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
import json
from unittest.mock import MagicMock, patch


def _resp(payload):
    r = MagicMock()
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    return r


_GAMMA_MARKET = [{
    "question": "US x Iran Effective Ceasefire by July 31?",
    "groupItemTitle": "July 31",
    "events": [{"title": "US x Iran Effective Ceasefire (2 week pause)"}],
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.385", "0.615"]',
    "clobTokenIds": '["95577979648711137561", "34707224416470630443"]',
    "bestBid": 0.38, "bestAsk": 0.39,
    "endDateIso": "2026-07-31", "negRisk": False, "closed": False,
}]


def test_fetch_market_returns_both_tokens_and_labels():
    from sniper.markets import pm_resolver

    with patch("urllib.request.urlopen", return_value=_resp(_GAMMA_MARKET)):
        m = pm_resolver.fetch_market("0xabc")

    assert m["token_ids"]["YES"] == "95577979648711137561"
    assert m["token_ids"]["NO"] == "34707224416470630443"
    assert m["prices"]["YES"] == 0.385
    assert m["prices"]["NO"] == 0.615
    assert m["outcome_label"] == "July 31"
    assert m["event_title"] == "US x Iran Effective Ceasefire (2 week pause)"
    assert m["neg_risk"] is False


def test_fetch_market_returns_none_when_absent():
    from sniper.markets import pm_resolver

    with patch("urllib.request.urlopen", return_value=_resp([])):
        assert pm_resolver.fetch_market("0xnope") is None
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert pm_resolver.fetch_market("0xnope") is None


def test_fetch_market_rejects_a_closed_market():
    from sniper.markets import pm_resolver

    payload = [dict(_GAMMA_MARKET[0], closed=True)]
    with patch("urllib.request.urlopen", return_value=_resp(payload)):
        assert pm_resolver.fetch_market("0xabc") is None, "a closed market is not openable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k fetch_market -v`
Expected: FAIL — `AttributeError: module 'sniper.markets.pm_resolver' has no attribute 'fetch_market'`

- [ ] **Step 3: Write minimal implementation**

Append to `sniper/markets/pm_resolver.py`:

```python
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _as_list(raw):
    """Gamma returns these as JSON-encoded strings."""
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


def fetch_market(condition_id: str, timeout: float = 10.0) -> Optional[dict]:
    """One-shot lookup: metadata AND both CLOB token ids in a single request.

    Gamma returns clobTokenIds alongside the metadata, so opening a position needs
    no separate CLOB call. Returns None when the market cannot be resolved or is
    already closed.
    """
    if not condition_id:
        return None
    url = f"{GAMMA_MARKETS_URL}?" + urllib.parse.urlencode({"condition_ids": condition_id})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — unresolvable is a normal outcome
        logger.info("fetch_market: %s unresolvable: %s", condition_id, exc)
        return None

    if not rows:
        return None
    m = rows[0]
    if m.get("closed"):
        return None

    outcomes = [str(o).upper() for o in _as_list(m.get("outcomes"))]
    tokens = [str(t) for t in _as_list(m.get("clobTokenIds"))]
    prices = _as_list(m.get("outcomePrices"))
    if len(outcomes) != 2 or len(tokens) != 2 or len(prices) != 2:
        logger.info("fetch_market: %s is not a binary market", condition_id)
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    events = m.get("events") or []
    return {
        "condition_id": condition_id,
        "question": m.get("question") or "",
        "event_title": (events[0].get("title") if events else None),
        "outcome_label": m.get("groupItemTitle"),
        "token_ids": dict(zip(outcomes, tokens)),
        "prices": {o: _f(p) for o, p in zip(outcomes, prices)},
        "best_bid": _f(m.get("bestBid")),
        "best_ask": _f(m.get("bestAsk")),
        "end_date_iso": m.get("endDateIso"),
        "neg_risk": bool(m.get("negRisk")),
        "closed": bool(m.get("closed")),
    }
```

Add `import urllib.parse` to the module imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py tests/test_intel_revival.py -q`
Expected: PASS — the existing `resolve_token` tests must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/markets/pm_resolver.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): one-shot Gamma market lookup with both token ids"
```

---

## Task 3: Hyperliquid universe resolution

**Files:**
- Create: `sniper/markets/hl_universe.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `load_universe(timeout: float = 10.0) -> set[str]` — every tradeable symbol, main universe bare (`BTC`) plus HIP-3 namespaced (`flx:OIL`)
  - `resolve_symbol(proposed: str, universe: set[str]) -> str | None` — exact match first, then unique suffix match across dexes; `None` when absent or ambiguous
  - `all_mids(symbols: set[str], timeout: float = 10.0) -> dict[str, float]` — one call for the main universe plus one per distinct dex

Equities and commodities are **not** in the main 232-asset universe; they live on
builder-deployed dexes (`xyz`, `flx`, `km`, `mkts`, `cash`, `vntl`, `para`). The dex list
is permissionless — always fetch, never hardcode.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_resolve_symbol_exact_and_suffix():
    from sniper.markets import hl_universe

    uni = {"BTC", "ETH", "flx:OIL", "km:USOIL", "xyz:TSLA", "cash:TSLA"}
    assert hl_universe.resolve_symbol("BTC", uni) == "BTC"
    assert hl_universe.resolve_symbol("flx:OIL", uni) == "flx:OIL"
    # unique across dexes -> resolves
    assert hl_universe.resolve_symbol("OIL", uni) == "flx:OIL"
    # present on two dexes -> ambiguous, refuse
    assert hl_universe.resolve_symbol("TSLA", uni) is None
    # absent -> refuse (this is the EIS class of bug)
    assert hl_universe.resolve_symbol("EIS", uni) is None
    assert hl_universe.resolve_symbol("OIL-USD", uni) is None
    assert hl_universe.resolve_symbol("", uni) is None


def test_load_universe_merges_main_and_dexes():
    from unittest.mock import patch
    from sniper.markets import hl_universe

    def fake_post(payload, timeout=10.0):
        if payload["type"] == "perpDexs":
            return [None, {"name": "flx"}]
        if payload["type"] == "meta" and payload.get("dex") == "flx":
            return {"universe": [{"name": "flx:OIL"}, {"name": "flx:GOLD"}]}
        if payload["type"] == "meta":
            return {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
        raise AssertionError(payload)

    with patch.object(hl_universe, "_post", fake_post):
        uni = hl_universe.load_universe()
    assert uni == {"BTC", "ETH", "flx:OIL", "flx:GOLD"}


def test_all_mids_groups_calls_by_dex():
    from unittest.mock import patch
    from sniper.markets import hl_universe

    calls = []

    def fake_post(payload, timeout=10.0):
        calls.append(payload.get("dex"))
        if payload.get("dex") == "flx":
            return {"flx:OIL": "62.4"}
        return {"BTC": "91470.2", "ETH": "3050.0"}

    with patch.object(hl_universe, "_post", fake_post):
        mids = hl_universe.all_mids({"BTC", "flx:OIL"})

    assert mids == {"BTC": 91470.2, "flx:OIL": 62.4}
    assert sorted(c or "" for c in calls) == ["", "flx"], "one call per distinct dex, no more"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k "hl_universe or resolve_symbol or all_mids" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.markets.hl_universe'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/markets/hl_universe.py
"""Hyperliquid instrument resolution across the main universe and HIP-3 dexes.

Equities, indices and commodities are NOT in the 232-asset main universe — they are
on builder-deployed perp dexes addressed as `dex:SYMBOL` (flx:OIL, xyz:TSLA,
km:USOIL, vntl:DEFENSE). The dex list is permissionless, so it is fetched per run
and never hardcoded.

Refusing an unknown symbol is the whole point: the prior system emitted EIS,
OIL-USD and OIL-PERP, none of which exist (audit F-01).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(payload: dict, timeout: float = 10.0):
    req = urllib.request.Request(
        INFO_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "sniper/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def load_universe(timeout: float = 10.0) -> set[str]:
    """Every tradeable symbol. Returns an empty set on failure — callers refuse to open."""
    universe: set[str] = set()
    try:
        for a in _post({"type": "meta"}, timeout).get("universe", []):
            universe.add(a["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hl load_universe: main meta failed: %s", exc)
        return set()

    try:
        dexes = [d.get("name") for d in _post({"type": "perpDexs"}, timeout) if d]
    except Exception as exc:  # noqa: BLE001
        logger.warning("hl load_universe: perpDexs failed: %s", exc)
        dexes = []

    for dex in dexes:
        if not dex:
            continue
        try:
            for a in _post({"type": "meta", "dex": dex}, timeout).get("universe", []):
                universe.add(a["name"])
        except Exception as exc:  # noqa: BLE001
            logger.info("hl load_universe: dex %s failed: %s", dex, exc)
    return universe


def resolve_symbol(proposed: str, universe: set[str]) -> Optional[str]:
    """Exact match, else a UNIQUE suffix match across dexes. None when absent or ambiguous."""
    s = str(proposed or "").strip()
    if not s or not universe:
        return None
    if s in universe:
        return s
    hits = [u for u in universe if ":" in u and u.split(":", 1)[1] == s]
    return hits[0] if len(hits) == 1 else None


def all_mids(symbols: set[str], timeout: float = 10.0) -> dict[str, float]:
    """Mids for the given symbols. One call for the main universe plus one per dex."""
    dexes = {s.split(":", 1)[0] for s in symbols if ":" in s}
    if any(":" not in s for s in symbols):
        dexes.add("")

    out: dict[str, float] = {}
    for dex in sorted(dexes):
        payload = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        try:
            raw = _post(payload, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hl all_mids failed for dex=%r: %s", dex, exc)
            continue
        for sym, px in (raw or {}).items():
            if sym in symbols:
                try:
                    out[sym] = float(px)
                except (TypeError, ValueError):
                    pass
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py -v`
Expected: PASS

- [ ] **Step 5: Verify against the live venue (one bounded read-only call)**

```bash
python3 -c "
from sniper.markets.hl_universe import load_universe, resolve_symbol
u = load_universe()
print('universe size:', len(u))
for s in ('OIL','USOIL','TSLA','EIS','OIL-USD','BTC'):
    print(f'  {s:9s} -> {resolve_symbol(s, u)}')
"
```
Expected: several hundred symbols; `EIS` and `OIL-USD` → `None`; `BTC` → `BTC`; `USOIL` resolves to a `km:`/`mkts:` symbol or `None` if it appears on both.

- [ ] **Step 6: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/markets/hl_universe.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): resolve HL symbols across main universe and HIP-3 dexes"
```

---

## Task 4: Routing and opening

**Files:**
- Create: `sniper/services/intel_paper_open.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `intel_paper.open_position`, `pm_resolver.fetch_market`, `hl_universe.load_universe`/`resolve_symbol`/`all_mids`
- Produces: `open_for_idea(conn, idea: dict, hl_universe_set: set[str] | None = None) -> dict` returning
  `{"disposition": "pm"|"hl"|"no_instrument", "position_id": int|None, "reason": str|None}`

Routing: a `condition_id` that `fetch_market` resolves → `pm`. Otherwise an
`hl_symbol` on the idea that `resolve_symbol` accepts → `hl`. Otherwise
`no_instrument`. Entry price is outcome-native per ADR-003.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_open_for_idea_routes_pm_with_outcome_native_price(conn):
    from unittest.mock import patch
    from sniper.services import intel_paper_open

    idea = {"id": 7, "condition_id": "0xabc", "direction": "buy_no",
            "adversarial_approved": 0, "adversarial_score": 87}
    market = {
        "condition_id": "0xabc", "question": "Q", "event_title": "E",
        "outcome_label": "July 31",
        "token_ids": {"YES": "111", "NO": "222"},
        "prices": {"YES": 0.385, "NO": 0.615},
        "best_bid": 0.38, "best_ask": 0.39,
        "end_date_iso": "2026-07-31", "neg_risk": False, "closed": False,
    }
    with patch.object(intel_paper_open, "fetch_market", return_value=market):
        out = intel_paper_open.open_for_idea(conn, idea)

    assert out["disposition"] == "pm"
    row = intel_paper.open_positions(conn)[0]
    assert row["token_id"] == "222", "buy_no must store the NO token (ADR-003)"
    assert row["entry_price"] == 0.615, "buy_no must store the NO price, not the YES price"
    assert row["outcome_label"] == "July 31"
    assert row["reviewer_verdict"] == "rejected", "a rejected idea still opens, tagged"
    assert row["reviewer_score"] == 87


def test_open_for_idea_routes_hl(conn):
    from unittest.mock import patch
    from sniper.services import intel_paper_open

    idea = {"id": 8, "condition_id": None, "hl_symbol": "OIL",
            "direction": "long", "adversarial_approved": None}
    with patch.object(intel_paper_open, "all_mids", return_value={"flx:OIL": 62.4}):
        out = intel_paper_open.open_for_idea(conn, idea, hl_universe_set={"flx:OIL"})

    assert out["disposition"] == "hl"
    row = intel_paper.open_positions(conn, venue="hl")[0]
    assert row["instrument"] == "flx:OIL"
    assert row["entry_price"] == 62.4
    assert row["reviewer_verdict"] == "pending"


def test_open_for_idea_refuses_unknown_instrument(conn):
    from unittest.mock import patch
    from sniper.services import intel_paper_open

    idea = {"id": 9, "condition_id": None, "hl_symbol": "EIS", "direction": "short"}
    with patch.object(intel_paper_open, "fetch_market", return_value=None):
        out = intel_paper_open.open_for_idea(conn, idea, hl_universe_set={"flx:OIL"})

    assert out["disposition"] == "no_instrument"
    assert out["position_id"] is None
    assert intel_paper.open_positions(conn) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k open_for_idea -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.services.intel_paper_open'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/intel_paper_open.py
"""Route an intel candidate to a venue and open a paper position.

Nothing here gates: a reviewer-rejected idea still opens, tagged. The verdict is
recorded so it can be measured, never enforced.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sniper.markets.hl_universe import all_mids, resolve_symbol
from sniper.markets.pm_resolver import fetch_market
from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

DEFAULT_SIZE_USDC = 10.0


def _verdict(idea: dict) -> tuple[str, Optional[int]]:
    approved = idea.get("adversarial_approved")
    if approved is None:
        label = "pending"
    elif int(approved):
        label = "approved"
    else:
        label = "rejected"
    return label, idea.get("adversarial_score")


def open_for_idea(conn, idea: dict[str, Any],
                  hl_universe_set: Optional[set[str]] = None) -> dict:
    """Open a paper position for one candidate. Never raises on an unopenable idea."""
    now = datetime.now(timezone.utc).isoformat()
    verdict, score = _verdict(idea)
    common = dict(idea_id=idea["id"], reviewer_verdict=verdict,
                  reviewer_score=score, holder_snapshot=None,
                  entry_ts=now, size_usdc=DEFAULT_SIZE_USDC)

    cid = idea.get("condition_id")
    if cid:
        market = fetch_market(cid)
        if market:
            side = "NO" if str(idea.get("direction")) == "buy_no" else "YES"
            token = market["token_ids"].get(side)
            price = market["prices"].get(side)
            if token and price is not None:
                pid = intel_paper.open_position(
                    conn, venue="pm", instrument=cid, token_id=token,
                    event_title=market.get("event_title"),
                    outcome_label=market.get("outcome_label"),
                    direction=idea["direction"], entry_price=price, **common)
                return {"disposition": "pm", "position_id": pid, "reason": None}

    sym = idea.get("hl_symbol")
    if sym and hl_universe_set:
        resolved = resolve_symbol(sym, hl_universe_set)
        if resolved:
            price = all_mids({resolved}).get(resolved)
            if price is not None:
                pid = intel_paper.open_position(
                    conn, venue="hl", instrument=resolved, token_id=None,
                    event_title=None, outcome_label=None,
                    direction=idea.get("direction") or "long",
                    entry_price=price, **common)
                return {"disposition": "hl", "position_id": pid, "reason": None}

    logger.info("idea %s: no tradeable instrument", idea.get("id"))
    return {"disposition": "no_instrument", "position_id": None,
            "reason": "no tradeable instrument on either venue"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/services/intel_paper_open.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): route candidates to a venue and open positions"
```

---

## Task 5: Mark cycle

**Files:**
- Create: `sniper/services/intel_paper_mark.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `intel_paper.open_positions`, `intel_paper.record_mark`, `hl_universe.all_mids`
- Produces:
  - `pnl_pct(direction: str, entry: float, current: float) -> float`
  - `mark_cycle(conn, timeout: float = 10.0) -> dict` returning `{"pm": int, "hl": int, "errors": int}`

PM marks come from `POST clob/prices` for every open token id in **one** request; a long
marks at the bid (conservative). HL marks come from `all_mids`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_pnl_pct_signs():
    from sniper.services.intel_paper_mark import pnl_pct

    # PM: buying NO at 0.60, NO now 0.66 -> +10%
    assert pnl_pct("buy_no", 0.60, 0.66) == pytest.approx(10.0)
    assert pnl_pct("buy_yes", 0.50, 0.40) == pytest.approx(-20.0)
    # HL
    assert pnl_pct("long", 100.0, 110.0) == pytest.approx(10.0)
    assert pnl_pct("short", 100.0, 90.0) == pytest.approx(10.0)
    assert pnl_pct("short", 100.0, 110.0) == pytest.approx(-10.0)
    # never divide by zero
    assert pnl_pct("long", 0.0, 5.0) == 0.0


def test_mark_cycle_uses_one_batch_call_per_venue(conn):
    from unittest.mock import patch
    from sniper.services import intel_paper_mark

    for i, (venue, instr, tok, direction, px) in enumerate([
        ("pm", "0xa", "111", "buy_no", 0.60),
        ("pm", "0xb", "222", "buy_yes", 0.30),
        ("hl", "flx:OIL", None, "long", 60.0),
    ], start=1):
        intel_paper.open_position(
            conn, idea_id=i, venue=venue, instrument=instr, token_id=tok,
            event_title=None, outcome_label=None, direction=direction,
            entry_price=px, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
            reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    calls = []

    def fake_clob(token_ids, timeout=10.0):
        calls.append(sorted(token_ids))
        return {"111": 0.66, "222": 0.27}

    with patch.object(intel_paper_mark, "_clob_bids", fake_clob), \
         patch.object(intel_paper_mark, "all_mids", return_value={"flx:OIL": 66.0}):
        out = intel_paper_mark.mark_cycle(conn)

    assert out == {"pm": 2, "hl": 1, "errors": 0}
    assert calls == [["111", "222"]], "all PM tokens must go in ONE request"

    rows = {r["instrument"]: r for r in intel_paper.open_positions(conn)}
    assert rows["0xa"]["last_pnl_pct"] == pytest.approx(10.0)
    assert rows["flx:OIL"]["last_pnl_pct"] == pytest.approx(10.0)


def test_mark_cycle_skips_missing_prices_without_failing(conn):
    from unittest.mock import patch
    from sniper.services import intel_paper_mark

    intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="111",
        event_title=None, outcome_label=None, direction="buy_no",
        entry_price=0.6, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    with patch.object(intel_paper_mark, "_clob_bids", return_value={}), \
         patch.object(intel_paper_mark, "all_mids", return_value={}):
        out = intel_paper_mark.mark_cycle(conn)

    assert out["pm"] == 0
    assert intel_paper.open_positions(conn)[0]["last_mark_price"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k "pnl_pct or mark_cycle" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.services.intel_paper_mark'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/intel_paper_mark.py
"""One mark cycle across both venues.

Cost is O(1) HTTP calls, not O(positions): every open PM token id goes in a single
clob POST /prices, and HL allMids returns every mid per call. A full cycle is ~3
requests regardless of book size — this is what makes a */5 cron affordable next to
the existing per-minute watchdogs.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from sniper.markets.hl_universe import all_mids
from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

CLOB_PRICES_URL = "https://clob.polymarket.com/prices"


def pnl_pct(direction: str, entry: float, current: float) -> float:
    """Signed PnL %. PM buy_* and HL long move with price; short moves against it."""
    entry = float(entry or 0.0)
    if entry <= 0:
        return 0.0
    raw = (float(current) - entry) / entry * 100.0
    return -raw if str(direction).lower() == "short" else raw


def _clob_bids(token_ids: list[str], timeout: float = 10.0) -> dict[str, float]:
    """Best bid per token in ONE request. Marking a long at the bid is conservative."""
    if not token_ids:
        return {}
    body = json.dumps([{"token_id": t, "side": "BUY"} for t in token_ids]).encode()
    req = urllib.request.Request(
        CLOB_PRICES_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "sniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        logger.warning("clob prices failed: %s", exc)
        return {}
    out: dict[str, float] = {}
    for tok, sides in (raw or {}).items():
        try:
            out[tok] = float(sides["BUY"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def mark_cycle(conn, timeout: float = 10.0) -> dict:
    """Mark every open position. Returns counts written per venue."""
    now = datetime.now(timezone.utc).isoformat()
    positions = intel_paper.open_positions(conn)
    written = {"pm": 0, "hl": 0, "errors": 0}

    pm = [p for p in positions if p["venue"] == "pm" and p["token_id"]]
    prices = _clob_bids([p["token_id"] for p in pm], timeout) if pm else {}
    for p in pm:
        px = prices.get(p["token_id"])
        if px is None:
            written["errors"] += 1
            continue
        intel_paper.record_mark(conn, p["position_id"], now, px,
                                pnl_pct(p["direction"], p["entry_price"], px))
        written["pm"] += 1

    hl = [p for p in positions if p["venue"] == "hl"]
    mids = all_mids({p["instrument"] for p in hl}, timeout) if hl else {}
    for p in hl:
        px = mids.get(p["instrument"])
        if px is None:
            written["errors"] += 1
            continue
        intel_paper.record_mark(conn, p["position_id"], now, px,
                                pnl_pct(p["direction"], p["entry_price"], px))
        written["hl"] += 1

    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/services/intel_paper_mark.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): O(1)-call mark cycle across both venues"
```

---

## Task 6: Cron entrypoint

**Files:**
- Create: `scripts/intel_paper_mark_job.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `intel_paper_mark.mark_cycle`
- Produces: CLI `python3 scripts/intel_paper_mark_job.py` — exit 0 always (a cron job must not spam mail on a transient API failure)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_mark_job_runs_as_a_bare_script():
    """Cron invokes this as `python3 scripts/...`, which does not put the repo on
    sys.path — the same bug that silently disabled intel_gate_read (audit F-11)."""
    import os
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/intel_paper_mark_job.py", "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )
    assert "Traceback" not in proc.stderr, proc.stderr[-400:]
    assert proc.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k mark_job -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Mark every open intel paper position. Cron every 5 minutes.

The mark series IS the measurement, so it must accrue whether or not anyone is
looking at the UI — which is why this is a cron and not a refresh-on-view.
Cost is ~3 HTTP calls per run regardless of book size.

Plan: docs/superpowers/plans/2026-07-29-intel-paper-testing.md (Task 6)
Cron: */5 * * * * cd <repo> && python3 scripts/intel_paper_mark_job.py >> logs/intel_paper_mark.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.persistence import intel_paper  # noqa: E402
from sniper.services.intel_paper_mark import mark_cycle  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("intel-paper-mark")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mark open intel paper positions")
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify wiring and open-position count without any network call")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    intel_paper.ensure_schema(conn)

    if args.dry_run:
        logger.info("dry-run: %d open position(s)", len(intel_paper.open_positions(conn)))
        return 0

    try:
        logger.info("marked %s", mark_cycle(conn))
    except Exception:
        # Never fail a cron run on a transient API problem.
        logger.exception("mark cycle failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
chmod +x scripts/intel_paper_mark_job.py
python3 -m pytest tests/test_intel_paper.py -v
```
Expected: PASS

- [ ] **Step 5: Commit (do not install cron yet — Task 9 gates that)**

```bash
python3 scripts/validate_schema_refs.py
git add scripts/intel_paper_mark_job.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): cron entrypoint for the mark cycle"
```

---

## Task 7: Read API

**Files:**
- Create: `sniper/routes/api_intel_paper.py`
- Modify: `sniper/web/server.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `intel_paper.open_positions`
- Produces:
  - `GET /api/intel-paper/positions?venue=pm|hl` → `{items, count, venue, mark_age_s, stale, updated_at}`
  - `POST /api/intel-paper/refresh` → runs one `mark_cycle` on demand
  - `create_api_intel_paper_blueprint(ctx) -> Blueprint`

`stale` is `true` when the newest mark is older than 900 s. GET touches SQLite only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_positions_endpoint_reports_mark_age_and_staleness(conn):
    from datetime import datetime, timedelta, timezone

    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    pid = intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="111",
        event_title="E", outcome_label="July 31", direction="buy_no",
        entry_price=0.60, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="rejected", reviewer_score=87, holder_snapshot=None)
    old = (datetime.now(timezone.utc) - timedelta(seconds=1800)).isoformat()
    intel_paper.record_mark(conn, pid, old, 0.66, 10.0)

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    body = app.test_client().get("/api/intel-paper/positions?venue=pm").get_json()

    assert body["count"] == 1
    assert body["items"][0]["outcome_label"] == "July 31"
    assert body["items"][0]["reviewer_verdict"] == "rejected"
    assert body["mark_age_s"] >= 1700
    assert body["stale"] is True, "a 30-minute-old mark must not present as current"


def test_positions_endpoint_makes_no_network_call(conn):
    from unittest.mock import patch

    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP in a GET")):
        assert app.test_client().get("/api/intel-paper/positions").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k positions_endpoint -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.routes.api_intel_paper'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/routes/api_intel_paper.py
"""Read API for the intel paper book. GET touches SQLite only — never the network."""
from __future__ import annotations

import datetime as dt
import logging

from flask import Blueprint, jsonify, request

from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

STALE_AFTER_S = 900


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()


def create_api_intel_paper_blueprint(ctx) -> Blueprint:
    bp = Blueprint("api_intel_paper", __name__)

    @bp.get("/api/intel-paper/positions")
    def positions():
        venue = request.args.get("venue")
        if venue not in (None, "", "pm", "hl"):
            return jsonify({"items": [], "count": 0, "error": "bad venue"}), 400
        try:
            items = intel_paper.open_positions(ctx["conn"], venue or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("intel paper positions failed")
            return jsonify({"items": [], "count": 0, "available": False,
                            "error": str(exc)}), 200

        ages = [a for a in (_age_s(i.get("last_mark_ts")) for i in items) if a is not None]
        newest = min(ages) if ages else None
        return jsonify({
            "items": items,
            "count": len(items),
            "venue": venue or "all",
            "available": True,
            "mark_age_s": newest,
            "stale": (newest is None or newest > STALE_AFTER_S),
            "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        })

    @bp.post("/api/intel-paper/refresh")
    def refresh():
        """Operator-triggered mark cycle. The only endpoint permitted to make network calls."""
        from sniper.services.intel_paper_mark import mark_cycle
        try:
            return jsonify({"ok": True, "written": mark_cycle(ctx["conn"])})
        except Exception as exc:  # noqa: BLE001
            logger.exception("manual mark refresh failed")
            return jsonify({"ok": False, "error": str(exc)}), 200

    return bp
```

In `sniper/web/server.py`, next to the other `register_blueprint` calls:

```python
from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint
app.register_blueprint(create_api_intel_paper_blueprint(api_ctx))
```

Use the same context object the existing `create_api_core_blueprint(...)` call is given.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_intel_paper.py tests/test_intelligence_resilience.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/routes/api_intel_paper.py sniper/web/server.py tests/test_intel_paper.py
git commit -m "feat(intel-paper): read API with mark-age staleness and manual refresh"
```

---

## Task 8: UI — both surfaces

**Files:**
- Modify: `templates/index.html` (Paper fold inside `#intel-geo-view`)
- Modify: `static/app.js` (renderer + both call sites)
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `GET /api/intel-paper/positions`
- Produces: `intelPaper.render(containerId, venue)` on the existing `intelligence` object

PM renders in the existing Paper Trades sub-tab; HL renders in a new fold in Geopolitics.
Rows show *event → outcome → side → entry → current → PnL*, plus mark age and the
reviewer badge.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_ui_wires_both_surfaces():
    from pathlib import Path

    html = Path("templates/index.html").read_text()
    js = Path("static/app.js").read_text()

    assert "intel-paper-hl" in html, "Geopolitics needs a Paper fold for HL positions"
    assert "intel-paper-pm" in html, "PM Paper Trades sub-tab needs an intel container"

    assert "/api/intel-paper/positions" in js
    body = js.split("renderIntelPaper")[1][:4000]
    assert "outcome_label" in body, "multi-outcome rows must name the outcome"
    assert "event_title" in body, "rows must name the parent event"
    assert "reviewer_verdict" in body, "the reviewer verdict is a visible tag"
    assert "mark_age_s" in js, "mark age must be rendered, not hidden"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k ui_wires -v`
Expected: FAIL — `assert "intel-paper-hl" in html`

- [ ] **Step 3: Write minimal implementation**

In `templates/index.html`, inside `#intel-geo-view`, immediately after the closing
`</details>` of the Recent News fold:

```html
                        <!-- Intel paper book — Hyperliquid leg -->
                        <details class="intel-secondary-fold" style="margin-top:24px" open>
                            <summary class="intel-secondary-fold-summary">
                                <span class="intel-secondary-fold-title">Paper positions (Hyperliquid)</span>
                                <span class="intel-secondary-fold-meta" id="intel-paper-hl-meta">—</span>
                            </summary>
                            <div id="intel-paper-hl" style="padding:4px 14px 14px"></div>
                        </details>
```

In `templates/index.html`, inside the `#paper-subtab` div, immediately before its
closing `</div>`:

```html
                        <div class="positions-section-heading" style="margin-top:18px">
                            INTEL PAPER <span class="count-badge" id="intel-paper-pm-count">0</span>
                        </div>
                        <div id="intel-paper-pm"></div>
```

In `static/app.js`, add to the `intelligence` object:

```javascript
    async renderIntelPaper(containerId, venue) {
      const container = document.getElementById(containerId);
      if (!container) return;
      let data = null;
      try {
        data = await fetchJson(`/api/intel-paper/positions?venue=${encodeURIComponent(venue)}`,
                               { suppressErrorToast: true });
      } catch (err) {
        container.innerHTML = `<div class="empty-state empty-state--warning"><p>${esc(`Paper book unavailable — ${err.message}`)}</p></div>`;
        return;
      }
      const items = (data && data.items) || [];
      const metaEl = document.getElementById(`${containerId}-meta`);
      const countEl = document.getElementById(`${containerId}-count`);
      const ageTxt = data.mark_age_s == null
        ? 'never marked'
        : `marked ${Math.round(data.mark_age_s / 60)}m ago${data.stale ? ' · STALE' : ''}`;
      if (metaEl) metaEl.textContent = `${items.length} open · ${ageTxt}`;
      if (countEl) countEl.textContent = String(items.length);

      if (!items.length) {
        container.innerHTML = '<p class="text-muted" style="font-size:0.8rem;padding:8px 0">No open intel paper positions.</p>';
        return;
      }

      const verdictColor = { approved: '#22c55e', rejected: '#ef4444', pending: '#f59e0b' };
      container.innerHTML = items.map((p) => {
        // Multi-outcome markets are N binary markets under one event, so a row is
        // meaningless without the outcome label: "Election 2028 -> JD Vance -> YES".
        const title = [p.event_title, p.outcome_label].filter(Boolean).join(' → ')
          || esc(p.instrument);
        const pnl = p.last_pnl_pct;
        const pnlTxt = pnl == null ? '—' : `${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%`;
        const pnlColor = pnl == null ? '#8b949e' : (pnl >= 0 ? '#22c55e' : '#ef4444');
        const cur = p.last_mark_price == null ? '—' : p.last_mark_price;
        const vc = verdictColor[p.reviewer_verdict] || '#6b7280';
        return `<div class="idea-card" style="padding:8px 10px;margin-bottom:6px;border:1px solid #1f2630;border-radius:6px">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">
            <span style="font-weight:600;font-size:0.82rem">${esc(title)}</span>
            <span style="font-size:0.72rem;font-weight:600;color:${pnlColor}">${pnlTxt}</span>
          </div>
          <div style="font-size:0.72rem;color:#8b949e;margin-top:3px">
            ${esc(String(p.direction).replace(/_/g, ' ').toUpperCase())}
            · entry ${p.entry_price} · now ${cur}
          </div>
          <div style="font-size:0.68rem;margin-top:4px;color:${vc}">
            review: ${esc(p.reviewer_verdict || 'pending')}${p.reviewer_score != null ? ` (${p.reviewer_score})` : ''}
          </div>
        </div>`;
      }).join('');
    },
```

Call it from the Geopolitics loader — in `load()`, inside the `geopolitics` branch,
add `this.renderIntelPaper('intel-paper-hl', 'hl')` to the `Promise.allSettled` array.

Call it for PM wherever the Paper Trades sub-tab renders, alongside the existing
paper-table fill:

```javascript
      void intelligence.renderIntelPaper('intel-paper-pm', 'pm');
```

- [ ] **Step 4: Run tests and check JS syntax**

```bash
python3 -m pytest tests/test_intel_paper.py tests/test_intelligence_resilience.py -q
node --check static/app.js
```
Expected: PASS, `app.js` syntax OK.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html static/app.js tests/test_intel_paper.py
git commit -m "feat(intel-paper): render paper positions in PM Paper Trades and Geopolitics"
```

---

## Task 9: Wire into the producer, then go live

**Files:**
- Modify: `sniper/services/news_intel.py`
- Test: `tests/test_intel_paper.py`

**Interfaces:**
- Consumes: `intel_paper_open.open_for_idea`, `hl_universe.load_universe`
- Produces: `run_once()` stats gain `paper_pm`, `paper_hl`, `paper_none`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intel_paper.py
def test_producer_opens_a_paper_position_per_candidate(tmp_path, monkeypatch):
    import brain as brain_mod
    from sniper.services import news_intel as ni

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    intel = ni.NewsIntel.__new__(ni.NewsIntel)
    intel.brain = b
    intel.db_conn = None

    monkeypatch.setattr(ni, "fetch_all_feeds", lambda: [
        {"source": "bbc_world", "title": "Iran escalation continues in the region",
         "description": "d", "url": "u", "published_at": "2026-07-29T08:00:00+00:00"}])
    monkeypatch.setattr(ni, "fetch_polymarket_markets", lambda: [
        {"conditionId": "0xgood", "question": "Q1", "outcomePrices": '["0.40","0.60"]'}])
    monkeypatch.setattr(b, "store_news", lambda items: len(items))
    monkeypatch.setattr(b, "expire_stale_ideas", lambda: 0)
    monkeypatch.setattr("sniper.analysis.world_context.update_world_context",
                        lambda news, markets: [
                            {"condition_id": "0xgood", "market_title": "Q1",
                             "direction": "buy_yes", "confidence": "medium",
                             "causal_chain": "c",
                             "news_trigger_ts": "2026-07-29T08:00:00+00:00"}])
    monkeypatch.setattr(ni, "resolve_token", lambda cid, outcome: "tok-1")
    monkeypatch.setattr(ni, "snapshot_prices", lambda t, e, d, price_fn=None: (0.31, 0.44))
    monkeypatch.setattr(ni, "_send_telegram_idea", lambda *a, **k: None)
    monkeypatch.setattr(ni, "open_for_idea",
                        lambda conn, idea, hl_universe_set=None:
                            {"disposition": "pm", "position_id": 1, "reason": None})
    monkeypatch.setattr(ni, "load_universe", lambda: set())

    stats = intel.run_once()
    assert stats["ideas_created"] == 1
    assert stats["paper_pm"] == 1
    assert stats["paper_hl"] == 0
    assert stats["paper_none"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_intel_paper.py -k producer_opens -v`
Expected: FAIL — `KeyError: 'paper_pm'`

- [ ] **Step 3: Write minimal implementation**

In `sniper/services/news_intel.py`, add to the imports:

```python
from sniper.markets.hl_universe import load_universe
from sniper.services.intel_paper_open import open_for_idea
```

In `run_once`, initialise counters next to `ideas_created = 0`:

```python
        paper_counts = {"pm": 0, "hl": 0, "no_instrument": 0}
        hl_universe_set = load_universe()
```

Immediately after a successful `add_trade_idea` (where `idea_id` is truthy and
`ideas_created` is incremented), open the paper position:

```python
                    disposition = open_for_idea(
                        self.brain._get_conn(),
                        {"id": idea_id, "condition_id": cid, "direction": direction,
                         "hl_symbol": idea.get("hl_symbol"),
                         "adversarial_approved": None, "adversarial_score": None},
                        hl_universe_set=hl_universe_set,
                    )["disposition"]
                    paper_counts[disposition] = paper_counts.get(disposition, 0) + 1
```

Add to the returned stats dict:

```python
            "paper_pm": paper_counts["pm"],
            "paper_hl": paper_counts["hl"],
            "paper_none": paper_counts["no_instrument"],
```

Add `hl_symbol` to `_OUTPUT_SCHEMA["properties"]["trade_ideas"]["items"]["properties"]`
in `sniper/analysis/world_context.py`:

```python
                    "hl_symbol": {"type": ["string", "null"], "description": "Hyperliquid symbol if the thesis is better expressed as a perp (e.g. flx:OIL, xyz:TSLA, vntl:DEFENSE). Null when no instrument fits — do not invent one."},
```

- [ ] **Step 4: Run the full affected suite**

```bash
python3 -m pytest tests/test_intel_paper.py tests/test_intel_revival.py tests/test_news_intel_v1.py tests/test_intelligence_resilience.py -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/services/news_intel.py sniper/analysis/world_context.py index/ tests/test_intel_paper.py
git commit -m "feat(intel-paper): open a paper position for every candidate the producer emits"
```

- [ ] **Step 6: Backfill the existing candidate, then go live**

Open a position for the candidate already in the DB:

```bash
python3 -c "
import sqlite3
from sniper.services.intel_paper_open import open_for_idea
c = sqlite3.connect('sniper_tracker.db', timeout=30); c.row_factory = sqlite3.Row
for r in c.execute(\"SELECT * FROM trade_ideas WHERE status='open'\"):
    print(dict(r)['id'], open_for_idea(c, dict(r)))
"
python3 scripts/intel_paper_mark_job.py
python3 -c "
import sqlite3
from sniper.persistence import intel_paper
c = sqlite3.connect('file:sniper_tracker.db?mode=ro', uri=True); c.row_factory = sqlite3.Row
for p in intel_paper.open_positions(c): print(p)
"
```
Expected: one `pm` position with a non-null `last_mark_price` and a signed `last_pnl_pct`.

Then install the cron:

```bash
crontab -l > /tmp/cron.bak
( crontab -l; echo '*/5 * * * * cd /home/bigpapi/Desktop/Sniper && /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/intel_paper_mark_job.py >> logs/intel_paper_mark.log 2>&1  # intel paper marks' ) | crontab -
crontab -l | grep intel_paper_mark
```

Redeploy the web release so the new blueprint and UI are served, then verify:

```bash
set -a; . ./.env; set +a
curl -s -u "$SNIPER_DASH_USER:$SNIPER_DASH_PASS" \
  'http://127.0.0.1:5000/api/intel-paper/positions?venue=pm' | head -c 400
```
Expected: `count >= 1`, `stale: false`, and each item carrying `event_title`,
`outcome_label`, `last_mark_price` and `reviewer_verdict`.

---

## Deferred, with reasons

- **Holder panel (spec §Confidence panel).** Its own task after this ships. It is
  display-only, needs per-wallet caching and must run off the entry path, and none of
  the measurement depends on it. `holder_snapshot` is already in the schema so adding
  it later is a write, not a migration.
- **Position closing.** PM positions should close at resolution and HL at
  `expires_at`; both are unanswered in the spec's Open questions. Positions stay open
  and keep marking until that is decided — the mark series is unaffected.
- **Execution-cost model.** Marking at the bid is conservative but is not fees,
  slippage or depth. A directional-signal result is valid without it; a profitability
  claim is not.

## Self-review

**Spec coverage.** Routing → Task 4. One-shot open → Task 2. HL HIP-3 resolution →
Task 3. Storage → Task 1. Pricing/refresh O(1) → Tasks 5–6. Multi-outcome labels →
Tasks 2, 4, 8. UI placement → Task 8. Reviewer-as-tag → Task 4. Holder panel and
closing rules → explicitly deferred above, with reasons.

**Placeholder scan.** Every code step carries runnable code. No "add validation" or
"similar to Task N". The deferred section states what is not built and why, which is a
decision rather than a placeholder.

**Type consistency.** `fetch_market` returns `token_ids`/`prices` keyed `"YES"`/`"NO"`
in Task 2 and is consumed with those exact keys in Task 4. `open_for_idea` returns
`disposition` ∈ `pm|hl|no_instrument` in Task 4 and is consumed under those names in
Task 9. `open_positions` yields `last_mark_price`/`last_mark_ts`/`last_pnl_pct` in
Task 1 and is consumed under those names in Tasks 5, 7 and 8. `mark_cycle` returns
`{"pm","hl","errors"}` in Task 5 and is logged as such in Task 6.
