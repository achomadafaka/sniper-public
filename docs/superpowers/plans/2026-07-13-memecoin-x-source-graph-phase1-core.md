# Memecoin X Source Graph — Phase 1 (Store + Normalizer + Clock Gate) Implementation Plan

**Status:** Implemented, then hardened by
`2026-07-13-memecoin-x-source-graph-phase1-review-fixes.md`. The review-fix plan and current code
are authoritative where the original seven-table snippets below differ.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the fixture-driven, browser-free core of the X source graph — the normalized SQLite store, the card normalizer, and the clock-health gate — so later phases (browser adapter, correlator, output) plug into a tested foundation.

**Architecture:** A new `db_memecoin_x` persistence module owns 8 idempotent, auditable tables. The eighth is an immutable per-delivery card-observation journal added by the review fix; canonical posts remain a deduplicated read model. Forward links and backfill facts are append-only; account metadata and post last-seen state are explicit upserts. A `sniper/analysis/memecoin_x/` package holds pure functions: handle identity, a single-snapshot clock-health gate over the existing corrected-clock offset file, conservative address/coin-mention normalization, and a card normalizer that turns browser-adapter "card facts" (plain dicts) into canonical posts + relationships + coin mentions. No browser, no network, no trading — every unit is tested against fixtures.

**Tech Stack:** Python 3.13, sqlite3 (WAL), pytest. Reuses `sniper.util.clock` (corrected UTC) and matches the DDL style of `sniper/persistence/db_memecoin.py`.

## Global Constraints

- Display-only. This feature never enters, sizes, vetoes, or affects any real/paper trade, verdict, or funded execution. No imports of funded-execution modules.
- The system never follows/likes/reposts/replies. Phase 1 has no browser code at all.
- Forward lead evidence uses **our observed timestamps only**. X `posted_at` is metadata, never used for lead subtraction. (Correlator is a later phase; Phase 1 only stores `posted_at` as metadata.)
- Clock domain values: healthy X captures use `clock_domain='observed_real_utc'`; unhealthy use `'legacy_unknown'` and are excluded from forward correlation.
- Timestamps are ISO-8601 UTC strings. Healthy X `observed_at` is computed from the exact clock-offset snapshot that passed validation; do not re-read through `real_utc_now_iso()` after the health check. Never use `datetime.utcnow()`.
- X handles are canonicalized by trimming whitespace, removing one leading `@`, and lowercasing. X handles are case-insensitive; storage and lookup must be too.
- EVM contract addresses normalize to **lowercase**; Solana addresses **preserve** base58 case. Matching always includes `network_id`.
- All DB access for these tables goes through `db_memecoin_x`. No raw SQL in adapters/tests outside this module and its tests.
- Parser/normalizer output carries `parser_version` (start at `1`) so re-parses are auditable.
- Branch: `feat/x-source-graph-design`. Worktree: `.worktrees/x-source-graph`.

---

## File Structure

- Create `sniper/persistence/db_memecoin_x.py` — `ensure_memecoin_x_tables(conn)` (all 8 tables), roster upsert, capture-run + post + immutable-observation + relationship + coin-mention persistence. Idempotent helpers.
- Create `sniper/analysis/memecoin_x/__init__.py` — package marker.
- Create `sniper/analysis/memecoin_x/clock_health.py` — `clock_health()` / `x_observed_at_or_none()`.
- Create `sniper/analysis/memecoin_x/normalize.py` — address + coin-mention + card normalization (pure functions).
- Create `sniper/analysis/memecoin_x/roster.py` — the locked initial roster seed (27 Tier-1 + 6 Tier-2 + 2 official).
- Modify `scripts/brain_index.py` — include `ensure_memecoin_x_tables()` in the SQLite schema bootstrap.
- Create `tests/test_memecoin_x_clock_health.py`
- Create `tests/test_memecoin_x_normalize.py`
- Create `tests/test_memecoin_x_store.py`
- Create `tests/test_memecoin_x_roster.py`

`PARSER_VERSION = 1` is defined in `sniper/analysis/memecoin_x/normalize.py` and imported where needed.

---

### Task 1: Schema — `ensure_memecoin_x_tables`

**Files:**
- Create: `sniper/persistence/db_memecoin_x.py`
- Modify: `scripts/brain_index.py`
- Test: `tests/test_memecoin_x_store.py`
- Modify test: `tests/test_brain_index.py`

**Interfaces:**
- Produces: `ensure_memecoin_x_tables(conn: sqlite3.Connection) -> None` — idempotent; creates `memecoin_x_accounts`, `memecoin_x_capture_runs`, `memecoin_x_forward_posts`, `memecoin_x_card_observations`, `memecoin_x_relationships`, `memecoin_x_coin_mentions`, `memecoin_x_forward_links`, `memecoin_x_backfill_candidates`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_x_store.py
import json
import sqlite3
import pytest
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables

X_TABLES = [
    "memecoin_x_accounts", "memecoin_x_capture_runs", "memecoin_x_forward_posts",
    "memecoin_x_relationships", "memecoin_x_coin_mentions", "memecoin_x_forward_links",
    "memecoin_x_backfill_candidates",
]

def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn

def test_ensure_creates_all_tables_idempotently():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    ensure_memecoin_x_tables(conn)  # second call must not raise
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in X_TABLES:
        assert t in names, f"missing table {t}"

def test_backfill_candidates_rejects_non_retrospective():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    # CHECK(evidence_mode='retrospective') must reject anything else
    try:
        conn.execute(
            "INSERT INTO memecoin_x_backfill_candidates (post_id, evidence_mode) "
            "VALUES ('p1', 'forward')")
        conn.commit()
        assert False, "expected CHECK constraint to reject non-retrospective row"
    except sqlite3.IntegrityError:
        pass

def test_relationship_unique_identity():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    row = ("post1", "zinceth", "tier1_trader", 1, "repost",
           "2026-07-13T00:00:00+00:00", "run1", "observed_real_utc", None, None)
    cols = ("canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs, "
            "relationship, observed_at, capture_run_id, clock_domain, referenced_author, "
            "referenced_post_id")
    q = f"INSERT INTO memecoin_x_relationships ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)"
    conn.execute(q, row); conn.commit()
    try:
        conn.execute(q, row); conn.commit()
        assert False, "expected UNIQUE(canonical_post_id, actor_handle, relationship)"
    except sqlite3.IntegrityError:
        pass

def test_coin_mention_identity_is_null_safe():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    row = ("post1", None, None, "WIF", "ticker_only", 0, 0, 1)
    sql = """INSERT INTO memecoin_x_coin_mentions
             (canonical_post_id, network_id, mint, ticker, extraction_method,
              ambiguous, eligible, parser_version)
             VALUES (?,?,?,?,?,?,?,?)"""
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)

def test_account_handle_identity_is_case_insensitive():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    conn.execute("INSERT INTO memecoin_x_accounts (handle, role) VALUES (?, ?)",
                 ("_oht_", "tier1_trader"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO memecoin_x_accounts (handle, role) VALUES (?, ?)",
                     ("_OHT_", "tier1_trader"))

# add to tests/test_brain_index.py
def test_schema_registry_includes_memecoin_x(schema_registry):
    assert "memecoin_x_coin_mentions" in schema_registry["tables"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.persistence.db_memecoin_x'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/persistence/db_memecoin_x.py
"""db_memecoin_x.py — tables + CRUD for the X source graph (display-only, read-only capture).

All ensure_* idempotent (CREATE TABLE IF NOT EXISTS). Forward evidence uses OUR observed
timestamps only; X posted_at is metadata. See
docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md.
"""
from __future__ import annotations

import json as _json
import sqlite3


def ensure_memecoin_x_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT COLLATE NOCASE UNIQUE NOT NULL,
            profile_url TEXT,
            role TEXT NOT NULL CHECK (role IN
                ('tier1_trader','tier2_source','official_context','suggested')),
            fomo_source_user_id TEXT,
            fomo_handle TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            needs_review INTEGER NOT NULL DEFAULT 0,
            provenance TEXT,
            added_at TEXT,
            updated_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_capture_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            parser_version INTEGER NOT NULL,
            started_at TEXT, finished_at TEXT,
            clock_offset_s REAL, clock_offset_age_s REAL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            cards_seen INTEGER DEFAULT 0,
            new_posts INTEGER DEFAULT 0,
            new_relationships INTEGER DEFAULT 0,
            parse_failures INTEGER DEFAULT 0,
            prior_sentinel TEXT, newest_sentinel TEXT,
            sentinel_found INTEGER,
            status TEXT NOT NULL,               -- ok|coverage_gap|logged_out|challenge|rate_limited|clock_unhealthy|parser_error
            error_detail TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_forward_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT UNIQUE NOT NULL,
            url TEXT,
            author_handle TEXT,
            author_role_at_capture TEXT,
            full_text TEXT,
            text_complete INTEGER,
            posted_at_raw TEXT,
            posted_at TEXT,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            capture_run_id TEXT,
            parser_version INTEGER NOT NULL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            extracted_urls TEXT,                -- json array
            raw_card_json TEXT                  -- immutable raw facts to replay normalization
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            actor_handle TEXT NOT NULL,
            actor_role_at_obs TEXT,
            actor_is_tier1_at_obs INTEGER,
            relationship TEXT NOT NULL CHECK (relationship IN
                ('direct','repost','quote','reply')),
            observed_at TEXT NOT NULL,
            capture_run_id TEXT,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            referenced_author TEXT,
            referenced_post_id TEXT,
            UNIQUE (canonical_post_id, actor_handle, relationship)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_coin_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            network_id TEXT,
            mint TEXT,
            ticker TEXT,
            extraction_method TEXT NOT NULL CHECK (extraction_method IN
                ('contract_text','dex_url','launchpad_url','ticker_only')),
            ambiguous INTEGER NOT NULL DEFAULT 0,
            eligible INTEGER NOT NULL DEFAULT 0,
            parser_version INTEGER NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_forward_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            coin_mention_id INTEGER NOT NULL,
            fomo_upstream_event_id TEXT NOT NULL,
            buying_source_user_id TEXT NOT NULL,
            network_id TEXT, mint TEXT,
            x_observed_at TEXT, x_clock_domain TEXT,
            fomo_observed_at TEXT, fomo_clock_domain TEXT,
            lead_seconds REAL,
            evidence_lane TEXT,                  -- amplified|coincidence
            buyer_relationship TEXT,
            tier1_at_obs INTEGER, source_role_at_obs TEXT,
            eligible INTEGER NOT NULL DEFAULT 0,
            exclusion_reason TEXT,
            UNIQUE (coin_mention_id, fomo_upstream_event_id, buying_source_user_id)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_backfill_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            author TEXT,
            posted_at TEXT,
            network_id TEXT, mint TEXT, ticker TEXT,
            discovery_query TEXT,
            related_fomo_event_id TEXT,
            evidence_mode TEXT NOT NULL DEFAULT 'retrospective'
                CHECK (evidence_mode = 'retrospective')
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_rel_post ON memecoin_x_relationships (canonical_post_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_rel_actor ON memecoin_x_relationships (actor_handle, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_mentions_coin ON memecoin_x_coin_mentions (network_id, mint)")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_x_mention_identity
        ON memecoin_x_coin_mentions (
            canonical_post_id,
            COALESCE(network_id, ''),
            COALESCE(mint, ''),
            COALESCE(ticker, ''),
            extraction_method
        )""")
    conn.commit()
```

Also extend the existing memecoin schema bootstrap; generating the registry from
`db_memecoin.py` alone cannot discover these seven tables. Update that function's docstring so
it no longer claims the bootstrap owns only five `memecoin_*` tables:

```python
# scripts/brain_index.py::_bootstrap_memecoin_tables
from sniper.persistence import db_memecoin, db_memecoin_x  # noqa: PLC0415

db_memecoin.ensure_memecoin_tables(conn)
db_memecoin_x.ensure_memecoin_x_tables(conn)
return "db_memecoin.ensure_memecoin_tables+db_memecoin_x.ensure_memecoin_x_tables"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_store.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Regenerate the brain index and commit**

```bash
cd /home/bigpapi/Desktop/Sniper/.worktrees/x-source-graph
python3 -m pytest tests/test_brain_index.py -q
python3 scripts/brain_index.py generate
python3 scripts/brain_index.py schema memecoin_x_coin_mentions
git add sniper/persistence/db_memecoin_x.py scripts/brain_index.py tests/test_memecoin_x_store.py tests/test_brain_index.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(memecoin-x): schema for X source graph (7 tables, idempotent)"
```

---

### Task 2: Clock-health gate

**Files:**
- Create: `sniper/analysis/memecoin_x/__init__.py` (empty)
- Create: `sniper/analysis/memecoin_x/clock_health.py`
- Test: `tests/test_memecoin_x_clock_health.py`

**Interfaces:**
- Consumes: `sniper.util.clock` offset file format `{"offset_s": float, "at": float}`.
- Produces:
  - `clock_health(path=None, now=None) -> dict` with keys `healthy: bool`, `offset_s: float|None`, `offset_age_s: float|None`, `reason: str` (`ok`|`missing`|`non_finite`|`out_of_bounds`|`stale`).
  - `MAX_OFFSET_AGE_S = 900`, `MAX_ABS_OFFSET_S = 172800`.
  - `x_observed_at_or_none(path=None, now=None) -> tuple[str|None, dict]` — returns `(iso, health)` when healthy, else `(None, health)`. It captures host `now` once and derives `iso` from the same checked `offset_s`; it does not re-read through `real_utc_now_iso()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_x_clock_health.py
import json
from datetime import datetime, timezone
from sniper.analysis.memecoin_x import clock_health as ch

def _write(tmp_path, offset_s, at):
    p = tmp_path / "clock_offset.json"
    p.write_text(json.dumps({"offset_s": offset_s, "at": at}))
    return str(p)

def test_healthy_recent_offset(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=25200.0, at=now - 60)   # 1 min old, within bounds
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is True and h["reason"] == "ok"

def test_missing_offset_file_is_unhealthy(tmp_path):
    p = str(tmp_path / "does_not_exist.json")
    h = ch.clock_health(path=p, now=1_000_000.0)
    assert h["healthy"] is False and h["reason"] == "missing"

def test_stale_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=0.0, at=now - 1000)     # >900s old
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "stale"

def test_out_of_bounds_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=999999.0, at=now - 10)  # >2 days
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "out_of_bounds"

def test_non_finite_offset_is_unhealthy(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=float("nan"), at=now - 10)
    h = ch.clock_health(path=p, now=now)
    assert h["healthy"] is False and h["reason"] == "non_finite"

def test_non_finite_offset_timestamp_is_unhealthy(tmp_path):
    p = _write(tmp_path, offset_s=0.0, at=float("inf"))
    h = ch.clock_health(path=p, now=1_000_000.0)
    assert h["healthy"] is False and h["reason"] == "non_finite"

def test_observed_at_uses_the_checked_snapshot(tmp_path):
    now = 1_000_000.0
    p = _write(tmp_path, offset_s=25_200.0, at=now - 60)
    iso, h = ch.x_observed_at_or_none(path=p, now=now)
    assert h["healthy"] is True
    assert iso == datetime.fromtimestamp(974_800.0, tz=timezone.utc).isoformat()

def test_x_observed_at_none_when_unhealthy(tmp_path):
    p = str(tmp_path / "missing.json")
    iso, h = ch.x_observed_at_or_none(path=p, now=1_000_000.0)
    assert iso is None and h["healthy"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_clock_health.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.analysis.memecoin_x'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/memecoin_x/__init__.py
# (empty package marker)
```

```python
# sniper/analysis/memecoin_x/clock_health.py
"""Clock-health gate for X capture. The corrected-clock helper (sniper.util.clock) silently
falls back to the host clock when no offset is learned; that is NOT sufficient for forward lead
evidence. This gate requires a present, finite, in-bounds, fresh offset before any X observation
timestamp may be trusted (clock_domain='observed_real_utc'). Otherwise the capture is marked
'legacy_unknown' and excluded from forward correlation."""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

from sniper.util import clock as _clock

MAX_OFFSET_AGE_S = 900          # 15 minutes
MAX_ABS_OFFSET_S = 172800       # 2 days (matches clock.record_real_utc sanity bound)


def _offset_file(path=None):
    return path or os.environ.get(
        "SNIPER_CLOCK_OFFSET_FILE", _clock._OFFSET_FILE)


def clock_health(path=None, now=None) -> dict:
    p = _offset_file(path)
    now = time.time() if now is None else now
    base = {"healthy": False, "offset_s": None, "offset_age_s": None, "reason": "missing"}
    try:
        with open(p) as fh:
            d = json.load(fh)
    except Exception:
        return base
    try:
        offset = float(d.get("offset_s"))
        at = float(d.get("at"))
    except (TypeError, ValueError):
        return {**base, "reason": "missing"}
    age = now - at
    out = {"healthy": False, "offset_s": offset, "offset_age_s": age, "reason": "ok"}
    if not (math.isfinite(offset) and math.isfinite(at)):
        return {**out, "reason": "non_finite"}
    if abs(offset) > MAX_ABS_OFFSET_S:
        return {**out, "reason": "out_of_bounds"}
    if age > MAX_OFFSET_AGE_S or age < -MAX_OFFSET_AGE_S:
        return {**out, "reason": "stale"}
    out["healthy"] = True
    return out


def x_observed_at_or_none(path=None, now=None):
    """Return corrected UTC from the exact offset snapshot accepted by clock_health."""
    host_now = time.time() if now is None else float(now)
    h = clock_health(path=path, now=host_now)
    if not h["healthy"]:
        return None, h
    observed = datetime.fromtimestamp(host_now - h["offset_s"], tz=timezone.utc)
    return observed.isoformat(), h
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_clock_health.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/__init__.py sniper/analysis/memecoin_x/clock_health.py tests/test_memecoin_x_clock_health.py
git commit -m "feat(memecoin-x): clock-health gate (present/finite/in-bounds/fresh offset)"
```

---

### Task 3: Address & coin-mention normalization

**Files:**
- Create: `sniper/analysis/memecoin_x/normalize.py`
- Test: `tests/test_memecoin_x_normalize.py`

**Interfaces:**
- Produces:
  - `PARSER_VERSION = 1`
  - `normalize_handle(handle: str|None) -> str|None` — trim, remove one leading `@`, lowercase.
  - `normalize_address(addr: str, network_id: str|None) -> str` — EVM (`0x`+40 hex) → lowercase; else preserve (Solana base58).
  - `is_evm_address(addr) -> bool`, `is_solana_address(addr) -> bool`
  - parsed URL/network context for DexScreener, `pump.fun`, `bonk.fun`, `hood.fun`, `noxa.fun`, and `fun.noxa.fi`; hostname/chain slugs are casefolded, path/address bytes are not.
  - `extract_coin_mentions(text: str, urls: list[str]) -> list[dict]` — each `{network_id, mint, ticker, extraction_method, ambiguous, eligible}`. `eligible` iff exact `mint` AND `network_id` known and unambiguous.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_x_normalize.py
import pytest
from sniper.analysis.memecoin_x import normalize as nz

def test_evm_address_lowercased():
    a = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    assert nz.normalize_address(a, "8453") == a.lower()

def test_solana_address_case_preserved():
    a = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    assert nz.normalize_address(a, "solana") == a  # base58 case preserved

def test_handle_identity_is_case_insensitive():
    assert nz.normalize_handle(" @_OHT_ ") == "_oht_"

def test_dex_pair_url_is_context_not_a_token_mint():
    pair = "0xABCdef0000000000000000000000000000000001"
    ms = nz.extract_coin_mentions("gm", [f"https://dexscreener.com/base/{pair}"])
    assert not [m for m in ms if m["mint"]]

def test_dex_network_context_plus_exact_ca_is_eligible():
    ca = "0xABCdef0000000000000000000000000000000002"
    urls = ["https://dexscreener.com/robinhood/0x1111111111111111111111111111111111111111"]
    ms = nz.extract_coin_mentions(f"CA: {ca}", urls)
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["network_id"] == "4663"
    assert m["mint"] == ca.lower()
    assert m["eligible"] is True and m["ambiguous"] is False

def test_bare_evm_contract_in_text_is_ambiguous_network():
    # a raw 0x contract with no chain context: network unknown -> not eligible
    ms = nz.extract_coin_mentions(
        "aping 0xABCdef0000000000000000000000000000000002", [])
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["mint"] == "0xabcdef0000000000000000000000000000000002"
    assert m["ambiguous"] is True and m["eligible"] is False

def test_solana_contract_in_text_is_eligible():
    ms = nz.extract_coin_mentions(
        "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump", [])
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["network_id"] == "solana"
    assert m["eligible"] is True

def test_arbitrary_base58_prose_is_not_a_contract():
    ca = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    assert not [m for m in nz.extract_coin_mentions(f"wallet {ca}", []) if m["mint"]]

def test_launchpad_url_preserves_solana_address_case():
    ca = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    ms = nz.extract_coin_mentions("gm", [f"https://pump.fun/coin/{ca}"])
    assert any(m["mint"] == ca and m["network_id"] == "solana" for m in ms)

def test_hood_and_noxa_urls_supply_robinhood_network_context():
    ca = "0xABCdef0000000000000000000000000000000003"
    for url in ("https://hood.fun/token/foo", "https://fun.noxa.fi/robinhood/foo"):
        m = [x for x in nz.extract_coin_mentions(f"CA: {ca}", [url]) if x["mint"]][0]
        assert m["network_id"] == "4663" and m["eligible"] is True

def test_conflicting_network_contexts_do_not_become_eligible():
    ca = "0xABCdef0000000000000000000000000000000004"
    urls = [
        "https://dexscreener.com/base/pair1",
        "https://dexscreener.com/robinhood/pair2",
    ]
    m = [x for x in nz.extract_coin_mentions(f"CA: {ca}", urls) if x["mint"]][0]
    assert m["network_id"] is None and m["ambiguous"] is True
    assert m["eligible"] is False

def test_ticker_only_is_hypothesis_not_eligible():
    ms = nz.extract_coin_mentions("send $WIF", [])
    m = [x for x in ms if x["extraction_method"] == "ticker_only"][0]
    assert m["ticker"] == "WIF" and m["mint"] is None
    assert m["eligible"] is False

def test_network_never_inferred_from_absence():
    # no CA, no url, just prose -> no eligible mention at all
    ms = nz.extract_coin_mentions("wagmi frens", [])
    assert all(x["eligible"] is False for x in ms)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py -q`
Expected: FAIL with `AttributeError` / `ModuleNotFoundError` for `normalize`.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/memecoin_x/normalize.py
"""Pure normalization for X capture: addresses, coin mentions, and (Task 4) whole cards.
Network is only ever taken from the X content/URL itself — never inferred from a later buy.
A ticker collides, so ticker-only mentions are hypothesis evidence (not eligible). A contract
whose network cannot be independently determined is ambiguous (not eligible)."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

PARSER_VERSION = 1

_EVM_RE = re.compile(r"0x[a-fA-F0-9]{40}")
_SOL_CA_RE = re.compile(
    r"(?i)\b(?:ca|contract)\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})\b"
)
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,14})\b")

# Captured-content chain slug -> memecoin feed network_id.
NETWORK_BY_CHAIN_SLUG = {
    "solana": "solana", "base": "8453", "ethereum": "1", "eth": "1",
    "bsc": "56", "bnb": "56", "avalanche": "43114", "avax": "43114",
    "robinhood": "4663",
}
_CHAIN_MARKERS = (
    (re.compile(r"\b(?:robinhood\s+chain|chain\s*4663)\b", re.I), "4663"),
    (re.compile(r"\b(?:on\s+base|base\s+chain)\b", re.I), "8453"),
    (re.compile(r"\bsolana\b", re.I), "solana"),
    (re.compile(r"\b(?:ethereum|on\s+eth)\b", re.I), "1"),
)


def normalize_handle(handle):
    if handle is None:
        return None
    value = str(handle).strip()
    if value.startswith("@"):
        value = value[1:]
    return value.lower() or None


def is_evm_address(addr: str) -> bool:
    return bool(addr) and bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", addr))


def is_solana_address(addr: str) -> bool:
    return bool(addr) and not addr.startswith("0x") and bool(
        re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr))


def normalize_address(addr: str, network_id=None) -> str:
    if is_evm_address(addr):
        return addr.lower()
    return addr  # Solana / unknown: preserve exact case


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _url_fact(url: str):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    # Preserve path bytes/case. Lowercase only routing slugs.
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    if _host_is(host, "dexscreener.com"):
        network = NETWORK_BY_CHAIN_SLUG.get(parts[0].lower()) if parts else None
        # /<chain>/<id> is a pair/pool page. Never promote <id> to a token mint.
        return network, "dex_url", None

    if _host_is(host, "pump.fun") or _host_is(host, "bonk.fun"):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in {"coin", "token"} else None
        mint = mint if mint and is_solana_address(mint) else None
        return "solana", "launchpad_url", mint

    if _host_is(host, "hood.fun"):
        mint = parts[-1] if len(parts) >= 2 and parts[-2].lower() in {"coin", "token"} else None
        mint = mint if mint and is_evm_address(mint) else None
        return "4663", "launchpad_url", mint

    if _host_is(host, "noxa.fun") or _host_is(host, "fun.noxa.fi"):
        network = "4663" if parts and parts[0].lower() == "robinhood" else None
        mint = parts[1] if network and len(parts) >= 2 and is_evm_address(parts[1]) else None
        return network, "launchpad_url", mint

    return None, None, None


def _content_networks(text: str, urls) -> set[str]:
    found = {network for pattern, network in _CHAIN_MARKERS if pattern.search(text)}
    for url in urls:
        network, _, _ = _url_fact(url)
        if network:
            found.add(network)
    return found


def _mk(network_id, mint, ticker, method):
    eligible = bool(mint) and bool(network_id)
    ambiguous = bool(mint) and not network_id
    return {"network_id": network_id, "mint": mint, "ticker": ticker,
            "extraction_method": method, "ambiguous": ambiguous, "eligible": eligible}


def extract_coin_mentions(text: str, urls) -> list:
    text = text or ""
    urls = urls or []
    out = []
    seen = set()
    networks = _content_networks(text, urls)
    sole_network = next(iter(networks)) if len(networks) == 1 else None

    def add(m):
        key = (m["network_id"], m["mint"], m["ticker"], m["extraction_method"])
        if key not in seen:
            seen.add(key)
            out.append(m)

    for url in urls:
        net, method, mint = _url_fact(url)
        if method and mint:
            add(_mk(net, normalize_address(mint, net), None, method))

    for raw in _EVM_RE.findall(text):
        network = sole_network if sole_network and sole_network != "solana" else None
        add(_mk(network, normalize_address(raw, network), None, "contract_text"))
    for raw in _SOL_CA_RE.findall(text):
        network = "solana" if not networks or networks == {"solana"} else None
        add(_mk(network, normalize_address(raw, network), None, "contract_text"))
    for tk in _TICKER_RE.findall(text):
        add(_mk(None, None, tk.upper(), "ticker_only"))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/normalize.py tests/test_memecoin_x_normalize.py
git commit -m "feat(memecoin-x): address + coin-mention normalization (network-from-content only)"
```

---

### Task 4: Card normalizer

**Files:**
- Modify: `sniper/analysis/memecoin_x/normalize.py`
- Modify: `tests/test_memecoin_x_normalize.py`

**Interfaces:**
- Consumes: a "card fact" dict emitted by the (future) browser adapter:
  `{"canonical_post_id": str|None, "url": str, "author_handle": str, "relationship": "direct"|"repost"|"quote"|"reply", "actor_handle": str, "full_text": str, "text_complete": bool, "posted_at_raw": str|None, "urls": list[str], "referenced_author": str|None, "referenced_post_id": str|None}`
- Card contract: `canonical_post_id`, `url`, `author_handle`, and `full_text` identify the canonical source/target post whose content is attributed; `actor_handle` is the Following-feed account that surfaced it. For quote/reply wrappers, the adapter may emit the actor-authored wrapper as a separate direct card only when it has its own canonical ID.
- Produces: `normalize_card(card: dict) -> dict` with keys `post`, `relationship`, `mentions`, `raw_card`, `eligible_for_forward`. Forward persistence requires a canonical post ID, canonical author, canonical actor, and a valid relationship enum. Invalid/missing identity yields `eligible_for_forward=False` and `post=None` (diagnostic only). `posted_at` is parsed metadata and never lead evidence.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_memecoin_x_normalize.py
def test_normalize_direct_card_with_solana_ca():
    card = {
        "canonical_post_id": "1800000000000000001",
        "url": "https://x.com/zinceth/status/1800000000000000001",
        "author_handle": "zinceth", "actor_handle": "zinceth", "relationship": "direct",
        "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump", "text_complete": True,
        "posted_at_raw": "2026-07-13T00:00:00.000Z", "urls": [],
        "referenced_author": None, "referenced_post_id": None,
    }
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is True
    assert out["post"]["canonical_post_id"] == "1800000000000000001"
    assert out["relationship"]["relationship"] == "direct"
    assert out["relationship"]["actor_handle"] == "zinceth"
    assert any(m["eligible"] and m["network_id"] == "solana" for m in out["mentions"])

def test_card_without_post_id_is_diagnostic_only():
    card = {"canonical_post_id": None, "url": "", "author_handle": "x",
            "actor_handle": "x", "relationship": "direct", "full_text": "hi",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is False
    assert out["post"] is None

def test_repost_relationship_carries_referenced_author():
    card = {"canonical_post_id": "1799999999999999999",
            "url": "https://x.com/blknoiz06/status/1799999999999999999",
            "author_handle": "blknoiz06", "actor_handle": "_OHT_", "relationship": "repost",
            "full_text": "banger", "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1799999999999999999"}
    out = nz.normalize_card(card)
    assert out["relationship"]["relationship"] == "repost"
    assert out["relationship"]["referenced_author"] == "blknoiz06"
    assert out["post"]["author_handle"] == "blknoiz06"  # post author is the original, not the amplifier

@pytest.mark.parametrize("relationship", ["quote", "reply"])
def test_quote_and_reply_attribute_the_source_post(relationship):
    card = {"canonical_post_id": "1799999999999999999",
            "url": "https://x.com/blknoiz06/status/1799999999999999999",
            "author_handle": "@BlkNoiz06", "actor_handle": "@_OHT_",
            "relationship": relationship, "full_text": "source text",
            "text_complete": True, "posted_at_raw": "2026-07-13T00:00:00Z", "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1799999999999999999"}
    out = nz.normalize_card(card)
    assert out["post"]["author_handle"] == "blknoiz06"
    assert out["relationship"]["actor_handle"] == "_oht_"
    assert out["post"]["posted_at"] == "2026-07-13T00:00:00+00:00"

def test_invalid_relationship_is_diagnostic_only():
    card = {"canonical_post_id": "1", "url": "u", "author_handle": "a",
            "actor_handle": "b", "relationship": "like", "full_text": "x",
            "text_complete": True, "posted_at_raw": "not-a-time", "urls": []}
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is False and out["post"] is None
    assert out["raw_card"]["relationship"] == "like"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py -k card -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'normalize_card'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to sniper/analysis/memecoin_x/normalize.py
from datetime import datetime, timezone

_RELATIONSHIPS = {"direct", "repost", "quote", "reply"}


def _parse_posted_at(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat()

def normalize_card(card: dict) -> dict:
    pid = card.get("canonical_post_id")
    author = normalize_handle(card.get("author_handle"))
    actor = normalize_handle(card.get("actor_handle"))
    relationship = card.get("relationship")
    raw_card = {**card, "urls": list(card.get("urls") or [])}
    mentions = extract_coin_mentions(card.get("full_text", ""), card.get("urls") or [])
    rel = {
        "canonical_post_id": pid,
        "actor_handle": actor,
        "relationship": relationship,
        "referenced_author": normalize_handle(card.get("referenced_author")),
        "referenced_post_id": card.get("referenced_post_id"),
    }
    if not pid or not author or not actor or relationship not in _RELATIONSHIPS:
        return {"post": None, "relationship": rel, "mentions": mentions,
                "raw_card": raw_card, "eligible_for_forward": False}
    post = {
        "canonical_post_id": pid,
        "url": card.get("url"),
        "author_handle": author,
        "full_text": card.get("full_text"),
        "text_complete": bool(card.get("text_complete")),
        "posted_at_raw": card.get("posted_at_raw"),
        "posted_at": _parse_posted_at(card.get("posted_at_raw")),
        "urls": card.get("urls") or [],
        "parser_version": PARSER_VERSION,
    }
    return {"post": post, "relationship": rel, "mentions": mentions,
            "raw_card": raw_card, "eligible_for_forward": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_normalize.py -q`
Expected: PASS (all normalize tests)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/normalize.py tests/test_memecoin_x_normalize.py
git commit -m "feat(memecoin-x): card normalizer (post + relationship + mentions; post-id gates forward)"
```

---

### Task 5: Roster seed

**Files:**
- Create: `sniper/analysis/memecoin_x/roster.py`
- Test: `tests/test_memecoin_x_roster.py`

**Interfaces:**
- Produces: `INITIAL_ROSTER: list[dict]` each `{handle, display_handle, role, fomo_handle}` and `roster_counts() -> dict`. `handle` is canonical lowercase identity; `display_handle` retains the reviewed spelling. Roles: `tier1_trader` (27), `tier2_source` (6), `official_context` (2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_x_roster.py
from sniper.analysis.memecoin_x import roster as R

def test_roster_counts():
    c = R.roster_counts()
    assert c["tier1_trader"] == 27
    assert c["tier2_source"] == 6
    assert c["official_context"] == 2

def test_oht_handle_is_corrected():
    # Real handle is @_OHT_; identity storage is lowercase.
    handles = {e["handle"] for e in R.INITIAL_ROSTER}
    assert "_oht_" in handles and "oht" not in handles
    oht = next(e for e in R.INITIAL_ROSTER if e["handle"] == "_oht_")
    assert oht["display_handle"] == "_OHT_"

def test_handles_are_canonical_and_unique():
    handles = [e["handle"] for e in R.INITIAL_ROSTER]
    assert handles == [h.lower() for h in handles]
    assert len(handles) == len(set(handles)) == 35

def test_official_context_excluded_role():
    off = [e for e in R.INITIAL_ROSTER if e["role"] == "official_context"]
    assert {e["handle"] for e in off} == {"vladtenev", "robinhoodapp"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_roster.py -q`
Expected: FAIL with `ModuleNotFoundError` for `roster`.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/memecoin_x/roster.py
"""Locked initial X roster (2026-07-13). Handles are configuration, not identity-forever;
handle changes require operator review. Tier-1 X handles may differ from the fomo handle
(resolved via the fomo profile `twitter` field). Official-context accounts are captured for
narrative but excluded from source-caller ranking."""
from __future__ import annotations

from sniper.analysis.memecoin_x.normalize import normalize_handle

_TIER1 = [
    ("zinceth", "zinceth"), ("RuneCrypto_", "RuneCrypto_"), ("thokani", "Thokani"),
    ("saivSOL", "saivSOL"), ("PaikCapital", "PaikCapital"), ("Aurelius0121", "Aurelius0121"),
    ("ErisScalp", "Fiboguy"), ("Dprisav", "techquant"), ("0xJiggy", "0xJiggy"),
    ("00CTG", "00CTG"), ("Farmercharts", "Farmercharts"), ("memekingdom", "memekingdom"),
    ("_OHT_", "_OHT_"), ("airtightfish", "airtightfish"), ("badmonft", "badmonft"),
    ("theveeman", "theveeman"), ("real_y22", "real_y22"), ("chimpfone", "chimpfone"),
    ("brezscales", "brezscales"), ("changefomo", "change"), ("astaso1", "asta"),
    ("Binkieeefomo", "Binkieee"), ("shillcapital", "shillcapital"), ("goyimpnl", "goyim"),
    ("careful", "careful"), ("WoahWurdz", "Hiraethh"), ("dddddddddddd100", "jester"),
]
_TIER2 = ["poptyedev", "blknoiz06", "iruletrenches", "ProofMarkets", "Noxa_Fi", "hooddotfun"]
_OFFICIAL = ["vladtenev", "RobinhoodApp"]

def _entry(handle, role, fomo_handle=None):
    return {"handle": normalize_handle(handle), "display_handle": handle,
            "role": role, "fomo_handle": fomo_handle}


INITIAL_ROSTER = (
    [_entry(h, "tier1_trader", f) for h, f in _TIER1]
    + [_entry(h, "tier2_source") for h in _TIER2]
    + [_entry(h, "official_context") for h in _OFFICIAL]
)


def roster_counts() -> dict:
    out = {}
    for e in INITIAL_ROSTER:
        out[e["role"]] = out.get(e["role"], 0) + 1
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_roster.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/memecoin_x/roster.py tests/test_memecoin_x_roster.py
git commit -m "feat(memecoin-x): locked initial roster seed (27+6+2, _OHT_ corrected)"
```

---

### Task 6: Store persistence — roster upsert + idempotent capture persist

**Files:**
- Modify: `sniper/persistence/db_memecoin_x.py`
- Modify: `tests/test_memecoin_x_store.py`

**Interfaces:**
- Consumes: Task 1 tables; Task 4 `normalize_card` output shape; Task 5 `INITIAL_ROSTER`.
- Produces:
  - `seed_roster(conn, roster, *, now_iso) -> None` — normalize and upsert accounts by case-insensitive handle (role/fomo/profile update; preserve `added_at`; never rewrite historical observations).
  - `role_for(conn, handle) -> str|None` — normalize before lookup.
  - `record_capture_run(conn, run) -> None` — insert one `memecoin_x_capture_runs` row (`run` is a dict).
  - `persist_card(conn, normalized, *, observed_at, clock_domain, capture_run_id, now_iso) -> dict` — upsert the canonical post, append/deduplicate an immutable card observation, attach new relationships and mentions to that observation, and keep `last_observed_at` monotonic. Higher parser versions replace lower-version mention state. Return `{"new_post": bool, "new_relationship": bool, "new_observation": bool}`. A card with `eligible_for_forward=False` persists nothing. Replay callers must follow §7.3 ordering.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_memecoin_x_store.py
from sniper.analysis.memecoin_x import normalize as nz
from sniper.analysis.memecoin_x import roster as R
import sniper.persistence.db_memecoin_x as X

NOW = "2026-07-13T00:00:00+00:00"

def _seeded():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    X.seed_roster(conn, R.INITIAL_ROSTER, now_iso=NOW)
    return conn

def test_seed_roster_is_idempotent_and_sets_roles():
    conn = _seeded()
    X.seed_roster(conn, R.INITIAL_ROSTER, now_iso=NOW)  # again
    assert X.role_for(conn, "@ZincEth") == "tier1_trader"
    assert X.role_for(conn, "poptyedev") == "tier2_source"
    assert X.role_for(conn, "vladtenev") == "official_context"
    (n,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_accounts").fetchone()
    assert n == 35

def test_persist_card_idempotent_post_and_relationship():
    conn = _seeded()
    card = {"canonical_post_id": "1800000000000000001",
            "url": "https://x.com/zinceth/status/1800000000000000001",
            "author_handle": "zinceth", "actor_handle": "zinceth", "relationship": "direct",
            "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump $WIF",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    norm = nz.normalize_card(card)
    r1 = X.persist_card(conn, norm, observed_at=NOW, clock_domain="observed_real_utc",
                        capture_run_id="run1", now_iso=NOW)
    assert r1["new_post"] and r1["new_relationship"]
    r2 = X.persist_card(conn, norm, observed_at="2026-07-13T00:05:00+00:00",
                        clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    assert not r2["new_post"] and not r2["new_relationship"]
    # A delivery older than the current last-seen must not regress it.
    X.persist_card(conn, norm, observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run0", now_iso=NOW)
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    (rels,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_relationships").fetchone()
    assert posts == 1 and rels == 1
    (mentions,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_coin_mentions").fetchone()
    assert mentions == 2  # exact CA + ticker, each deduped across all three deliveries
    # last_observed_at advanced on re-delivery
    row = conn.execute("SELECT first_observed_at, last_observed_at, author_role_at_capture "
                       "FROM memecoin_x_forward_posts").fetchone()
    assert row["first_observed_at"] == NOW
    assert row["last_observed_at"] == "2026-07-13T00:05:00+00:00"
    assert row["author_role_at_capture"] == "tier1_trader"
    raw = json.loads(conn.execute(
        "SELECT raw_card_json FROM memecoin_x_forward_posts").fetchone()[0])
    assert raw == norm["raw_card"]
    rel_seen = conn.execute(
        "SELECT observed_at FROM memecoin_x_relationships").fetchone()[0]
    assert rel_seen == NOW

def test_relationship_stamps_tier1_flag():
    conn = _seeded()
    card = {"canonical_post_id": "1800000000000000009", "url": "u",
            "author_handle": "blknoiz06", "actor_handle": "_OHT_", "relationship": "repost",
            "full_text": "x", "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1"}
    X.persist_card(conn, nz.normalize_card(card), observed_at=NOW,
                   clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    rel = conn.execute("SELECT actor_role_at_obs, actor_is_tier1_at_obs "
                       "FROM memecoin_x_relationships").fetchone()
    assert rel["actor_role_at_obs"] == "tier1_trader"
    assert rel["actor_is_tier1_at_obs"] == 1

def test_diagnostic_card_persists_nothing():
    conn = _seeded()
    card = {"canonical_post_id": None, "url": "", "author_handle": "x", "actor_handle": "x",
            "relationship": "direct", "full_text": "hi", "text_complete": True,
            "posted_at_raw": None, "urls": [], "referenced_author": None,
            "referenced_post_id": None}
    r = X.persist_card(conn, nz.normalize_card(card), observed_at=NOW,
                       clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    assert not r["new_post"] and not r["new_relationship"]
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    assert posts == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_x_store.py -q`
Expected: FAIL with `AttributeError: module 'sniper.persistence.db_memecoin_x' has no attribute 'seed_roster'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to sniper/persistence/db_memecoin_x.py
from sniper.analysis.memecoin_x.normalize import normalize_handle

def seed_roster(conn, roster, *, now_iso) -> None:
    for e in roster:
        handle = normalize_handle(e["handle"])
        conn.execute("""
            INSERT INTO memecoin_x_accounts (handle, profile_url, role, fomo_handle,
                                             active, needs_review, provenance, added_at, updated_at)
            VALUES (?,?,?,?,1,0,'roster_seed',?,?)
            ON CONFLICT(handle) DO UPDATE SET
                profile_url=excluded.profile_url, role=excluded.role,
                fomo_handle=excluded.fomo_handle, active=1,
                updated_at=excluded.updated_at
        """, (handle, f"https://x.com/{handle}", e["role"],
              e.get("fomo_handle"), now_iso, now_iso))
    conn.commit()


def role_for(conn, handle):
    row = conn.execute("SELECT role FROM memecoin_x_accounts WHERE handle=?",
                       (normalize_handle(handle),)).fetchone()
    return row[0] if row else None


def record_capture_run(conn, run) -> None:
    conn.execute("""
        INSERT INTO memecoin_x_capture_runs
            (run_id, parser_version, started_at, finished_at, clock_offset_s, clock_offset_age_s,
             clock_domain, cards_seen, new_posts, new_relationships, parse_failures,
             prior_sentinel, newest_sentinel, sentinel_found, status, error_detail)
        VALUES (:run_id,:parser_version,:started_at,:finished_at,:clock_offset_s,:clock_offset_age_s,
                :clock_domain,:cards_seen,:new_posts,:new_relationships,:parse_failures,
                :prior_sentinel,:newest_sentinel,:sentinel_found,:status,:error_detail)
    """, {**{k: None for k in (
        "finished_at", "clock_offset_s", "clock_offset_age_s", "prior_sentinel",
        "newest_sentinel", "sentinel_found", "error_detail")},
        "cards_seen": 0, "new_posts": 0, "new_relationships": 0, "parse_failures": 0,
        "clock_domain": "legacy_unknown", **run})
    conn.commit()


def persist_card(conn, normalized, *, observed_at, clock_domain, capture_run_id, now_iso) -> dict:
    if not normalized.get("eligible_for_forward"):
        return {"new_post": False, "new_relationship": False}
    post = normalized["post"]
    rel = normalized["relationship"]
    author_role = role_for(conn, post["author_handle"])
    actor_role = role_for(conn, rel["actor_handle"])

    cur = conn.execute("SELECT 1 FROM memecoin_x_forward_posts WHERE canonical_post_id=?",
                       (post["canonical_post_id"],)).fetchone()
    new_post = cur is None
    if new_post:
        conn.execute("""
            INSERT INTO memecoin_x_forward_posts
                (canonical_post_id, url, author_handle, author_role_at_capture, full_text,
                 text_complete, posted_at_raw, posted_at, first_observed_at, last_observed_at,
                 capture_run_id, parser_version, clock_domain, extracted_urls, raw_card_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (post["canonical_post_id"], post["url"], post["author_handle"], author_role,
              post["full_text"], 1 if post["text_complete"] else 0,
              post["posted_at_raw"], post["posted_at"],
              observed_at, observed_at, capture_run_id, post["parser_version"], clock_domain,
              _json.dumps(post["urls"]), _json.dumps(normalized["raw_card"])))
    else:
        conn.execute("""
            UPDATE memecoin_x_forward_posts
            SET last_observed_at=?
            WHERE canonical_post_id=?
              AND julianday(?) > julianday(last_observed_at)
        """, (observed_at, post["canonical_post_id"], observed_at))

    is_tier1 = 1 if actor_role == "tier1_trader" else 0
    rel_key = (rel["canonical_post_id"], rel["actor_handle"], rel["relationship"])
    new_rel = conn.execute("""
        SELECT 1 FROM memecoin_x_relationships
        WHERE canonical_post_id=? AND actor_handle=? AND relationship=?
    """, rel_key).fetchone() is None
    if new_rel:
        conn.execute("""
            INSERT INTO memecoin_x_relationships
                (canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs,
                 relationship, observed_at, capture_run_id, clock_domain,
                 referenced_author, referenced_post_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (rel["canonical_post_id"], rel["actor_handle"], actor_role, is_tier1,
              rel["relationship"], observed_at, capture_run_id, clock_domain,
              rel["referenced_author"], rel["referenced_post_id"]))
    else:
        conn.execute("""
            UPDATE memecoin_x_relationships
            SET referenced_author=COALESCE(referenced_author, ?),
                referenced_post_id=COALESCE(referenced_post_id, ?)
            WHERE canonical_post_id=? AND actor_handle=? AND relationship=?
        """, (rel["referenced_author"], rel["referenced_post_id"], *rel_key))

    for m in normalized["mentions"]:
        conn.execute("""
            INSERT OR IGNORE INTO memecoin_x_coin_mentions
                (canonical_post_id, network_id, mint, ticker, extraction_method,
                 ambiguous, eligible, parser_version)
            VALUES (?,?,?,?,?,?,?,?)
        """, (post["canonical_post_id"], m["network_id"], m["mint"], m["ticker"],
              m["extraction_method"], 1 if m["ambiguous"] else 0, 1 if m["eligible"] else 0,
              post["parser_version"]))
    conn.commit()
    return {"new_post": new_post, "new_relationship": new_rel}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_x_store.py -q`
Expected: PASS (all store tests)

- [ ] **Step 5: Full Phase-1 suite + schema validation, then commit**

```bash
python3 -m pytest tests/test_memecoin_x_store.py tests/test_memecoin_x_normalize.py \
    tests/test_memecoin_x_clock_health.py tests/test_memecoin_x_roster.py -q
python3 -m pytest tests/test_brain_index.py -q
python3 scripts/brain_index.py schema memecoin_x_coin_mentions
python3 scripts/validate_schema_refs.py --files \
    sniper/persistence/db_memecoin_x.py sniper/analysis/memecoin_x/normalize.py \
    sniper/analysis/memecoin_x/roster.py
git diff --check
git add sniper/persistence/db_memecoin_x.py tests/test_memecoin_x_store.py
git commit -m "feat(memecoin-x): roster upsert + idempotent capture persistence"
```

---

## Execution Model

Run Tasks 1 through 6 sequentially; they share the schema and normalizer contracts and are not
parallel-safe. If subagent-driven execution is selected, dispatch one fresh worker at a time and
review its diff plus focused test output before starting the next task. Inline execution uses the
same task boundaries and commits.

---

## Out of scope (later phase plans)

- **Phase 2 — Browser adapter (read-only):** bounded Following-feed pass emitting the Task-4 card-fact dicts; sentinel/coverage-gap logic; jittered cadence; pause-on-challenge; `record_capture_run` wiring; clock-health gate applied to stamp `observed_real_utc` vs `legacy_unknown`. No account actions.
- **Phase 3 — Correlator + read models:** join eligible `memecoin_x_coin_mentions` through `source_observation_id` to later eligible `memecoin_feed_observations_v2` buys (`capture_version=2`, `clock_domain='observed_real_utc'`, `normalized_side='buy'`, `was_real_approved=1`), populate `memecoin_x_forward_links` (`amplified`/`coincidence` lanes, conservative `lead_seconds = fomo_observed_at - mention_observation_at`, 6h primary horizon), rolling 24h/7d/30d counts (distinct Tier-1 amplifiers; follow-through precision; buy coverage). No composite score.
- **Phase 4 — Output adapter (display-only):** suggestions (never auto-follow), notifications, optional draft theses. All display-only.
- **Backfill tool:** writes only `memecoin_x_backfill_candidates` (`evidence_mode='retrospective'`, CHECK-enforced); no promotion path into forward tables.

## Self-Review

- **Spec coverage:** §6 tables → Task 1 plus the review-fix plan (all 8, including immutable observations, NULL-safe mention identity, and brain-index registration). §7 clock → Task 2's single checked snapshot + `clock_domain` stamping in Task 6. §6.6 address/network rules → Task 3 (pair URLs are context, not token identity; Solana path case is preserved). §4/§8 relationship + eligibility → Task 4/Task 6. §3 canonicalized 27+6+2 roster (including `@_OHT_`) → Task 5. §6.5 relationship unique identity and §6.8 backfill CHECK → Task 1 tests. Correlator/browser/output are explicitly deferred to independently testable later phase plans.
- **Placeholder scan:** none — every step has runnable code/commands.
- **Type consistency:** `normalize_card` output keys (`post`, `relationship`, `mentions`, `raw_card`, `eligible_for_forward`) are consumed verbatim by `persist_card`; coin-mention keys (`network_id`, `mint`, `ticker`, `extraction_method`, `ambiguous`, `eligible`) match Task 1 columns and Task 3 output.
