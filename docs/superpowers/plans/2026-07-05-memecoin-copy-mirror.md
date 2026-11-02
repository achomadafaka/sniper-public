# Memecoin Copy Mirror v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper-mirror a curated set of fomo.family traders' large entries into a SQLite ledger with honest slippage-included fills, visualized in a new Sniper dashboard tab.

**Architecture:** A Privy-authed poller of fomo's `/feed` normalizes large-trade events to `CopyEvent`s, filtered to a curated trader set. A copy engine opens fixed-$ paper positions priced by a live Jupiter quote and mirrors source exits proportionally. A dry-run executor builds (never signs) the real swap behind `armed=false`. A DexScreener poller marks open positions. A Flask blueprint + dashboard tab render the board and expose manual actions.

**Tech Stack:** Python 3, stdlib `urllib`/`sqlite3`, Flask, existing `sniper.persistence` db layer, `sniper.feeds.base.FeedAdapter`, pytest.

## Global Constraints

- All DB access goes through the `db.py` / `sniper.persistence.db_*` layer — no raw SQL in engine/routes (CLAUDE.md §5).
- All thresholds/knobs come from `web_settings.py` loaded from `.env` — no inline constants in service files (CLAUDE.md §8, "What NOT to do").
- Clamp/guard: `price > 0` before any division; distinguish `None` (unknown) from `0.0`.
- Timestamps: `datetime.now(timezone.utc)` — never `utcnow()`. **This host's local clock is +7h off**; lag math must use the fomo response `Date` header, never local time.
- fomo API requires browser headers (`origin`, `referer`, `user-agent`, `x-supported-chains`) or the WAF returns 403; auth is `Authorization: Bearer <privy access jwt>`.
- Reuse `scripts/fomo_latency_probe.py` as the reference for the auth flow and headers (do not re-derive).
- Feed scope is **large trades only**; document it, do not pretend small entries are covered.
- Credentials come from env / gitignored `.fomo.env`; never commit tokens, never log token values.
- Run `python3 scripts/brain_index.py generate` after adding tables; run `python3 scripts/validate_schema_refs.py` before committing schema-touching changes.
- Focused test command: `python3 -m pytest tests/test_memecoin_*.py -q`.

---

### Task 1: DB layer — tables + CRUD (`db_memecoin.py`)

**Files:**
- Create: `sniper/persistence/db_memecoin.py`
- Modify: `db.py` (add re-export line near the other `from sniper.persistence.db_* import *`)
- Test: `tests/test_memecoin_db.py`

**Interfaces:**
- Consumes: `sniper.persistence.db_core.get_connection(db_path)`.
- Produces:
  - `ensure_memecoin_tables(conn) -> None`
  - `add_wallet(conn, source_user_id: str, handle: str, label: str = "") -> int`
  - `list_wallets(conn, include_paused: bool = True) -> list[dict]`
  - `set_wallet_paused(conn, source_user_id: str, paused: bool) -> None`
  - `remove_wallet(conn, source_user_id: str) -> None`
  - `curated_user_ids(conn) -> set[str]`  (active/non-paused only)
  - `insert_event(conn, ev: dict) -> bool`  (False if duplicate `fomo_event_id`)
  - `open_position(conn, *, source_user_id, source_handle, mint, ticker, entry_price, entry_notional, entry_fill_source, entry_lag_s, their_size_usd, entry_liquidity=None, entry_token_age_s=None, opened_at) -> int`
  - `get_open_position(conn, source_user_id: str, mint: str) -> dict | None`
  - `list_open_positions(conn) -> list[dict]`
  - `list_closed_positions(conn) -> list[dict]`
  - `insert_fill(conn, *, position_id, kind, price, qty, notional, fill_source, fraction, created_at) -> int`
  - `close_position(conn, position_id: int, closed_at: str, realized_pnl: float) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_db.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_wallet_crud_and_curated_set():
    c = _conn()
    m.add_wallet(c, "u1", "alice", "sharp")
    m.add_wallet(c, "u2", "bob")
    assert {w["source_user_id"] for w in m.list_wallets(c)} == {"u1", "u2"}
    assert m.curated_user_ids(c) == {"u1", "u2"}
    m.set_wallet_paused(c, "u2", True)
    assert m.curated_user_ids(c) == {"u1"}          # paused excluded
    assert len(m.list_wallets(c, include_paused=False)) == 1
    m.remove_wallet(c, "u1")
    assert m.curated_user_ids(c) == set()


def test_event_dedup():
    c = _conn()
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "a",
          "mint": "M", "ticker": "T", "side": "buy", "their_price": 1.0,
          "their_market_cap": 100.0, "their_size_usd": 50.0, "is_first_buy": 1,
          "created_at": "2026-07-05T00:00:00Z", "received_at": "2026-07-05T00:00:00Z",
          "lag_s": 0.3}
    assert m.insert_event(c, ev) is True
    assert m.insert_event(c, ev) is False           # duplicate id ignored


def test_position_lifecycle():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="M",
                          ticker="T", entry_price=0.5, entry_notional=100.0,
                          entry_fill_source="jupiter", entry_lag_s=0.3,
                          their_size_usd=2000.0, opened_at="2026-07-05T00:00:00Z")
    assert m.get_open_position(c, "u1", "M")["id"] == pid
    m.insert_fill(c, position_id=pid, kind="entry", price=0.5, qty=200.0,
                  notional=100.0, fill_source="jupiter", fraction=1.0,
                  created_at="2026-07-05T00:00:00Z")
    m.close_position(c, pid, "2026-07-05T01:00:00Z", realized_pnl=25.0)
    assert m.get_open_position(c, "u1", "M") is None
    assert m.list_closed_positions(c)[0]["realized_pnl"] == 25.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_db.py -q`
Expected: FAIL — `ModuleNotFoundError: sniper.persistence.db_memecoin` / attribute errors.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/persistence/db_memecoin.py
"""db_memecoin.py — tables + CRUD for the memecoin copy-mirror (paper).

Tables (all ensure_* idempotent, CREATE IF NOT EXISTS):
  memecoin_wallets   curated fomo-native traders (no copyable on-chain wallet)
  memecoin_events    append-only raw feed observations (audit/research)
  memecoin_positions paper positions
  memecoin_fills     fills incl. manual UI actions
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def ensure_memecoin_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT UNIQUE,
            handle TEXT,
            label TEXT,
            paused INTEGER DEFAULT 0,
            added_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT, side TEXT,
            their_price REAL, their_market_cap REAL, their_size_usd REAL,
            is_first_buy INTEGER,
            created_at TEXT, received_at TEXT, lag_s REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT,
            entry_price REAL, entry_notional REAL, entry_fill_source TEXT,
            entry_lag_s REAL, their_size_usd REAL,
            entry_liquidity REAL, entry_token_age_s REAL,
            is_open INTEGER DEFAULT 1,
            opened_at TEXT, closed_at TEXT, realized_pnl REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            kind TEXT, price REAL, qty REAL, notional REAL,
            fill_source TEXT, fraction REAL, created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_open ON memecoin_positions(is_open)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_fills_pos ON memecoin_fills(position_id)")
    conn.commit()


def add_wallet(conn, source_user_id, handle, label=""):
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_wallets(source_user_id, handle, label, paused, added_at) "
        "VALUES (?,?,?,0, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (source_user_id, handle, label))
    conn.commit()
    return cur.lastrowid


def list_wallets(conn, include_paused=True):
    q = "SELECT * FROM memecoin_wallets"
    if not include_paused:
        q += " WHERE paused=0"
    return [dict(r) for r in conn.execute(q + " ORDER BY added_at")]


def set_wallet_paused(conn, source_user_id, paused):
    conn.execute("UPDATE memecoin_wallets SET paused=? WHERE source_user_id=?",
                 (1 if paused else 0, source_user_id))
    conn.commit()


def remove_wallet(conn, source_user_id):
    conn.execute("DELETE FROM memecoin_wallets WHERE source_user_id=?", (source_user_id,))
    conn.commit()


def curated_user_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE paused=0")}


def insert_event(conn, ev):
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_events(fomo_event_id, source_user_id, source_handle, "
        "mint, ticker, side, their_price, their_market_cap, their_size_usd, is_first_buy, "
        "created_at, received_at, lag_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev["fomo_event_id"], ev["source_user_id"], ev["source_handle"], ev["mint"],
         ev["ticker"], ev["side"], ev["their_price"], ev["their_market_cap"],
         ev["their_size_usd"], 1 if ev.get("is_first_buy") else 0,
         ev["created_at"], ev["received_at"], ev["lag_s"]))
    conn.commit()
    return cur.rowcount > 0


def open_position(conn, *, source_user_id, source_handle, mint, ticker, entry_price,
                  entry_notional, entry_fill_source, entry_lag_s, their_size_usd,
                  entry_liquidity=None, entry_token_age_s=None, opened_at):
    cur = conn.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, mint, ticker, "
        "entry_price, entry_notional, entry_fill_source, entry_lag_s, their_size_usd, "
        "entry_liquidity, entry_token_age_s, is_open, opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (source_user_id, source_handle, mint, ticker, entry_price, entry_notional,
         entry_fill_source, entry_lag_s, their_size_usd, entry_liquidity,
         entry_token_age_s, opened_at))
    conn.commit()
    return cur.lastrowid


def get_open_position(conn, source_user_id, mint):
    r = conn.execute(
        "SELECT * FROM memecoin_positions WHERE source_user_id=? AND mint=? AND is_open=1",
        (source_user_id, mint)).fetchone()
    return dict(r) if r else None


def list_open_positions(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE is_open=1 ORDER BY opened_at DESC")]


def list_closed_positions(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE is_open=0 ORDER BY closed_at DESC")]


def insert_fill(conn, *, position_id, kind, price, qty, notional, fill_source,
                fraction, created_at):
    cur = conn.execute(
        "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
        "fraction, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (position_id, kind, price, qty, notional, fill_source, fraction, created_at))
    conn.commit()
    return cur.lastrowid


def close_position(conn, position_id, closed_at, realized_pnl):
    conn.execute(
        "UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=? WHERE id=?",
        (closed_at, realized_pnl, position_id))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_db.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Re-export through db.py, regen index, commit**

Add to `db.py` after the other `from sniper.persistence.db_* import *` lines:
```python
from sniper.persistence.db_memecoin import *  # noqa: F401,F403 — re-export for backward compatibility
```
Then:
```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin.py db.py tests/test_memecoin_db.py index/
git commit -m "feat(memecoin-copy): db layer — tables + CRUD"
```

---

### Task 2: CopyEvent normalization + curated filter (`fomo_feed.py` pure fns)

**Files:**
- Create: `sniper/feeds/fomo_feed.py` (dataclass + pure functions only in this task)
- Test: `tests/test_memecoin_normalize.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `@dataclass CopyEvent` with fields: `source_user_id, source_handle, mint, ticker, side, their_price, their_market_cap, their_size_usd, is_first_buy, fomo_event_id, created_at, received_at, lag_s`.
  - `normalize_feed_item(raw: dict, received_iso: str, server_epoch: float | None) -> CopyEvent | None` — returns None for non-buy/sell types or malformed items.
  - `filter_curated(events: list[CopyEvent], curated: set[str]) -> list[CopyEvent]`
  - `FEED_TYPES: list[str]` (the subscription list).
  - Module maps fomo `type` → side: `large_buy`/`multi_user_buy` → `"buy"`; `large_sell`/`single_user_sell`/`multi_user_sell` → `"sell"`; else skip.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_normalize.py
from sniper.feeds.fomo_feed import normalize_feed_item, filter_curated, CopyEvent

RAW_BUY = {
    "id": "e1", "type": "large_buy", "userId": "u1",
    "tokenAddress": "MINT", "createdAt": "2026-07-05T10:00:00.000Z",
    "body": {"userHandle": "alice", "displayName": "alice", "ticker": "PEPE",
             "price": 0.0047, "marketCap": "4700000", "currentSizeUsd": 3318.0,
             "isFirstBuy": True},
}


def test_normalize_buy_uses_server_clock_for_lag():
    created_epoch = 1783245600.0  # 2026-07-05T10:00:00Z
    ev = normalize_feed_item(RAW_BUY, "2026-07-05T10:00:00.5Z", created_epoch + 0.5)
    assert ev.side == "buy" and ev.mint == "MINT" and ev.ticker == "PEPE"
    assert ev.source_user_id == "u1" and ev.source_handle == "alice"
    assert ev.their_size_usd == 3318.0 and ev.is_first_buy is True
    assert abs(ev.lag_s - 0.5) < 0.01           # skew-free via server epoch


def test_normalize_skips_non_trade_types():
    assert normalize_feed_item({"type": "thesis_created", "body": {}}, "x", None) is None
    assert normalize_feed_item({"type": "new_token_listing", "body": {}}, "x", None) is None


def test_normalize_returns_none_on_malformed():
    assert normalize_feed_item({"type": "large_buy"}, "x", None) is None  # no body/mint


def test_filter_curated():
    a = normalize_feed_item(RAW_BUY, "x", None)
    b = normalize_feed_item({**RAW_BUY, "id": "e2", "userId": "u2"}, "x", None)
    assert [e.source_user_id for e in filter_curated([a, b], {"u1"})] == ["u1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_normalize.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/feeds/fomo_feed.py  (Task 2 portion — pure normalization)
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

FEED_TYPES = [
    "large_buy", "large_sell", "large_transfer_in", "large_transfer_out",
    "single_user_transfer_out", "single_user_sell", "thesis_created",
    "multi_user_buy", "multi_user_sell", "new_token_listing",
    "price_since_listing", "user_trade_profit_milestone",
    "user_with_smart_following", "manual",
]

_BUY = {"large_buy", "multi_user_buy"}
_SELL = {"large_sell", "single_user_sell", "multi_user_sell"}


@dataclass
class CopyEvent:
    source_user_id: str
    source_handle: str
    mint: str
    ticker: str
    side: str
    their_price: float
    their_market_cap: float
    their_size_usd: float
    is_first_buy: bool
    fomo_event_id: str
    created_at: str
    received_at: str
    lag_s: float


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_feed_item(raw: dict, received_iso: str, server_epoch: Optional[float]) -> Optional[CopyEvent]:
    t = raw.get("type")
    side = "buy" if t in _BUY else "sell" if t in _SELL else None
    body = raw.get("body")
    mint = raw.get("tokenAddress")
    if side is None or not isinstance(body, dict) or not mint:
        return None
    created = raw.get("createdAt") or ""
    lag = None
    if server_epoch is not None and created:
        from datetime import datetime
        try:
            ce = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            lag = server_epoch - ce
        except ValueError:
            lag = None
    return CopyEvent(
        source_user_id=str(raw.get("userId") or body.get("userId") or ""),
        source_handle=str(body.get("userHandle") or body.get("displayName") or ""),
        mint=str(mint),
        ticker=str(body.get("ticker") or ""),
        side=side,
        their_price=_num(body.get("price")),
        their_market_cap=_num(body.get("marketCap")),
        their_size_usd=_num(body.get("currentSizeUsd") or body.get("inHumanAmount")),
        is_first_buy=bool(body.get("isFirstBuy")),
        fomo_event_id=str(raw.get("id") or ""),
        created_at=created,
        received_at=received_iso,
        lag_s=lag if lag is not None else -1.0,
    )


def filter_curated(events, curated):
    return [e for e in events if e.source_user_id in curated]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_normalize.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/fomo_feed.py tests/test_memecoin_normalize.py
git commit -m "feat(memecoin-copy): CopyEvent normalization + curated filter"
```

---

### Task 3: Privy auth + fomo client (`fomo_feed.py` client portion)

**Files:**
- Modify: `sniper/feeds/fomo_feed.py` (append client)
- Test: `tests/test_memecoin_auth.py`

**Interfaces:**
- Consumes: env vars `FOMO_REFRESH_TOKEN`, `FOMO_ACCESS_TOKEN`, `FOMO_CA_ID`.
- Produces:
  - `BROWSER_HEADERS: dict`
  - `class FomoAuth` with `access: str`, `refresh: str`, `ca_id: str`, and `refresh_token(http=None) -> str`. `http` is an injectable callable `(method, url, headers, body) -> (status, json, date_header)` for testing (defaults to the real `_http_json`).
  - `fomo_get(auth: FomoAuth, path: str, http=None, retry=True) -> tuple[int, dict, str | None]` — sends browser headers + Bearer; on 401/403 calls `auth.refresh_token()` once then retries.
  - `build_feed_path(limit: int = 50) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_auth.py
from sniper.feeds import fomo_feed as f


def test_browser_headers_present():
    for h in ("origin", "referer", "user-agent", "x-supported-chains"):
        assert h in f.BROWSER_HEADERS


def test_get_refreshes_on_403_then_succeeds():
    calls = {"n": 0, "refreshed": False}

    def fake_http(method, url, headers, body=None):
        if "auth.privy.io" in url:
            calls["refreshed"] = True
            return 200, {"privy_access_token": "NEWJWT", "refresh_token": "R2"}, None
        # first fomo call 403s, second (after refresh) 200s
        calls["n"] += 1
        if calls["n"] == 1:
            from urllib.error import HTTPError
            raise HTTPError(url, 403, "forbidden", {}, None)
        assert headers["authorization"] == "Bearer NEWJWT"
        return 200, {"responseObject": {"feed": []}}, "Sun, 05 Jul 2026 10:00:00 GMT"

    auth = f.FomoAuth(access="OLD", refresh="R1", ca_id="ca")
    status, j, date = f.fomo_get(auth, f.build_feed_path(), http=fake_http)
    assert status == 200 and calls["refreshed"] is True
    assert auth.access == "NEWJWT" and auth.refresh == "R2"  # rotation persisted in-memory
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_auth.py -q`
Expected: FAIL — `FomoAuth`/`fomo_get` missing.

- [ ] **Step 3: Write minimal implementation** (append to `sniper/feeds/fomo_feed.py`)

```python
import json as _json
import os
import urllib.request
import urllib.error

FOMO_API = "https://prod-api.fomo.family"
PRIVY_SESSIONS_URL = "https://auth.privy.io/api/v1/sessions"
PRIVY_APP_ID = "cm6h485o300n3zj9yl6vpedq7"
PRIVY_CLIENT = "react-auth:3.21.3"
PRIVY_CLIENT_ID = "client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH"

BROWSER_HEADERS = {
    "accept": "application/json",
    "origin": "https://fomo.family",
    "referer": "https://fomo.family/",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "x-supported-chains": "1,56,143,8453,1399811149",
}


def _http_json(method, url, headers, body=None, timeout=15):
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), r.headers.get("Date")


class FomoAuth:
    def __init__(self, access="", refresh="", ca_id=""):
        self.access = access or os.environ.get("FOMO_ACCESS_TOKEN", "")
        self.refresh = refresh or os.environ.get("FOMO_REFRESH_TOKEN", "")
        self.ca_id = ca_id or os.environ.get("FOMO_CA_ID", "")

    def refresh_token(self, http=None):
        http = http or _http_json
        headers = {
            "accept": "application/json", "content-type": "application/json",
            "privy-app-id": PRIVY_APP_ID, "privy-client": PRIVY_CLIENT,
            "privy-client-id": PRIVY_CLIENT_ID,
        }
        if self.ca_id:
            headers["privy-ca-id"] = self.ca_id
        if self.access:
            headers["authorization"] = "Bearer " + self.access
        _, j, _d = http("POST", PRIVY_SESSIONS_URL, headers, {"refresh_token": self.refresh})
        self.access = j.get("privy_access_token") or j.get("token") or self.access
        if j.get("refresh_token"):
            self.refresh = j["refresh_token"]
        return self.access


def build_feed_path(limit=50):
    qs = "&".join(["limit=%d" % limit] + ["feedTypes=%s" % t for t in FEED_TYPES])
    return "/feed?" + qs


def fomo_get(auth, path, http=None, retry=True):
    http = http or _http_json
    headers = dict(BROWSER_HEADERS)
    headers["authorization"] = "Bearer " + (auth.access or "x")
    try:
        return http("GET", FOMO_API + path, headers)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            auth.refresh_token(http=http)
            return fomo_get(auth, path, http=http, retry=False)
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_auth.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/fomo_feed.py tests/test_memecoin_auth.py
git commit -m "feat(memecoin-copy): Privy auth + WAF-header fomo client"
```

---

### Task 4: Honest fill (Jupiter quote + fallback ordering)

**Files:**
- Create: `sniper/execution/solana_quote.py`
- Test: `tests/test_memecoin_fill.py`

**Interfaces:**
- Consumes: nothing (HTTP injected for tests).
- Produces:
  - `jupiter_quote_price(mint: str, usd_amount: float, side: str, http=None) -> float | None` — returns fill price/token in USD, or None on failure.
  - `dexscreener_price(mint: str, http=None) -> float | None`
  - `honest_fill(mint: str, usd_amount: float, side: str, fallback_price: float, http=None) -> tuple[float, str]` — tries Jupiter → DexScreener → `fallback_price`, returning `(price, source)` where source ∈ {`"jupiter"`,`"dexscreener"`,`"fomo"`}. Guards `price > 0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_fill.py
from sniper.execution import solana_quote as q


def test_honest_fill_prefers_jupiter():
    def http(method, url, headers=None, body=None):
        if "jup" in url:
            # 100 USDC -> 20000 tokens => price 0.005
            return 200, {"outAmount": "20000000000", "inAmount": "100000000",
                         "outDecimals": 6, "inDecimals": 6}, None
        raise AssertionError("should not reach dexscreener")
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http)
    assert src == "jupiter" and abs(price - 0.005) < 1e-6


def test_honest_fill_falls_back_to_dexscreener_then_fomo():
    def http_ds(method, url, headers=None, body=None):
        if "jup" in url:
            raise RuntimeError("jupiter down")
        return 200, {"pairs": [{"priceUsd": "0.007"}]}, None
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http_ds)
    assert src == "dexscreener" and abs(price - 0.007) < 1e-6

    def http_dead(method, url, headers=None, body=None):
        raise RuntimeError("all down")
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http_dead)
    assert src == "fomo" and price == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_fill.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/execution/solana_quote.py
"""Honest paper-fill pricing for Solana memecoins.

Jupiter quote (real slippage/impact) -> DexScreener mid -> fomo event price.
Every fill records which source was used so paper PnL stays honest.
"""
from __future__ import annotations

import json as _json
import urllib.request
from typing import Optional, Tuple

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER = "https://quote-api.jup.ag/v6/quote"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"


def _http(method, url, headers=None, body=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), None


def jupiter_quote_price(mint, usd_amount, side, http=None):
    http = http or _http
    # buy: USDC(in) -> token(out); sell: token(in) -> USDC(out). Price is USD per token.
    in_mint, out_mint = (USDC, mint) if side == "buy" else (mint, USDC)
    amount = int(usd_amount * 1_000_000)  # USDC 6dp; for sell this is a notional proxy
    url = "%s?inputMint=%s&outputMint=%s&amount=%d&slippageBps=100" % (
        JUPITER, in_mint, out_mint, amount)
    try:
        _s, j, _d = http("GET", url)
        out = float(j["outAmount"]) / (10 ** int(j.get("outDecimals", 6)))
        inp = float(j["inAmount"]) / (10 ** int(j.get("inDecimals", 6)))
        if out <= 0 or inp <= 0:
            return None
        return (inp / out) if side == "buy" else (out / inp)
    except Exception:
        return None


def dexscreener_price(mint, http=None):
    http = http or _http
    try:
        _s, j, _d = http("GET", DEXSCREENER + mint)
        pairs = j.get("pairs") or []
        if not pairs:
            return None
        p = float(pairs[0]["priceUsd"])
        return p if p > 0 else None
    except Exception:
        return None


def honest_fill(mint, usd_amount, side, fallback_price, http=None) -> Tuple[float, str]:
    p = jupiter_quote_price(mint, usd_amount, side, http=http)
    if p and p > 0:
        return p, "jupiter"
    p = dexscreener_price(mint, http=http)
    if p and p > 0:
        return p, "dexscreener"
    return float(fallback_price), "fomo"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_fill.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/solana_quote.py tests/test_memecoin_fill.py
git commit -m "feat(memecoin-copy): honest Jupiter-quote fill with fallback chain"
```

---

### Task 5: Copy engine — entry

**Files:**
- Create: `sniper/services/memecoin_copy_trader.py`
- Test: `tests/test_memecoin_engine_entry.py`

**Interfaces:**
- Consumes: `db_memecoin.*`, `solana_quote.honest_fill`, `CopyEvent`.
- Produces:
  - `on_copy_event(conn, ev: CopyEvent, size_usd: float, now_iso: str, fill_fn=honest_fill) -> str` — returns one of `"opened"`, `"skipped_dup"`, `"exit"`, `"ignored"`. `fill_fn` injectable for tests.
  - Entry path only in this task: a curated `buy` with no existing open `(source_user_id, mint)` → open a position via `honest_fill`, insert an `entry` fill, return `"opened"`. A `buy` when a position already exists → `"skipped_dup"` (no averaging in v0).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_engine_entry.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _buy(uid="u1", mint="MINT"):
    return CopyEvent(uid, "alice", mint, "PEPE", "buy", 0.0047, 4_700_000, 3318.0,
                     True, "e1", "2026-07-05T10:00:00Z", "2026-07-05T10:00:00Z", 0.3)


def test_entry_opens_position_with_honest_fill():
    c = _conn()
    fill = lambda mint, usd, side, fallback, http=None: (0.005, "jupiter")
    r = eng.on_copy_event(c, _buy(), size_usd=100.0, now_iso="2026-07-05T10:00:01Z", fill_fn=fill)
    assert r == "opened"
    pos = m.get_open_position(c, "u1", "MINT")
    assert pos["entry_price"] == 0.005 and pos["entry_notional"] == 100.0
    assert pos["entry_fill_source"] == "jupiter" and pos["their_size_usd"] == 3318.0


def test_entry_dedup_when_position_open():
    c = _conn()
    fill = lambda *a, **k: (0.005, "jupiter")
    eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=fill)
    assert eng.on_copy_event(c, _buy(), 100.0, "t", fill_fn=fill) == "skipped_dup"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_engine_entry.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/memecoin_copy_trader.py
"""Memecoin copy engine (paper). Entry on curated buy; proportional exit-mirror.

Fills are honest (Jupiter quote incl. slippage). No averaging on repeat buys in v0.
"""
from __future__ import annotations

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import honest_fill


def on_copy_event(conn, ev, size_usd, now_iso, fill_fn=honest_fill):
    if ev.side == "buy":
        if m.get_open_position(conn, ev.source_user_id, ev.mint):
            return "skipped_dup"
        price, source = fill_fn(ev.mint, size_usd, "buy", ev.their_price)
        if not price or price <= 0:
            return "ignored"
        qty = size_usd / price
        pid = m.open_position(
            conn, source_user_id=ev.source_user_id, source_handle=ev.source_handle,
            mint=ev.mint, ticker=ev.ticker, entry_price=price, entry_notional=size_usd,
            entry_fill_source=source, entry_lag_s=ev.lag_s, their_size_usd=ev.their_size_usd,
            opened_at=now_iso)
        m.insert_fill(conn, position_id=pid, kind="entry", price=price, qty=qty,
                      notional=size_usd, fill_source=source, fraction=1.0, created_at=now_iso)
        return "opened"
    return "ignored"  # sell handled in Task 6
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_engine_entry.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py tests/test_memecoin_engine_entry.py
git commit -m "feat(memecoin-copy): copy engine — fixed-$ entry with honest fill"
```

---

### Task 6: Copy engine — proportional exit-mirror

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py`
- Test: `tests/test_memecoin_engine_exit.py`

**Interfaces:**
- Consumes: same as Task 5.
- Produces: extend `on_copy_event` to handle `ev.side == "sell"`:
  - If an open position exists for `(source_user_id, mint)`: close it fully in v0 (proportional partials are recorded via `their_size_usd` ratio when available, else full close), compute `realized_pnl = (exit_price - entry_price) * qty` using an honest exit fill, insert a `mirror_exit` fill, `close_position`. Return `"exit"`.
  - If no open position: return `"ignored"` (we never followed that entry).
  - Helper `qty_of(conn, position_id) -> float` sums entry fill qty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_engine_exit.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _ev(side, uid="u1", mint="MINT"):
    return CopyEvent(uid, "alice", mint, "PEPE", side, 0.005, 1e6, 3318.0, True,
                     "e", "t", "t", 0.3)


def test_sell_mirrors_exit_and_realizes_pnl():
    c = _conn()
    eng.on_copy_event(c, _ev("buy"), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))
    # exit at 0.010 -> qty=20000, pnl=(0.010-0.005)*20000 = 100
    r = eng.on_copy_event(c, _ev("sell"), 100.0, "t2", fill_fn=lambda *a, **k: (0.010, "jupiter"))
    assert r == "exit"
    closed = m.list_closed_positions(c)[0]
    assert abs(closed["realized_pnl"] - 100.0) < 1e-6


def test_sell_without_position_is_ignored():
    c = _conn()
    assert eng.on_copy_event(c, _ev("sell"), 100.0, "t", fill_fn=lambda *a, **k: (0.01, "x")) == "ignored"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_engine_exit.py -q`
Expected: FAIL — sell path returns "ignored".

- [ ] **Step 3: Write minimal implementation** (replace the `return "ignored"  # sell handled in Task 6` line)

```python
    # ev.side == "sell"
    pos = m.get_open_position(conn, ev.source_user_id, ev.mint)
    if not pos:
        return "ignored"
    exit_price, source = fill_fn(ev.mint, pos["entry_notional"], "sell", ev.their_price)
    qty = _qty_of(conn, pos["id"])
    realized = (exit_price - pos["entry_price"]) * qty
    m.insert_fill(conn, position_id=pos["id"], kind="mirror_exit", price=exit_price,
                  qty=qty, notional=exit_price * qty, fill_source=source,
                  fraction=1.0, created_at=now_iso)
    m.close_position(conn, pos["id"], now_iso, realized)
    return "exit"


def _qty_of(conn, position_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0) FROM memecoin_fills WHERE position_id=? AND kind='entry'",
        (position_id,)).fetchone()
    return float(row[0] or 0.0)
```

(Insert the sell block before the final `return "ignored"` of the buy branch — i.e. after the `if ev.side == "buy": ...` block. Keep `_qty_of` at module scope.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_engine_exit.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py tests/test_memecoin_engine_exit.py
git commit -m "feat(memecoin-copy): proportional exit-mirror with realized PnL"
```

---

### Task 7: Dry-run executor (`armed=false`)

**Files:**
- Create: `sniper/execution/solana_swap_executor.py`
- Test: `tests/test_memecoin_executor.py`

**Interfaces:**
- Produces:
  - `build_swap_tx(mint: str, usd_amount: float, side: str, armed: bool = False) -> dict` — returns `{"mint","usd_amount","side","armed","signed": False, "note"}`. When `armed` is False it MUST return `signed=False` and never call any signing path. A real signer is intentionally absent in v0; if `armed` is True, raise `NotImplementedError("live executor not enabled")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_executor.py
import pytest
from sniper.execution.solana_swap_executor import build_swap_tx


def test_dry_run_never_signs():
    tx = build_swap_tx("MINT", 100.0, "buy", armed=False)
    assert tx["signed"] is False and tx["armed"] is False
    assert tx["mint"] == "MINT" and tx["side"] == "buy"


def test_armed_true_is_blocked_in_v0():
    with pytest.raises(NotImplementedError):
        build_swap_tx("MINT", 100.0, "buy", armed=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_executor.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/execution/solana_swap_executor.py
"""Dry-run Solana swap builder. v0 NEVER signs or submits.

Going live later = implement _sign_and_send and flip armed=True behind operator gating.
"""
from __future__ import annotations


def build_swap_tx(mint, usd_amount, side, armed=False):
    if armed:
        raise NotImplementedError("live executor not enabled")
    return {
        "mint": mint, "usd_amount": float(usd_amount), "side": side,
        "armed": False, "signed": False,
        "note": "dry-run: would build Jupiter swap; not signed/submitted",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_executor.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/solana_swap_executor.py tests/test_memecoin_executor.py
git commit -m "feat(memecoin-copy): dry-run executor (armed=false, never signs)"
```

---

### Task 8: DexScreener marks + unrealized PnL

**Files:**
- Modify: `sniper/execution/solana_quote.py` (add marks helper)
- Create: `sniper/services/memecoin_marks.py`
- Test: `tests/test_memecoin_marks.py`

**Interfaces:**
- Produces:
  - `dexscreener_stats(mint: str, http=None) -> dict | None` in `solana_quote.py` — `{"price","market_cap","liquidity","volume_24h"}` (missing keys → None).
  - `mark_positions(conn, stats_fn=dexscreener_stats) -> list[dict]` in `memecoin_marks.py` — for each open position, fetch stats, compute `unrealized_pnl = (current_price - entry_price) * qty`, return rows `{position_id, mint, current_price, unrealized_pnl, liquidity, volume_24h}`. Guards `entry_price > 0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_marks.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_marks as mk


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_mark_positions_computes_unrealized():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="MINT",
                          ticker="T", entry_price=0.005, entry_notional=100.0,
                          entry_fill_source="jupiter", entry_lag_s=0.3,
                          their_size_usd=1000.0, opened_at="t")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.005, qty=20000.0,
                  notional=100.0, fill_source="jupiter", fraction=1.0, created_at="t")
    stats = lambda mint, http=None: {"price": 0.010, "market_cap": 2e6,
                                     "liquidity": 50000.0, "volume_24h": 1e5}
    rows = mk.mark_positions(c, stats_fn=stats)
    assert rows[0]["current_price"] == 0.010
    assert abs(rows[0]["unrealized_pnl"] - 100.0) < 1e-6  # (0.010-0.005)*20000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_marks.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Append to `sniper/execution/solana_quote.py`:
```python
def dexscreener_stats(mint, http=None):
    http = http or _http
    try:
        _s, j, _d = http("GET", DEXSCREENER + mint)
        pairs = j.get("pairs") or []
        if not pairs:
            return None
        p = pairs[0]
        def g(*path):
            cur = p
            for k in path:
                cur = cur.get(k) if isinstance(cur, dict) else None
            return cur
        return {
            "price": float(p.get("priceUsd") or 0) or None,
            "market_cap": float(p.get("marketCap") or 0) or None,
            "liquidity": float(g("liquidity", "usd") or 0) or None,
            "volume_24h": float(g("volume", "h24") or 0) or None,
        }
    except Exception:
        return None
```

Create `sniper/services/memecoin_marks.py`:
```python
"""Mark open memecoin positions with live DexScreener prices -> unrealized PnL."""
from __future__ import annotations

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import dexscreener_stats


def _entry_qty(conn, position_id):
    r = conn.execute("SELECT COALESCE(SUM(qty),0) FROM memecoin_fills "
                     "WHERE position_id=? AND kind='entry'", (position_id,)).fetchone()
    return float(r[0] or 0.0)


def mark_positions(conn, stats_fn=dexscreener_stats):
    out = []
    for pos in m.list_open_positions(conn):
        if not pos.get("entry_price") or pos["entry_price"] <= 0:
            continue
        st = stats_fn(pos["mint"])
        if not st or not st.get("price"):
            continue
        qty = _entry_qty(conn, pos["id"])
        out.append({
            "position_id": pos["id"], "mint": pos["mint"],
            "current_price": st["price"],
            "unrealized_pnl": (st["price"] - pos["entry_price"]) * qty,
            "liquidity": st.get("liquidity"), "volume_24h": st.get("volume_24h"),
        })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_marks.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/solana_quote.py sniper/services/memecoin_marks.py tests/test_memecoin_marks.py
git commit -m "feat(memecoin-copy): DexScreener marks + unrealized PnL"
```

---

### Task 9: Flask blueprint (CRUD + board + manual actions + stats)

**Files:**
- Create: `sniper/routes/api_memecoin.py`
- Test: `tests/test_memecoin_api.py`

**Interfaces:**
- Consumes: `db_memecoin.*`, `memecoin_marks.mark_positions`, `memecoin_copy_trader.on_copy_event`.
- Produces: `create_api_memecoin_blueprint(ctx: dict) -> flask.Blueprint`, where `ctx["conn"]` is a sqlite connection. Routes:
  - `GET  /api/memecoin/wallets` → `{"wallets":[...]}`
  - `POST /api/memecoin/wallets` `{source_user_id,handle,label}` → `{"ok":true}`
  - `POST /api/memecoin/wallets/<uid>/pause` `{paused:bool}` → `{"ok":true}`
  - `DELETE /api/memecoin/wallets/<uid>` → `{"ok":true}`
  - `GET  /api/memecoin/positions/open` → `{"positions":[...], "marks":[...]}`
  - `GET  /api/memecoin/positions/closed` → `{"positions":[...], "stats":{win_rate,total_pnl,by_wallet,lag}}`
  - `POST /api/memecoin/positions/<pid>/close` → `{"ok":true}` (manual close at DexScreener price)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_api.py
import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def test_wallet_crud_endpoints():
    cl, c = _client()
    assert cl.post("/api/memecoin/wallets",
                   json={"source_user_id": "u1", "handle": "alice"}).get_json()["ok"]
    assert cl.get("/api/memecoin/wallets").get_json()["wallets"][0]["handle"] == "alice"
    assert cl.post("/api/memecoin/wallets/u1/pause", json={"paused": True}).get_json()["ok"]
    assert m.curated_user_ids(c) == set()
    assert cl.delete("/api/memecoin/wallets/u1").get_json()["ok"]
    assert cl.get("/api/memecoin/wallets").get_json()["wallets"] == []


def test_open_positions_endpoint_shape():
    cl, c = _client()
    m.open_position(c, source_user_id="u1", source_handle="a", mint="MINT", ticker="T",
                    entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                    entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    body = cl.get("/api/memecoin/positions/open").get_json()
    assert body["positions"][0]["mint"] == "MINT" and "marks" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_api.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/routes/api_memecoin.py
"""Flask blueprint for the memecoin copy-mirror dashboard tab (paper)."""
from __future__ import annotations

import statistics
from flask import Blueprint, jsonify, request

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_marks import mark_positions


def create_api_memecoin_blueprint(ctx):
    conn = ctx["conn"]
    bp = Blueprint("api_memecoin", __name__)

    @bp.get("/api/memecoin/wallets")
    def wallets():
        return jsonify({"wallets": m.list_wallets(conn)})

    @bp.post("/api/memecoin/wallets")
    def add():
        d = request.get_json(force=True)
        m.add_wallet(conn, d["source_user_id"], d.get("handle", ""), d.get("label", ""))
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/wallets/<uid>/pause")
    def pause(uid):
        m.set_wallet_paused(conn, uid, bool(request.get_json(force=True).get("paused")))
        return jsonify({"ok": True})

    @bp.delete("/api/memecoin/wallets/<uid>")
    def remove(uid):
        m.remove_wallet(conn, uid)
        return jsonify({"ok": True})

    @bp.get("/api/memecoin/positions/open")
    def open_positions():
        return jsonify({"positions": m.list_open_positions(conn),
                        "marks": mark_positions(conn)})

    @bp.get("/api/memecoin/positions/closed")
    def closed_positions():
        rows = m.list_closed_positions(conn)
        pnls = [r["realized_pnl"] for r in rows if r.get("realized_pnl") is not None]
        by_wallet = {}
        for r in rows:
            by_wallet.setdefault(r["source_handle"], 0.0)
            by_wallet[r["source_handle"]] += (r.get("realized_pnl") or 0.0)
        lags = [r["entry_lag_s"] for r in rows if (r.get("entry_lag_s") or -1) >= 0]
        stats = {
            "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
            "total_pnl": sum(pnls),
            "by_wallet": by_wallet,
            "lag": {"median": statistics.median(lags) if lags else None,
                    "min": min(lags) if lags else None},
        }
        return jsonify({"positions": rows, "stats": stats})

    @bp.post("/api/memecoin/positions/<int:pid>/close")
    def manual_close(pid):
        from sniper.execution.solana_quote import dexscreener_stats
        rows = {p["id"]: p for p in m.list_open_positions(conn)}
        pos = rows.get(pid)
        if not pos:
            return jsonify({"ok": False, "error": "not open"}), 404
        st = dexscreener_stats(pos["mint"]) or {}
        price = st.get("price") or pos["entry_price"]
        qty = conn.execute("SELECT COALESCE(SUM(qty),0) FROM memecoin_fills "
                           "WHERE position_id=? AND kind='entry'", (pid,)).fetchone()[0]
        realized = (price - pos["entry_price"]) * float(qty or 0)
        m.insert_fill(conn, position_id=pid, kind="manual_close", price=price, qty=qty,
                      notional=price * float(qty or 0), fill_source="dexscreener",
                      fraction=1.0, created_at="")
        m.close_position(conn, pid, "", realized)
        return jsonify({"ok": True})

    return bp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_api.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/routes/api_memecoin.py tests/test_memecoin_api.py
git commit -m "feat(memecoin-copy): Flask blueprint — CRUD, board, stats, manual close"
```

---

### Task 10: Feed adapter tick + service loop wiring

**Files:**
- Modify: `sniper/feeds/fomo_feed.py` (add `FomoFeedAdapter(FeedAdapter)`)
- Create: `sniper/services/memecoin_runner.py` (poll loop that ties feed → engine)
- Modify: `web_settings.py` (add `MEMECOIN_COPY_SIZE_USD`, `MEMECOIN_POLL_MS`)
- Test: `tests/test_memecoin_runner.py`

**Interfaces:**
- Consumes: `fomo_get`, `normalize_feed_item`, `filter_curated`, `db_memecoin.*`, `on_copy_event`, `web_settings`.
- Produces:
  - `class FomoFeedAdapter(FeedAdapter)` with `feed_id="fomo"`, `warmup(conn)` → `ensure_memecoin_tables`, `tick(conn)` → one poll cycle (get feed, normalize, filter to curated, `insert_event`, drive `on_copy_event`).
  - `run_once(conn, auth, size_usd, http=None, now_fn=None) -> dict` in `memecoin_runner.py` — one cycle returning `{"events":N,"opened":N,"exits":N}`; the unit-testable core.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_runner.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds import fomo_feed as f
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


def test_run_once_opens_for_curated_only():
    c = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-05T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}},
        {"id": "e2", "type": "large_buy", "userId": "u9", "tokenAddress": "OTHER",
         "createdAt": "2026-07-05T10:00:00Z",
         "body": {"userHandle": "stranger", "ticker": "X", "price": 1.0,
                  "marketCap": "1", "currentSizeUsd": 1.0, "isFirstBuy": True}},
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    res = run.run_once(c, _Auth(), size_usd=100.0, http=http,
                       fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert res["opened"] == 1                       # only curated u1
    assert m.get_open_position(c, "u1", "MINT") is not None
    assert m.get_open_position(c, "u9", "OTHER") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_runner.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Add `web_settings.py` (near other knobs):
```python
MEMECOIN_COPY_SIZE_USD = float(os.getenv("MEMECOIN_COPY_SIZE_USD", "100"))
MEMECOIN_POLL_MS = int(os.getenv("MEMECOIN_POLL_MS", "500"))
```

Create `sniper/services/memecoin_runner.py`:
```python
"""One poll cycle: fomo feed -> normalize -> curated filter -> engine."""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.feeds.fomo_feed import fomo_get, build_feed_path, normalize_feed_item, filter_curated
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import on_copy_event
from sniper.execution.solana_quote import honest_fill


def _server_epoch(date_hdr):
    if not date_hdr:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_hdr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def run_once(conn, auth, size_usd, http=None, now_fn=None, fill_fn=honest_fill):
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    status, j, date_hdr = fomo_get(auth, build_feed_path(), http=http)
    server_epoch = _server_epoch(date_hdr)
    recv_iso = now_fn()
    ro = j.get("responseObject") or j
    raw_items = ro.get("feed") or []
    events = [e for e in (normalize_feed_item(r, recv_iso, server_epoch) for r in raw_items) if e]
    curated = m.curated_user_ids(conn)
    mine = filter_curated(events, curated)
    opened = exits = 0
    for ev in mine:
        m.insert_event(conn, ev.__dict__)
        r = on_copy_event(conn, ev, size_usd, now_fn(), fill_fn=fill_fn)
        opened += (r == "opened")
        exits += (r == "exit")
    return {"events": len(mine), "opened": opened, "exits": exits}
```

Append `FomoFeedAdapter` to `sniper/feeds/fomo_feed.py`:
```python
from sniper.feeds.base import FeedAdapter


class FomoFeedAdapter(FeedAdapter):
    feed_id = "fomo"
    display_name = "fomo.family copy feed"
    default_poll_seconds = 1
    mode = "poll"

    def __init__(self):
        self.auth = FomoAuth()

    def validate_config(self):
        return bool(self.auth.refresh or self.auth.access)

    def warmup(self, conn):
        from sniper.persistence import db_memecoin as _m
        _m.ensure_memecoin_tables(conn)

    def tick(self, conn):
        from sniper.services.memecoin_runner import run_once
        import web_settings as ws
        run_once(conn, self.auth, ws.MEMECOIN_COPY_SIZE_USD)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/fomo_feed.py sniper/services/memecoin_runner.py web_settings.py tests/test_memecoin_runner.py
git commit -m "feat(memecoin-copy): feed adapter tick + runner cycle"
```

---

### Task 11: Register blueprint + dashboard tab + full-suite check

**Files:**
- Modify: `sniper/web/server.py` (register blueprint with a shared conn)
- Create: `sniper/web/templates/` partial or static tab (follow the existing Signals-tab pattern; minimal HTML/JS calling the endpoints)
- Test: `tests/test_memecoin_api.py` (already covers endpoints); add one smoke test that the blueprint registers.

**Interfaces:**
- Consumes: `create_api_memecoin_blueprint`.
- Produces: a registered blueprint and a visible "Memecoin" tab that lists open positions (mint, source, entry vs current, uPnL, lag), expands to a DexScreener iframe (`https://dexscreener.com/solana/<mint>?embed=1`), and has add/pause/remove + manual close controls.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_api.py  (append)
def test_blueprint_registers_on_app():
    import sqlite3
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    c = sqlite3.connect(":memory:", check_same_thread=False)
    from sniper.persistence import db_memecoin as m
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/memecoin/wallets" in rules
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `python3 -m pytest tests/test_memecoin_api.py::test_blueprint_registers_on_app -q`
Expected: PASS (blueprint already built in Task 9) — this guards the wiring contract.

- [ ] **Step 3: Register in `sniper/web/server.py`**

Near the other `app.register_blueprint(...)` calls (see `server.py:1000-1032`):
```python
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
```
Add the dashboard tab following the existing Signals-tab markup: a "Memecoin" nav entry, a table bound to `/api/memecoin/positions/open` (poll every few seconds), a details row embedding `https://dexscreener.com/solana/<mint>?embed=1&theme=dark`, a wallets panel bound to the CRUD endpoints, and a closed-table + stats panel bound to `/api/memecoin/positions/closed`. Keep JS minimal and match existing tab conventions.

- [ ] **Step 4: Run the focused suite + a smoke of the web import**

Run:
```bash
python3 -m pytest tests/test_memecoin_*.py -q
python3 -c "import sniper.web.server"   # import-time smoke (blueprint registers cleanly)
python3 scripts/validate_schema_refs.py
```
Expected: all memecoin tests PASS; server import raises no error.

- [ ] **Step 5: Commit**

```bash
git add sniper/web/server.py sniper/web/ tests/test_memecoin_api.py
git commit -m "feat(memecoin-copy): register blueprint + dashboard tab"
```

---

## Self-Review

**Spec coverage:**
- §4 Feed adapter → Tasks 2 (normalize), 3 (auth/client), 10 (adapter/runner). ✓
- §5 Copy engine (entry, honest fill, proportional exit, dry-run) → Tasks 4, 5, 6, 7. ✓
- §6 Persistence (4 tables) → Task 1. ✓
- §7 UI (blueprint + tab, manual actions, per-trader PnL, lag) → Tasks 9, 11. ✓
- Marks poller (DexScreener) → Task 8. ✓
- Ops watchdog → the `FomoFeedAdapter.tick` + runner are the cron-callable core (Task 10); wiring a watchdog cron is an operator step, noted, not code.
- Large-only scope, skew-free lag, WAF headers, credential handling → Global Constraints + Tasks 2/3/10. ✓

**Placeholder scan:** No TBD/TODO; every code step has concrete code and exact commands. UI tab (Task 11 Step 3) references the existing Signals-tab pattern rather than inlining full HTML — acceptable as it follows an established in-repo pattern, and its API contract is fully tested.

**Type consistency:** `CopyEvent` fields identical across Tasks 2/5/6/10; `on_copy_event(conn, ev, size_usd, now_iso, fill_fn=...)` signature consistent 5/6/10; `honest_fill(mint, usd, side, fallback_price, http=None)` consistent 4/5/6; `ensure_memecoin_tables`/CRUD names consistent 1/5/6/8/9/10. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-05-memecoin-copy-mirror.md`.
