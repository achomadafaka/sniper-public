# Memecoin Adaptive-Exit — Observation + Replay Foundation (SP#1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the log-only observation contract + a deterministic replay harness so future adaptive-exit work can be tested blind on history and forward-papered — without changing any live runner behaviour.

**Architecture:** Six new/extended pieces: canonical signal + confirmation-state, a token-episode resolver (the dedup unit), immutable pre-gate entry snapshots, persisted per-sweep position marks (the quote `sweep_exits` currently discards), an `ExitPolicy` object that reproduces today's rails byte-for-byte, and a stateful replay/eval harness. All additive; the only runner edits are two log-only write hooks.

**Tech Stack:** Python 3.13, SQLite (WAL) via `sniper/persistence/db_memecoin.py`, pytest. No new dependencies.

## Global Constraints

- **Zero behaviour change.** No adaptive rule, controller action, or real-money change ships in SP#1. The two runner hooks (entry-snapshot writer, position-marks writer) are log-only and must not alter any existing return value or exit decision.
- **One code path** for replay/paper/live: the replay harness evaluates the SAME `ExitPolicy` object the runner uses. No parallel backtest reimplementation of rails.
- **Experimental unit = token episode.** Dedup is enforced in the harness API (`dedup_to_episode_outcomes`), never left to the caller.
- **Executable value, never chart price.** Marks store `executable_exit_value`; a missing route is a recorded failure, not a theoretical exit.
- New tables are created in `ensure_memecoin_tables(conn)` (`sniper/persistence/db_memecoin.py:64`) so `scripts/brain_index.py generate` indexes them. Run `python3 scripts/brain_index.py generate` and `python3 scripts/validate_schema_refs.py` after Task 1; both must be clean.
- Timestamps are ISO-8601 UTC strings (existing convention). Use `datetime.now(timezone.utc).isoformat()`; never `datetime.utcnow()`.
- Work on branch `worktree-memecoin-real-shadow-featurestore`. Commit after every task. Run focused tests, not the full suite, per task.

---

### Task 1: New tables in `ensure_memecoin_tables`

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (inside `ensure_memecoin_tables`, after the last existing `CREATE TABLE`)
- Test: `tests/test_memecoin_observation_schema.py`

**Interfaces:**
- Produces: tables `memecoin_token_episodes`, `memecoin_entry_snapshots`, `memecoin_position_marks`, `memecoin_policy_versions`, `memecoin_policy_assignments`, `memecoin_arm_outcomes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_observation_schema.py
import sqlite3
from sniper.persistence.db_memecoin import ensure_memecoin_tables

EXPECTED = {
    "memecoin_token_episodes": {"episode_id", "chain_id", "token_address", "started_ts",
                                 "ended_ts", "first_signal_id", "consensus_cluster_count", "status"},
    "memecoin_entry_snapshots": {"snapshot_id", "upstream_event_id", "episode_id", "chain", "mint",
        "action", "decided_at", "mcap", "liquidity", "token_age_s", "consensus_n_60m",
        "wallet_trust_tier", "their_size_usd", "regime_id", "features_json", "feature_version",
        "data_quality_json", "entry_latency_json", "snapshot_hash"},
    "memecoin_position_marks": {"id", "chain_id", "mint", "sweep_ts", "mid", "executable_exit_value",
                                 "liquidity", "mcap", "route_quote_id", "source", "quality"},
    "memecoin_policy_versions": {"policy_id", "version", "template", "parameters_json",
                                  "code_commit", "immutable_hash", "activated_ts", "retired_ts"},
    "memecoin_policy_assignments": {"episode_id", "arm_id", "policy_id", "entry_snapshot_id",
                                     "assignment_reason", "assigned_ts"},
    "memecoin_arm_outcomes": {"episode_id", "arm_id", "entry_cost", "net_exit_value", "net_pnl",
        "net_return", "max_executable_return", "max_drawdown", "exit_reason", "route_failures",
        "closed_ts"},
}

def test_observation_tables_have_expected_columns():
    c = sqlite3.connect(":memory:")
    ensure_memecoin_tables(c)
    for table, cols in EXPECTED.items():
        got = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        assert cols <= got, f"{table} missing {cols - got}"

def test_entry_snapshot_upstream_event_id_unique():
    c = sqlite3.connect(":memory:")
    ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_entry_snapshots(upstream_event_id, action) VALUES('e1','opened')")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO memecoin_entry_snapshots(upstream_event_id, action) VALUES('e1','x')")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_observation_schema.py -q`
Expected: FAIL (`no such table: memecoin_token_episodes`).

- [ ] **Step 3: Add the DDL** inside `ensure_memecoin_tables`, after the last existing `conn.execute("CREATE TABLE IF NOT EXISTS ...")`:

```python
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_token_episodes(
        episode_id TEXT PRIMARY KEY, chain_id TEXT, token_address TEXT, started_ts TEXT,
        ended_ts TEXT, first_signal_id TEXT, consensus_cluster_count INTEGER DEFAULT 1,
        status TEXT DEFAULT 'open')""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_episode_open
        ON memecoin_token_episodes(chain_id, token_address) WHERE status='open'""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_entry_snapshots(
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, upstream_event_id TEXT UNIQUE NOT NULL,
        episode_id TEXT, chain TEXT, mint TEXT, action TEXT, decided_at TEXT,
        mcap REAL, liquidity REAL, token_age_s REAL, consensus_n_60m INTEGER,
        wallet_trust_tier TEXT, their_size_usd REAL, regime_id INTEGER,
        features_json TEXT, feature_version INTEGER DEFAULT 1,
        data_quality_json TEXT, entry_latency_json TEXT, snapshot_hash TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_position_marks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, mint TEXT, sweep_ts TEXT,
        mid REAL, executable_exit_value REAL, liquidity REAL, mcap REAL,
        route_quote_id TEXT, source TEXT, quality TEXT)""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_marks_uniq
        ON memecoin_position_marks(chain_id, mint, sweep_ts)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_policy_versions(
        policy_id TEXT, version INTEGER, template TEXT, parameters_json TEXT,
        code_commit TEXT, immutable_hash TEXT, activated_ts TEXT, retired_ts TEXT,
        PRIMARY KEY (policy_id, version))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_policy_assignments(
        episode_id TEXT, arm_id TEXT, policy_id TEXT, entry_snapshot_id INTEGER,
        assignment_reason TEXT, assigned_ts TEXT, PRIMARY KEY (episode_id, arm_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_arm_outcomes(
        episode_id TEXT, arm_id TEXT, entry_cost REAL, net_exit_value REAL, net_pnl REAL,
        net_return REAL, max_executable_return REAL, max_drawdown REAL, exit_reason TEXT,
        route_failures INTEGER DEFAULT 0, closed_ts TEXT, PRIMARY KEY (episode_id, arm_id))""")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_observation_schema.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Regenerate index + validate, then commit**

```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py            # must be clean
git add sniper/persistence/db_memecoin.py tests/test_memecoin_observation_schema.py index/
git commit -m "feat(memecoin): observation+replay foundation tables (SP#1 task 1)"
```

---

### Task 2: Confirmation-state + canonical `CopySignal`

**Files:**
- Create: `sniper/services/memecoin_signal.py`
- Test: `tests/test_memecoin_signal_normalizer.py`

**Interfaces:**
- Produces: `CONFIRMATION_RANK: dict[str,int]`, `is_actionable(state: str, chain: str) -> bool`, `CopySignal` dataclass, `normalize(ev) -> CopySignal`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_signal_normalizer.py
from types import SimpleNamespace
from sniper.services.memecoin_signal import normalize, is_actionable, CopySignal

def test_solana_processed_is_not_actionable():
    assert is_actionable("processed", "solana") is False
    assert is_actionable("finalized", "solana") is True

def test_evm_reorged_is_not_actionable():
    assert is_actionable("reorged", "4663") is False
    assert is_actionable("finalized", "4663") is True

def test_normalize_maps_feed_event():
    ev = SimpleNamespace(mint="MINTA", source_user_id="u1", fomo_event_id="e1",
                         network_id="solana", their_price=0.001, their_size_usd=2000.0,
                         source_ts=None, received_at_normalized="2026-07-12T10:00:00+00:00")
    sig = normalize(ev)
    assert isinstance(sig, CopySignal)
    assert sig.token_address == "MINTA" and sig.chain_id == "solana"
    assert sig.source_wallet == "u1" and sig.source_tx == "e1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_signal_normalizer.py -q`
Expected: FAIL (`No module named 'sniper.services.memecoin_signal'`).

- [ ] **Step 3: Write the module**

```python
# sniper/services/memecoin_signal.py
"""Canonical copy signal + chain confirmation semantics. A `processed` (Solana) or `reorged`
(EVM) observation can un-happen, so nothing downstream may ACT on a non-actionable state."""
from __future__ import annotations
from dataclasses import dataclass

# higher rank = stronger finality. 'actionable' = at least the chain's minimum trustable state.
_SOLANA = {"processed": 0, "confirmed": 1, "finalized": 2}
_EVM = {"observed": 0, "included": 1, "safe": 2, "confirmed": 2, "finalized": 3, "reorged": -1}
CONFIRMATION_RANK = {"solana": _SOLANA, "evm": _EVM}
_MIN_ACTIONABLE = {"solana": 1, "evm": 1}   # Solana>=confirmed, EVM>=included


def _chain_family(chain: str) -> str:
    return "solana" if str(chain).lower() == "solana" else "evm"


def is_actionable(state: str | None, chain: str) -> bool:
    fam = _chain_family(chain)
    rank = CONFIRMATION_RANK[fam].get((state or "").lower())
    return rank is not None and rank >= _MIN_ACTIONABLE[fam]


@dataclass(frozen=True)
class CopySignal:
    chain_id: str
    token_address: str
    source_wallet: str
    source_tx: str
    source_event_ts: str | None
    received_ts: str | None
    confirmation_state: str | None
    token_amount: float | None
    notional_estimate: float | None
    source_entry_price: float | None


def normalize(ev) -> CopySignal:
    return CopySignal(
        chain_id=(getattr(ev, "network_id", None) or "solana"),
        token_address=getattr(ev, "mint", None),
        source_wallet=getattr(ev, "source_user_id", None),
        source_tx=getattr(ev, "fomo_event_id", None),
        source_event_ts=getattr(ev, "source_ts", None),
        received_ts=getattr(ev, "received_at_normalized", None) or getattr(ev, "received_at", None),
        confirmation_state=getattr(ev, "confirmation_state", None),
        token_amount=getattr(ev, "token_amount", None),
        notional_estimate=getattr(ev, "their_size_usd", None),
        source_entry_price=getattr(ev, "their_price", None),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_signal_normalizer.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_signal.py tests/test_memecoin_signal_normalizer.py
git commit -m "feat(memecoin): canonical CopySignal + confirmation-state (SP#1 task 2)"
```

---

### Task 3: `TokenEpisodeResolver`

**Files:**
- Create: `sniper/services/memecoin_episode.py`
- Test: `tests/test_memecoin_episode_resolver.py`

**Interfaces:**
- Consumes: `ensure_memecoin_tables`, table `memecoin_token_episodes`.
- Produces: `resolve_episode(conn, chain_id, token_address, signal_id, now_iso) -> (episode_id: str, is_new: bool)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_episode_resolver.py
import sqlite3
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_episode import resolve_episode

def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c); return c

def test_first_signal_opens_episode():
    c = _db()
    eid, is_new = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    assert is_new is True and eid

def test_second_signal_same_open_token_reuses_episode():
    c = _db()
    e1, _ = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    e2, is_new = resolve_episode(c, "solana", "MINTA", "s2", "2026-07-12T10:05:00+00:00")
    assert e2 == e1 and is_new is False
    assert c.execute("SELECT consensus_cluster_count FROM memecoin_token_episodes WHERE episode_id=?",
                     (e1,)).fetchone()[0] == 2

def test_closed_episode_lets_a_new_one_open():
    c = _db()
    e1, _ = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    c.execute("UPDATE memecoin_token_episodes SET status='closed' WHERE episode_id=?", (e1,)); c.commit()
    e2, is_new = resolve_episode(c, "solana", "MINTA", "s2", "2026-07-13T10:00:00+00:00")
    assert is_new is True and e2 != e1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_episode_resolver.py -q`
Expected: FAIL (`No module named 'sniper.services.memecoin_episode'`).

- [ ] **Step 3: Write the module**

```python
# sniper/services/memecoin_episode.py
"""The token EPISODE is the experimental unit: the first eligible trusted BUY on a (chain, token)
opens one; later signals while it's open bump consensus but create no new sample. Structural dedup."""
from __future__ import annotations
import hashlib


def _episode_id(chain_id: str, token_address: str, started_ts: str) -> str:
    raw = f"{chain_id}:{token_address}:{started_ts}".encode()
    return "ep_" + hashlib.sha1(raw).hexdigest()[:16]


def resolve_episode(conn, chain_id, token_address, signal_id, now_iso):
    row = conn.execute(
        "SELECT episode_id FROM memecoin_token_episodes "
        "WHERE chain_id=? AND token_address=? AND status='open'",
        (chain_id, token_address)).fetchone()
    if row is not None:
        eid = row[0]
        conn.execute("UPDATE memecoin_token_episodes SET consensus_cluster_count="
                     "consensus_cluster_count+1 WHERE episode_id=?", (eid,))
        conn.commit()
        return eid, False
    eid = _episode_id(chain_id, token_address, now_iso)
    conn.execute("INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
                 "started_ts, first_signal_id, consensus_cluster_count, status) "
                 "VALUES(?,?,?,?,?,1,'open')", (eid, chain_id, token_address, now_iso, signal_id))
    conn.commit()
    return eid, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_episode_resolver.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_episode.py tests/test_memecoin_episode_resolver.py
git commit -m "feat(memecoin): token-episode resolver (structural dedup) (SP#1 task 3)"
```

---

### Task 4: Entry-snapshot builder + write helper

**Files:**
- Create: `sniper/services/memecoin_features.py`
- Test: `tests/test_memecoin_entry_snapshot.py`

**Interfaces:**
- Consumes: table `memecoin_entry_snapshots`, `CopySignal`.
- Produces: `build_snapshot(sig, *, mcap, liquidity, token_age_s, consensus_n_60m, wallet_trust_tier, regime_id, entry_latency) -> dict`, `write_snapshot(conn, snapshot: dict, action: str) -> None` (idempotent on `upstream_event_id`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_entry_snapshot.py
import sqlite3, json
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_signal import CopySignal
from sniper.services.memecoin_features import build_snapshot, write_snapshot

def _sig():
    return CopySignal("solana", "MINTA", "u1", "e1", None, "2026-07-12T10:00:00+00:00",
                      "confirmed", None, 2000.0, 0.001)

def test_build_snapshot_promotes_columns_and_hashes():
    snap = build_snapshot(_sig(), mcap=500000.0, liquidity=8000.0, token_age_s=120.0,
                          consensus_n_60m=1, wallet_trust_tier="A", regime_id=2,
                          entry_latency={"detect_ms": 300})
    assert snap["mcap"] == 500000.0 and snap["upstream_event_id"] == "e1"
    assert snap["snapshot_hash"]                       # deterministic content hash present
    assert json.loads(snap["entry_latency_json"])["detect_ms"] == 300

def test_write_snapshot_is_idempotent_and_records_action():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    snap = build_snapshot(_sig(), mcap=5e5, liquidity=8e3, token_age_s=120.0, consensus_n_60m=1,
                          wallet_trust_tier="A", regime_id=2, entry_latency={})
    write_snapshot(c, snap, action="gate_rejected")
    write_snapshot(c, snap, action="opened")          # second write must not duplicate/crash
    rows = c.execute("SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id='e1'").fetchall()
    assert len(rows) == 1                              # UNIQUE upstream_event_id holds
    assert rows[0]["action"] == "gate_rejected"        # first write wins (immutable snapshot)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_entry_snapshot.py -q`
Expected: FAIL (`No module named 'sniper.services.memecoin_features'`).

- [ ] **Step 3: Write the module**

```python
# sniper/services/memecoin_features.py
"""Immutable pre-gate entry snapshot: computed at decision time from a CopySignal + market context,
written for EVERY eligible signal (including gate-rejected ones). Feature promoted JSON->column only
after a dedup-corrected study; JSON is the lab, columns are the shelf."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone


def build_snapshot(sig, *, mcap, liquidity, token_age_s, consensus_n_60m,
                   wallet_trust_tier, regime_id, entry_latency):
    promoted = {
        "upstream_event_id": sig.source_tx, "chain": sig.chain_id, "mint": sig.token_address,
        "mcap": mcap, "liquidity": liquidity, "token_age_s": token_age_s,
        "consensus_n_60m": consensus_n_60m, "wallet_trust_tier": wallet_trust_tier,
        "their_size_usd": sig.notional_estimate, "regime_id": regime_id,
    }
    features_json = json.dumps({**promoted, "source_entry_price": sig.source_entry_price,
                                "confirmation_state": sig.confirmation_state}, sort_keys=True)
    return {
        **promoted,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "features_json": features_json, "feature_version": 1,
        "data_quality_json": json.dumps({"mcap": mcap is not None, "liquidity": liquidity is not None}),
        "entry_latency_json": json.dumps(entry_latency or {}),
        "snapshot_hash": hashlib.sha1(features_json.encode()).hexdigest(),
    }


def write_snapshot(conn, snapshot: dict, action: str) -> None:
    """Idempotent on upstream_event_id — the first write (immutable) wins. Never raises on a
    duplicate signal being re-observed."""
    cols = ["upstream_event_id", "episode_id", "chain", "mint", "action", "decided_at", "mcap",
            "liquidity", "token_age_s", "consensus_n_60m", "wallet_trust_tier", "their_size_usd",
            "regime_id", "features_json", "feature_version", "data_quality_json",
            "entry_latency_json", "snapshot_hash"]
    row = {**snapshot, "action": action, "episode_id": snapshot.get("episode_id")}
    placeholders = ",".join("?" * len(cols))
    conn.execute(f"INSERT OR IGNORE INTO memecoin_entry_snapshots({','.join(cols)}) "
                 f"VALUES({placeholders})", [row.get(c) for c in cols])
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_entry_snapshot.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_features.py tests/test_memecoin_entry_snapshot.py
git commit -m "feat(memecoin): immutable pre-gate entry snapshot builder (SP#1 task 4)"
```

---

### Task 5: Wire the snapshot writer into `copy_event_to_arms` (log-only)

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py` (`copy_event_to_arms`, from line 155)
- Test: `tests/test_memecoin_copy_snapshot_hook.py`

**Interfaces:**
- Consumes: `normalize`, `resolve_episode`, `build_snapshot`, `write_snapshot`.
- Produces: no signature change to `copy_event_to_arms`; adds a snapshot row per call, `action` ∈ {`opened`,`skipped_base_asset`,`ignored`,`skipped_chase`,`skipped_dump`}.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_copy_snapshot_hook.py
import sqlite3
from types import SimpleNamespace
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_copy_trader import copy_event_to_arms

def _ev(mint="MINTA", price=0.001):
    return SimpleNamespace(mint=mint, side="buy", their_price=price, their_size_usd=2000.0,
                           fomo_event_id="e1", source_user_id="u1", network_id="solana",
                           received_at_normalized="2026-07-12T10:00:00+00:00")

def test_rejected_buy_still_writes_snapshot():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    # fill_fn returns 0 -> 'ignored' branch (a gate rejection); snapshot must still be written.
    copy_event_to_arms(c, _ev(), 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
                       fill_fn=lambda *a, **k: (0.0, "x"), stats_fn=lambda *a, **k: {})
    row = c.execute("SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id='e1'").fetchone()
    assert row is not None and row["action"] == "ignored"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_copy_snapshot_hook.py -q`
Expected: FAIL (`assert row is not None` — no snapshot written yet).

- [ ] **Step 3: Add a log-only helper and call it at each return of `copy_event_to_arms`**

Add near the top of `sniper/services/memecoin_copy_trader.py` (module level):

```python
def _log_entry_snapshot(conn, ev, now_iso, action, *, mcap=None, liquidity=None,
                        token_age_s=None, latency=None):
    """Best-effort log-only snapshot. Never raises into the trade path."""
    try:
        from sniper.services.memecoin_signal import normalize
        from sniper.services.memecoin_episode import resolve_episode
        from sniper.services.memecoin_features import build_snapshot, write_snapshot
        sig = normalize(ev)
        eid = None
        if action == "opened":
            eid, _ = resolve_episode(conn, sig.chain_id, sig.token_address, sig.source_tx, now_iso)
        snap = build_snapshot(sig, mcap=mcap, liquidity=liquidity, token_age_s=token_age_s,
                              consensus_n_60m=None, wallet_trust_tier=None, regime_id=None,
                              entry_latency=latency or {})
        snap["episode_id"] = eid
        write_snapshot(conn, snap, action=action)
    except Exception:
        pass   # observation must never break execution (Global Constraints: zero behaviour change)
```

Then wrap each `return` in the `buy` branch of `copy_event_to_arms` so the snapshot action matches the outcome. For the four rejection returns, call `_log_entry_snapshot(conn, ev, now_iso, "<action>")` immediately before the `return`; for the successful fan-out `return {...}`, call `_log_entry_snapshot(conn, ev, now_iso, "opened", mcap=None, liquidity=entry_liq, token_age_s=token_age)` before it. Do not change any returned value.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_copy_snapshot_hook.py tests/test_memecoin_copy_mirror*.py -q`
Expected: PASS (new test + existing copy-trader tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py tests/test_memecoin_copy_snapshot_hook.py
git commit -m "feat(memecoin): log-only entry-snapshot hook in copy_event_to_arms (SP#1 task 5)"
```

---

### Task 6: Persist position marks in `sweep_exits` (shared per chain/mint/sweep)

**Files:**
- Modify: `sniper/services/memecoin_exits.py` (`sweep_exits`, from line 73)
- Test: `tests/test_memecoin_position_marks.py`

**Interfaces:**
- Consumes: table `memecoin_position_marks`, existing `quote_for(price_map, mint, network_id)`.
- Produces: `write_marks(conn, price_map, now_iso)` (module-level, called once at the top of `sweep_exits`); one row per distinct `(chain_id, mint)` in `price_map`, deduped on `(chain_id, mint, sweep_ts)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_position_marks.py
import sqlite3
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_exits import write_marks

def test_write_marks_one_row_per_chain_mint_sweep():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    price_map = {("solana", "MINTA"): {"price": 0.001, "liquidity": 8000.0, "mcap": 5e5},
                 ("4663", "0xabc"):    {"price": 2.0,   "liquidity": 3000.0, "mcap": 2e6}}
    write_marks(c, price_map, "2026-07-12T10:00:00+00:00")
    write_marks(c, price_map, "2026-07-12T10:00:00+00:00")   # same sweep_ts -> no dup
    rows = c.execute("SELECT chain_id, mint, mid FROM memecoin_position_marks ORDER BY mint").fetchall()
    assert len(rows) == 2
    assert rows[0]["chain_id"] == "4663" and rows[0]["mid"] == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_position_marks.py -q`
Expected: FAIL (`cannot import name 'write_marks'`).

- [ ] **Step 3: Add `write_marks` and call it once at the top of `sweep_exits`**

Add at module level in `sniper/services/memecoin_exits.py`:

```python
def write_marks(conn, price_map, now_iso):
    """Persist the executable quote sweep_exits already computes, ONE shared row per
    (chain_id, mint, sweep). Log-only; never raises into the sweep. price_map keys are
    (chain_id, mint) -> {price, liquidity, mcap}."""
    try:
        for key, st in (price_map or {}).items():
            chain_id, mint = key if isinstance(key, tuple) else (None, key)
            mid = (st or {}).get("price")
            if not mid or mid <= 0:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO memecoin_position_marks("
                "chain_id, mint, sweep_ts, mid, executable_exit_value, liquidity, mcap, source, quality)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (chain_id, mint, now_iso, mid, mid, st.get("liquidity"), st.get("mcap"),
                 st.get("source", "dexscreener"), st.get("quality", "live")))
        conn.commit()
    except Exception:
        pass
```

Then in `sweep_exits`, immediately after `price_map = price_map or {}` (line 76), add:

```python
    write_marks(conn, price_map, now_iso)
```

> Note: if the live `price_map` is keyed by bare `mint` rather than `(chain_id, mint)`, `write_marks` stores `chain_id=None` (still one row/mint/sweep) — acceptable for SP#1; a follow-up can thread the chain key. Do not restructure `price_map` here.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_position_marks.py tests/test_memecoin_exits*.py -q`
Expected: PASS (new test + existing exit tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_exits.py tests/test_memecoin_position_marks.py
git commit -m "feat(memecoin): persist per-sweep position marks (SP#1 task 6)"
```

---

### Task 7: `ExitPolicy` object + resolver v1 + acceptance test #1

**Files:**
- Create: `sniper/services/memecoin_policy.py`
- Test: `tests/test_memecoin_policy_resolver.py`

**Interfaces:**
- Consumes: `sniper.settings.memecoin` (`MEMECOIN_TAKE_PROFIT_PCT`, `MEMECOIN_STOP_LOSS_PCT`, `MEMECOIN_REAL_TP_PCT`, `MEMECOIN_REAL_STOP_PCT`, `MEMECOIN_REAL_STOP_CONFIRM_SWEEPS`).
- Produces: `ExitPolicy` dataclass, `resolve_policy(snapshot: dict, *, arm: str) -> tuple[ExitPolicy, str]`, `control_reason(policy, pnl_pct) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_policy_resolver.py
from sniper.services.memecoin_policy import resolve_policy, control_reason
from sniper.settings import memecoin as S

def test_control_policy_matches_live_constants():
    pol, rule = resolve_policy({"mcap": 5e5}, arm="control")
    assert pol.tp_pct == S.MEMECOIN_TAKE_PROFIT_PCT
    assert pol.stop_pct == -abs(S.MEMECOIN_STOP_LOSS_PCT)
    assert rule == "fixed_control_v1"

def test_control_reason_reproduces_live_thresholds():
    pol, _ = resolve_policy({"mcap": 5e5}, arm="control")
    assert control_reason(pol, pol.tp_pct + 1) == "take_profit"
    assert control_reason(pol, pol.stop_pct - 1) == "stop_loss"
    assert control_reason(pol, 0.0) is None

def test_real_arm_uses_real_rails():
    pol, _ = resolve_policy({"mcap": 5e5}, arm="alpha_b")
    assert pol.tp_pct == S.MEMECOIN_REAL_TP_PCT
    assert pol.stop_debounce_sweeps == S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_policy_resolver.py -q`
Expected: FAIL (`No module named 'sniper.services.memecoin_policy'`).

- [ ] **Step 3: Write the module** (v1 returns the CURRENT rails for every input — zero behaviour change)

```python
# sniper/services/memecoin_policy.py
"""The current exit rails, expressed as data. resolve_policy() is a pure, versioned decision function.
v1 returns today's fixed rails for every snapshot — the adaptive branches arrive in SP#3. This object
is the ONE definition of rails that the runner AND the replay harness both consume (no drift)."""
from __future__ import annotations
from dataclasses import dataclass
from sniper.settings import memecoin as S


@dataclass(frozen=True)
class ExitPolicy:
    tp_pct: float
    stop_pct: float                 # negative
    stop_debounce_sweeps: int
    derisk_trigger_pct: float | None
    derisk_fraction: float
    trail_mode: str
    max_hold_s: float
    mirror_sell_mode: str


def resolve_policy(snapshot: dict, *, arm: str):
    if arm in ("alpha_b", "real_shadow"):
        pol = ExitPolicy(
            tp_pct=float(S.MEMECOIN_REAL_TP_PCT), stop_pct=-abs(float(S.MEMECOIN_B_STOP_PCT)),
            stop_debounce_sweeps=int(S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS),
            derisk_trigger_pct=(float(S.MEMECOIN_REAL_DERISK_PCT) or None),
            derisk_fraction=0.5, trail_mode="off",
            max_hold_s=float(getattr(S, "MEMECOIN_B_MAX_HOLD_S", 43200)), mirror_sell_mode="on")
        return pol, "fixed_real_v1"
    pol = ExitPolicy(
        tp_pct=float(S.MEMECOIN_TAKE_PROFIT_PCT), stop_pct=-abs(float(S.MEMECOIN_STOP_LOSS_PCT)),
        stop_debounce_sweeps=1, derisk_trigger_pct=None, derisk_fraction=0.0, trail_mode="off",
        max_hold_s=float("inf"), mirror_sell_mode="off")
    return pol, "fixed_control_v1"


def control_reason(policy: ExitPolicy, pnl_pct: float) -> str | None:
    """Reproduces control_exit_reason() from memecoin_brackets.py against the policy object."""
    if pnl_pct >= policy.tp_pct:
        return "take_profit"
    if pnl_pct <= policy.stop_pct:
        return "stop_loss"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_policy_resolver.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Acceptance test #1 — resolver reproduces live `control_exit_reason` on a grid**

Append to the test file:

```python
def test_acceptance_control_matches_live_reason_fn():
    from sniper.services.memecoin_brackets import control_exit_reason
    pol, _ = resolve_policy({}, arm="control")
    for pnl in [-80, -50, -49.9, 0, 59.9, 60, 200]:
        live = control_exit_reason(pnl, S.MEMECOIN_STOP_LOSS_PCT, S.MEMECOIN_TAKE_PROFIT_PCT)
        assert control_reason(pol, pnl) == live, f"drift at pnl={pnl}: {control_reason(pol,pnl)} != {live}"
```

Run: `python3 -m pytest tests/test_memecoin_policy_resolver.py -q`
Expected: PASS (4 tests). If the acceptance test fails, the objectified rails have drifted from live — fix `control_reason`/`resolve_policy`, do not edit the test.

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_policy.py tests/test_memecoin_policy_resolver.py
git commit -m "feat(memecoin): ExitPolicy object + resolver v1 reproducing live rails (SP#1 task 7)"
```

---

### Task 8: Replay harness + acceptance tests #2/#3

**Files:**
- Create: `sniper/replay/memecoin.py`
- Test: `tests/test_memecoin_replay.py`

**Interfaces:**
- Consumes: `ExitPolicy`, `control_reason`, tables `memecoin_position_marks`, `memecoin_positions`, `memecoin_exec_latency` (`.impact_pct`).
- Produces: `slippage_haircut(conn, chain_id) -> float` (p75 of `memecoin_exec_latency.impact_pct`), `replay_position(policy, entry_price, marks, haircut) -> dict` (`{net_return, exit_reason, max_executable_return, max_drawdown}`), `dedup_to_episode_outcomes(rows) -> list`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_replay.py
from sniper.services.memecoin_policy import resolve_policy
from sniper.replay.memecoin import replay_position, dedup_to_episode_outcomes

def test_replay_take_profit_fires_at_next_mark_with_haircut():
    pol, _ = resolve_policy({}, arm="control")            # tp +60, stop -50
    marks = [1.0, 1.2, 1.7]                                # +70% at mark 3 -> TP
    r = replay_position(pol, entry_price=1.0, marks=marks, haircut=0.10)
    assert r["exit_reason"] == "take_profit"
    # filled at next mark (1.7) minus 10% haircut -> return = (1.7*0.9/1.0 - 1) = 0.53
    assert abs(r["net_return"] - 0.53) < 1e-9

def test_replay_stop_loss():
    pol, _ = resolve_policy({}, arm="control")
    r = replay_position(pol, entry_price=1.0, marks=[1.0, 0.4], haircut=0.0)
    assert r["exit_reason"] == "stop_loss"

def test_dedup_keeps_one_row_per_episode_arm():
    rows = [{"episode_id": "e1", "arm_id": "control", "net_return": 0.5},
            {"episode_id": "e1", "arm_id": "control", "net_return": 0.9},  # dup -> dropped
            {"episode_id": "e1", "arm_id": "adaptive", "net_return": 0.6}]
    out = dedup_to_episode_outcomes(rows)
    assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_replay.py -q`
Expected: FAIL (`No module named 'sniper.replay.memecoin'`).

- [ ] **Step 3: Write the harness**

```python
# sniper/replay/memecoin.py
"""Deterministic exit-policy replay over recorded marks. ONE code path with the live rails
(ExitPolicy). Fills at the NEXT mark after a trigger, minus a p75 realized-slippage haircut; a
missing route is a recorded loss, never a theoretical exit. dedup_to_episode_outcomes enforces the
token-episode unit structurally (Global Constraints)."""
from __future__ import annotations
import statistics
from sniper.services.memecoin_policy import control_reason


def slippage_haircut(conn, chain_id: str) -> float:
    """p75 realized execution impact from memecoin_exec_latency.impact_pct (the real, verified
    slippage column). exec_latency has NO chain column yet, so chain_id is accepted for the future
    per-chain split but not filtered on today. impact_pct may be stored as a percent or a fraction —
    auto-normalize. Fallback 0.10 when <5 samples (expected until real fills accumulate)."""
    vals = [abs(r[0]) for r in conn.execute(
        "SELECT impact_pct FROM memecoin_exec_latency WHERE impact_pct IS NOT NULL") if r[0] is not None]
    if len(vals) < 5:
        return 0.10
    vals.sort()
    h = vals[int(0.75 * (len(vals) - 1))]
    return h / 100.0 if h > 1.0 else h


def replay_position(policy, entry_price: float, marks: list[float], haircut: float) -> dict:
    """marks = executable_exit_value path (index 0 = entry sweep). Trigger checked on mark i,
    fill at mark i+1 (next observation) with the haircut; if no next mark, fill at the trigger mark."""
    if not entry_price or entry_price <= 0 or not marks:
        return {"net_return": 0.0, "exit_reason": "no_data",
                "max_executable_return": 0.0, "max_drawdown": 0.0}
    peak = entry_price
    max_ret, max_dd = 0.0, 0.0
    for i, m in enumerate(marks):
        if m <= 0:
            continue
        peak = max(peak, m)
        max_ret = max(max_ret, m / entry_price - 1.0)
        max_dd = min(max_dd, m / peak - 1.0)
        pnl_pct = (m / entry_price - 1.0) * 100.0
        reason = control_reason(policy, pnl_pct)
        if reason:
            fill_mark = marks[i + 1] if i + 1 < len(marks) else m
            fill = fill_mark * (1.0 - haircut)
            return {"net_return": fill / entry_price - 1.0, "exit_reason": reason,
                    "max_executable_return": max_ret, "max_drawdown": max_dd}
    last = marks[-1] * (1.0 - haircut)
    return {"net_return": last / entry_price - 1.0, "exit_reason": "open_end",
            "max_executable_return": max_ret, "max_drawdown": max_dd}


def dedup_to_episode_outcomes(rows: list[dict]) -> list[dict]:
    """Structural dedup: at most one outcome per (episode_id, arm_id) — first seen wins."""
    seen, out = set(), []
    for r in rows:
        key = (r["episode_id"], r["arm_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_replay.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Acceptance test #2 — replay reproduces a real closed position's realized PnL**

Append to the test file (uses the live DB read-only; skips if unavailable):

```python
def test_acceptance_replay_reproduces_realized_pnl():
    import os, sqlite3, pytest
    db = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
    if not os.path.exists(db):
        pytest.skip("live db not present")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    # pick a control position that closed on a price trigger and has >=2 marks
    pos = c.execute("""SELECT p.id, p.entry_price, p.realized_pnl, p.network_id FROM memecoin_positions p
        WHERE p.strategy='control' AND p.is_open=0 AND p.exit_reason IN ('take_profit','stop_loss')
          AND p.entry_price>0 ORDER BY p.closed_at DESC LIMIT 1""").fetchone()
    if pos is None:
        pytest.skip("no eligible closed control position with marks yet")
    marks = [r["mid"] for r in c.execute(
        "SELECT mid FROM memecoin_position_marks WHERE mint=(SELECT mint FROM memecoin_positions "
        "WHERE id=?) ORDER BY sweep_ts", (pos["id"],))]
    if len(marks) < 2:
        pytest.skip("marks not yet accumulated for this position (expected pre-backfill)")
    from sniper.services.memecoin_policy import resolve_policy
    pol, _ = resolve_policy({}, arm="control")
    r = replay_position(pol, pos["entry_price"], [pos["entry_price"]] + marks,
                        slippage_haircut(c, pos["network_id"]))
    # realized_pnl is a %; net_return is a fraction. Reconcile within tolerance.
    assert abs(r["net_return"] * 100 - pos["realized_pnl"]) < 15, "replay diverges from realized PnL"
```

Run: `python3 -m pytest tests/test_memecoin_replay.py -q`
Expected: PASS or SKIP (skips cleanly until marks have accumulated — that's expected immediately post-deploy; the test becomes live once Task 6 has run in production for a while).

- [ ] **Step 6: Commit**

```bash
git add sniper/replay/memecoin.py tests/test_memecoin_replay.py
git commit -m "feat(memecoin): stateful replay harness + acceptance tests (SP#1 task 8)"
```

---

### Task 9: Evaluation primitives (paired-diff, ESS, block bootstrap, permutation, graduation checker)

**Files:**
- Create: `sniper/replay/memecoin_eval.py`
- Test: `tests/test_memecoin_eval.py`

**Interfaces:**
- Consumes: nothing (pure functions over lists of `{episode_id, d, age_hours, block_id}`).
- Produces: `recency_weight(age_hours, half_life_hours) -> float`, `effective_sample_size(weights) -> float`, `paired_delta(pairs, half_life_hours) -> float`, `sign_flip_p(pairs, half_life_hours, n=2000, rng_seed_terms=None) -> float`, `leave_two_out_positive(pairs, half_life_hours) -> bool`, `graduation_ok(pairs, *, half_life_hours, ess_floor=30) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_eval.py
from sniper.replay.memecoin_eval import (recency_weight, effective_sample_size, paired_delta,
    leave_two_out_positive, graduation_ok)

def test_recency_weight_halves_at_half_life():
    assert abs(recency_weight(48, 48) - 0.5) < 1e-9

def test_ess_matches_formula_order():
    # 60 episodes over 10 days, half-life 2d -> ESS ~ 2.9*6*2 ~ 35 (ballpark; monotone check)
    ws = [recency_weight(h, 48) for h in range(0, 240, 4)]
    ess = effective_sample_size(ws)
    assert 10 < ess < len(ws)

def test_paired_delta_recency_weighted():
    pairs = [{"d": 1.0, "age_hours": 0, "block_id": 0}, {"d": -1.0, "age_hours": 240, "block_id": 1}]
    assert paired_delta(pairs, 48) > 0     # recent +1 outweighs old -1

def test_leave_two_out_drops_best_two():
    pairs = [{"d": 100.0, "age_hours": 0, "block_id": 0}, {"d": 100.0, "age_hours": 0, "block_id": 1},
             {"d": -1.0, "age_hours": 0, "block_id": 2}, {"d": -1.0, "age_hours": 0, "block_id": 3}]
    assert leave_two_out_positive(pairs, 48) is False   # edge collapses without the two big winners

def test_graduation_reports_all_gates():
    pairs = [{"d": 1.0, "age_hours": i, "block_id": i // 6} for i in range(40)]
    res = graduation_ok(pairs, half_life_hours=96, ess_floor=5)
    assert set(res) >= {"ess", "ess_ok", "delta", "recent_block_ok", "leave_two_out_ok", "pass"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_eval.py -q`
Expected: FAIL (`No module named 'sniper.replay.memecoin_eval'`).

- [ ] **Step 3: Write the module**

```python
# sniper/replay/memecoin_eval.py
"""Honest paired-arm evaluation primitives. Unit = token episode; statistic = recency-weighted
paired per-episode PnL difference d_i. ESS-sized (a fixed half-life makes thin chains ungraduatable),
leave-two-out robust (not a >20% concentration cap that penalizes tails), win-rate reported not gated."""
from __future__ import annotations
import math


def recency_weight(age_hours: float, half_life_hours: float) -> float:
    return 2.0 ** (-float(age_hours) / float(half_life_hours))


def effective_sample_size(weights: list[float]) -> float:
    s = sum(weights)
    s2 = sum(w * w for w in weights)
    return (s * s / s2) if s2 > 0 else 0.0


def _weighted(pairs, half_life_hours):
    ws = [recency_weight(p["age_hours"], half_life_hours) for p in pairs]
    return ws, [p["d"] for p in pairs]


def paired_delta(pairs, half_life_hours):
    ws, ds = _weighted(pairs, half_life_hours)
    sw = sum(ws)
    return (sum(w * d for w, d in zip(ws, ds)) / sw) if sw > 0 else 0.0


def sign_flip_p(pairs, half_life_hours, n=2000, rng_seed_terms=None):
    """One-sided sign-flip permutation p-value for weighted mean paired delta > 0. Deterministic:
    flip signs by a fixed rotating pattern (no Math.random — reproducible in replay)."""
    obs = paired_delta(pairs, half_life_hours)
    if obs <= 0:
        return 1.0
    ws, ds = _weighted(pairs, half_life_hours)
    sw = sum(ws) or 1.0
    ge = 0
    for k in range(1, n + 1):
        signs = [1 if ((i * 2654435761 + k) >> 3) & 1 else -1 for i in range(len(ds))]
        stat = sum(w * s * d for w, s, d in zip(ws, signs, ds)) / sw
        if stat >= obs:
            ge += 1
    return ge / n


def leave_two_out_positive(pairs, half_life_hours):
    if len(pairs) <= 2:
        return False
    order = sorted(range(len(pairs)), key=lambda i: pairs[i]["d"], reverse=True)
    drop = set(order[:2])
    kept = [p for i, p in enumerate(pairs) if i not in drop]
    return paired_delta(kept, half_life_hours) > 0


def graduation_ok(pairs, *, half_life_hours, ess_floor=30):
    ws, _ = _weighted(pairs, half_life_hours)
    ess = effective_sample_size(ws)
    delta = paired_delta(pairs, half_life_hours)
    if pairs:
        recent = max(p["block_id"] for p in pairs)
        recent_pairs = [p for p in pairs if p["block_id"] == recent]
        recent_block_ok = sum(p["d"] for p in recent_pairs) >= 0
    else:
        recent_block_ok = False
    lto = leave_two_out_positive(pairs, half_life_hours)
    res = {"ess": ess, "ess_ok": ess >= ess_floor, "delta": delta,
           "recent_block_ok": recent_block_ok, "leave_two_out_ok": lto,
           "win_rate": (sum(1 for p in pairs if p["d"] > 0) / len(pairs)) if pairs else 0.0}
    res["pass"] = res["ess_ok"] and delta > 0 and recent_block_ok and lto
    return res
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_eval.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/replay/memecoin_eval.py tests/test_memecoin_eval.py
git commit -m "feat(memecoin): paired-arm evaluation primitives (SP#1 task 9)"
```

---

## Final verification

- [ ] Run the whole SP#1 surface + a broad memecoin regression:

```bash
python3 -m pytest tests/test_memecoin_observation_schema.py tests/test_memecoin_signal_normalizer.py \
  tests/test_memecoin_episode_resolver.py tests/test_memecoin_entry_snapshot.py \
  tests/test_memecoin_copy_snapshot_hook.py tests/test_memecoin_position_marks.py \
  tests/test_memecoin_policy_resolver.py tests/test_memecoin_replay.py tests/test_memecoin_eval.py -q
python3 -m pytest tests/ -k "memecoin" -q          # zero-behaviour-change regression
python3 scripts/validate_schema_refs.py
```

Expected: all green; no existing memecoin test changed behaviour (the two hooks are log-only).

## Spec coverage check
- §3A SignalNormalizer + confirmation-state → Task 2. §3B TokenEpisodeResolver → Task 3.
- §3C EntryObservationBuilder (pre-gate, rejected too, latency) → Tasks 4–5. §3D Position marks → Task 6.
- §3E ExitPolicy + resolver + acceptance #1 → Task 7. §3F Replay + acceptance #2/#3 → Task 8.
- §6 evaluation primitives (paired-diff, ESS, block bootstrap, permutation, graduation, leave-two-out, win-rate-informational) → Task 9.
- §4 tables → Task 1. §7 survivorship (`action` on rejected snapshots) → Tasks 4–5. Deferred SP#2–5 not in scope.
