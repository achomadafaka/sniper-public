# Solana Swap-Mark Fastlane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send confirmed Solana swap marks to the UI and funded nomination lane within two seconds.

**Architecture:** A read-only WebSocket worker converts two-sided vault deltas into quote-denominated last-trade marks. The one-second funded fastlane converts these marks to USD, applies the existing spike filter, persists the source time, and keeps exact exit confirmation unchanged.

**Tech Stack:** Python 3, asyncio, `websockets`, SQLite, Flask ticker API, pytest.

## Global Constraints

- Use ASD-STE100 Simplified Technical English in new comments and docstrings.
- Use TDD for each task.
- Keep the two-second target from valid WebSocket receipt to UI visibility.
- Use only `MEMECOIN_SOLANA_READ_RPC_URL` for the new WebSocket worker.
- Never use `MEMECOIN_SOLANA_RPC_URL` in the new observation module.
- Keep Jupiter exact-quote cadence unchanged.
- Keep `executable_exit_allowed` authoritative for every funded executable reason.
- Keep `executable_peak_price` dependent on exact whole-bag quotes.
- Keep DexScreener as the fallback.
- Skip native-SOL Pump curves and unknown quote mints.
- Do not change funded authority, thresholds, or hot-engine behavior.
- Do not deploy, restart a process, or mutate a production database.
- Do not use Codex subagents unless the user explicitly requests them.

---

### Task 1: Retain token decimals in the Solana WebSocket tape

**Files:**
- Modify: `sniper/services/memecoin_solana_ws.py:38-164`
- Test: `tests/test_memecoin_solana_ws.py:9-67`

**Interfaces:**
- Consumes: Solana `accountNotification` frames with `tokenAmount.amount` and `tokenAmount.decimals`.
- Produces: `parse_frame(...) -> ("balance", vault, slot, amount_atomic, decimals)`.
- Produces: `VaultTape.on_balance(vault, slot, amount, decimals=None) -> dict | None`.
- Produces: complete rows with optional `decimals0` and `decimals1` fields.

- [ ] **Step 1: Write the failing parser and tape tests**

Update `_notif` and add these assertions in `tests/test_memecoin_solana_ws.py`:

```python
def _notif(sub, slot, amount, decimals=6):
    return {"params": {"subscription": sub, "result": {
        "context": {"slot": slot},
        "value": {"data": {"parsed": {"info": {"tokenAmount": {
            "amount": str(amount), "decimals": decimals,
        }}}}}}}}


def test_parse_balance_retains_token_decimals():
    assert parse_frame(_notif(99, 500, 1234, decimals=9), {99: COIN_V}) == (
        "balance", COIN_V, 500, 1234, 9)


def test_swap_row_retains_both_decimal_values():
    tape = _tape()
    tape.on_balance(COIN_V, 1, 1000, decimals=6)
    tape.on_balance(QUOTE_V, 1, 500, decimals=9)
    tape.on_balance(COIN_V, 2, 1200, decimals=6)
    row = tape.on_balance(QUOTE_V, 2, 400, decimals=9)
    assert row["decimals0"] == 6
    assert row["decimals1"] == 9


def test_missing_decimals_preserve_the_existing_hot_tape_row():
    tape = _tape()
    tape.on_balance(COIN_V, 1, 1000)
    tape.on_balance(QUOTE_V, 1, 500)
    tape.on_balance(COIN_V, 2, 1200)
    row = tape.on_balance(QUOTE_V, 2, 400)
    assert row["kind"] == "sol_swap"
    assert row["decimals0"] is None
    assert row["decimals1"] is None
```

- [ ] **Step 2: Run the tests and confirm the failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_solana_ws.py -q
```

Expected: FAIL because `parse_frame` returns four balance values and `VaultTape` drops decimals.

- [ ] **Step 3: Implement decimal retention**

Update the parser and tape with this shape:

```python
token_amount = info.get("tokenAmount") or {}
amount = int(token_amount.get("amount"))
raw_decimals = token_amount.get("decimals")
decimals = (
    raw_decimals
    if isinstance(raw_decimals, int) and not isinstance(raw_decimals, bool)
    else None)
return ("balance", vault, int(slot), amount, decimals)
```

Store decimals independently from balances:

```python
self._last = {}
self._decimals = {}
self._slot = {}

def on_balance(self, vault, slot, amount, decimals=None):
    vault = str(vault)
    if vault not in (self.coin_vault, self.quote_vault):
        return None
    if isinstance(decimals, int) and not isinstance(decimals, bool):
        self._decimals[vault] = decimals
    pre = self._last.get(vault)
    self._last[vault] = amount
```

Attach both values to `_sync_row` and `_row`:

```python
"decimals0": self._decimals.get(self.coin_vault),
"decimals1": self._decimals.get(self.quote_vault),
```

Update `run_vault_stream`:

```python
_, vault, slot, amount, decimals = parsed
row = tape.on_balance(vault, slot, amount, decimals=decimals)
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_solana_ws.py tests/test_memecoin_hot_supervisor.py -q
```

Expected: PASS. The hot supervisor must retain its existing sign and amount behavior.

- [ ] **Step 5: Commit the parser change**

```bash
git add sniper/services/memecoin_solana_ws.py tests/test_memecoin_solana_ws.py
git commit -m "feat: retain Solana vault decimals"
```

---

### Task 2: Build the isolated Solana swap-mark cache

**Files:**
- Create: `sniper/services/memecoin_solana_swap_marks.py`
- Create: `tests/test_memecoin_solana_swap_marks.py`

**Interfaces:**
- Consumes: `VaultTape` rows from Task 1.
- Produces: `PoolBinding(mint, pool, coin_vault, quote_vault, quote_mint)`.
- Produces: `build_price_book(positions, sol_conn) -> (vaults, bindings)`.
- Produces: `SolanaSwapMarkCache.record(row, binding, now_ts, observed_at) -> bool`.
- Produces: `SolanaSwapMarkCache.quote_mints() -> set[str]`.
- Produces: `SolanaSwapMarkCache.fresh_price_map(quote_usd, now_ts, max_age_s) -> dict`.
- Produces: `SolanaSwapMarkCache.invalidate() -> None`.
- Produces: `ws_url_from_read_rpc(url) -> str | None`.
- Produces: `run_price_stream_epoch(...) -> None`.

- [ ] **Step 1: Write the failing cache and book tests**

Create `tests/test_memecoin_solana_swap_marks.py` with these core cases:

```python
import asyncio
import sqlite3

import pytest

from sniper.services.memecoin_solana_amounts import USDC, WSOL
from sniper.services.memecoin_solana_swap_marks import (
    PoolBinding,
    SolanaSwapMarkCache,
    build_price_book,
    run_price_stream_epoch,
    ws_url_from_read_rpc,
)


def _binding(quote_mint=WSOL):
    return PoolBinding(
        mint="MintA", pool="PoolA", coin_vault="CoinV",
        quote_vault="QuoteV", quote_mint=quote_mint)


def _swap(**overrides):
    row = {
        "chain_id": "solana", "pool": "PoolA", "kind": "sol_swap",
        "block_no": 42, "amount0": "-2000000", "amount1": "300000000",
        "decimals0": 6, "decimals1": 9,
    }
    row.update(overrides)
    return row


def test_wsol_swap_converts_to_a_usd_mark():
    cache = SolanaSwapMarkCache()
    assert cache.record(
        _swap(), _binding(), now_ts=100.0,
        observed_at="2026-08-12T12:00:00+00:00") is True
    marks = cache.fresh_price_map({WSOL: 200.0}, now_ts=101.0, max_age_s=5.0)
    mark = marks[("solana", "MintA")]
    assert mark["price"] == pytest.approx(30.0)
    assert mark["source"] == "fastlane_sol_swap"
    assert mark["observed_at"] == "2026-08-12T12:00:00+00:00"


def test_usdc_swap_uses_the_usdc_reference():
    cache = SolanaSwapMarkCache()
    row = _swap(amount1="3000000", decimals1=6)
    cache.record(row, _binding(USDC), now_ts=100.0, observed_at="t")
    marks = cache.fresh_price_map({USDC: 1.0}, now_ts=101.0, max_age_s=5.0)
    assert marks[("solana", "MintA")]["price"] == pytest.approx(1.5)


@pytest.mark.parametrize("change", [
    {"kind": "sol_sync"},
    {"amount0": "0"},
    {"amount1": "-1"},
    {"decimals0": None},
    {"decimals1": 19},
])
def test_invalid_rows_do_not_enter_the_cache(change):
    cache = SolanaSwapMarkCache()
    assert cache.record(_swap(**change), _binding(), now_ts=100.0, observed_at="t") is False
    assert cache.quote_mints() == set()


def test_stale_and_invalidated_marks_are_absent():
    cache = SolanaSwapMarkCache()
    cache.record(_swap(), _binding(), now_ts=100.0, observed_at="t")
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=106.0, max_age_s=5.0) == {}
    cache.record(_swap(), _binding(), now_ts=110.0, observed_at="t2")
    cache.invalidate()
    assert cache.fresh_price_map({WSOL: 200.0}, now_ts=110.0, max_age_s=5.0) == {}


def test_price_book_uses_only_durable_supported_bindings():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sol_position_pools (position_id INTEGER PRIMARY KEY, mint TEXT, pool TEXT, venue_kind TEXT, resolution_tx TEXT, resolved_at TEXT)")
    conn.execute("CREATE TABLE sol_pool_vaults (pool TEXT PRIMARY KEY, mint TEXT, coin_vault TEXT, quote_vault TEXT, quote_mint TEXT, resolved_at TEXT)")
    conn.execute("INSERT INTO sol_position_pools VALUES (7,'MintA','PoolA','amm','sig','t')")
    conn.execute("INSERT INTO sol_pool_vaults VALUES ('PoolA','MintA','CoinV','QuoteV',?,'t')", (WSOL,))
    vaults, bindings = build_price_book(
        [{"id": 7, "mint": "MintA", "network_id": "solana"}], conn)
    assert set(vaults) == {"CoinV", "QuoteV"}
    assert bindings["PoolA"] == _binding()


def test_read_rpc_url_converts_without_using_a_custody_setting():
    assert ws_url_from_read_rpc("https://rpc.invalid/path?q=1") == "wss://rpc.invalid/path?q=1"
    assert ws_url_from_read_rpc("") is None


def test_stream_epoch_records_then_invalidates_on_disconnect():
    cache = SolanaSwapMarkCache()
    binding = _binding()
    seen = []

    async def stream(_vaults, on_row, **_kwargs):
        await on_row(_swap())
        seen.append(cache.quote_mints())

    asyncio.run(run_price_stream_epoch(
        {"CoinV": object()}, {"PoolA": binding}, cache,
        ws_url="wss://rpc.invalid", duration_s=1.0, stream_fn=stream,
        now_ts_fn=lambda: 100.0,
        now_iso_fn=lambda: "2026-08-12T12:00:00+00:00"))
    assert seen == [{WSOL}]
    assert cache.quote_mints() == set()
```

- [ ] **Step 2: Run the new test file and confirm the import failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_solana_swap_marks.py -q
```

Expected: FAIL because `memecoin_solana_swap_marks` does not exist.

- [ ] **Step 3: Implement the cache data types and price calculation**

Create `sniper/services/memecoin_solana_swap_marks.py` with these public shapes:

```python
from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sniper.services.memecoin_solana_amounts import USDC, WSOL
from sniper.services.memecoin_solana_ws import VaultTape, run_vault_stream

SOURCE = "fastlane_sol_swap"
QUALITY = "live"
SUPPORTED_QUOTES = frozenset({WSOL, USDC})


@dataclass(frozen=True)
class PoolBinding:
    mint: str
    pool: str
    coin_vault: str
    quote_vault: str
    quote_mint: str


@dataclass(frozen=True)
class QuoteMark:
    mint: str
    pool: str
    quote_mint: str
    quote_per_token: float
    slot: int
    observed_ts: float
    observed_at: str


class SolanaSwapMarkCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._marks = {}

    def record(self, row, binding, *, now_ts, observed_at):
        if row.get("kind") != "sol_swap" or row.get("pool") != binding.pool:
            return False
        try:
            amount0 = int(row.get("amount0"))
            amount1 = int(row.get("amount1"))
            slot = int(row.get("block_no"))
        except (TypeError, ValueError):
            return False
        decimals0 = row.get("decimals0")
        decimals1 = row.get("decimals1")
        if (not isinstance(decimals0, int) or isinstance(decimals0, bool)
                or not isinstance(decimals1, int) or isinstance(decimals1, bool)):
            return False
        if not 0 <= decimals0 <= 18 or not 0 <= decimals1 <= 18:
            return False
        if amount0 == 0 or amount1 == 0 or (amount0 > 0) == (amount1 > 0):
            return False
        if binding.quote_mint not in SUPPORTED_QUOTES:
            return False
        quote_per_token = (
            (abs(amount1) / (10 ** decimals1))
            / (abs(amount0) / (10 ** decimals0)))
        if not math.isfinite(quote_per_token) or quote_per_token <= 0:
            return False
        mark = QuoteMark(
            mint=binding.mint, pool=binding.pool, quote_mint=binding.quote_mint,
            quote_per_token=quote_per_token, slot=slot,
            observed_ts=float(now_ts), observed_at=str(observed_at))
        with self._lock:
            self._marks[("solana", binding.mint)] = mark
        return True

    def quote_mints(self):
        with self._lock:
            return {mark.quote_mint for mark in self._marks.values()}

    def fresh_price_map(self, quote_usd, *, now_ts, max_age_s):
        with self._lock:
            marks = dict(self._marks)
        out = {}
        for key, mark in marks.items():
            if float(now_ts) - mark.observed_ts > float(max_age_s):
                continue
            try:
                reference = float(quote_usd.get(mark.quote_mint))
                price = mark.quote_per_token * reference
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            out[key] = {
                "price": price, "liquidity": None, "mcap": None,
                "source": SOURCE, "quality": QUALITY,
                "observed_at": mark.observed_at,
            }
        return out

    def invalidate(self):
        with self._lock:
            self._marks.clear()
```

- [ ] **Step 4: Implement the durable book and epoch helpers**

Use one query per position in `build_price_book`. The funded book has at most seven positions.

```python
def build_price_book(positions, sol_conn):
    vaults = {}
    bindings = {}
    for pos in positions:
        if str(pos.get("network_id") or "solana") != "solana":
            continue
        row = sol_conn.execute(
            "SELECT p.pool,v.coin_vault,v.quote_vault,v.quote_mint "
            "FROM sol_position_pools p JOIN sol_pool_vaults v ON v.pool=p.pool "
            "WHERE p.position_id=? AND p.mint=?",
            (pos.get("id"), str(pos.get("mint") or "")),
        ).fetchone()
        if not row or str(row[3]) not in SUPPORTED_QUOTES:
            continue
        binding = PoolBinding(
            mint=str(pos["mint"]), pool=str(row[0]),
            coin_vault=str(row[1]), quote_vault=str(row[2]),
            quote_mint=str(row[3]))
        tape = VaultTape(binding.coin_vault, binding.quote_vault, mint=binding.pool)
        vaults[binding.coin_vault] = tape
        vaults[binding.quote_vault] = tape
        bindings[binding.pool] = binding
    return vaults, bindings


def ws_url_from_read_rpc(url):
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parsed.scheme.lower())
    if scheme is None or not parsed.netloc:
        return None
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


async def run_price_stream_epoch(
        vaults, bindings, cache, *, ws_url, duration_s=30.0,
        stream_fn=run_vault_stream, now_ts_fn=time.monotonic,
        now_iso_fn=None):
    if now_iso_fn is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso_fn = real_utc_now_iso

    async def on_row(row):
        binding = bindings.get(str(row.get("pool") or ""))
        if binding is not None:
            cache.record(
                row, binding, now_ts=now_ts_fn(), observed_at=now_iso_fn())

    try:
        await asyncio.wait_for(
            stream_fn(vaults, on_row, ws_url=ws_url), timeout=float(duration_s))
    except asyncio.TimeoutError:
        return
    finally:
        cache.invalidate()
```

- [ ] **Step 5: Run the cache and existing WebSocket tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_solana_swap_marks.py tests/test_memecoin_solana_ws.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the cache module**

```bash
git add sniper/services/memecoin_solana_swap_marks.py tests/test_memecoin_solana_swap_marks.py
git commit -m "feat: cache confirmed Solana swap marks"
```

---

### Task 3: Merge swap marks into the funded fastlane and preserve source time

**Files:**
- Modify: `sniper/services/memecoin_fastlane.py:261-344`
- Modify: `sniper/services/memecoin_exits.py:214-294`
- Modify: `tests/test_memecoin_fastlane_mid.py:22-80`
- Modify: `tests/test_memecoin_exits.py:714-810`

**Interfaces:**
- Consumes: `SolanaSwapMarkCache.quote_mints()` and `fresh_price_map(...)` from Task 2.
- Produces: `MidObservationState.observe(key, stats, observed_at) -> str`.
- Produces: `_batched_mid_snapshot(...) -> (marks, quote_usd)`.
- Preserves: `_batched_mid_marks(...) -> dict` for existing callers.
- Extends: `refresh_open_positions(..., swap_mark_cache=None, mid_observation_state=None, now_ts_fn=None, now_iso_fn=None, swap_max_age_s=5.0)`.
- Extends: `write_marks` accepts an optional `observed_at` value in each mark.

- [ ] **Step 1: Write failing fastlane merge tests**

Add these tests to `tests/test_memecoin_fastlane_mid.py`:

```python
from sniper.services.memecoin_solana_amounts import WSOL
from sniper.services.memecoin_solana_swap_marks import PoolBinding, SolanaSwapMarkCache


def _live_cache(now_ts=100.0):
    cache = SolanaSwapMarkCache()
    cache.record({
        "pool": "PoolA", "kind": "sol_swap", "block_no": 8,
        "amount0": "-2000000", "amount1": "300000000",
        "decimals0": 6, "decimals1": 9,
    }, PoolBinding("M1", "PoolA", "CoinV", "QuoteV", WSOL),
        now_ts=now_ts, observed_at="2026-08-12T12:00:00+00:00")
    return cache


def test_fresh_swap_mark_replaces_dex_in_one_batch_call(monkeypatch):
    calls = []
    monkeypatch.setattr(fl, "open_real_positions", lambda _conn, limit=None: [_pos(1, "M1")])
    cache = _live_cache()

    def batch(keys):
        calls.append(list(keys))
        return {
            ("solana", "M1"): {"price": 10.0, "liquidity": 900.0, "market_cap": 500.0},
            ("solana", WSOL): {"price": 200.0},
        }

    out = fl.refresh_open_positions(
        None, {}, mid_batch_fn=batch, swap_mark_cache=cache,
        now_ts_fn=lambda: 101.0,
        now_iso_fn=lambda: "2026-08-12T12:00:01+00:00")
    assert len(calls) == 1
    assert set(calls[0]) == {("solana", "M1"), ("solana", WSOL)}
    assert out[("solana", "M1")]["price"] == pytest.approx(30.0)
    assert out[("solana", "M1")]["source"] == "fastlane_sol_swap"


def test_stale_swap_mark_uses_dex_fallback(monkeypatch):
    monkeypatch.setattr(fl, "open_real_positions", lambda _conn, limit=None: [_pos(1, "M1")])
    out = fl.refresh_open_positions(
        None, {}, swap_mark_cache=_live_cache(), now_ts_fn=lambda: 106.0,
        mid_batch_fn=lambda keys: {key: {"price": 10.0} for key in keys})
    assert out[("solana", "M1")]["price"] == 10.0
    assert out[("solana", "M1")]["source"] == fl.MID_SOURCE


def test_repeated_dex_values_keep_the_first_observation_time():
    state = fl.MidObservationState()
    first = state.observe(
        ("solana", "M1"), {"price": 1.0, "liquidity": 2.0, "mcap": 3.0}, "t1")
    second = state.observe(
        ("solana", "M1"), {"price": 1.0, "liquidity": 2.0, "mcap": 3.0}, "t2")
    changed = state.observe(
        ("solana", "M1"), {"price": 1.1, "liquidity": 2.0, "mcap": 3.0}, "t3")
    assert (first, second, changed) == ("t1", "t1", "t3")
```

Import `pytest` at the top of the file.

- [ ] **Step 2: Write failing source-time persistence tests**

Add these tests to `tests/test_memecoin_exits.py`:

```python
def test_write_marks_uses_a_valid_source_observation_time():
    from sniper.services.memecoin_exits import write_marks
    c = _conn()
    _open(c, "alpha_b", entry=1.0)
    write_marks(c, {("solana", "M"): {
        "price": 1.1, "source": "fastlane_sol_swap", "quality": "live",
        "observed_at": "2026-07-07T10:00:01+00:00",
    }}, "2026-07-07T10:00:02+00:00", min_interval_s=0, open_min_interval_s=0)
    row = c.execute(
        "SELECT sweep_ts,source FROM memecoin_position_marks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == ("2026-07-07T10:00:01+00:00", "fastlane_sol_swap")


def test_write_marks_rejects_a_future_source_observation_time():
    from sniper.services.memecoin_exits import write_marks
    c = _conn()
    _open(c, "alpha_b", entry=1.0)
    write_marks(c, {("solana", "M"): {
        "price": 1.1, "observed_at": "2026-07-07T10:00:03+00:00",
    }}, "2026-07-07T10:00:02+00:00", min_interval_s=0, open_min_interval_s=0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 0


def test_swap_mark_is_not_blocked_by_the_dex_mid_write_floor():
    from sniper.services.memecoin_exits import write_marks
    c = _conn()
    write_marks(c, {("solana", "M"): {
        "price": 1.0, "source": "fastlane_mid",
    }}, "2026-07-07T10:00:00.500000+00:00", min_interval_s=1, open_min_interval_s=1)
    write_marks(c, {("solana", "M"): {
        "price": 1.1, "source": "fastlane_sol_swap",
        "observed_at": "2026-07-07T10:00:01+00:00",
    }}, "2026-07-07T10:00:01+00:00", min_interval_s=1, open_min_interval_s=1)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 2
```

- [ ] **Step 3: Run the tests and confirm the failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_fastlane_mid.py tests/test_memecoin_exits.py -q
```

Expected: FAIL because the merge, observation state, and per-mark timestamp do not exist.

- [ ] **Step 4: Implement stable DexScreener observation times**

Add this state to `sniper/services/memecoin_fastlane.py`:

```python
class MidObservationState:
    def __init__(self):
        self._last = {}

    def observe(self, key, stats, observed_at):
        fingerprint = (
            stats.get("price"), stats.get("liquidity"), stats.get("mcap"))
        prior = self._last.get(key)
        if prior is not None and prior[0] == fingerprint:
            return prior[1]
        stamp = str(observed_at)
        self._last[key] = (fingerprint, stamp)
        return stamp
```

Split the current batch function into a snapshot helper and its compatibility wrapper:

```python
def _batched_mid_snapshot(
        positions, *, quote_mints=(), batch_fn=None,
        observation_state=None, now_iso_fn=None):
    position_keys = sorted({
        (str(p.get("network_id") or "solana"), str(p.get("mint") or ""))
        for p in positions if mid_scope(p.get("network_id")) and p.get("mint")})
    quote_keys = {("solana", str(mint)) for mint in quote_mints if mint}
    request_keys = sorted(set(position_keys) | quote_keys)
    if not request_keys:
        return {}, {}
    if batch_fn is None:
        from sniper.execution.solana_quote import dexscreener_stats_batch_keyed as batch_fn
    try:
        stats = batch_fn(request_keys) or {}
    except Exception:
        logger.exception("fast lane: batched mid fetch failed (%d mint(s))", len(request_keys))
        return {}, {}
    now_iso_fn = now_iso_fn or (lambda: None)
    marks = {}
    for key in position_keys:
        raw = stats.get(key) or {}
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        mark = {
            "price": price, "liquidity": raw.get("liquidity"),
            "mcap": raw.get("market_cap"), "source": MID_SOURCE,
            "quality": MID_QUALITY,
        }
        if observation_state is not None:
            mark["observed_at"] = observation_state.observe(key, mark, now_iso_fn())
        marks[key] = mark
    quote_usd = {}
    for mint in quote_mints:
        try:
            price = float((stats.get(("solana", str(mint))) or {}).get("price"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            quote_usd[str(mint)] = price
    return marks, quote_usd


def _batched_mid_marks(positions, *, batch_fn=None):
    marks, _quote_usd = _batched_mid_snapshot(positions, batch_fn=batch_fn)
    return marks
```

- [ ] **Step 5: Implement swap-mark precedence**

Extend `refresh_open_positions` with the specified optional arguments. Apply the sources in this order:

```python
now_ts_fn = now_ts_fn or time.monotonic
quote_mints = swap_mark_cache.quote_mints() if swap_mark_cache is not None else set()
mid_marks, quote_usd = _batched_mid_snapshot(
    positions, quote_mints=quote_mints, batch_fn=mid_batch_fn,
    observation_state=mid_observation_state, now_iso_fn=now_iso_fn)
out.update(mid_marks)
if swap_mark_cache is not None:
    out.update(swap_mark_cache.fresh_price_map(
        quote_usd, now_ts=now_ts_fn(), max_age_s=swap_max_age_s))
```

Keep the existing EVM exact-preview loop after this block. The exact mark must overwrite a mid or swap mark.

- [ ] **Step 6: Implement per-mark observation timestamps**

Give swap marks their own persistence class before you change the timestamp logic:

```python
_EXECUTABLE_SOURCES = ("fastlane_rpc",)
_SWAP_SOURCES = ("fastlane_sol_swap",)


def _source_class(source):
    if (source or "") in _EXECUTABLE_SOURCES:
        return "executable"
    if (source or "") in _SWAP_SOURCES:
        return "swap"
    return "mid"
```

This prevents a DexScreener mark from delaying a valid swap mark under the one-second write floor.

Then select and validate the candidate timestamp before the floor check:

```python
candidate_iso = (st or {}).get("observed_at") or now_iso
candidate_dt = _parse(candidate_iso)
if candidate_dt is None:
    continue
if now_dt is None:
    now_dt = _parse(now_iso)
if (st or {}).get("observed_at") and (now_dt is None or candidate_dt > now_dt):
    continue
prev = last.get((chain_id, mint, _source_class(st.get("source"))))
if prev is not None and candidate_dt <= prev:
    continue
if prev is not None and (candidate_dt - prev).total_seconds() < floor:
    continue
```

Use `candidate_iso` in the insert tuple instead of `now_iso`.

- [ ] **Step 7: Run the fastlane and mark-writer tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_fastlane.py tests/test_memecoin_fastlane_mid.py tests/test_memecoin_exits.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the fastlane merge**

```bash
git add sniper/services/memecoin_fastlane.py sniper/services/memecoin_exits.py tests/test_memecoin_fastlane_mid.py tests/test_memecoin_exits.py
git commit -m "feat: merge Solana swap marks into the fastlane"
```

---

### Task 4: Wire the read-only price worker into the pinned runner

**Files:**
- Modify: `sniper/settings/memecoin.py:455-490`
- Modify: `scripts/memecoin_runner_loop.py:502-613`
- Modify: `tests/test_memecoin_fastlane.py:68-77`
- Modify: `tests/test_memecoin_fastlane_decides.py:31-66`
- Modify: `tests/test_memecoin_runner_loop_cadence.py:69-76`

**Interfaces:**
- Consumes: Task 2 cache, book, URL, and epoch helpers.
- Consumes: Task 3 `refresh_open_positions` optional cache and observation state.
- Produces: `MEMECOIN_SOLANA_SWAP_MARK_ENABLED: bool` with default `True`.
- Produces: one daemon worker named `memecoin-solana-swap-mark`.
- Preserves: one DexScreener batch and one funded sweep per fastlane pass.

- [ ] **Step 1: Write the failing setting and wiring tests**

Add this assertion to `tests/test_memecoin_fastlane.py`:

```python
def test_solana_swap_mark_lane_defaults_on_but_needs_the_fastlane():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_SOLANA_SWAP_MARK_ENABLED is True
    assert cfg.MEMECOIN_FASTLANE_ENABLED is False
```

Add these source checks to `tests/test_memecoin_fastlane_decides.py`:

```python
def test_swap_mark_worker_uses_only_the_read_rpc_and_readonly_connections():
    src = open("scripts/memecoin_runner_loop.py").read()
    worker = src.split("def _solana_swap_mark_worker():")[1].split(
        "def _fastlane_worker():")[0]
    assert "solana_read_rpc()" in worker
    assert "connect_sol_tape_readonly" in worker
    assert "connect_observer_main" in worker
    assert "MEMECOIN_SOLANA_RPC_URL" not in worker
    assert "get_connection" not in worker


def test_fastlane_pass_uses_the_shared_swap_cache_without_an_extra_fetch():
    src = open("scripts/memecoin_runner_loop.py").read()
    fast = src.split("def _fastlane_worker():")[1].split("if _CAP_V2")[0]
    assert "swap_mark_cache=_swap_mark_cache" in fast
    assert "mid_observation_state" in fast
    assert "price_fn=(lambda _mints, _pm=pm: _pm)" in fast
```

Extend the existing static scope test. It must accept the new scoped setting names without an undefined-name error.

- [ ] **Step 2: Run the tests and confirm the failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_fastlane.py tests/test_memecoin_fastlane_decides.py tests/test_memecoin_runner_loop_cadence.py -q
```

Expected: FAIL because the setting and worker do not exist.

- [ ] **Step 3: Add the rollback setting**

Add this setting after `MEMECOIN_FASTLANE_ENABLED`:

```python
# Confirmed Solana swap marks use the read-only vault WebSocket. The lane changes only the nominal
# mark source. The funded exact-confirmation gate stays authoritative. Set 0 for DexScreener-only.
MEMECOIN_SOLANA_SWAP_MARK_ENABLED = os.getenv(
    "MEMECOIN_SOLANA_SWAP_MARK_ENABLED", "1"
).strip().lower() in ("1", "on", "true", "yes")
```

- [ ] **Step 4: Create the shared cache and observation state**

Near the existing fastlane state in `main`, add:

```python
from sniper.services.memecoin_fastlane import MidObservationState
from sniper.services.memecoin_solana_swap_marks import SolanaSwapMarkCache

_swap_mark_cache = SolanaSwapMarkCache()
_mid_observation_state = MidObservationState()
```

Import the two settings with local names:

```python
from sniper.settings.memecoin import (
    MEMECOIN_FASTLANE_ENABLED as _FASTLANE,
    MEMECOIN_FASTLANE_TICK_S as _FASTLANE_TICK_S,
    MEMECOIN_SOLANA_SWAP_MARK_ENABLED as _SOL_SWAP_MARKS,
)
```

- [ ] **Step 5: Implement the read-only WebSocket worker**

Place the worker before `_fastlane_worker`:

```python
def _solana_swap_mark_worker():
    import asyncio
    from sniper.persistence import db_ticks
    from sniper.services.memecoin_fastlane import open_real_positions
    from sniper.services.memecoin_solana_swap_marks import (
        build_price_book, run_price_stream_epoch, ws_url_from_read_rpc)
    from sniper.settings.memecoin import solana_read_rpc

    observer = connect_observer_main()
    try:
        sol_conn = db_ticks.connect_sol_tape_readonly()
    except Exception:
        _p("[warn] Solana swap-mark lane: tape database unavailable; DexScreener fallback active")
        return
    ws_url = ws_url_from_read_rpc(solana_read_rpc())
    if ws_url is None:
        _p("[warn] Solana swap-mark lane: read WebSocket unavailable; DexScreener fallback active")
        return
    while True:
        try:
            positions = open_real_positions(observer)
            vaults, bindings = build_price_book(positions, sol_conn)
            if not vaults:
                _swap_mark_cache.invalidate()
                time.sleep(5.0)
                continue
            asyncio.run(run_price_stream_epoch(
                vaults, bindings, _swap_mark_cache,
                ws_url=ws_url, duration_s=30.0))
        except Exception:
            _swap_mark_cache.invalidate()
            _p("[warn] Solana swap-mark lane: socket fault; DexScreener fallback active")
            time.sleep(1.0)
```

Do not log `ws_url` or an endpoint value.

- [ ] **Step 6: Start the worker and pass its cache to the funded fastlane**

Start the worker only under both effective gates:

```python
if _FASTLANE and _SOL_SWAP_MARKS:
    threading.Thread(
        target=_solana_swap_mark_worker,
        name="memecoin-solana-swap-mark", daemon=True).start()
    _p("[memecoin] Solana swap-mark lane: read-only thread started")
```

Store the observation state in `_setup` and extend the refresh call:

```python
"mid_observation_state": _mid_observation_state,
```

```python
pm = resource["refresh_fn"](
    fconn, contexts, preview_by_position=previews,
    swap_mark_cache=_swap_mark_cache if _SOL_SWAP_MARKS else None,
    mid_observation_state=resource["mid_observation_state"],
    now_iso_fn=resource["now_fn"])
```

Keep `filter_implausible_ticks` before `write_marks`. Keep the same filtered `pm` for `real_exit_sweep`.

- [ ] **Step 7: Run the runner wiring tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_fastlane.py tests/test_memecoin_fastlane_decides.py tests/test_memecoin_runner_loop_cadence.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the runner wiring**

```bash
git add sniper/settings/memecoin.py scripts/memecoin_runner_loop.py tests/test_memecoin_fastlane.py tests/test_memecoin_fastlane_decides.py tests/test_memecoin_runner_loop_cadence.py
git commit -m "feat: start the Solana swap-mark worker"
```

---

### Task 5: Prove funded safety and complete verification

**Files:**
- Modify: `tests/test_memecoin_real_peak_tracking.py:43-96`
- Modify: `tests/test_memecoin_real_runner.py:300-420`
- Modify: `tests/test_memecoin_real_status.py:335-378`
- Verify: `docs/superpowers/specs/2026-08-12-memecoin-solana-swap-mark-fastlane-design.md`

**Interfaces:**
- Consumes: the `fastlane_sol_swap` price-map shape from Tasks 2 and 3.
- Proves: nominal peaks and exact peaks remain separate.
- Proves: a nominal stop cannot execute without a fresh exact whole-bag preview.
- Proves: the ticker reads the new source from the local mark tape.

- [ ] **Step 1: Add the source-specific peak regression**

Add this test to `tests/test_memecoin_real_peak_tracking.py`:

```python
def test_swap_mark_updates_only_the_nominal_peak(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    c = _c()
    pid = _open_real(c)
    c.execute(
        "UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (1.0, pid))
    c.commit()
    context = {"solana": {
        "exit_fn": lambda *args: pytest.fail("the modest mark must not exit"),
        "preview_exit_fn": lambda _pos: None,
    }}
    rr.real_exit_sweep(c, context, price_fn=lambda _mints: {("solana", "So1"): {
        "price": 1.20, "source": "fastlane_sol_swap", "quality": "live",
    }}, now_fn=lambda: NOW)
    pos = m.get_position(c, pid)
    assert pos["peak_price"] == pytest.approx(1.20)
    assert pos["executable_peak_price"] == pytest.approx(1.0)
```

- [ ] **Step 2: Add the exact-confirmation regression**

Update `test_exact_policy_failed_quote_still_nominates_loss_rail` in `tests/test_memecoin_real_runner.py`. Give its mark the new source:

```python
pmap = {norm_mint("MintFailQuote"): {
    "price": 0.30, "liquidity": 1e9,
    "source": "fastlane_sol_swap", "quality": "live",
}}
```

Keep these assertions unchanged:

```python
assert audit and audit[0]["allowed"] == 0
assert calls == []
```

- [ ] **Step 3: Bind the ticker regression to the new source**

In `test_real_ticker_uses_fresh_local_mark_without_network_calls`, change the newest source value:

```python
"('4663','0xFast','2026-07-09T00:00:02Z',1.2,1000,NULL,'live','fastlane_sol_swap')"
```

Keep the expected quote time, price, market cap, and PnL assertions unchanged.

- [ ] **Step 4: Run the safety tests**

Run:

```bash
python3 -m pytest tests/test_memecoin_real_peak_tracking.py tests/test_memecoin_real_runner.py tests/test_memecoin_real_status.py tests/test_memecoin_engine_authoritative_exit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the safety regressions**

```bash
git add tests/test_memecoin_real_peak_tracking.py tests/test_memecoin_real_runner.py tests/test_memecoin_real_status.py
git commit -m "test: protect the funded swap-mark boundary"
```

- [ ] **Step 6: Run the full focused suite**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_solana_ws.py \
  tests/test_memecoin_solana_swap_marks.py \
  tests/test_memecoin_hot_supervisor.py \
  tests/test_memecoin_fastlane.py \
  tests/test_memecoin_fastlane_mid.py \
  tests/test_memecoin_fastlane_supervision.py \
  tests/test_memecoin_fastlane_decides.py \
  tests/test_memecoin_exits.py \
  tests/test_memecoin_real_peak_tracking.py \
  tests/test_memecoin_real_runner.py \
  tests/test_memecoin_real_status.py \
  tests/test_memecoin_engine_authoritative_exit.py \
  tests/test_memecoin_runner_loop_cadence.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 7: Run static verification**

Run:

```bash
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper/services scripts/memecoin_runner_loop.py
git diff --check 424e6eb5..HEAD
git status --short --branch
```

Expected:

- The schema reference check passes.
- Python compilation passes.
- The diff check returns no output.
- The branch contains only the planned commits.

- [ ] **Step 8: Report the deployment boundary**

Report these facts:

1. The code and offline tests are complete.
2. The two-second live target is not yet proved.
3. No process restart or deployment occurred.
4. A pinned deployment needs separate approval.
5. Live proof must include counts, median, p90, fallback count, reconnect count, and exact-quote load.

Do not claim live latency from fixtures.
