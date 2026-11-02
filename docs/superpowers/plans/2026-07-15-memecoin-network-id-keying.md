# Memecoin `(network_id, mint)` keying — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development per task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give the four chain-ambiguous memecoin tables (`quote_ledger`, `alert_latency`, `exec_latency`; `seen_sells` documented-safe) an explicit `network_id` so a `0x` mint can never be read/written as the wrong chain.

**Architecture:** Nullable `network_id TEXT` added idempotently in `db_memecoin.ensure_memecoin_tables`; writers record the chain they already have; a one-time batched ops script backfills history unambiguous-only from `memecoin_events`/`memecoin_pending_entries`; the one research read stops hardcoding `solana`.

**Tech Stack:** Python 3, SQLite (WAL), pytest.

## Global Constraints (verbatim from spec)

- Real-money DB (`sniper_tracker.db`). Back up before any mutation; never break the exit/mark sweep. The ledger is **observability-only** — not in the exit money path.
- No table rewrites / no `UNIQUE`-constraint rebuilds (guardrail: don't lock the DB vs the live 15 s sweep).
- `NULL` = unknown. **Never coerce unknown to a default chain.** Leave unresolvable rows `NULL` and count them.
- Backfill is unambiguous-only, mirroring `resolve_network_from_events`.
- No raw SQL outside `db.py` for app code (the one-time backfill is an ops script — prior art: `scripts/research/memecoin_backfill_evm_unknown.py`, commit b45523e1).
- Real-UTC timestamps (`sniper.util.clock.real_utc_now_iso`), never host clock.
- `python3 scripts/validate_schema_refs.py` + `python3 scripts/brain_index.py generate` before committing.
- `memecoin_seen_sells` is NOT migrated — chain-safe via globally-unique `swap_id`; documented, no churn.

**Insertion points (verified line numbers, `sniper/persistence/db_memecoin.py`):** quote_ledger CREATE at 420 / index at 431; `_el_cols` (exec_latency) at 492; `al_cols` (alert_latency) at 639; writer `record_quote_observations` at 1499 (mint extraction at ~1509); `record_alert` at ~2429; `record_exec_latency` at 1726; `resolve_network_from_events` at 1585. Research read `_legacy_quotes` at `scripts/research/memecoin_pattern_study.py:227`.

---

### Task 1: `quote_ledger` schema — add `network_id` + covering index

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (after line 431, the `idx_mc_quotes_mint_ts` create)
- Test: `tests/test_memecoin_network_id_keying.py` (new)

**Interfaces:**
- Produces: `memecoin_quote_ledger.network_id TEXT` (nullable); index `idx_mc_quotes_net_mint_ts (network_id, mint, ts)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_network_id_keying.py
import sqlite3
import sniper.persistence.db_memecoin as m


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_quote_ledger_has_network_id_column_and_index():
    c = _mem_db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_quote_ledger)")}
    assert "network_id" in cols
    idx = {r[1] for r in c.execute("PRAGMA index_list(memecoin_quote_ledger)")}
    assert "idx_mc_quotes_net_mint_ts" in idx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py::test_quote_ledger_has_network_id_column_and_index -q`
Expected: FAIL (`network_id` not in cols).

- [ ] **Step 3: Implement the migration**

In `sniper/persistence/db_memecoin.py`, immediately after the existing line
`conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_mint_ts ON memecoin_quote_ledger(mint, ts)")`, add:

```python
    # network provenance (2026-07-15): a 0x mint is not a coin identity across EVM chains. The
    # sweep price_map is already (network_id, mint)-keyed; record it so per-chain research reads
    # can't conflate chains. Nullable — NULL means unknown, never a coerced default chain.
    _ql_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_quote_ledger)")}
    if "network_id" not in _ql_cols:
        conn.execute("ALTER TABLE memecoin_quote_ledger ADD COLUMN network_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_net_mint_ts "
                 "ON memecoin_quote_ledger(network_id, mint, ts)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py::test_quote_ledger_has_network_id_column_and_index -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memecoin_network_id_keying.py sniper/persistence/db_memecoin.py
git commit -m "feat(memecoin): add network_id column + covering index to quote_ledger"
```

---

### Task 2: `quote_ledger` writer — stop discarding the chain

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (`record_quote_observations`, ~1499-1518)
- Test: `tests/test_memecoin_network_id_keying.py`

**Interfaces:**
- Consumes: `record_quote_observations(conn, price_map, now_iso, source="dexscreener")` where `price_map` keys are `(network_id, mint)` tuples OR bare `mint` strings.
- Produces: ledger rows carry `network_id = key[0]` for tuple keys, `NULL` for bare-mint keys.

- [ ] **Step 1: Write the failing test**

```python
def test_writer_preserves_network_id_for_tuple_keys_and_null_for_bare():
    c = _mem_db()
    pm = {
        ("8453", "0xSAME"): {"price": 1.0, "liquidity": 100.0},
        ("4663", "0xSAME"): {"price": 2.0, "liquidity": 200.0},
        "SOLBARE": {"price": 3.0},                       # legacy bare-mint key
    }
    assert m.record_quote_observations(c, pm, "2026-07-15T20:00:05+00:00") == 3
    rows = {(r["network_id"], r["mint"]): r["price"]
            for r in c.execute("SELECT network_id, mint, price FROM memecoin_quote_ledger")}
    assert rows[("8453", "0xSAME")] == 1.0
    assert rows[("4663", "0xSAME")] == 2.0          # same mint, two chains, both distinguishable
    assert rows[(None, "SOLBARE")] == 3.0           # bare key -> NULL, never coerced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py::test_writer_preserves_network_id_for_tuple_keys_and_null_for_bare -q`
Expected: FAIL (writer discards `key[0]`; also the two-chain rows collide today because network isn't written — the dict comprehension keys collapse).

- [ ] **Step 3: Implement**

Replace the loop body + INSERT in `record_quote_observations`. Current:

```python
    minute = (now_iso or "")[:16]
    for key, st in (price_map or {}).items():
        mint = key[1] if isinstance(key, tuple) and len(key) == 2 else key
        price = (st or {}).get("price")
        if not price or price <= 0:
            continue
        rows.append((mint, minute, now_iso, float(price), (st or {}).get("liquidity"),
                     source, None))
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO memecoin_quote_ledger(mint, ts_minute, ts, price, liquidity, "
        "source, quality) VALUES (?,?,?,?,?,?,?)", rows)
```

New:

```python
    minute = (now_iso or "")[:16]
    for key, st in (price_map or {}).items():
        is_tuple = isinstance(key, tuple) and len(key) == 2
        mint = key[1] if is_tuple else key
        network_id = key[0] if is_tuple else None   # NULL for legacy bare-mint keys — never defaulted
        price = (st or {}).get("price")
        if not price or price <= 0:
            continue
        rows.append((mint, minute, now_iso, float(price), (st or {}).get("liquidity"),
                     source, None, network_id))
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO memecoin_quote_ledger(mint, ts_minute, ts, price, liquidity, "
        "source, quality, network_id) VALUES (?,?,?,?,?,?,?,?)", rows)
```

> Note on the residual `UNIQUE(mint, ts_minute)`: the two same-mint/same-minute rows above use
> DIFFERENT minutes only if their ts differ; in the test they share a minute but the INSERT OR
> IGNORE keeps the FIRST (`8453`). To make the two-chain assertion pass deterministically, give
> each a distinct minute in the test OR assert on the row that wins. **Use distinct minutes in the
> test** (`20:00` vs `20:01`) so both persist — this documents the residual honestly.

- [ ] **Step 3b: Fix the test to use distinct minutes (documents the UNIQUE residual)**

```python
def test_writer_preserves_network_id_for_tuple_keys_and_null_for_bare():
    c = _mem_db()
    assert m.record_quote_observations(
        c, {("8453", "0xSAME"): {"price": 1.0}}, "2026-07-15T20:00:05+00:00") == 1
    assert m.record_quote_observations(
        c, {("4663", "0xSAME"): {"price": 2.0}}, "2026-07-15T20:01:05+00:00") == 1
    assert m.record_quote_observations(
        c, {"SOLBARE": {"price": 3.0}}, "2026-07-15T20:02:05+00:00") == 1
    rows = {(r["network_id"], r["mint"]): r["price"]
            for r in c.execute("SELECT network_id, mint, price FROM memecoin_quote_ledger")}
    assert rows[("8453", "0xSAME")] == 1.0
    assert rows[("4663", "0xSAME")] == 2.0
    assert rows[(None, "SOLBARE")] == 3.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q`
Expected: PASS.

- [ ] **Step 5: Run the existing writer regression**

Run: `python3 -m pytest tests/test_memecoin_real_deploy.py -q -k quote`
Expected: PASS (existing `record_quote_observations == 2 / 0 / 2` assertions unaffected — they use bare-mint keys, now written with `NULL` network).

- [ ] **Step 6: Commit**

```bash
git add tests/test_memecoin_network_id_keying.py sniper/persistence/db_memecoin.py
git commit -m "fix(memecoin): quote_ledger writer records network_id instead of discarding key[0]"
```

---

### Task 3: `alert_latency` + `exec_latency` schema + writers (low tier)

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (exec_latency ensure ~492; alert_latency ensure ~639; `record_alert` ~2429; `record_exec_latency` 1726)
- Modify: `sniper/services/memecoin_real_runner.py` (the `record_exec_latency(...)` call site — pass `network_id`)
- Test: `tests/test_memecoin_network_id_keying.py`

**Interfaces:**
- Produces: `memecoin_alert_latency.network_id TEXT`, `memecoin_exec_latency.network_id TEXT`.
- `record_alert(conn, fields)` reads `fields.get("network_id")` (no signature change).
- `record_exec_latency(conn, *, mint, pending_id, ..., network_id=None)`.

- [ ] **Step 1: Write the failing test**

```python
def test_alert_and_exec_latency_have_network_id():
    c = _mem_db()
    for t in ("memecoin_alert_latency", "memecoin_exec_latency"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
        assert "network_id" in cols, t


def test_record_alert_and_exec_persist_network_id():
    c = _mem_db()
    m.record_alert(c, {"fomo_event_id": "E1", "mint": "0xA", "network_id": "8453"})
    assert c.execute("SELECT network_id FROM memecoin_alert_latency WHERE fomo_event_id='E1'"
                     ).fetchone()[0] == "8453"
    m.record_exec_latency(c, mint="0xA", pending_id=1, status="filled", sig="s",
                          timing={}, at="2026-07-15T20:00:00+00:00", network_id="4663")
    assert c.execute("SELECT network_id FROM memecoin_exec_latency WHERE mint='0xA'"
                     ).fetchone()[0] == "4663"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q -k "latency"`
Expected: FAIL (column missing / kwarg unknown).

- [ ] **Step 3: Implement schema**

After `_el_cols = {...}` (exec_latency, ~492), before/after the `impact_pct` guard:

```python
    if "network_id" not in _el_cols:
        conn.execute("ALTER TABLE memecoin_exec_latency ADD COLUMN network_id TEXT")
```

After `al_cols = {...}` (alert_latency, ~639), next to the `local_ms` guard:

```python
    if "network_id" not in al_cols:
        conn.execute("ALTER TABLE memecoin_alert_latency ADD COLUMN network_id TEXT")
```

- [ ] **Step 4: Implement writers**

`record_alert` — add `network_id` to the column list and values tuple:

```python
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_alert_latency(fomo_event_id, source_user_id, handle, "
        "mint, ticker, their_size_usd, market_cap, fomo_created_at, detected_at, lag_s, "
        "signal_source, network_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fields["fomo_event_id"], fields.get("source_user_id"), fields.get("handle"),
         fields.get("mint"), fields.get("ticker"), fields.get("their_size_usd"),
         fields.get("market_cap"), fields.get("fomo_created_at"), fields.get("detected_at"),
         fields.get("lag_s"), fields.get("signal_source") or "global_feed",
         fields.get("network_id")))
```

`record_exec_latency` — add the kwarg and column:

```python
def record_exec_latency(conn, *, mint, pending_id, status, sig, timing, at, impact_pct=None,
                        network_id=None):
    """... (unchanged docstring) ..."""
    conn.execute(
        "INSERT INTO memecoin_exec_latency(mint, pending_id, status, sig, gate_ms, safety_ms, "
        "exec_ms, total_ms, at, impact_pct, network_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mint, pending_id, status, sig, timing.get("gate_ms"), timing.get("safety_ms"),
         timing.get("exec_ms"), timing.get("total_ms"), at, impact_pct, network_id))
    conn.commit()
```

- [ ] **Step 5: Pass `network_id` at the real-runner call site**

Find the `record_exec_latency(` call in `sniper/services/memecoin_real_runner.py` and add
`network_id=<the chain var already in scope for that entry>` (e.g. `network_id=network_id` or
`pos.get("network_id")` / `pending.get("network_id")` — use whatever the surrounding code already
holds). If no chain var is in scope at that point, leave the call unchanged (backfill covers the
36 rows); do NOT invent one. Verify with:

Run: `grep -n "record_exec_latency" sniper/services/memecoin_real_runner.py`

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_memecoin_network_id_keying.py sniper/persistence/db_memecoin.py sniper/services/memecoin_real_runner.py
git commit -m "feat(memecoin): network_id on alert_latency + exec_latency instruments"
```

---

### Task 4: research read — `_legacy_quotes` stops hardcoding `solana`

**Files:**
- Modify: `scripts/research/memecoin_pattern_study.py` (`_legacy_quotes`, ~227)
- Test: `tests/test_memecoin_network_id_keying.py`

**Interfaces:**
- Consumes: `memecoin_quote_ledger` rows now carrying `network_id`.
- Produces: `_legacy_quotes(conn, mint, signal_at, cutoff, network_id=None)` returns items whose
  `network_id` is the stored value, else inferred `solana` **only** for non-`0x` mints, else `None`.

- [ ] **Step 1: Write the failing test**

```python
import importlib.util, pathlib
def _load_study():
    p = pathlib.Path("scripts/research/memecoin_pattern_study.py")
    spec = importlib.util.spec_from_file_location("mps", p)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_legacy_quotes_does_not_mislabel_evm_mint_as_solana():
    from datetime import datetime, timezone, timedelta
    c = _mem_db()
    t0 = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
    m.record_quote_observations(c, {("8453", "0xEVM"): {"price": 1.0}}, t0.isoformat())
    m.record_quote_observations(c, {"SOLNATIVE": {"price": 2.0}}, t0.isoformat())
    mps = _load_study()
    evm = mps._legacy_quotes(c, "0xEVM", t0, t0 + timedelta(hours=1))
    assert evm and evm[0]["network_id"] == "8453"       # stored chain, NOT hardcoded solana
    sol = mps._legacy_quotes(c, "SOLNATIVE", t0, t0 + timedelta(hours=1))
    assert sol and sol[0]["network_id"] == "solana"     # non-0x inferred solana is fine
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py::test_legacy_quotes_does_not_mislabel_evm_mint_as_solana -q`
Expected: FAIL (`"0xEVM"` labeled `"solana"`).

- [ ] **Step 3: Implement**

Replace `_legacy_quotes`:

```python
def _legacy_quotes(conn, mint, signal_at, cutoff, network_id=None):
    deadline = min(cutoff, signal_at + timedelta(hours=24, minutes=5))
    q = ("SELECT id,mint,ts observed_at,price,liquidity,quality,network_id "
         "FROM memecoin_quote_ledger WHERE mint=? AND ts>=? AND ts<=? ")
    params = [mint, signal_at.isoformat(), deadline.isoformat()]
    if network_id is not None:
        q += "AND COALESCE(network_id,?)=? "     # NULL legacy rows match the requested chain
        params += [str(network_id), str(network_id)]
    q += "ORDER BY ts,id"
    rows = []
    for row in conn.execute(q, params):
        item = dict(row)
        # Stored chain wins. For legacy NULL rows, infer solana ONLY for non-0x (Solana-native,
        # unambiguous); a 0x mint stays unknown rather than being mislabeled solana.
        if not item.get("network_id"):
            item["network_id"] = "solana" if not str(mint).startswith("0x") else None
        item["legacy_ungraded"] = item.get("quality") is None
        if item.get("quality") is None:
            item["quality"] = "ok"
        rows.append(item)
    return rows
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q`
Expected: PASS.

- [ ] **Step 5: Guard the study's caller (no behavior change)**

Run: `grep -n "_legacy_quotes(" scripts/research/memecoin_pattern_study.py`
If the existing caller passes only 4 args, it still works (new arg defaults `None`). Do not change it unless the study already knows the chain in scope.

- [ ] **Step 6: Commit**

```bash
git add tests/test_memecoin_network_id_keying.py scripts/research/memecoin_pattern_study.py
git commit -m "fix(memecoin): legacy quote read uses stored network_id, no solana hardcode"
```

---

### Task 5: one-time backfill script (TDD on a temp DB)

**Files:**
- Create: `scripts/research/memecoin_backfill_network_id.py`
- Modify: `sniper/persistence/db_memecoin.py` — add pure resolver helpers used by the script + tests
- Test: `tests/test_memecoin_network_id_keying.py`

**Interfaces:**
- Produces: `resolve_ledger_network(conn, mint)` → concrete chain unambiguous-only, else `None`:
  non-`0x` → `'solana'`; `0x` → `resolve_network_from_events(conn, mint)` (single concrete chain) else `None`.
- Produces: `backfill_quote_ledger_network(conn, batch=5000)`, `backfill_alert_latency_network(conn)`,
  `backfill_exec_latency_network(conn)` → dict `{resolved, left_null, scanned}`.

- [ ] **Step 1: Write the failing test**

```python
def test_resolve_ledger_network_unambiguous_only():
    c = _mem_db()
    # feed says 0xEVM is on 8453 (one concrete chain)
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('e1','0xEVM','8453')")
    c.commit()
    assert m.resolve_ledger_network(c, "SOLNATIVE") == "solana"   # non-0x unambiguous
    assert m.resolve_ledger_network(c, "0xEVM") == "8453"         # 0x resolved from events
    assert m.resolve_ledger_network(c, "0xGHOST") is None         # 0x, feed silent -> NULL


def test_backfill_quote_ledger_fills_unambiguous_leaves_rest_null():
    c = _mem_db()
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id) VALUES('e1','0xEVM','8453')")
    c.commit()
    m.record_quote_observations(c, {"SOLNATIVE": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    m.record_quote_observations(c, {"0xEVM": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    m.record_quote_observations(c, {"0xGHOST": {"price": 1.0}}, "2026-07-15T20:00:05+00:00")
    # simulate legacy rows (network_id NULL): they were written bare above -> already NULL
    stats = m.backfill_quote_ledger_network(c, batch=2)
    got = {r[0]: r[1] for r in c.execute("SELECT mint, network_id FROM memecoin_quote_ledger")}
    assert got["SOLNATIVE"] == "solana"
    assert got["0xEVM"] == "8453"
    assert got["0xGHOST"] is None
    assert stats["resolved"] == 2 and stats["left_null"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q -k "resolve_ledger or backfill_quote"`
Expected: FAIL (`resolve_ledger_network` / `backfill_quote_ledger_network` undefined).

- [ ] **Step 3: Implement resolver + backfills in `db_memecoin.py`**

Add near `resolve_network_from_events` (~1585):

```python
def resolve_ledger_network(conn, mint):
    """Unambiguous-only chain for a bare-mint ledger/instrument row. A non-0x mint is Solana-native
    (unambiguous). A 0x mint is resolved from the feed's own events (single concrete chain) or left
    None — never coerced to a default chain."""
    if not mint:
        return None
    if not str(mint).startswith("0x"):
        return "solana"
    return resolve_network_from_events(conn, mint)


def _backfill_table_network(conn, table, resolver, batch=5000):
    """Fill NULL network_id rows of `table` via resolver(conn, mint)->chain|None, batched so each
    write-lock is short vs the live 15s sweep (WAL: readers never block). Unresolvable rows stay
    NULL and are counted."""
    resolved = left_null = 0
    cache = {}
    while True:
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM {table} WHERE network_id IS NULL LIMIT ?", (batch,))]
        if not ids:
            break
        for rid in ids:
            mint = conn.execute(f"SELECT mint FROM {table} WHERE id=?", (rid,)).fetchone()[0]
            if mint not in cache:
                cache[mint] = resolver(conn, mint)
            net = cache[mint]
            if net is None:
                left_null += 1
                # mark scanned by writing a sentinel? No — must not coerce. Instead break the loop
                # deterministically: NULL rows we cannot resolve would re-select forever. Use a
                # bounded scan by id watermark instead (see below).
        break  # replaced by watermark loop in Step 3b
    return {"resolved": resolved, "left_null": left_null}
```

- [ ] **Step 3b: Fix the infinite-reselect bug — scan by id watermark, not `WHERE network_id IS NULL LIMIT`**

The naive `WHERE network_id IS NULL LIMIT` loops forever on unresolvable rows (they stay NULL and re-select). Use an id watermark:

```python
def _backfill_table_network(conn, table, resolver, batch=5000):
    resolved = left_null = scanned = 0
    cache = {}
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id, mint FROM {table} WHERE id>? AND network_id IS NULL ORDER BY id LIMIT ?",
            (last_id, batch)).fetchall()
        if not rows:
            break
        for rid, mint in rows:
            scanned += 1
            last_id = rid
            if mint not in cache:
                cache[mint] = resolver(conn, mint)
            net = cache[mint]
            if net is None:
                left_null += 1
                continue
            conn.execute(f"UPDATE {table} SET network_id=? WHERE id=?", (net, rid))
            resolved += 1
        conn.commit()   # short transaction per batch
    return {"resolved": resolved, "left_null": left_null, "scanned": scanned}


def backfill_quote_ledger_network(conn, batch=5000):
    return _backfill_table_network(conn, "memecoin_quote_ledger", resolve_ledger_network, batch)


def backfill_alert_latency_network(conn, batch=5000):
    def _r(cn, mint):  # exact path: fomo_event_id -> event chain; fallback unambiguous mint
        return resolve_ledger_network(cn, mint)
    return _backfill_table_network(conn, "memecoin_alert_latency", _r, batch)


def backfill_exec_latency_network(conn, batch=5000):
    def _r(cn, mint):
        return resolve_ledger_network(cn, mint)
    return _backfill_table_network(conn, "memecoin_exec_latency", _r, batch)
```

> Alert/exec exact-provenance refinement (optional, only if cheap in Step 3c): prefer the row's own
> `fomo_event_id`/`pending_id` join over the mint resolver. Skipped here — the mint resolver is
> already unambiguous-only and correct; the join is a nicety, not a correctness need.

- [ ] **Step 3c: Write the ops script**

```python
# scripts/research/memecoin_backfill_network_id.py
"""One-time backfill of network_id for the four chain-ambiguous memecoin tables.
Unambiguous-only (mirrors resolve_network_from_events): non-0x -> solana; 0x -> single concrete
feed chain or left NULL. Batched so each write-lock is short vs the live 15s sweep. Idempotent."""
import sqlite3, sys
import sniper.persistence.db_memecoin as m
from sniper.util.clock import real_utc_now_iso

DB = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)   # idempotent: guarantees the columns exist
    for name, fn in (("quote_ledger", m.backfill_quote_ledger_network),
                     ("alert_latency", m.backfill_alert_latency_network),
                     ("exec_latency", m.backfill_exec_latency_network)):
        t0 = real_utc_now_iso()
        stats = fn(c)
        print(f"[{real_utc_now_iso()}] {name}: {stats} (started {t0})")
    c.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memecoin_network_id_keying.py sniper/persistence/db_memecoin.py scripts/research/memecoin_backfill_network_id.py
git commit -m "feat(memecoin): batched unambiguous-only network_id backfill script + resolvers"
```

---

### Task 6: full regression, schema validator, brain index

- [ ] **Step 1: Focused memecoin suite**

Run: `python3 -m pytest tests/test_memecoin_network_id_keying.py tests/test_memecoin_real_deploy.py tests/test_memecoin_real_mcap_gate.py tests/test_memecoin_signal_features.py tests/test_memecoin_pattern_study.py -q`
Expected: PASS.

- [ ] **Step 2: Broad suite**

Run: `python3 -m pytest tests/ -q -k memecoin`
Expected: PASS.

- [ ] **Step 3: Schema validator + brain index**

```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git add index/schema_registry.json index/dependency_index.json
git commit -m "chore(memecoin): regen brain index after network_id columns"
```

Expected: validator clean; index diff shows the three new columns + index.

---

### Task 7: live migration + backfill + funded redeploy (ops, behind backup)

- [ ] **Step 1: Backup**

```bash
bash scripts/daily_backup.sh
```

- [ ] **Step 2: Apply the schema migration to the live DB (idempotent ensure)**

```bash
python3 -c "import sqlite3, sniper.persistence.db_memecoin as m; c=sqlite3.connect('sniper_tracker.db', timeout=30); m.ensure_memecoin_tables(c); c.close(); print('ensured')"
```

Verify columns + index landed:

```bash
python3 -c "import sqlite3;c=sqlite3.connect('sniper_tracker.db');print('ql', 'network_id' in {r[1] for r in c.execute('PRAGMA table_info(memecoin_quote_ledger)')});print('al','network_id' in {r[1] for r in c.execute('PRAGMA table_info(memecoin_alert_latency)')});print('el','network_id' in {r[1] for r in c.execute('PRAGMA table_info(memecoin_exec_latency)')})"
```

- [ ] **Step 3: Run the backfill (time it; verify sweep unharmed)**

```bash
time python3 scripts/research/memecoin_backfill_network_id.py sniper_tracker.db
```

Record resolved / left_null per table. Confirm the runner heartbeat stayed fresh during the run:

```bash
cat data/memecoin_runner.heartbeat
```

- [ ] **Step 4: Verify backfill result + residual NULLs are only truly-unresolvable**

```bash
python3 -c "import sqlite3;c=sqlite3.connect('sniper_tracker.db');[print(t, c.execute(f'SELECT COUNT(*) FROM {t} WHERE network_id IS NULL').fetchone()[0], 'null of', c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ('memecoin_quote_ledger','memecoin_alert_latency','memecoin_exec_latency')]"
```

- [ ] **Step 5: Funded runner redeploy (only if HEAD is on origin/main, tests green, zero pending reservations)**

```bash
git log --oneline -1                     # confirm the SHA
python3 -c "import sqlite3;c=sqlite3.connect('sniper_tracker.db');print('pending', c.execute(\"SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'\").fetchone()[0])"
# deploy only a full SHA reachable from origin/main, after backup + zero pending + test gate:
bash scripts/deploy_memecoin_runtime.sh <full-sha>
```

- [ ] **Step 6: Post-deploy health**

```bash
cat data/memecoin_runner.heartbeat        # age < 30s, auth_ok true
python3 scripts/brain_health.py
```

- [ ] **Step 7: Update HANDOFF.md** with what changed, counts left NULL, residual risk.

---

## Self-Review notes

- **Spec coverage:** quote_ledger (Tasks 1,2,4), alert/exec latency (Task 3), backfill unambiguous-only (Task 5), seen_sells skip (documented in spec, no task — correct), verify+deploy (Tasks 6,7). ✔
- **Residual documented:** `UNIQUE(mint, ts_minute)` unchanged; Task 2 test uses distinct minutes to avoid asserting on a collision that can't happen today. ✔
- **None ≠ default:** resolver returns `None` for unresolvable 0x; writer writes `NULL` for bare keys. ✔
- **Infinite-reselect trap:** Task 5 Step 3b switches to an id watermark. ✔
- **Type consistency:** `resolve_ledger_network(conn, mint)`, `backfill_*_network(conn, batch=5000)` used identically across tasks. ✔
