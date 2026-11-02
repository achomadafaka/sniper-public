"""Exit-brain persistence (its OWN db): versioned MarketState snapshots + shadow/production decisions.

Spec §11. Own-DB split (P0-3, 2026-07-24 activation-blocker review): these tables — plus the exit
-state worker's RPC-backfilled `block_times` headers — live in `data/memecoin_exit_state.db`, never
in the funded runner's shared `data/memecoin_ticks.db`. Two writers on that shared file already
caused a WAL-lock incident once (see `db_ticks.sol_tape_db_path`); the worker must end every pass
with zero write handles on it. Log-only: no funded ledger table is ever referenced here. Writers are
idempotent single-commit upserts so a re-materialize at the same tick is a no-op, never a duplicate.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

# schema lives in this DB, not the main registry
# <!-- schema:external=memecoin_exit_market_state -->
# <!-- schema:external=memecoin_exit_candidate_decisions -->

DEFAULT_EXIT_STATE_DB = "data/memecoin_exit_state.db"


def exit_state_db_path():
    """Anchored at the STATE root, never the process CWD (mirrors `db_ticks.ticks_db_path`)."""
    override = os.getenv("MEMECOIN_EXIT_STATE_DB")
    if override:
        return override
    from sniper.util.state_paths import state_root
    return str(state_root() / DEFAULT_EXIT_STATE_DB)


def connect_exit_state(path=None, busy_ms=None):
    """The exit-state worker's OWN file. Owns `memecoin_exit_market_state`,
    `memecoin_exit_candidate_decisions`, AND its own RPC-backfilled `block_times` copy — never the
    funded runner's shared ticks DB. `db_ticks.block_times`'s table shape is reused verbatim
    (`ensure_block_times`/`backfill_block_times`/`block_ts_map` all just take a connection)."""
    from sniper.persistence import db_ticks as t
    # busy_ms: the FUNDED RUNNER passes ~250 so an audit write can never sit on the worker's
    # write lock between an engine decision and its execution (P1-A, Codex round 2 — the default
    # 30s timeout could delay an emergency CUT by 30s). "Never raises" was not "never blocks".
    c = sqlite3.connect(path or exit_state_db_path(),
                        timeout=(busy_ms / 1000.0) if busy_ms else 30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    ensure_exit_state_tables(c)
    t.ensure_block_times(c)
    return c


def connect_exit_state_existing(path=None, busy_ms=None):
    """Open the worker-owned DB without creating, migrating, or changing journal mode.

    The funded runner uses this handle for state reads and the deferred post-hook audit flush.
    `mode=rw` refuses a missing file instead of creating one, while omitting every `ensure_*` and
    PRAGMA write keeps worker lock contention out of the pre-execution path.
    """
    resolved = Path(path or exit_state_db_path()).expanduser().resolve()
    c = sqlite3.connect(
        f"{resolved.as_uri()}?mode=rw", uri=True,
        timeout=(busy_ms / 1000.0) if busy_ms else 30)
    c.row_factory = sqlite3.Row
    return c


def merged_block_ts_map(tick_c, exit_c, chain_id, blocks):
    """Block timestamps for `blocks` from BOTH stores, preferring the ticks-DB copy the funded
    runner captures FREE at ingest (memecoin_tick_stream's getLogs blockTimestamp, zero extra RPC)
    over the exit-state worker's own RPC-backfilled copy — the worker's copy only fills what the
    free path never saw. Either connection may be None (no seam yet); that store simply contributes
    nothing."""
    from sniper.persistence import db_ticks as t
    merged = dict(t.block_ts_map(exit_c, chain_id, blocks)) if exit_c is not None else {}
    if tick_c is not None:
        merged.update(t.block_ts_map(tick_c, chain_id, blocks))
    return merged


def ensure_exit_state_tables(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_market_state (
            id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, chain_id TEXT NOT NULL, mint TEXT NOT NULL,
            pool TEXT, engine_version TEXT NOT NULL, feature_version TEXT NOT NULL,
            computed_at TEXT NOT NULL, source_event_time TEXT, freshness_s REAL, state TEXT NOT NULL,
            nominal_price REAL, executable_price REAL, executable_peak REAL, drawdown REAL, drawdown_velocity REAL,
            realized_vol REAL, net_flow_quote TEXT, arrival_rate REAL, arrival_accel REAL,
            reserve_quote TEXT, state_json TEXT,
            UNIQUE(position_id, computed_at))""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_candidate_decisions (
            id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, decided_at TEXT NOT NULL,
            engine_version TEXT NOT NULL, feature_version TEXT NOT NULL, state_id INTEGER,
            candidate_action TEXT NOT NULL, candidate_reason_codes_json TEXT NOT NULL DEFAULT '[]',
            proposed_fraction REAL, legacy_action TEXT, divergent INTEGER NOT NULL DEFAULT 0,
            executable_confirmed INTEGER NOT NULL DEFAULT 0, censored INTEGER NOT NULL DEFAULT 0,
            input_block_ts TEXT, no_action_reason TEXT, outcome TEXT,
            UNIQUE(position_id, decided_at, engine_version))""")
    decision_cols = {
        r[1] for r in c.execute("PRAGMA table_info(memecoin_exit_candidate_decisions)")}
    # episode_context_json (WO-3), regime_json (WO-4), deployer_activity_json (WO-6) are
    # DISPLAY/SHADOW context only: logged alongside the decision, never consumed by any rule
    # without its own new prereg.
    for name, ddl in (("proposed_fraction", "REAL"), ("outcome", "TEXT"),
                      ("episode_context_json", "TEXT"), ("regime_json", "TEXT"),
                      ("deployer_activity_json", "TEXT"), ("v5_shadow_json", "TEXT"),
                      ("future_context_json", "TEXT")):
        if name not in decision_cols:
            c.execute(
                f"ALTER TABLE memecoin_exit_candidate_decisions ADD COLUMN {name} {ddl}")
    c.commit()


def write_market_state(c, state, *, computed_at) -> int:
    """Upsert one MarketState snapshot, keyed `(position_id, computed_at)`. Returns 1 if inserted,
    0 if it already existed (idempotent). `computed_at` is the caller's tick time (ISO) — supplied
    explicitly because non-actionable states may carry no `computed_time`."""
    # state_json is the COMPLETE, explainable, versioned record: every field + full provenance
    # (structure, per-family coverage/quality/source/freshness/price_kind, required_families,
    # entry_contract_version, flow counts/volumes, reserve_delta, wash metadata). The scalar columns
    # are just the queryable projection; use json_extract for anything else — separate structure /
    # coverage / quality blobs were byte-identical subsets of this and are gone.
    state_json = json.dumps(state.to_dict())
    before = c.total_changes
    # ON CONFLICT on the natural key only (NOT `OR IGNORE`, which would also swallow a NOT NULL /
    # type violation as if it were a duplicate — a malformed row must raise, not vanish).
    c.execute(
        "INSERT INTO memecoin_exit_market_state (position_id, chain_id, mint, pool, "
        "engine_version, feature_version, computed_at, source_event_time, freshness_s, state, "
        "nominal_price, executable_price, executable_peak, drawdown, drawdown_velocity, realized_vol, "
        "net_flow_quote, arrival_rate, arrival_accel, reserve_quote, state_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(position_id, computed_at) DO NOTHING",
        (state.position_id, state.chain_id, state.mint, state.pool, state.engine_version,
         state.feature_version, computed_at, state.source_event_time, state.age_s, state.state.value,
         state.nominal_price, state.executable_price, state.executable_peak,
         state.drawdown_from_exec_peak, state.drawdown_velocity, state.realized_vol,
         state.net_flow_quote, state.arrival_rate, state.arrival_accel, state.reserve_quote,
         state_json))
    c.commit()
    return c.total_changes - before


def ensure_outcome_column(c):
    """Backward-compatible migration entry point; normal connections migrate both audit fields."""
    ensure_exit_state_tables(c)


def update_candidate_decision_outcome(c, position_id, decided_at, engine_version, *,
                                      executable_confirmed, outcome) -> int:
    """Stamp what actually HAPPENED onto the decision row written at decide time — the prereg's
    per-action outcome reads need decision and result on one row, keyed by the natural key."""
    ensure_outcome_column(c)
    cur = c.execute(
        "UPDATE memecoin_exit_candidate_decisions SET executable_confirmed=?, outcome=? "
        "WHERE position_id=? AND decided_at=? AND engine_version=?",
        (1 if executable_confirmed else 0, str(outcome)[:120], position_id, decided_at,
         engine_version))
    c.commit()
    return cur.rowcount


def write_candidate_decision(c, rec) -> int:
    """Insert one complete decision/evidence row after the funded hooks have returned.

    The natural key preserves one row per position/tick. Callers supply the final exact-confirmation
    result and executor outcome up front; funded execution never depends on a pre-action insert or
    a later outcome update.
    """
    before = c.total_changes
    # episode_context / regime are optional SHADOW context (WO-3/WO-4): stored as JSON, never read
    # by a rule. Accept either a dict (serialize) or a pre-serialized string.
    def _ctx_json(v):
        return v if v is None or isinstance(v, str) else json.dumps(v)
    c.execute(
        "INSERT INTO memecoin_exit_candidate_decisions (position_id, decided_at, "
        "engine_version, feature_version, state_id, candidate_action, candidate_reason_codes_json, "
        "proposed_fraction, legacy_action, divergent, executable_confirmed, censored, "
        "input_block_ts, no_action_reason, outcome, episode_context_json, regime_json, "
        "deployer_activity_json, v5_shadow_json, future_context_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(position_id, decided_at, engine_version) DO NOTHING",
        (rec["position_id"], rec["decided_at"], rec["engine_version"], rec.get("feature_version"),
         rec.get("state_id"), rec["candidate_action"], json.dumps(rec.get("reason_codes", [])),
         rec.get("proposed_fraction"), rec.get("legacy_action"),
         1 if rec.get("divergent") else 0,
         1 if rec.get("executable_confirmed") else 0, 1 if rec.get("censored") else 0,
         rec.get("input_block_ts"), rec.get("no_action_reason"), rec.get("outcome"),
         _ctx_json(rec.get("episode_context")), _ctx_json(rec.get("regime")),
         _ctx_json(rec.get("deployer_activity")), _ctx_json(rec.get("v5_shadow")),
         _ctx_json(rec.get("future_context"))))
    c.commit()
    return c.total_changes - before


def latest_state(c, position_id) -> dict | None:
    r = c.execute("SELECT * FROM memecoin_exit_market_state WHERE position_id=? "
                  "ORDER BY computed_at DESC LIMIT 1", (int(position_id),)).fetchone()
    return dict(r) if r else None
