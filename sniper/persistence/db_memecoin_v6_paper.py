"""Isolated V6 live-paper ledger.

A dedicated SQLite database (separate from ``memecoin_positions`` and the exit-state/tape DBs) that
owns V6 paper entries, positions, point-in-time observations, decisions, action intents,
modeled fills, and worker cursors. Per
``docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md``:

- it holds NO funded credentials, signed payloads, transaction identities, or reservation rows;
- it imports NO funded execution/reservation module — only the pure engine's serialization helpers;
- position quantity == entry quantity minus applied exit quantities; one applied fill per action
  token; every mutation is one atomic ``BEGIN IMMEDIATE`` transaction so an interrupted worker never
  half-applies a fill.

Only ``connect()`` initializes the schema; ``connect_existing()`` opens read-only.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sniper.services.memecoin_v6_engine import (
    SUPPORTED_NETWORKS, RuleStatus, V6Action, V6Decision, V6PositionState, V6RuleResult,
    canonical_json, fingerprint, state_document, state_from_document,
)
from sniper.services.memecoin_v6_position_policy import V6State
from sniper.util.state_paths import state_path

FRACTION_PPM_SCALE = 1_000_000
# ponytail: dust threshold 0 atomic units — an integer-floor partial can leave a 1-atom crumb open;
# raise this (and close on remaining<=DUST) only if crumb positions prove noisy in live paper.
DUST_ATOMIC = 0

SIGNAL_OUTCOMES = {"filled", "quote_unavailable", "quote_stale", "capacity_full", "identity_invalid"}


class ImmutableConflict(Exception):
    """Same immutable identity redelivered with different immutable content."""


class StateConflict(Exception):
    """An action was applied against a position whose current inventory no longer matches the
    decision it was computed from (a stale decision)."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS v6_paper_signal_attempts (
    origin_event_id     TEXT NOT NULL,
    policy_id           TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id = 'v6'),
    network_id          TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    mint                TEXT NOT NULL CHECK (length(trim(mint)) > 0),
    outcome             TEXT NOT NULL CHECK (outcome IN
                            ('filled','quote_unavailable','quote_stale','capacity_full','identity_invalid')),
    attempted_at        TEXT NOT NULL,
    detail_json         TEXT,
    entry_id            TEXT,
    content_fingerprint TEXT NOT NULL,
    PRIMARY KEY (origin_event_id, policy_id)
);

CREATE TABLE IF NOT EXISTS v6_paper_admission_queue (
    origin_event_id     TEXT NOT NULL,
    policy_id           TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id='v6'),
    source_position_id  INTEGER NOT NULL UNIQUE,
    network_id          TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    mint                TEXT NOT NULL,
    signal_json         TEXT NOT NULL,
    signal_received_at  TEXT NOT NULL,
    enqueued_at         TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN
                            ('queued','inflight','retry_wait','filled','terminal_unfilled')),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at     TEXT,
    claim_token         TEXT,
    claimed_at          TEXT,
    lease_expires_at    TEXT,
    last_outcome        TEXT,
    last_detail         TEXT,
    entry_id            TEXT,
    updated_at          TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    PRIMARY KEY (origin_event_id, policy_id)
);

CREATE TABLE IF NOT EXISTS v6_paper_admission_rejections (
    source_position_id  INTEGER PRIMARY KEY CHECK (source_position_id > 0),
    origin_event_id     TEXT,
    network_id          TEXT,
    mint                TEXT,
    reason_code         TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
    detail_json         TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v6_paper_entry_quote_attempts (
    origin_event_id     TEXT NOT NULL,
    policy_id           TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id='v6'),
    attempt_no          INTEGER NOT NULL CHECK (attempt_no > 0),
    claim_token         TEXT NOT NULL UNIQUE,
    started_at          TEXT NOT NULL,
    completed_at        TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    detail_code         TEXT,
    provider            TEXT,
    content_fingerprint TEXT NOT NULL,
    PRIMARY KEY (origin_event_id, policy_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS v6_paper_entries (
    entry_id            TEXT PRIMARY KEY,
    origin_event_id     TEXT NOT NULL,
    network_id          TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    mint                TEXT NOT NULL CHECK (length(trim(mint)) > 0),
    policy_id           TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id = 'v6'),
    ticker              TEXT,
    source_wallet_id    TEXT,
    source_wallet_handle TEXT,
    signal_source       TEXT,
    notional_usd_micros INTEGER NOT NULL CHECK (notional_usd_micros > 0),
    entry_price         TEXT NOT NULL,
    quote_in_asset      TEXT NOT NULL, quote_in_atomic  TEXT NOT NULL,
    quote_in_decimals   INTEGER NOT NULL CHECK (quote_in_decimals BETWEEN 0 AND 30),
    quote_out_asset     TEXT NOT NULL, quote_out_atomic TEXT NOT NULL,
    quote_out_decimals  INTEGER NOT NULL CHECK (quote_out_decimals BETWEEN 0 AND 30),
    token_qty_atomic    TEXT NOT NULL,
    route_target        TEXT,
    pool_or_vault       TEXT,
    quote_id            TEXT,
    quote_provider      TEXT NOT NULL,
    quote_age_s         REAL NOT NULL CHECK (quote_age_s >= 0),
    quote_fingerprint   TEXT NOT NULL,
    quote_payload_json  TEXT NOT NULL,
    route_binding_json  TEXT NOT NULL,
    costs_json          TEXT NOT NULL,
    net_pnl_computable  INTEGER NOT NULL CHECK (net_pnl_computable IN (0,1)),
    entry_mcap          REAL,
    entry_liquidity     REAL,
    signal_ts           TEXT NOT NULL,
    quote_ts            TEXT NOT NULL,
    fill_ts             TEXT NOT NULL,
    fill_kind           TEXT NOT NULL DEFAULT 'modeled_live_quote'
                            CHECK (fill_kind = 'modeled_live_quote'),
    -- Provenance for non-fomo signal sources. Nullable so every existing row, which
    -- has no radar origin, is untouched and keeps meaning exactly what it meant.
    source_receipt_id   TEXT,
    adapter_version     TEXT,
    fill_model_version  TEXT,
    rule_version        TEXT,
    evaluator_version   TEXT,
    launch_mode         TEXT,
    policy_hash         TEXT NOT NULL,
    policy_document     TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    UNIQUE (origin_event_id, network_id, mint, policy_id)
);

CREATE TABLE IF NOT EXISTS v6_paper_positions (
    position_id             TEXT PRIMARY KEY,
    entry_id                TEXT NOT NULL UNIQUE REFERENCES v6_paper_entries(entry_id),
    network_id              TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    mint                    TEXT NOT NULL,
    -- censored_operator means a human intervened. The measurement-censoring values
    -- mean the measurement could not be completed honestly, which is a different
    -- fact: migration exiting on pre-migration state, and a handoff that arrived too
    -- late to be a faithful live position. Collapsing them into censored_operator
    -- would let coverage accounting read our own failures as operator action.
    status                  TEXT NOT NULL DEFAULT 'open'
                                CHECK (status IN ('open','closed','censored_operator',
                                                  'censored_migration',
                                                  'censored_handoff_stale')),
    entry_qty_atomic        TEXT NOT NULL,
    remaining_qty_atomic    TEXT NOT NULL,
    engine_state_json       TEXT NOT NULL,
    engine_state_fingerprint TEXT NOT NULL,
    latest_observation_id   TEXT,
    latest_decision_token   TEXT,
    latest_action           TEXT,
    terminal_reason         TEXT,
    closed_at               TEXT,
    censor_reason           TEXT,
    censored_at             TEXT,
    updated_at              TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_v6_paper_entries_source_receipt
ON v6_paper_entries(source_receipt_id) WHERE source_receipt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS v6_paper_observations (
    observation_id      TEXT NOT NULL,
    position_id         TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
    observed_at         TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    block_identity      TEXT,
    payload_json        TEXT NOT NULL,
    input_fingerprint   TEXT NOT NULL,
    capture_version     TEXT,
    PRIMARY KEY (position_id, observation_id),
    UNIQUE (position_id, input_fingerprint)
);

CREATE TABLE IF NOT EXISTS v6_paper_decisions (
    action_token        TEXT PRIMARY KEY,
    position_id         TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
    observation_id      TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('HOLD','DERISK','EXIT_FULL')),
    fraction_ppm        INTEGER NOT NULL CHECK (fraction_ppm BETWEEN 0 AND 1000000),
    state_before_hash   TEXT NOT NULL,
    state_after_hash    TEXT,
    state_delta_json    TEXT,
    pending_state_json  TEXT,
    reason_codes_json   TEXT NOT NULL,
    rule_results_json   TEXT NOT NULL,
    thought             TEXT,
    input_fingerprint   TEXT NOT NULL,
    policy_hash         TEXT NOT NULL,
    semantic_transition INTEGER NOT NULL CHECK (semantic_transition IN (0,1)),
    action_intent_token TEXT,
    supersedes_token    TEXT,
    content_fingerprint TEXT NOT NULL,
    decided_at          TEXT NOT NULL,
    applied_outcome     TEXT CHECK (applied_outcome IN ('held','filled','unfilled')),
    applied_at          TEXT
);

CREATE TABLE IF NOT EXISTS v6_paper_action_intents (
    action_token    TEXT PRIMARY KEY REFERENCES v6_paper_decisions(action_token),
    position_id     TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
    action          TEXT NOT NULL CHECK (action IN ('DERISK','EXIT_FULL')),
    fraction_ppm    INTEGER NOT NULL CHECK (fraction_ppm BETWEEN 1 AND 1000000),
    priority        INTEGER NOT NULL CHECK (priority > 0),
    trigger_evidence_id TEXT NOT NULL,
    state_if_filled_json TEXT NOT NULL,
    state_if_unfilled_json TEXT NOT NULL,
    supersedes_token TEXT,
    status          TEXT NOT NULL CHECK (status IN ('active','superseded','filled')),
    reason          TEXT,
    last_quote_status TEXT,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    fill_state_before_hash TEXT,
    fill_state_after_hash TEXT,
    fill_state_delta_json TEXT,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS v6_paper_fills (
    fill_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
    leg                 TEXT NOT NULL CHECK (leg IN ('entry','action')),
    action_token        TEXT UNIQUE REFERENCES v6_paper_decisions(action_token),
    token_qty_atomic    TEXT NOT NULL,
    quote_in_asset      TEXT, quote_in_atomic  TEXT, quote_in_decimals  INTEGER,
    quote_out_asset     TEXT, quote_out_atomic TEXT, quote_out_decimals INTEGER,
    route_quote_fingerprint TEXT,
    quote_provider      TEXT NOT NULL,
    quote_payload_json  TEXT NOT NULL,
    route_binding_json  TEXT NOT NULL,
    costs_json          TEXT NOT NULL,
    net_pnl_computable  INTEGER NOT NULL CHECK (net_pnl_computable IN (0,1)),
    unit_price          TEXT,
    quote_age_s         REAL,
    fill_kind           TEXT NOT NULL DEFAULT 'modeled_live_quote'
                            CHECK (fill_kind = 'modeled_live_quote'),
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v6_paper_cursors (
    name            TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    corrected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v6_paper_feed_health (
    network_id             TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    pool                   TEXT NOT NULL,
    mode                   TEXT NOT NULL,
    current                INTEGER NOT NULL CHECK (current IN (0,1)),
    gap                    INTEGER NOT NULL CHECK (gap IN (0,1)),
    last_received_at       TEXT,
    reconnect_count        INTEGER NOT NULL CHECK (reconnect_count >= 0),
    cursor_height          INTEGER,
    cursor_event_identity  TEXT,
    accumulator_freshness_s REAL,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (network_id, pool)
);

CREATE TABLE IF NOT EXISTS v6_paper_latency_samples (
    sample_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           TEXT NOT NULL CHECK (stage IN
                        ('provider_to_receive','receive_to_observation',
                         'observation_to_decision','step','quote')),
    duration_us     INTEGER NOT NULL CHECK (duration_us >= 0),
    network_id      TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    provider        TEXT NOT NULL,
    feed_mode       TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v6_paper_market_events (
    event_identity   TEXT PRIMARY KEY,
    network_id       TEXT NOT NULL CHECK (network_id IN ('solana','4663')),
    pool             TEXT NOT NULL,
    block_height     INTEGER NOT NULL CHECK (block_height >= 0),
    block_hash       TEXT,
    tx_id            TEXT NOT NULL,
    event_index      INTEGER NOT NULL CHECK (event_index >= 0),
    source_event_at  TEXT,
    received_at      TEXT NOT NULL,
    kind             TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    UNIQUE (network_id, pool, block_height, tx_id, event_index, kind)
);

-- Durable Solana arrival queue. Forward-ported with the v9 migration (2026-08-03): the live
-- paper DB was migrated to schema 9 by an unmerged branch, so every commit on main refused to
-- open it. Main's own arrival handling is in-memory (subscribe_solana_arrivals), so this table
-- is created and migrated but not yet written here; the restart-safe recovery that fills it
-- still lives on that branch. Creating it keeps a fresh main DB and a migrated v9 DB identical.
CREATE TABLE IF NOT EXISTS v6_paper_solana_arrivals (
    pool                TEXT NOT NULL,
    signature           TEXT NOT NULL,
    slot                INTEGER NOT NULL CHECK (slot >= 0),
    mint                TEXT NOT NULL,
    binding_json        TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','decoded','no_relevant_event')),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error          TEXT,
    next_attempt_at     TEXT NOT NULL,
    resolved_at         TEXT,
    content_fingerprint TEXT NOT NULL,
    PRIMARY KEY (pool, signature)
);

CREATE TABLE IF NOT EXISTS v6_paper_exit_followup_schedules (
    followup_id      TEXT PRIMARY KEY,
    position_id     TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
    action_token    TEXT NOT NULL REFERENCES v6_paper_action_intents(action_token),
    horizon_s       INTEGER NOT NULL CHECK (horizon_s > 0),
    due_at          TEXT NOT NULL,
    token_qty_atomic TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (action_token, horizon_s)
);

CREATE TABLE IF NOT EXISTS v6_paper_exit_followup_results (
    followup_id       TEXT PRIMARY KEY REFERENCES v6_paper_exit_followup_schedules(followup_id),
    status            TEXT NOT NULL CHECK (status IN
                          ('healthy','route_absent','provider_unavailable','stale')),
    observed_at       TEXT NOT NULL,
    quote_out_asset   TEXT,
    quote_out_atomic  TEXT,
    quote_out_decimals INTEGER,
    unit_price        TEXT,
    quote_provider    TEXT NOT NULL,
    quote_fingerprint TEXT,
    quote_payload_json TEXT,
    route_binding_json TEXT,
    costs_json        TEXT,
    detail_code       TEXT,
    content_fingerprint TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_v6_market_events_pool
ON v6_paper_market_events(network_id, pool, block_height, event_index, tx_id);

CREATE INDEX IF NOT EXISTS idx_v6_solana_arrivals_due
ON v6_paper_solana_arrivals(state, next_attempt_at, slot, signature);

CREATE INDEX IF NOT EXISTS idx_v6_solana_arrivals_frontier
ON v6_paper_solana_arrivals(pool, state, slot, signature);

CREATE INDEX IF NOT EXISTS idx_v6_latency_recorded
ON v6_paper_latency_samples(recorded_at, sample_id);

CREATE INDEX IF NOT EXISTS idx_v6_followup_due
ON v6_paper_exit_followup_schedules(due_at, followup_id);

CREATE INDEX IF NOT EXISTS idx_v6_admission_ready
ON v6_paper_admission_queue(state, next_attempt_at, source_position_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_v6_one_unapplied_decision
ON v6_paper_decisions(position_id) WHERE applied_outcome IS NULL;

CREATE INDEX IF NOT EXISTS idx_v6_decisions_position_observation
ON v6_paper_decisions(position_id, observation_id);

CREATE INDEX IF NOT EXISTS idx_v6_decisions_quarantined
ON v6_paper_decisions(position_id, observation_id)
WHERE instr(rule_results_json,'catastrophe:quote_quarantined')>0;

CREATE INDEX IF NOT EXISTS idx_v6_decisions_staged_score
ON v6_paper_decisions(position_id, observation_id)
WHERE instr(reason_codes_json,'score:')>0;

CREATE INDEX IF NOT EXISTS idx_v6_decisions_summary
ON v6_paper_decisions(position_id, action, action_token, decided_at, reason_codes_json);

CREATE UNIQUE INDEX IF NOT EXISTS idx_v6_one_active_intent
ON v6_paper_action_intents(position_id) WHERE status='active';

CREATE INDEX IF NOT EXISTS idx_v6_positions_open_coin
ON v6_paper_positions(network_id, mint) WHERE status='open';

PRAGMA user_version=10;
"""


# ---- connection -------------------------------------------------------------------------------

def _default_path() -> str:
    from sniper.settings.memecoin_v6_paper import MEMECOIN_V6_PAPER_DB
    return MEMECOIN_V6_PAPER_DB or str(state_path("data", "memecoin_v6_paper.sqlite"))


_ATOMIC_COLUMNS = {
    "v6_paper_entries": ("quote_in_atomic", "quote_out_atomic", "token_qty_atomic"),
    "v6_paper_positions": ("entry_qty_atomic", "remaining_qty_atomic"),
    "v6_paper_fills": ("token_qty_atomic", "quote_in_atomic", "quote_out_atomic"),
}


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Rebuild quantity-bearing tables with TEXT atomics; SQLite INTEGER is signed int64 only."""
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, columns in _ATOMIC_COLUMNS.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None or not row[0]:
                raise StateConflict(f"V6 v2 migration missing table {table}")
            replacement = table + "__v3"
            ddl = re.sub(
                rf'^CREATE TABLE\s+["\']?{re.escape(table)}["\']?',
                f"CREATE TABLE {replacement}",
                str(row[0]), count=1, flags=re.IGNORECASE)
            for column in columns:
                ddl = re.sub(
                    rf"(\b{re.escape(column)}\s+)INTEGER\b", rf"\1TEXT",
                    ddl, count=1, flags=re.IGNORECASE)
            conn.execute(ddl)
            names = [item[1] for item in conn.execute(f"PRAGMA table_info({table})")]
            joined = ",".join(names)
            conn.execute(f"INSERT INTO {replacement} ({joined}) SELECT {joined} FROM {table}")
        for table in ("v6_paper_fills", "v6_paper_positions", "v6_paper_entries"):
            conn.execute(f"DROP TABLE {table}")
        for table in ("v6_paper_entries", "v6_paper_positions", "v6_paper_fills"):
            conn.execute(f"ALTER TABLE {table}__v3 RENAME TO {table}")
        conn.execute("PRAGMA user_version=3")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise StateConflict(f"V6 v3 migration broke foreign keys: {violations[:3]}")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS v6_paper_market_events (
        event_identity TEXT PRIMARY KEY, network_id TEXT NOT NULL,
        pool TEXT NOT NULL, block_height INTEGER NOT NULL, block_hash TEXT,
        tx_id TEXT NOT NULL, event_index INTEGER NOT NULL, source_event_at TEXT,
        received_at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL,
        UNIQUE (network_id,pool,block_height,tx_id,event_index,kind));
    CREATE INDEX IF NOT EXISTS idx_v6_market_events_pool
        ON v6_paper_market_events(network_id,pool,block_height,event_index,tx_id);
    PRAGMA user_version=4;
    COMMIT;
    """)


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS v6_paper_exit_followup_schedules (
        followup_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL REFERENCES v6_paper_positions(position_id),
        action_token TEXT NOT NULL REFERENCES v6_paper_action_intents(action_token),
        horizon_s INTEGER NOT NULL CHECK (horizon_s > 0), due_at TEXT NOT NULL,
        token_qty_atomic TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE (action_token,horizon_s));
    CREATE TABLE IF NOT EXISTS v6_paper_exit_followup_results (
        followup_id TEXT PRIMARY KEY REFERENCES v6_paper_exit_followup_schedules(followup_id),
        status TEXT NOT NULL, observed_at TEXT NOT NULL, quote_out_asset TEXT,
        quote_out_atomic TEXT, quote_out_decimals INTEGER, unit_price TEXT,
        quote_provider TEXT NOT NULL, quote_fingerprint TEXT, quote_payload_json TEXT,
        route_binding_json TEXT, costs_json TEXT, detail_code TEXT,
        content_fingerprint TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_v6_followup_due
        ON v6_paper_exit_followup_schedules(due_at,followup_id);
    PRAGMA user_version=5;
    COMMIT;
    """)


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS v6_paper_admission_queue (
        origin_event_id TEXT NOT NULL, policy_id TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id='v6'),
        source_position_id INTEGER NOT NULL UNIQUE,
        network_id TEXT NOT NULL CHECK (network_id IN ('solana','4663')), mint TEXT NOT NULL,
        signal_json TEXT NOT NULL, signal_received_at TEXT NOT NULL, enqueued_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('queued','inflight','retry_wait','filled','terminal_unfilled')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at TEXT, claim_token TEXT, claimed_at TEXT, lease_expires_at TEXT,
        last_outcome TEXT, last_detail TEXT, entry_id TEXT, updated_at TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL, PRIMARY KEY (origin_event_id,policy_id));
    CREATE TABLE IF NOT EXISTS v6_paper_entry_quote_attempts (
        origin_event_id TEXT NOT NULL, policy_id TEXT NOT NULL DEFAULT 'v6' CHECK (policy_id='v6'),
        attempt_no INTEGER NOT NULL CHECK (attempt_no > 0), claim_token TEXT NOT NULL UNIQUE,
        started_at TEXT NOT NULL, completed_at TEXT NOT NULL, outcome TEXT NOT NULL,
        detail_code TEXT, provider TEXT, content_fingerprint TEXT NOT NULL,
        PRIMARY KEY (origin_event_id,policy_id,attempt_no));
    CREATE INDEX IF NOT EXISTS idx_v6_admission_ready
        ON v6_paper_admission_queue(state,next_attempt_at,source_position_id);
    PRAGMA user_version=6;
    COMMIT;
    """)


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS v6_paper_admission_rejections (
        source_position_id INTEGER PRIMARY KEY CHECK (source_position_id > 0),
        origin_event_id TEXT, network_id TEXT, mint TEXT,
        reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
        detail_json TEXT NOT NULL, observed_at TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL);
    PRAGMA user_version=7;
    COMMIT;
    """)


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE INDEX IF NOT EXISTS idx_v6_positions_open_coin
        ON v6_paper_positions(network_id,mint) WHERE status='open';
    PRAGMA user_version=8;
    COMMIT;
    """)


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Forward-ported from the unmerged v6 branch (d4507ee4) on 2026-08-03.

    The live paper DB was migrated to schema 9 there, so main — which stopped at 8 — refused
    to open it with `unsupported future V6 paper schema version: 9` and the worker could only
    run from that unmerged commit. Porting the migration is what lets main open it again.

    Two effects, both taken verbatim so a DB migrated by either side ends up identical:
      - create the durable Solana arrival queue (main does not write it yet; see SCHEMA);
      - backfill `applied_outcome='filled'` for decisions that already have a fill. Those rows
        were left NULL, and NULL is the "not yet applied" marker that
        `idx_v6_decisions_open_position` and the pending-decision lookup key on — so a decision
        whose fill already landed still read as outstanding.
    """
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS v6_paper_solana_arrivals (
        pool TEXT NOT NULL, signature TEXT NOT NULL,
        slot INTEGER NOT NULL CHECK (slot >= 0), mint TEXT NOT NULL,
        binding_json TEXT NOT NULL, received_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending','decoded','no_relevant_event')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        last_error TEXT, next_attempt_at TEXT NOT NULL, resolved_at TEXT,
        content_fingerprint TEXT NOT NULL, PRIMARY KEY (pool,signature));
    CREATE INDEX IF NOT EXISTS idx_v6_solana_arrivals_due
        ON v6_paper_solana_arrivals(state,next_attempt_at,slot,signature);
    CREATE INDEX IF NOT EXISTS idx_v6_solana_arrivals_frontier
        ON v6_paper_solana_arrivals(pool,state,slot,signature);
    UPDATE v6_paper_decisions SET applied_outcome='filled',
        applied_at=COALESCE(
            (SELECT f.created_at FROM v6_paper_fills f
             WHERE f.action_token=v6_paper_decisions.action_token), applied_at)
        WHERE EXISTS (
            SELECT 1 FROM v6_paper_fills f
            WHERE f.action_token=v6_paper_decisions.action_token)
        AND COALESCE(applied_outcome,'')!='filled';
    PRAGMA user_version=9;
    COMMIT;
    """)


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Radar provenance columns and measurement-censoring statuses.

    The provenance columns are nullable ALTERs, so every existing fomo-source row is
    untouched and keeps meaning what it meant. The status CHECK has to be rebuilt --
    SQLite cannot alter a constraint in place -- so the rows are copied out, the table
    is dropped, SCHEMA recreates it with the widened CHECK, and the rows go back.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(v6_paper_entries)")}
    position_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='v6_paper_positions'"
    ).fetchone()
    if position_row is None or not position_row[0]:
        raise StateConflict("V6 v9 migration missing table v6_paper_positions")
    position_sql = str(position_row[0])
    widened = "censored_migration" in position_sql
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for column in ("source_receipt_id", "adapter_version", "fill_model_version",
                       "rule_version", "evaluator_version", "launch_mode"):
            if column not in existing:
                conn.execute(f"ALTER TABLE v6_paper_entries ADD COLUMN {column} TEXT")
        if not widened:
            replacement = "v6_paper_positions__v10"
            ddl = re.sub(
                r'^CREATE TABLE\s+["\']?v6_paper_positions["\']?',
                f"CREATE TABLE {replacement}",
                position_sql,
                count=1,
                flags=re.IGNORECASE,
            )
            ddl, changed = re.subn(
                r"('censored_operator')\s*\)\s*\)",
                r"\1,'censored_migration','censored_handoff_stale'))",
                ddl,
                count=1,
            )
            if changed != 1:
                raise StateConflict("V6 v9 migration could not widen position status")
            conn.execute(ddl)
            names = [row[1] for row in conn.execute("PRAGMA table_info(v6_paper_positions)")]
            joined = ",".join(names)
            conn.execute(
                f"INSERT INTO {replacement} ({joined}) SELECT {joined} FROM v6_paper_positions"
            )
            conn.execute("DROP TABLE v6_paper_positions")
            conn.execute(f"ALTER TABLE {replacement} RENAME TO v6_paper_positions")
        conn.execute("PRAGMA user_version=10")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise StateConflict(f"V6 v10 migration broke foreign keys: {violations[:3]}")


_REQUIRED_COLUMNS = {
    "v6_paper_observations": {"observation_id", "block_identity", "capture_version",
                              "payload_json", "input_fingerprint"},
    "v6_paper_decisions": {"action_token", "rule_results_json", "state_before_hash",
                           "state_after_hash", "state_delta_json", "applied_outcome"},
    "v6_paper_action_intents": {"action_token", "attempt_count", "next_attempt_at",
                                "last_quote_status", "fill_state_delta_json"},
    "v6_paper_positions": {"position_id", "engine_state_json", "engine_state_fingerprint",
                           "remaining_qty_atomic"},
    "v6_paper_entries": {"entry_id", "origin_event_id", "source_receipt_id",
                         "adapter_version", "fill_model_version", "rule_version",
                         "evaluator_version", "launch_mode"},
    "v6_paper_admission_queue": {"origin_event_id", "source_position_id", "state",
                                  "claim_token", "lease_expires_at", "next_attempt_at",
                                  "last_detail"},
    "v6_paper_entry_quote_attempts": {"origin_event_id", "attempt_no", "claim_token",
                                       "outcome", "detail_code", "provider"},
    "v6_paper_admission_rejections": {"source_position_id", "origin_event_id", "network_id",
                                        "mint", "reason_code", "detail_json", "observed_at",
                                        "content_fingerprint"},
    # Missed in the 2026-08-03 schema-9 forward-port; `d4507ee4` had it. Without the contract,
    # connect() accepted a schema-9 DB whose arrival table carried only pool/signature/slot/
    # state/next_attempt_at — no mint, binding, timestamps, retry fields or fingerprint — and
    # reported it healthy. Latent today because main does not write the queue, but the whole
    # point of the required-column check is to fail on exactly that shape.
    "v6_paper_solana_arrivals": {"pool", "signature", "slot", "mint", "binding_json",
                                   "received_at", "state", "attempt_count", "last_error",
                                   "next_attempt_at", "resolved_at", "content_fingerprint"},
}


def validate_schema_columns(conn: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(required - actual)
        if missing:
            raise StateConflict(f"V6 schema {table} missing columns: {missing}")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or _default_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None                 # explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    conn.execute("PRAGMA foreign_keys=ON")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v6_paper_decisions'"
    ).fetchone()
    if legacy is not None and version < 2:
        conn.close()
        raise StateConflict(
            "legacy V6 paper schema cannot be opened as V1; censor/archive it before migration")
    if legacy is not None and version == 2:
        _migrate_v2_to_v3(conn)
        version = 3
    if legacy is not None and version == 3:
        _migrate_v3_to_v4(conn)
        version = 4
    if legacy is not None and version == 4:
        _migrate_v4_to_v5(conn)
        version = 5
    if legacy is not None and version == 5:
        _migrate_v5_to_v6(conn)
        version = 6
    if legacy is not None and version == 6:
        _migrate_v6_to_v7(conn)
        version = 7
    if legacy is not None and version == 7:
        _migrate_v7_to_v8(conn)
        version = 8
    if legacy is not None and version == 8:
        _migrate_v8_to_v9(conn)
        version = 9
    if legacy is not None and version == 9:
        _migrate_v9_to_v10(conn)
        version = 10
    if legacy is not None and version > 10:
        conn.close()
        raise StateConflict(f"unsupported future V6 paper schema version: {version}")
    conn.executescript(SCHEMA)
    validate_schema_columns(conn)
    return conn


def connect_existing(path: str | None = None) -> sqlite3.Connection:
    resolved = Path(path or _default_path()).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"v6 paper db does not exist: {resolved}")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---- compact fast-runtime telemetry -----------------------------------------------------------

def append_market_events(conn, events) -> int:
    """Persist normalized read-only chain events in the isolated V6 book."""
    from sniper.services.memecoin_v6_events import V6MarketEvent, event_identity

    unique = {}
    for event in events:
        if not isinstance(event, V6MarketEvent):
            raise TypeError("market event must be V6MarketEvent")
        identity = event_identity(event)
        unique[identity] = event
    if not unique:
        return 0
    before = conn.total_changes
    with _tx(conn):
        for identity, event in unique.items():
            normalized_pool = (event.pool.lower()
                               if event.network_id == "4663" else event.pool)
            payload_json = canonical_json(dict(event.payload))
            content = fingerprint({
                "network_id": event.network_id, "pool": normalized_pool,
                "block_height": event.block_height, "block_hash": event.block_hash,
                "tx_id": event.tx_id, "event_index": event.event_index,
                "source_event_at": event.source_event_at,
                "kind": event.kind, "payload": dict(event.payload),
            })
            coordinate = conn.execute(
                "SELECT event_identity,content_fingerprint FROM v6_paper_market_events "
                "WHERE network_id=? AND pool=? AND block_height=? AND tx_id=? "
                "AND event_index=? AND kind=?",
                (event.network_id, normalized_pool, event.block_height, event.tx_id,
                 event.event_index, event.kind),
            ).fetchone()
            if coordinate is not None:
                if coordinate[0] != identity or coordinate[1] != content:
                    raise ImmutableConflict("market event coordinate redelivered with new content")
                continue
            conn.execute(
                "INSERT INTO v6_paper_market_events "
                "(event_identity,network_id,pool,block_height,block_hash,tx_id,event_index,"
                "source_event_at,received_at,kind,payload_json,content_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, event.network_id, normalized_pool, event.block_height,
                 event.block_hash, event.tx_id, event.event_index, event.source_event_at,
                 event.received_at, event.kind, payload_json, content),
            )
    return conn.total_changes - before


def market_events(conn, network_id: str, pool: str, *, after: str | None = None):
    """Reconstruct one pool's durable causal event stream in chain order."""
    from sniper.services.memecoin_v6_events import V6MarketEvent

    normalized_pool = str(pool).lower() if network_id == "4663" else str(pool)
    params = [network_id, normalized_pool]
    predicate = ""
    if after is not None:
        _aware_timestamp(after, "after")
        predicate = " AND source_event_at>=?"
        params.append(after)
    rows = conn.execute(
        "SELECT * FROM v6_paper_market_events WHERE network_id=? AND pool=?" + predicate
        + " ORDER BY block_height,event_index,tx_id", params,
    ).fetchall()
    return tuple(V6MarketEvent(
        network_id=row["network_id"], pool=row["pool"], block_height=int(row["block_height"]),
        block_hash=row["block_hash"], tx_id=row["tx_id"], event_index=int(row["event_index"]),
        source_event_at=row["source_event_at"], received_at=row["received_at"],
        kind=row["kind"], payload=json.loads(row["payload_json"]),
    ) for row in rows)

_LATENCY_STAGES = frozenset({
    "provider_to_receive", "receive_to_observation", "observation_to_decision", "step", "quote",
})
_MAX_LATENCY_SAMPLES = 100_000


def upsert_feed_health(conn, network_id: str, pool: str, health, *, cursor=None,
                       accumulator_freshness_s=None, updated_at: str) -> None:
    if network_id not in SUPPORTED_NETWORKS or not str(pool or "").strip():
        raise ValueError("feed health identity is invalid")
    _aware_timestamp(updated_at, "updated_at")
    if health.last_received_at is not None:
        _aware_timestamp(health.last_received_at, "last_received_at")
    if accumulator_freshness_s is not None and (
            isinstance(accumulator_freshness_s, bool)
            or not isinstance(accumulator_freshness_s, (int, float))
            or not math.isfinite(accumulator_freshness_s)
            or accumulator_freshness_s < 0):
        raise ValueError("accumulator_freshness_s must be finite and non-negative")
    cursor_height = cursor.height if cursor is not None else None
    cursor_identity = cursor.event_identity if cursor is not None else None
    normalized_pool = str(pool).lower() if network_id == "4663" else str(pool)
    with _tx(conn):
        conn.execute(
            "INSERT INTO v6_paper_feed_health "
            "(network_id,pool,mode,current,gap,last_received_at,reconnect_count,cursor_height,"
            "cursor_event_identity,accumulator_freshness_s,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(network_id,pool) DO UPDATE SET mode=excluded.mode,current=excluded.current,"
            "gap=excluded.gap,last_received_at=excluded.last_received_at,"
            "reconnect_count=excluded.reconnect_count,cursor_height=excluded.cursor_height,"
            "cursor_event_identity=excluded.cursor_event_identity,"
            "accumulator_freshness_s=excluded.accumulator_freshness_s,updated_at=excluded.updated_at",
            (network_id, normalized_pool, health.mode, int(health.current), int(health.gap),
             health.last_received_at, int(health.reconnect_count), cursor_height, cursor_identity,
             accumulator_freshness_s, updated_at),
        )


def append_latency_sample(conn, stage: str, duration_us: int, *, network_id: str,
                          provider: str, feed_mode: str, recorded_at: str) -> None:
    append_latency_samples(conn, ({
        "stage": stage, "duration_us": duration_us, "network_id": network_id,
        "provider": provider, "feed_mode": feed_mode, "recorded_at": recorded_at,
    },))


def append_latency_samples(conn, samples) -> None:
    samples = tuple(dict(sample) for sample in samples)
    if not samples:
        return
    rows = []
    newest = None
    for sample in samples:
        stage = sample.get("stage")
        duration_us = sample.get("duration_us")
        network_id = sample.get("network_id")
        provider = sample.get("provider")
        feed_mode = sample.get("feed_mode")
        recorded_at = sample.get("recorded_at")
        if stage not in _LATENCY_STAGES:
            raise ValueError(f"unsupported latency stage: {stage}")
        if isinstance(duration_us, bool) or not isinstance(duration_us, int) or duration_us < 0:
            raise ValueError("duration_us must be a non-negative integer")
        if network_id not in SUPPORTED_NETWORKS or not provider or not feed_mode:
            raise ValueError("latency tags are invalid")
        _aware_timestamp(recorded_at, "recorded_at")
        parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        newest = parsed if newest is None or parsed > newest else newest
        rows.append((stage, duration_us, network_id, provider, feed_mode, recorded_at))
    cutoff = (newest - timedelta(hours=24)).isoformat()
    with _tx(conn):
        conn.executemany(
            "INSERT INTO v6_paper_latency_samples "
            "(stage,duration_us,network_id,provider,feed_mode,recorded_at) VALUES(?,?,?,?,?,?)",
            rows,
        )
        conn.execute("DELETE FROM v6_paper_latency_samples WHERE recorded_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM v6_paper_latency_samples WHERE sample_id IN ("
            "SELECT sample_id FROM v6_paper_latency_samples ORDER BY sample_id DESC "
            "LIMIT -1 OFFSET ?)", (_MAX_LATENCY_SAMPLES,),
        )


@contextmanager
def _tx(conn: sqlite3.Connection):
    if conn.in_transaction:
        conn.execute("SAVEPOINT v6_nested_tx")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO v6_nested_tx")
            conn.execute("RELEASE v6_nested_tx")
            raise
        conn.execute("RELEASE v6_nested_tx")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _aware_timestamp(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a parseable aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _positive_decimal(value, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive finite decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field_name} must be a positive finite decimal")
    return parsed


def _atomic_text(value: int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("atomic quantity must be a non-negative integer")
    return str(value)


def _sum_action_fill_qty(conn, position_id: str) -> int:
    rows = conn.execute(
        "SELECT token_qty_atomic FROM v6_paper_fills WHERE position_id=? AND leg='action'",
        (position_id,),
    ).fetchall()
    return sum(int(row[0]) for row in rows)


def _quote_evidence(*, quote_payload, quote_fingerprint, route_binding, costs,
                    net_pnl_computable, field_prefix: str) -> dict:
    if not isinstance(quote_payload, Mapping):
        raise ValueError(f"{field_prefix} quote_payload must be a mapping")
    if quote_fingerprint != fingerprint(quote_payload):
        raise ValueError(f"{field_prefix} quote_fingerprint does not match quote_payload")
    if not isinstance(route_binding, Mapping) or not route_binding:
        raise ValueError(f"{field_prefix} route_binding must be a non-empty mapping")
    if not isinstance(costs, Mapping):
        raise ValueError(f"{field_prefix} costs must be a mapping")
    if not isinstance(net_pnl_computable, bool):
        raise ValueError(f"{field_prefix} net_pnl_computable must be boolean")
    return {
        "quote_payload_json": canonical_json(quote_payload),
        "route_binding_json": canonical_json(route_binding),
        "costs_json": canonical_json(costs),
        "net_pnl_computable": int(net_pnl_computable),
    }


def _validate_entry(entry: dict) -> None:
    for name in ("entry_id", "origin_event_id", "mint", "quote_in_asset", "quote_out_asset",
                 "quote_provider"):
        if not str(entry.get(name) or "").strip():
            raise ValueError(f"{name} must not be empty")
    if entry.get("network_id") not in SUPPORTED_NETWORKS:
        raise ValueError(f"unsupported network_id: {entry.get('network_id')!r}")
    for name in ("notional_usd_micros", "quote_in_atomic", "quote_out_atomic",
                 "token_qty_atomic"):
        value = entry.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if entry["quote_out_atomic"] != entry["token_qty_atomic"]:
        raise ValueError("entry quote_out_atomic must equal token_qty_atomic")
    for name in ("quote_in_decimals", "quote_out_decimals"):
        value = entry.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30:
            raise ValueError(f"{name} must be an integer between 0 and 30")
    _positive_decimal(entry.get("entry_price"), "entry_price")
    age = entry.get("quote_age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)) \
            or not math.isfinite(age) or age < 0:
        raise ValueError("quote_age_s must be finite and >= 0")
    for name in ("signal_ts", "quote_ts", "fill_ts"):
        _aware_timestamp(entry.get(name), name)
    document = entry.get("policy_document")
    if not isinstance(document, Mapping):
        raise ValueError("policy_document must be a mapping")
    expected_hash = fingerprint(document)
    if entry.get("policy_hash") != expected_hash:
        raise ValueError("policy_hash does not match policy_document")
    if not str(document.get("engine_policy_id") or "").startswith("v6-"):
        raise ValueError("policy_document engine_policy_id is not V6")
    _quote_evidence(
        quote_payload=entry.get("quote_payload"),
        quote_fingerprint=entry.get("quote_fingerprint"),
        route_binding=entry.get("route_binding"),
        costs=entry.get("costs"),
        net_pnl_computable=entry.get("net_pnl_computable"),
        field_prefix="entry",
    )


# ---- engine-state (de)serialization (restores tuples so V6PositionState equality holds) -------

def _state_to_json(state: V6PositionState) -> str:
    return canonical_json(state_document(state))


def _state_from_json(text: str) -> V6PositionState:
    d = json.loads(text)
    if d.get("state_version") in (1, 2):
        return state_from_document(d)
    # Read-only compatibility for databases created before the V1 state contract. Startup rejects
    # an open legacy-policy book; retaining the decoder preserves closed/censored audit evidence.
    b = d["break_state"]
    return V6PositionState(
        position_id=d["position_id"],
        remaining_qty_atomic=d["remaining_qty_atomic"],
        break_state=V6State(
            tranches=b["tranches"], awaiting=b["awaiting"], break_level=b["break_level"],
            neg_streak=b["neg_streak"], recovered=b["recovered"], vol_hist=tuple(b["vol_hist"]),
        ),
        pending_action_token=d["pending_action_token"],
        last_action_outcome=d["last_action_outcome"],
    )


def _state_delta(before: V6PositionState, after: V6PositionState) -> dict:
    before_doc = state_document(before)
    after_doc = state_document(after)
    return {key: value for key, value in after_doc.items() if before_doc.get(key) != value}


def _apply_state_delta(state: V6PositionState, delta_json: str) -> V6PositionState:
    document = state_document(state)
    delta = json.loads(delta_json)
    if not isinstance(delta, dict):
        raise StateConflict("decision state delta must be a JSON object")
    document.update(delta)
    return state_from_document(document)


@dataclass(frozen=True)
class V6PaperPosition:
    entry_id: str
    network_id: str
    mint: str
    status: str
    entry_qty_atomic: int
    remaining_qty_atomic: int
    engine_state: V6PositionState
    terminal_reason: str | None = None
    exited_qty_atomic: int = 0


@dataclass(frozen=True)
class UnappliedV6Decision:
    position_id: str
    decision: V6Decision
    decided_at: str


@dataclass(frozen=True)
class PersistedV6Intent:
    token: str
    position_id: str
    action: V6Action
    fraction_ppm: int
    priority: int
    trigger_evidence_id: str
    supersedes_token: str | None
    attempt_count: int
    last_attempt_at: str | None
    next_attempt_at: str | None
    last_quote_status: str | None


@dataclass(frozen=True)
class ExitFollowupSchedule:
    followup_id: str
    position_id: str
    action_token: str
    horizon_s: int
    due_at: str
    token_qty_atomic: int
    created_at: str


@dataclass(frozen=True)
class AdmissionClaim:
    origin_event_id: str
    source_position_id: int
    network_id: str
    mint: str
    signal: dict
    signal_received_at: str
    attempt_no: int
    claim_token: str
    claimed_at: str
    lease_expires_at: str


@dataclass(frozen=True)
class PendingSolanaArrival:
    pool: str
    signature: str
    slot: int
    mint: str
    binding: dict
    received_at: str
    attempt_count: int
    last_error: str | None


def _solana_arrival_fields(arrival) -> tuple[str, str, int, str, dict, str]:
    def value(name):
        return arrival.get(name) if isinstance(arrival, Mapping) else getattr(arrival, name)

    pool = str(value("pool") or "").strip()
    signature = str(value("signature") or "").strip()
    mint = str(value("mint") or "").strip()
    slot = value("slot")
    binding = value("binding")
    received_at = value("received_at")
    if not pool or not signature or not mint:
        raise ValueError("Solana arrival identity is incomplete")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError("Solana arrival slot must be a non-negative integer")
    if not isinstance(binding, Mapping):
        raise ValueError("Solana arrival binding must be a mapping")
    _aware_timestamp(received_at, "received_at")
    return pool, signature, slot, mint, dict(binding), received_at


def _utc_timestamp(value: str) -> str:
    _aware_timestamp(value, "timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def enqueue_solana_arrivals(conn, arrivals) -> int:
    """Durably retain confirmed notifications before any transaction-provider read."""
    inserted = 0
    with _tx(conn):
        for arrival in arrivals:
            pool, signature, slot, mint, binding, received_at = _solana_arrival_fields(arrival)
            binding_json = canonical_json(binding)
            content = fingerprint({
                "pool": pool, "signature": signature, "slot": slot, "mint": mint,
                "binding": binding,
            })
            existing = conn.execute(
                "SELECT content_fingerprint FROM v6_paper_solana_arrivals "
                "WHERE pool=? AND signature=?", (pool, signature),
            ).fetchone()
            if existing is not None:
                if existing[0] != content:
                    raise ImmutableConflict(
                        f"Solana arrival {pool}/{signature} conflicts with existing identity")
                continue
            conn.execute(
                "INSERT INTO v6_paper_solana_arrivals "
                "(pool,signature,slot,mint,binding_json,received_at,next_attempt_at,"
                "content_fingerprint) VALUES(?,?,?,?,?,?,?,?)",
                (pool, signature, slot, mint, binding_json, received_at,
                 _utc_timestamp(received_at), content),
            )
            inserted += 1
    return inserted


def due_solana_arrivals(conn, now_iso: str, *, limit: int = 100) \
        -> tuple[PendingSolanaArrival, ...]:
    _aware_timestamp(now_iso, "now_iso")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    rows = conn.execute(
        "SELECT a.* FROM v6_paper_solana_arrivals a WHERE a.state='pending' "
        "AND a.next_attempt_at<=? AND NOT EXISTS ("
        "SELECT 1 FROM v6_paper_solana_arrivals prior WHERE prior.pool=a.pool "
        "AND prior.state='pending' AND (prior.slot<a.slot OR "
        "(prior.slot=a.slot AND prior.signature<a.signature))) "
        "ORDER BY a.slot,a.signature LIMIT ?",
        (_utc_timestamp(now_iso), limit),
    ).fetchall()
    return tuple(PendingSolanaArrival(
        pool=row["pool"], signature=row["signature"], slot=int(row["slot"]),
        mint=row["mint"], binding=json.loads(row["binding_json"]),
        received_at=row["received_at"], attempt_count=int(row["attempt_count"]),
        last_error=row["last_error"],
    ) for row in rows)


def pending_solana_pool_count(conn, pool: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM v6_paper_solana_arrivals WHERE pool=? AND state='pending'",
        (pool,),
    ).fetchone()[0])


def pending_solana_pools(conn) -> tuple[str, ...]:
    return tuple(row[0] for row in conn.execute(
        "SELECT DISTINCT pool FROM v6_paper_solana_arrivals "
        "WHERE state='pending' ORDER BY pool"))


def retry_solana_arrival(conn, pool: str, signature: str, *, error: str,
                         attempted_at: str, next_attempt_at: str) -> bool:
    _aware_timestamp(attempted_at, "attempted_at")
    _aware_timestamp(next_attempt_at, "next_attempt_at")
    message = str(error or "").strip()
    if not message:
        raise ValueError("retry error must not be empty")
    with _tx(conn):
        return conn.execute(
            "UPDATE v6_paper_solana_arrivals SET attempt_count=attempt_count+1,last_error=?,"
            "next_attempt_at=? WHERE pool=? AND signature=? AND state='pending'",
            (message[:160], _utc_timestamp(next_attempt_at), pool, signature),
        ).rowcount == 1


def resolve_solana_arrival(conn, pool: str, signature: str, *, outcome: str,
                           resolved_at: str) -> bool:
    if outcome not in {"decoded", "no_relevant_event"}:
        raise ValueError("invalid Solana arrival outcome")
    _aware_timestamp(resolved_at, "resolved_at")
    with _tx(conn):
        return conn.execute(
            "UPDATE v6_paper_solana_arrivals SET state=?,resolved_at=?,last_error=NULL "
            "WHERE pool=? AND signature=? AND state='pending'",
            (outcome, resolved_at, pool, signature),
        ).rowcount == 1


def _row_to_decision(row, intent=None) -> V6Decision:
    if row["action"] == V6Action.HOLD.value:
        filled = unfilled = _state_from_json(row["pending_state_json"])
    else:
        if intent is None:
            raise StateConflict(f"action decision {row['action_token']} has no persisted intent")
        filled = _state_from_json(intent["state_if_filled_json"])
        unfilled = _state_from_json(intent["state_if_unfilled_json"])
    return V6Decision(
        action=V6Action(row["action"]), fraction_ppm=int(row["fraction_ppm"]),
        action_token=row["action_token"],
        state_if_filled=filled, state_if_unfilled=unfilled,
        reason_codes=tuple(json.loads(row["reason_codes_json"])), thought=row["thought"],
        input_fingerprint=row["input_fingerprint"], policy_hash=row["policy_hash"],
        semantic_transition=bool(row["semantic_transition"]),
        action_intent_token=row["action_intent_token"],
        supersedes_token=row["supersedes_token"],
        rule_results=tuple(
            V6RuleResult(
                rule_id=result["rule_id"], status=RuleStatus(result["status"]),
                action=V6Action(result["action"]), fraction_ppm=result["fraction_ppm"],
                priority=result["priority"], reason_codes=tuple(result["reason_codes"]),
                missing_fields=tuple(result["missing_fields"]),
            )
            for result in json.loads(row["rule_results_json"])
        ),
    )


def _rule_results_document(results: tuple[V6RuleResult, ...]) -> list[dict]:
    return [{
        "rule_id": result.rule_id, "status": result.status.value,
        "action": result.action.value, "fraction_ppm": result.fraction_ppm,
        "priority": result.priority, "reason_codes": list(result.reason_codes),
        "missing_fields": list(result.missing_fields),
    } for result in results]


def _row_to_position(conn, row) -> V6PaperPosition:
    exited = _sum_action_fill_qty(conn, row["position_id"])
    return V6PaperPosition(
        entry_id=row["entry_id"], network_id=row["network_id"], mint=row["mint"],
        status=row["status"], entry_qty_atomic=int(row["entry_qty_atomic"]),
        remaining_qty_atomic=int(row["remaining_qty_atomic"]),
        engine_state=_state_from_json(row["engine_state_json"]),
        terminal_reason=row["terminal_reason"], exited_qty_atomic=int(exited),
    )


def _load_position(conn, position_id: str) -> V6PaperPosition:
    row = conn.execute("SELECT * FROM v6_paper_positions WHERE position_id=?",
                       (position_id,)).fetchone()
    if row is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    return _row_to_position(conn, row)


# ---- immutable writes -------------------------------------------------------------------------

_ADMISSION_RETRY_DELAYS_S = (2, 5)
_ADMISSION_MAX_ATTEMPTS = 3
ADMISSION_FRESHNESS_S = 90
ADMISSION_LEASE_S = 120
_ADMISSION_RETRYABLE = {"quote_unavailable", "provider_error", "lease_expired"}


def _aware_datetime(value: str, field_name: str) -> datetime:
    _aware_timestamp(value, field_name)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def record_admission_rejection(conn, rejection: Mapping, observed_at: str) -> bool:
    """Persist one terminal source-page classification before its cursor can advance."""
    source_id = rejection.get("source_position_id")
    reason = str(rejection.get("reason_code") or "").strip()
    detail = rejection.get("detail", {})
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ValueError("source_position_id must be a positive integer")
    if not reason:
        raise ValueError("reason_code must not be empty")
    if not isinstance(detail, Mapping):
        raise ValueError("admission rejection detail must be a mapping")
    _aware_timestamp(observed_at, "observed_at")
    origin = str(rejection.get("origin_event_id") or "").strip() or None
    network = str(rejection.get("network_id") or "").strip().lower() or None
    mint = str(rejection.get("mint") or "").strip() or None
    detail_json = canonical_json(dict(detail))
    content = fingerprint({
        "source_position_id": source_id, "origin_event_id": origin,
        "network_id": network, "mint": mint, "reason_code": reason,
        "detail_json": detail_json,
    })
    with _tx(conn):
        existing = conn.execute(
            "SELECT content_fingerprint FROM v6_paper_admission_rejections "
            "WHERE source_position_id=?", (source_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != content:
                raise ImmutableConflict(
                    f"admission rejection {source_id} conflicts with existing identity")
            return False
        conn.execute(
            "INSERT INTO v6_paper_admission_rejections "
            "(source_position_id,origin_event_id,network_id,mint,reason_code,detail_json,"
            "observed_at,content_fingerprint) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, origin, network, mint, reason, detail_json, observed_at, content),
        )
    return True


def enqueue_admission(conn, candidate: Mapping, enqueued_at: str) -> bool:
    """Persist one immutable source candidate. Duplicate strategy arms cannot quote twice."""
    origin = str(candidate.get("origin_event_id") or "").strip()
    source_id = candidate.get("source_position_id")
    network = str(candidate.get("network_id") or "").strip().lower()
    mint = str(candidate.get("mint") or "").strip()
    received = str(candidate.get("received_at") or "").strip()
    signal = candidate.get("signal")
    if not origin or isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ValueError("admission origin and source_position_id are required")
    if network not in SUPPORTED_NETWORKS or not mint or not isinstance(signal, Mapping):
        raise ValueError("admission network, mint, and signal are required")
    _aware_timestamp(received, "signal_received_at")
    _aware_timestamp(enqueued_at, "enqueued_at")
    signal_json = canonical_json(dict(signal))
    content = fingerprint({
        "origin_event_id": origin, "source_position_id": source_id,
        "network_id": network, "mint": mint, "signal_json": signal_json,
        "signal_received_at": received,
    })
    with _tx(conn):
        existing = conn.execute(
            "SELECT content_fingerprint FROM v6_paper_admission_queue "
            "WHERE origin_event_id=? AND policy_id='v6'", (origin,)).fetchone()
        if existing is not None:
            if existing[0] != content:
                raise ImmutableConflict(f"admission {origin}/v6 conflicts with existing identity")
            return False
        conn.execute(
            "INSERT INTO v6_paper_admission_queue "
            "(origin_event_id,policy_id,source_position_id,network_id,mint,signal_json,"
            "signal_received_at,enqueued_at,state,next_attempt_at,updated_at,content_fingerprint) "
            "VALUES (?,'v6',?,?,?,?,?,?,'queued',?,?,?)",
            (origin, source_id, network, mint, signal_json, received, enqueued_at,
             enqueued_at, enqueued_at, content),
        )
    return True


def _claim_from_row(row) -> AdmissionClaim:
    return AdmissionClaim(
        origin_event_id=row["origin_event_id"], source_position_id=int(row["source_position_id"]),
        network_id=row["network_id"], mint=row["mint"], signal=json.loads(row["signal_json"]),
        signal_received_at=row["signal_received_at"], attempt_no=int(row["attempt_count"]),
        claim_token=row["claim_token"], claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
    )


def has_open_coin(conn, network_id: str, mint: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM v6_paper_positions WHERE network_id=? AND mint=? "
        "AND status='open' LIMIT 1",
        (network_id, mint),
    ).fetchone() is not None


def has_coin_lifecycle(conn, network_id: str, mint: str, cohort_policy_hash: str) -> bool:
    """Whether this immutable policy cohort already admitted this coin once."""
    if not str(cohort_policy_hash or "").strip():
        raise ValueError("cohort_policy_hash must not be empty")
    return conn.execute(
        "SELECT 1 FROM v6_paper_positions p JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
        "WHERE p.network_id=? AND p.mint=? AND e.policy_hash=? LIMIT 1",
        (network_id, mint, cohort_policy_hash),
    ).fetchone() is not None


def claim_admissions(conn, now_iso: str, *, max_claims: int, max_open: int, open_count: int,
                     lease_s: int = 15,
                     cohort_policy_hash: str | None = None) -> tuple[AdmissionClaim, ...]:
    """Atomically claim oldest ready candidates without exceeding book capacity."""
    now = _aware_datetime(now_iso, "now_iso")
    for name, value in (("max_claims", max_claims), ("max_open", max_open),
                        ("open_count", open_count), ("lease_s", lease_s)):
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if name == "open_count" else 1):
            raise ValueError(f"{name} is invalid")
    claimed: list[AdmissionClaim] = []
    with _tx(conn):
        duplicate_rows = conn.execute(
            "SELECT q.origin_event_id FROM v6_paper_admission_queue q "
            "WHERE q.state IN ('queued','retry_wait') "
            "AND EXISTS ("
            "SELECT 1 FROM v6_paper_positions p WHERE p.network_id=q.network_id "
            "AND p.mint=q.mint AND p.status='open')",
        ).fetchall()
        for row in duplicate_rows:
            conn.execute(
                "UPDATE v6_paper_admission_queue SET state='terminal_unfilled',"
                "last_outcome='duplicate_open_coin',last_detail='duplicate_open_coin',"
                "next_attempt_at=NULL,updated_at=? "
                "WHERE origin_event_id=? AND policy_id='v6'",
                (now_iso, row["origin_event_id"]),
            )
        if cohort_policy_hash is not None:
            duplicate_rows = conn.execute(
                "SELECT q.origin_event_id FROM v6_paper_admission_queue q "
                "WHERE q.state IN ('queued','retry_wait') AND EXISTS ("
                "SELECT 1 FROM v6_paper_positions p "
                "JOIN v6_paper_entries e ON e.entry_id=p.entry_id "
                "WHERE p.network_id=q.network_id AND p.mint=q.mint "
                "AND e.policy_hash=?)",
                (cohort_policy_hash,),
            ).fetchall()
            for row in duplicate_rows:
                conn.execute(
                    "UPDATE v6_paper_admission_queue SET state='terminal_unfilled',"
                    "last_outcome='duplicate_policy_coin',last_detail='duplicate_policy_coin',"
                    "next_attempt_at=NULL,updated_at=? "
                    "WHERE origin_event_id=? AND policy_id='v6'",
                    (now_iso, row["origin_event_id"]),
                )
        inflight = int(conn.execute(
            "SELECT COUNT(*) FROM v6_paper_admission_queue WHERE state='inflight'"
        ).fetchone()[0])
        available = max(0, min(max_claims, max_open - open_count - inflight))
        if available == 0:
            return ()
        busy_coins = {
            (row["network_id"], row["mint"])
            for row in conn.execute(
                "SELECT network_id,mint FROM v6_paper_positions WHERE status='open' "
                "UNION SELECT network_id,mint FROM v6_paper_admission_queue "
                "WHERE state='inflight'",
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT * FROM v6_paper_admission_queue WHERE state IN ('queued','retry_wait') "
            "AND COALESCE(next_attempt_at,enqueued_at)<=? ORDER BY source_position_id",
            (now_iso,),
        ).fetchall()
        for row in rows:
            coin = (row["network_id"], row["mint"])
            if coin in busy_coins:
                continue
            received = _aware_datetime(row["signal_received_at"], "signal_received_at")
            if max(0.0, (now - received).total_seconds()) > ADMISSION_FRESHNESS_S:
                conn.execute(
                    "UPDATE v6_paper_admission_queue SET state='terminal_unfilled',"
                    "last_outcome='quote_stale',last_detail='admission_freshness_expired',"
                    "updated_at=? WHERE origin_event_id=? AND policy_id='v6'",
                    (now_iso, row["origin_event_id"]),
                )
                continue
            attempt_no = int(row["attempt_count"] or 0) + 1
            token = fingerprint({
                "origin": row["origin_event_id"], "attempt": attempt_no,
                "claimed_at": now_iso, "prior": row["claim_token"],
            })
            lease_expires = (now + timedelta(seconds=lease_s)).isoformat()
            conn.execute(
                "UPDATE v6_paper_admission_queue SET state='inflight',attempt_count=?,"
                "claim_token=?,claimed_at=?,lease_expires_at=?,next_attempt_at=NULL,updated_at=? "
                "WHERE origin_event_id=? AND policy_id='v6'",
                (attempt_no, token, now_iso, lease_expires, now_iso, row["origin_event_id"]),
            )
            current = conn.execute(
                "SELECT * FROM v6_paper_admission_queue WHERE origin_event_id=? AND policy_id='v6'",
                (row["origin_event_id"],),
            ).fetchone()
            claimed.append(_claim_from_row(current))
            busy_coins.add(coin)
            if len(claimed) >= available:
                break
    return tuple(claimed)


def record_entry_quote_result(conn, claim: AdmissionClaim, *, outcome: str, completed_at: str,
                              detail: str | None = None, provider: str | None = None,
                              entry_id: str | None = None) -> str:
    """Record one immutable attempt and advance its queue state under the exact claim token."""
    completed = _aware_datetime(completed_at, "completed_at")
    if outcome == "filled" and not entry_id:
        raise ValueError("filled admission requires entry_id")
    detail = str(detail or "")[:160] or None
    provider = str(provider or "")[:120] or None
    attempt_content = fingerprint({
        "origin": claim.origin_event_id, "attempt_no": claim.attempt_no,
        "claim_token": claim.claim_token, "started_at": claim.claimed_at,
        "completed_at": completed_at, "outcome": outcome, "detail": detail,
        "provider": provider,
    })
    with _tx(conn):
        row = conn.execute(
            "SELECT * FROM v6_paper_admission_queue WHERE origin_event_id=? AND policy_id='v6'",
            (claim.origin_event_id,),
        ).fetchone()
        if row is None or row["state"] != "inflight" or row["claim_token"] != claim.claim_token:
            raise StateConflict(f"obsolete admission claim: {claim.origin_event_id}")
        conn.execute(
            "INSERT INTO v6_paper_entry_quote_attempts "
            "(origin_event_id,policy_id,attempt_no,claim_token,started_at,completed_at,outcome,"
            "detail_code,provider,content_fingerprint) VALUES (?,'v6',?,?,?,?,?,?,?,?)",
            (claim.origin_event_id, claim.attempt_no, claim.claim_token, claim.claimed_at,
             completed_at, outcome, detail, provider, attempt_content),
        )
        received = _aware_datetime(row["signal_received_at"], "signal_received_at")
        fresh = max(0.0, (completed - received).total_seconds()) <= ADMISSION_FRESHNESS_S
        retry = (outcome in _ADMISSION_RETRYABLE
                 and claim.attempt_no < _ADMISSION_MAX_ATTEMPTS and fresh)
        if outcome == "filled":
            state, next_at = "filled", None
        elif retry:
            state = "retry_wait"
            delay = _ADMISSION_RETRY_DELAYS_S[claim.attempt_no - 1]
            next_at = (completed + timedelta(seconds=delay)).isoformat()
        else:
            state, next_at = "terminal_unfilled", None
        conn.execute(
            "UPDATE v6_paper_admission_queue SET state=?,next_attempt_at=?,claim_token=NULL,"
            "claimed_at=NULL,lease_expires_at=NULL,last_outcome=?,last_detail=?,entry_id=?,"
            "updated_at=? WHERE origin_event_id=? AND policy_id='v6'",
            (state, next_at, outcome, detail, entry_id, completed_at, claim.origin_event_id),
        )
    return state


def recover_admission_leases(conn, now_iso: str) -> int:
    """Turn expired in-flight work into a bounded retry; late provider results become obsolete."""
    now = _aware_datetime(now_iso, "now_iso")
    recovered = 0
    with _tx(conn):
        rows = conn.execute(
            "SELECT * FROM v6_paper_admission_queue WHERE state='inflight' "
            "AND lease_expires_at<=? ORDER BY source_position_id", (now_iso,),
        ).fetchall()
        for row in rows:
            summary = conn.execute(
                "SELECT outcome,entry_id FROM v6_paper_signal_attempts "
                "WHERE origin_event_id=? AND policy_id='v6'",
                (row["origin_event_id"],),
            ).fetchone()
            entry = conn.execute(
                "SELECT entry_id FROM v6_paper_entries WHERE origin_event_id=? "
                "AND network_id=? AND mint=? AND policy_id='v6'",
                (row["origin_event_id"], row["network_id"], row["mint"]),
            ).fetchone()
            if entry is not None:
                entry_id = entry["entry_id"]
                if summary is not None and (
                        summary["outcome"] != "filled"
                        or summary["entry_id"] not in (None, entry_id)):
                    raise StateConflict("admission entry conflicts with terminal signal summary")
                conn.execute(
                    "UPDATE v6_paper_admission_queue SET state='filled',next_attempt_at=NULL,"
                    "claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,last_outcome='filled',"
                    "last_detail='reconciled_existing_entry',entry_id=?,updated_at=? "
                    "WHERE origin_event_id=? AND policy_id='v6'",
                    (entry_id, now_iso, row["origin_event_id"]),
                )
                recovered += 1
                continue
            if summary is not None:
                if summary["outcome"] == "filled":
                    raise StateConflict("filled admission summary has no matching entry")
                conn.execute(
                    "UPDATE v6_paper_admission_queue SET state='terminal_unfilled',"
                    "next_attempt_at=NULL,claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,"
                    "last_outcome=?,last_detail='reconciled_terminal_summary',entry_id=NULL,"
                    "updated_at=? WHERE origin_event_id=? AND policy_id='v6'",
                    (summary["outcome"], now_iso, row["origin_event_id"]),
                )
                recovered += 1
                continue
            attempt_no = int(row["attempt_count"])
            received = _aware_datetime(row["signal_received_at"], "signal_received_at")
            fresh = max(0.0, (now - received).total_seconds()) <= ADMISSION_FRESHNESS_S
            retry = attempt_no < _ADMISSION_MAX_ATTEMPTS and fresh
            state = "retry_wait" if retry else "terminal_unfilled"
            next_at = ((now + timedelta(seconds=_ADMISSION_RETRY_DELAYS_S[attempt_no - 1])).isoformat()
                       if retry else None)
            content = fingerprint({
                "origin": row["origin_event_id"], "attempt_no": attempt_no,
                "claim_token": row["claim_token"], "started_at": row["claimed_at"],
                "completed_at": now_iso, "outcome": "lease_expired", "detail": "lease_expired",
                "provider": None,
            })
            conn.execute(
                "INSERT OR IGNORE INTO v6_paper_entry_quote_attempts "
                "(origin_event_id,policy_id,attempt_no,claim_token,started_at,completed_at,outcome,"
                "detail_code,provider,content_fingerprint) VALUES (?,'v6',?,?,?,?,?,?,NULL,?)",
                (row["origin_event_id"], attempt_no, row["claim_token"], row["claimed_at"],
                 now_iso, "lease_expired", "lease_expired", content),
            )
            conn.execute(
                "UPDATE v6_paper_admission_queue SET state=?,next_attempt_at=?,claim_token=NULL,"
                "claimed_at=NULL,lease_expires_at=NULL,last_outcome='lease_expired',"
                "last_detail='lease_expired',updated_at=? WHERE origin_event_id=? AND policy_id='v6'",
                (state, next_at, now_iso, row["origin_event_id"]),
            )
            recovered += 1
    return recovered


def admission_summary(conn, now_iso: str | None = None) -> dict:
    rows = conn.execute(
        "SELECT state,last_outcome,last_detail,enqueued_at FROM v6_paper_admission_queue"
    ).fetchall()
    states = {name: 0 for name in
              ("queued", "inflight", "retry_wait", "filled", "terminal_unfilled")}
    reasons: dict[str, int] = {}
    for row in rows:
        states[row["state"]] += 1
        if row["state"] == "terminal_unfilled":
            reason = row["last_detail"] or row["last_outcome"] or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
    attempts = int(conn.execute(
        "SELECT COUNT(*) FROM v6_paper_entry_quote_attempts").fetchone()[0])
    rejection_rows = conn.execute(
        "SELECT reason_code,COUNT(*) AS count FROM v6_paper_admission_rejections "
        "GROUP BY reason_code ORDER BY reason_code"
    ).fetchall()
    rejection_reasons = {row["reason_code"]: int(row["count"]) for row in rejection_rows}
    historical = int(conn.execute(
        "SELECT COUNT(*) FROM v6_paper_admission_queue "
        "WHERE state='terminal_unfilled' AND last_outcome='historical_intake_gap'"
    ).fetchone()[0])
    migration_marker = get_cursor(conn, "source_position_historical_gap")
    try:
        migration_marker = json.loads(migration_marker) if migration_marker else None
    except (TypeError, ValueError):
        migration_marker = {"kind": "invalid_marker", "raw": str(migration_marker)[:160]}
    if migration_marker is not None:
        # This marker denotes one bounded cursor-domain gap episode. Its row count is deliberately
        # unknown because startup must not scan the multi-GB source ledger before tailing live.
        historical += 1
    pending_times = [row["enqueued_at"] for row in rows
                     if row["state"] in {"queued", "inflight", "retry_wait"}]
    oldest_at = min(pending_times) if pending_times else None
    oldest_age = None
    if oldest_at is not None:
        if now_iso is None:
            from sniper.util.clock import real_utc_now_iso
            now_iso = real_utc_now_iso()
        oldest_age = max(0.0, (_aware_datetime(now_iso, "now_iso")
                               - _aware_datetime(oldest_at, "enqueued_at")).total_seconds())
    return {
        "states": states, "terminal_reasons": reasons, "attempts": attempts,
        "source_rejections": {
            "total": sum(rejection_reasons.values()), "reasons": rejection_reasons,
        },
        "historical_gaps": historical, "oldest_pending_at": oldest_at,
        "oldest_pending_age_s": oldest_age, "historical_gap_marker": migration_marker,
    }


def mark_admission_terminal(conn, origin_event_id: str, *, outcome: str, detail: str,
                            updated_at: str) -> bool:
    """Finalize a queued historical/invalid candidate without contacting a quote provider."""
    _aware_timestamp(updated_at, "updated_at")
    with _tx(conn):
        row = conn.execute(
            "SELECT state FROM v6_paper_admission_queue WHERE origin_event_id=? AND policy_id='v6'",
            (origin_event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no admission: {origin_event_id}")
        if row["state"] in ("filled", "terminal_unfilled"):
            return False
        if row["state"] == "inflight":
            raise StateConflict("cannot terminalize an in-flight admission")
        conn.execute(
            "UPDATE v6_paper_admission_queue SET state='terminal_unfilled',last_outcome=?,"
            "last_detail=?,next_attempt_at=NULL,updated_at=? "
            "WHERE origin_event_id=? AND policy_id='v6'",
            (str(outcome)[:80], str(detail)[:160], updated_at, origin_event_id),
        )
    return True

def record_signal_attempt(conn, *, origin_event_id, network_id, mint, outcome, attempted_at,
                          policy_id="v6", detail=None, entry_id=None):
    if not str(origin_event_id or "").strip():
        raise ValueError("origin_event_id must not be empty")
    if policy_id != "v6":
        raise ValueError("policy_id must be 'v6'")
    if network_id not in SUPPORTED_NETWORKS:
        raise ValueError(f"unsupported network_id: {network_id!r}")
    if not str(mint or "").strip():
        raise ValueError("mint must not be empty")
    if outcome not in SIGNAL_OUTCOMES:
        raise ValueError(f"unknown signal outcome: {outcome!r}")
    _aware_timestamp(attempted_at, "attempted_at")
    detail_json = canonical_json(detail) if detail is not None else None
    content = fingerprint({"network_id": network_id, "mint": mint, "outcome": outcome,
                           "attempted_at": attempted_at, "detail": detail_json})
    with _tx(conn):
        existing = conn.execute(
            "SELECT content_fingerprint, entry_id FROM v6_paper_signal_attempts "
            "WHERE origin_event_id=? AND policy_id=?", (origin_event_id, policy_id)).fetchone()
        if existing is not None:
            if existing[0] != content:
                raise ImmutableConflict(f"signal attempt {origin_event_id}/{policy_id} redelivered "
                                        "with different content")
            if entry_id is not None and existing["entry_id"] not in (None, entry_id):
                raise ImmutableConflict(
                    f"signal attempt {origin_event_id}/{policy_id} linked to another entry")
            if entry_id is not None and existing["entry_id"] is None:
                conn.execute(
                    "UPDATE v6_paper_signal_attempts SET entry_id=? "
                    "WHERE origin_event_id=? AND policy_id=?",
                    (entry_id, origin_event_id, policy_id))
            return
        conn.execute(
            "INSERT INTO v6_paper_signal_attempts (origin_event_id, policy_id, network_id, mint, "
            "outcome, attempted_at, detail_json, entry_id, content_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (origin_event_id, policy_id, network_id, mint, outcome, attempted_at, detail_json,
             entry_id, content))


_ENTRY_IMMUTABLE_FIELDS = (
    "origin_event_id", "network_id", "mint", "ticker", "source_wallet_id", "source_wallet_handle",
    "signal_source", "notional_usd_micros", "entry_price", "quote_in_asset", "quote_in_atomic",
    "quote_in_decimals", "quote_out_asset", "quote_out_atomic", "quote_out_decimals",
    "token_qty_atomic", "route_target", "pool_or_vault", "quote_id", "quote_provider",
    "quote_age_s", "quote_fingerprint", "net_pnl_computable", "entry_mcap", "entry_liquidity",
    "signal_ts", "quote_ts", "fill_ts", "policy_hash", "source_receipt_id",
    "adapter_version", "fill_model_version", "rule_version", "evaluator_version",
    "launch_mode",
)


def record_entry_fill(conn, entry) -> V6PaperPosition:
    entry = dict(entry)
    _validate_entry(entry)
    entry_id = entry["entry_id"]
    policy_id = "v6"                                   # fixed; never caller-overridable
    token_qty = entry["token_qty_atomic"]
    policy_document = canonical_json(entry.get("policy_document"))
    evidence = _quote_evidence(
        quote_payload=entry["quote_payload"], quote_fingerprint=entry["quote_fingerprint"],
        route_binding=entry["route_binding"], costs=entry["costs"],
        net_pnl_computable=entry["net_pnl_computable"], field_prefix="entry")
    content = fingerprint({k: entry.get(k) for k in _ENTRY_IMMUTABLE_FIELDS}
                          | {"policy_document": policy_document, "policy_id": policy_id,
                             **evidence})
    with _tx(conn):
        existing = conn.execute("SELECT content_fingerprint FROM v6_paper_entries WHERE entry_id=?",
                                (entry_id,)).fetchone()
        if existing is not None:
            if existing[0] != content:
                raise ImmutableConflict(f"entry {entry_id} redelivered with different content")
            return _load_position(conn, entry_id)
        natural = conn.execute(
            "SELECT entry_id, content_fingerprint FROM v6_paper_entries "
            "WHERE origin_event_id=? AND network_id=? AND mint=? AND policy_id=?",
            (entry["origin_event_id"], entry["network_id"], entry["mint"], policy_id)).fetchone()
        if natural is not None:
            if natural["entry_id"] != entry_id or natural["content_fingerprint"] != content:
                raise ImmutableConflict(
                    "entry natural identity redelivered with different immutable identity/content")
            return _load_position(conn, natural["entry_id"])
        attempt = conn.execute(
            "SELECT network_id, mint, outcome, entry_id FROM v6_paper_signal_attempts "
            "WHERE origin_event_id=? AND policy_id=?",
            (entry["origin_event_id"], policy_id)).fetchone()
        if attempt is not None and (
                attempt["network_id"] != entry["network_id"]
                or attempt["mint"] != entry["mint"]
                or attempt["outcome"] != "filled"
                or attempt["entry_id"] not in (None, entry_id)):
            raise StateConflict("signal attempt does not authorize this filled entry")
        stored_entry = {
            **entry, **evidence, "policy_document": policy_document,
            "content_fingerprint": content,
            "quote_in_atomic": _atomic_text(entry["quote_in_atomic"]),
            "quote_out_atomic": _atomic_text(entry["quote_out_atomic"]),
            "token_qty_atomic": _atomic_text(entry["token_qty_atomic"]),
        }
        conn.execute(
            "INSERT INTO v6_paper_entries (entry_id, origin_event_id, network_id, mint, policy_id, "
            "ticker, source_wallet_id, source_wallet_handle, signal_source, notional_usd_micros, "
            "entry_price, quote_in_asset, quote_in_atomic, quote_in_decimals, quote_out_asset, "
            "quote_out_atomic, quote_out_decimals, token_qty_atomic, route_target, pool_or_vault, "
            "quote_id, quote_provider, quote_age_s, quote_fingerprint, quote_payload_json, "
            "route_binding_json, costs_json, net_pnl_computable, entry_mcap, entry_liquidity, "
            "signal_ts, quote_ts, fill_ts, fill_kind, policy_hash, policy_document, "
            "source_receipt_id, adapter_version, fill_model_version, rule_version, "
            "evaluator_version, launch_mode, content_fingerprint) "
            "VALUES (:entry_id,:origin_event_id,:network_id,:mint,'v6',"
            ":ticker,:source_wallet_id,:source_wallet_handle,:signal_source,:notional_usd_micros,"
            ":entry_price,:quote_in_asset,:quote_in_atomic,:quote_in_decimals,:quote_out_asset,"
            ":quote_out_atomic,:quote_out_decimals,:token_qty_atomic,:route_target,:pool_or_vault,"
            ":quote_id,:quote_provider,:quote_age_s,:quote_fingerprint,:quote_payload_json,"
            ":route_binding_json,:costs_json,:net_pnl_computable,:entry_mcap,:entry_liquidity,"
            ":signal_ts,:quote_ts,:fill_ts,'modeled_live_quote',:policy_hash,:policy_document,"
            ":source_receipt_id,:adapter_version,:fill_model_version,:rule_version,"
            ":evaluator_version,:launch_mode,"
            ":content_fingerprint)",
            {**stored_entry, **{
                name: entry.get(name) for name in (
                    "source_receipt_id", "adapter_version", "fill_model_version", "rule_version",
                    "evaluator_version", "launch_mode")
            }})
        init_state = V6PositionState(
            position_id=entry_id, remaining_qty_atomic=token_qty,
            entry_unit_price=str(entry["entry_price"]),
            entry_quote_asset=entry["quote_in_asset"], opened_at=entry["fill_ts"],
            break_state=V6State())
        state_json = _state_to_json(init_state)
        conn.execute(
            "INSERT INTO v6_paper_positions (position_id, entry_id, network_id, mint, status, "
            "entry_qty_atomic, remaining_qty_atomic, engine_state_json, engine_state_fingerprint, "
            "updated_at) VALUES (?,?,?,?, 'open', ?,?,?,?,?)",
            (entry_id, entry_id, entry["network_id"], entry["mint"],
             _atomic_text(token_qty), _atomic_text(token_qty),
             state_json, fingerprint(state_json), entry.get("fill_ts")))
        conn.execute(
            "INSERT INTO v6_paper_fills (position_id, leg, action_token, token_qty_atomic, "
            "quote_in_asset, quote_in_atomic, quote_in_decimals, quote_out_asset, quote_out_atomic, "
            "quote_out_decimals, route_quote_fingerprint, quote_provider, quote_payload_json, "
            "route_binding_json, costs_json, net_pnl_computable, unit_price, quote_age_s, created_at) "
            "VALUES (:position_id, 'entry', NULL, :token_qty_atomic, :quote_in_asset, "
            ":quote_in_atomic, :quote_in_decimals, :quote_out_asset, :quote_out_atomic, "
            ":quote_out_decimals, :quote_fingerprint, :quote_provider, :quote_payload_json, "
            ":route_binding_json, :costs_json, :net_pnl_computable, :entry_price, :quote_age_s, "
            ":fill_ts)",
            {**stored_entry, "position_id": entry_id})
        # link an already-recorded signal attempt to this entry (idempotent)
        conn.execute(
            "UPDATE v6_paper_signal_attempts SET entry_id=? WHERE origin_event_id=? AND policy_id=?",
            (entry_id, entry["origin_event_id"], policy_id))
    return _load_position(conn, entry_id)


def finalize_direct_entry_fill(conn, entry: Mapping, *, attempted_at: str) -> V6PaperPosition | None:
    """Atomically finalize the compatibility one-shot runner's successful paper admission."""
    entry = dict(entry)
    with _tx(conn):
        if has_open_coin(conn, entry.get("network_id"), entry.get("mint")) \
                or has_coin_lifecycle(
                    conn, entry.get("network_id"), entry.get("mint"),
                    entry.get("policy_hash")):
            return None
        record_signal_attempt(
            conn, origin_event_id=entry.get("origin_event_id"),
            network_id=entry.get("network_id"), mint=entry.get("mint"), outcome="filled",
            attempted_at=attempted_at, entry_id=entry.get("entry_id"),
        )
        return record_entry_fill(conn, entry)


def finalize_admission_fill(conn, claim: AdmissionClaim, entry: Mapping, *, completed_at: str,
                            provider: str | None = None) -> V6PaperPosition | None:
    """Commit the successful quote attempt, signal summary, entry book, and queue result together."""
    entry = dict(entry)
    if entry.get("origin_event_id") != claim.origin_event_id \
            or entry.get("network_id") != claim.network_id \
            or entry.get("mint") != claim.mint:
        raise StateConflict("admission claim does not match filled entry identity")
    with _tx(conn):
        row = conn.execute(
            "SELECT state,claim_token FROM v6_paper_admission_queue "
            "WHERE origin_event_id=? AND policy_id='v6'",
            (claim.origin_event_id,),
        ).fetchone()
        if row is None or row["state"] != "inflight" or row["claim_token"] != claim.claim_token:
            raise StateConflict(f"obsolete admission claim: {claim.origin_event_id}")
        if has_open_coin(conn, claim.network_id, claim.mint) or has_coin_lifecycle(
                conn, claim.network_id, claim.mint, entry.get("policy_hash")):
            reason = ("duplicate_open_coin" if has_open_coin(
                conn, claim.network_id, claim.mint) else "duplicate_policy_coin")
            state = record_entry_quote_result(
                conn, claim, outcome=reason, completed_at=completed_at,
                detail=reason, provider=provider,
            )
            if state != "terminal_unfilled":
                raise StateConflict(f"duplicate admission finalized as {state}")
            return None
        record_signal_attempt(
            conn, origin_event_id=claim.origin_event_id, network_id=claim.network_id,
            mint=claim.mint, outcome="filled", attempted_at=completed_at,
            entry_id=entry["entry_id"],
        )
        position = record_entry_fill(conn, entry)
        state = record_entry_quote_result(
            conn, claim, outcome="filled", completed_at=completed_at, detail="filled",
            provider=provider, entry_id=entry["entry_id"],
        )
        if state != "filled":
            raise StateConflict(f"successful admission finalized as {state}")
        return position


def _append_observation_in_tx(conn, position_id, observation, *, block_identity=None,
                              capture_version=None):
    payload_json = canonical_json(dict(observation.payload))
    expected_input = fingerprint({
        "observation_id": observation.observation_id,
        "observed_at": observation.observed_at,
        "network_id": observation.network_id,
        "payload": dict(observation.payload),
        "evidence": {
            "market": observation.evidence.market,
            "quote": observation.evidence.quote,
            "source": observation.evidence.source,
        },
    })
    if expected_input != observation.input_fingerprint:
        raise ValueError("observation input_fingerprint mismatch")
    _aware_timestamp(observation.observed_at, "observed_at")
    content = fingerprint({"observed_at": observation.observed_at,
                           "network_id": observation.network_id, "payload": payload_json,
                           "input_fingerprint": observation.input_fingerprint,
                           "block_identity": block_identity, "capture_version": capture_version})
    position = conn.execute(
        "SELECT status, network_id FROM v6_paper_positions WHERE position_id=?",
        (position_id,)).fetchone()
    if position is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    if position["status"] != "open":
        raise StateConflict(f"position {position_id} is {position['status']}, not open")
    if position["network_id"] != observation.network_id:
        raise ValueError("observation network_id does not match position")
    existing = conn.execute(
        "SELECT payload_json, observed_at, network_id, input_fingerprint, block_identity, "
        "capture_version FROM v6_paper_observations "
        "WHERE position_id=? AND observation_id=?",
        (position_id, observation.observation_id)).fetchone()
    if existing is not None:
        prior = fingerprint({"observed_at": existing["observed_at"],
                             "network_id": existing["network_id"],
                             "payload": existing["payload_json"],
                             "input_fingerprint": existing["input_fingerprint"],
                             "block_identity": existing["block_identity"],
                             "capture_version": existing["capture_version"]})
        if prior != content:
            raise ImmutableConflict(
                f"observation {observation.observation_id} redelivered with different content")
        return False
    conn.execute(
        "INSERT INTO v6_paper_observations (observation_id, position_id, observed_at, network_id, "
        "block_identity, payload_json, input_fingerprint, capture_version) VALUES (?,?,?,?,?,?,?,?)",
        (observation.observation_id, position_id, observation.observed_at,
         observation.network_id, block_identity, payload_json, observation.input_fingerprint,
         capture_version))
    conn.execute("UPDATE v6_paper_positions SET latest_observation_id=? WHERE position_id=?",
                 (observation.observation_id, position_id))
    return True


def append_observation(conn, position_id, observation, *, block_identity=None,
                       capture_version=None):
    with _tx(conn):
        return _append_observation_in_tx(
            conn, position_id, observation, block_identity=block_identity,
            capture_version=capture_version)


def _decision_content(position_id: str, decision: V6Decision) -> str:
    return fingerprint({
        "position_id": position_id, "action": decision.action.value,
        "fraction_ppm": decision.fraction_ppm, "input_fingerprint": decision.input_fingerprint,
        "policy_hash": decision.policy_hash,
        "state_if_filled": _state_to_json(decision.state_if_filled),
        "state_if_unfilled": _state_to_json(decision.state_if_unfilled),
        "reason_codes": list(decision.reason_codes),
        "thought": decision.thought,
        "semantic_transition": decision.semantic_transition,
        "action_intent_token": decision.action_intent_token,
        "supersedes_token": decision.supersedes_token,
        "rule_results": _rule_results_document(decision.rule_results),
    })


def _validate_decision_shape(position_id: str, current: V6PositionState,
                             decision: V6Decision) -> None:
    if decision.state_if_filled.position_id != position_id \
            or decision.state_if_unfilled.position_id != position_id:
        raise ValueError("decision successor position_id does not match position")
    if decision.action is V6Action.HOLD:
        if decision.fraction_ppm != 0:
            raise ValueError("HOLD decision fraction_ppm must be zero")
        if decision.state_if_filled != decision.state_if_unfilled:
            raise ValueError("HOLD decision successors must match")
        if decision.state_if_filled.remaining_qty_atomic != current.remaining_qty_atomic:
            raise StateConflict("HOLD decision changed inventory")
        return
    if not 0 < decision.fraction_ppm <= FRACTION_PPM_SCALE:
        raise ValueError("action decision fraction_ppm must be in 1..1000000")
    if decision.state_if_filled.last_action_outcome != "filled":
        raise ValueError("state_if_filled must record last_action_outcome='filled'")
    if decision.state_if_unfilled.last_action_outcome != "unfilled":
        raise ValueError("state_if_unfilled must record last_action_outcome='unfilled'")
    requested = min(
        current.remaining_qty_atomic,
        max(1, (current.remaining_qty_atomic * decision.fraction_ppm) // FRACTION_PPM_SCALE),
    )
    if decision.state_if_filled.remaining_qty_atomic != current.remaining_qty_atomic - requested:
        raise StateConflict("decision state_if_filled inventory does not match fraction")
    if decision.state_if_unfilled.remaining_qty_atomic != current.remaining_qty_atomic:
        raise StateConflict("decision state_if_unfilled changed inventory")


def _append_decision_in_tx(conn, position_id, decision: V6Decision):
    content = _decision_content(position_id, decision)
    existing = conn.execute(
        "SELECT content_fingerprint FROM v6_paper_decisions WHERE action_token=?",
        (decision.action_token,)).fetchone()
    if existing is not None:
        if existing[0] != content:
            raise ImmutableConflict(
                f"decision {decision.action_token} redelivered with different content")
        return False
    pending = conn.execute(
        "SELECT action_token FROM v6_paper_decisions "
        "WHERE position_id=? AND applied_outcome IS NULL LIMIT 1", (position_id,)).fetchone()
    if pending is not None:
        raise StateConflict(
            f"position {position_id} already has unapplied decision {pending['action_token']}")
    pos = conn.execute(
        "SELECT p.engine_state_json, p.status, e.policy_hash FROM v6_paper_positions p "
        "JOIN v6_paper_entries e ON e.entry_id=p.entry_id WHERE p.position_id=?",
        (position_id,)).fetchone()
    if pos is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    if pos["status"] != "open":
        raise StateConflict(f"position {position_id} is {pos['status']}, not open")
    if decision.policy_hash != pos["policy_hash"]:
        raise ValueError("decision policy_hash does not match entry")
    current_state = _state_from_json(pos["engine_state_json"])
    _validate_decision_shape(position_id, current_state, decision)
    observation = conn.execute(
        "SELECT observation_id, observed_at FROM v6_paper_observations "
        "WHERE position_id=? AND input_fingerprint=? LIMIT 1",
        (position_id, decision.input_fingerprint)).fetchone()
    if observation is None:
        raise ValueError(
            f"decision references no stored observation (input_fingerprint="
            f"{decision.input_fingerprint})")
    pending_state_json = (_state_to_json(decision.state_if_filled)
                          if decision.action is V6Action.HOLD else None)
    conn.execute(
        "INSERT INTO v6_paper_decisions (action_token, position_id, observation_id, action, "
        "fraction_ppm, state_before_hash, state_after_hash, state_delta_json, "
        "pending_state_json, reason_codes_json, rule_results_json, thought, input_fingerprint, policy_hash, "
        "semantic_transition, action_intent_token, supersedes_token, content_fingerprint, "
        "decided_at) VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)",
        (decision.action_token, position_id, observation["observation_id"],
         decision.action.value, decision.fraction_ppm, fingerprint(pos["engine_state_json"]),
         pending_state_json, canonical_json(list(decision.reason_codes)),
         canonical_json(_rule_results_document(decision.rule_results)), decision.thought,
         decision.input_fingerprint, decision.policy_hash, int(decision.semantic_transition),
         decision.action_intent_token, decision.supersedes_token, content,
         observation["observed_at"]))
    if decision.supersedes_token:
        cancellation = decision.action is V6Action.HOLD
        updated = conn.execute(
            "UPDATE v6_paper_action_intents SET status='superseded', reason=?, resolved_at=? "
            "WHERE action_token=? AND position_id=? AND status='active'",
            ("trigger_invalidated" if cancellation else "stronger_intent",
             observation["observed_at"], decision.supersedes_token, position_id),
        ).rowcount
        if updated != 1:
            raise StateConflict("superseded action intent is not active")
    if decision.action is not V6Action.HOLD:
        persisted_intent = decision.state_if_unfilled.active_intent
        intent_token = decision.action_intent_token or decision.action_token
        priority = (persisted_intent.priority if persisted_intent is not None
                    else 2 if decision.action is V6Action.EXIT_FULL else 1)
        trigger_id = (persisted_intent.trigger_evidence_id if persisted_intent is not None
                      else observation["observation_id"])
        conn.execute(
            "INSERT INTO v6_paper_action_intents (action_token, position_id, action, "
            "fraction_ppm, priority, trigger_evidence_id, state_if_filled_json, "
            "state_if_unfilled_json, supersedes_token, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,'active')",
            (intent_token, position_id, decision.action.value, decision.fraction_ppm,
             priority, trigger_id, _state_to_json(decision.state_if_filled),
             _state_to_json(decision.state_if_unfilled), decision.supersedes_token))
    conn.execute("UPDATE v6_paper_positions SET latest_decision_token=?, latest_action=? "
                 "WHERE position_id=?",
                 (decision.action_token, decision.action.value, position_id))
    return True


def append_decision(conn, position_id, decision: V6Decision):
    with _tx(conn):
        return _append_decision_in_tx(conn, position_id, decision)


def _apply_pending_action_in_tx(conn, position_id, decision: V6Decision, at: str) -> None:
    stored, row = _persisted_decision(conn, decision, position_id)
    if fingerprint(row["engine_state_json"]) != stored["state_before_hash"]:
        raise StateConflict("stale action: current engine state differs from state_before")
    before_state = _state_from_json(row["engine_state_json"])
    pending_state = decision.state_if_unfilled
    state_json = _state_to_json(pending_state)
    conn.execute(
        "UPDATE v6_paper_positions SET engine_state_json=?, engine_state_fingerprint=?, "
        "latest_action=?, updated_at=? WHERE position_id=?",
        (state_json, fingerprint(state_json), decision.action.value, at, position_id),
    )
    _record_state_transition(conn, stored, before_state, pending_state, "unfilled", at)


def append_transition(conn, position_id, observation, decision: V6Decision, *,
                      block_identity=None, capture_version=None) -> bool:
    """Persist one semantic observation/decision transition atomically.

    A repeated semantic heartbeat has no causal effect and writes no append-only row. Action
    decisions remain unapplied until their exact-quantity modeled quote resolves; HOLD state is
    committed in this transaction because it has no network-result fork.
    """
    if decision.input_fingerprint != observation.input_fingerprint:
        raise ValueError("decision input_fingerprint does not match observation")
    if not decision.semantic_transition:
        if decision.action is not V6Action.HOLD:
            raise ValueError("semantic no-op must be HOLD")
        current = _load_position(conn, position_id).engine_state
        _validate_decision_shape(position_id, current, decision)
        if decision.state_if_filled != current:
            raise StateConflict("semantic no-op changed engine state")
        return False
    with _tx(conn):
        _append_observation_in_tx(
            conn, position_id, observation, block_identity=block_identity,
            capture_version=capture_version)
        inserted = _append_decision_in_tx(conn, position_id, decision)
        if not inserted:
            return False
        if decision.action is V6Action.HOLD:
            stored, row = _persisted_decision(conn, decision, position_id)
            _apply_hold_in_tx(conn, position_id, decision, stored, row, observation.observed_at)
        else:
            _apply_pending_action_in_tx(
                conn, position_id, decision, observation.observed_at)
        return True


# ---- action application (the money-safety core) ----------------------------------------------

def _persisted_decision(conn, decision: V6Decision, position_id: str):
    stored = conn.execute(
        "SELECT * FROM v6_paper_decisions WHERE action_token=? AND position_id=?",
        (decision.action_token, position_id)).fetchone()
    if stored is None:
        raise ValueError(f"action {decision.action_token} has no persisted decision")
    if stored["content_fingerprint"] != _decision_content(position_id, decision):
        raise ImmutableConflict(
            f"decision {decision.action_token} does not match persisted immutable content")
    position = conn.execute(
        "SELECT * FROM v6_paper_positions WHERE position_id=?", (position_id,)).fetchone()
    if position is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    return stored, position


def _validate_exit_fill(modeled_fill, expected_qty: int) -> tuple[str, dict]:
    fill_qty = modeled_fill.get("token_qty_atomic")
    if isinstance(fill_qty, bool) or not isinstance(fill_qty, int) or fill_qty <= 0:
        raise ValueError("modeled fill token_qty_atomic must be a positive integer")
    if fill_qty != expected_qty:
        raise ValueError(f"modeled fill {fill_qty} != requested {expected_qty}")
    if modeled_fill.get("quote_in_atomic") != fill_qty:
        raise ValueError("modeled exit quote_in_atomic must equal token_qty_atomic")
    quote_out = modeled_fill.get("quote_out_atomic")
    if isinstance(quote_out, bool) or not isinstance(quote_out, int) or quote_out <= 0:
        raise ValueError("modeled exit quote_out_atomic must be a positive integer")
    for name in ("quote_in_decimals", "quote_out_decimals"):
        value = modeled_fill.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30:
            raise ValueError(f"{name} must be an integer between 0 and 30")
    for name in ("quote_in_asset", "quote_out_asset", "quote_fingerprint", "quote_provider"):
        if not str(modeled_fill.get(name) or "").strip():
            raise ValueError(f"modeled exit {name} must not be empty")
    _positive_decimal(modeled_fill.get("unit_price"), "unit_price")
    age = modeled_fill.get("quote_age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)) \
            or not math.isfinite(age) or age < 0:
        raise ValueError("modeled exit quote_age_s must be finite and >= 0")
    evidence = _quote_evidence(
        quote_payload=modeled_fill.get("quote_payload"),
        quote_fingerprint=modeled_fill.get("quote_fingerprint"),
        route_binding=modeled_fill.get("route_binding"), costs=modeled_fill.get("costs"),
        net_pnl_computable=modeled_fill.get("net_pnl_computable"),
        field_prefix="modeled exit")
    return _aware_timestamp(modeled_fill.get("observed_at"), "observed_at"), evidence

def _verify_post_fill(conn, position_id, expected_remaining):
    """Invariant guard run inside the fill transaction, before COMMIT: the row's remaining must
    equal what we computed. A real check; also the seam the rollback test injects a failure at."""
    got = conn.execute("SELECT remaining_qty_atomic FROM v6_paper_positions WHERE position_id=?",
                       (position_id,)).fetchone()[0]
    if int(got) != expected_remaining:
        raise StateConflict(f"post-fill remaining {got} != expected {expected_remaining}")


_RETRY_DELAYS_S = (0, 1, 2, 5, 10, 30)


def retry_delay_s(completed_attempt_count: int) -> float:
    if isinstance(completed_attempt_count, bool) or not isinstance(completed_attempt_count, int) \
            or completed_attempt_count < 0:
        raise ValueError("completed_attempt_count must be a non-negative integer")
    return float(_RETRY_DELAYS_S[min(completed_attempt_count, len(_RETRY_DELAYS_S) - 1)])


def _row_to_intent(row) -> PersistedV6Intent:
    return PersistedV6Intent(
        token=row["action_token"], position_id=row["position_id"],
        action=V6Action(row["action"]), fraction_ppm=int(row["fraction_ppm"]),
        priority=int(row["priority"]), trigger_evidence_id=row["trigger_evidence_id"],
        supersedes_token=row["supersedes_token"], attempt_count=int(row["attempt_count"]),
        last_attempt_at=row["last_attempt_at"], next_attempt_at=row["next_attempt_at"],
        last_quote_status=row["last_quote_status"],
    )


def active_intent(conn, position_id: str) -> PersistedV6Intent | None:
    row = conn.execute(
        "SELECT * FROM v6_paper_action_intents WHERE position_id=? AND status='active'",
        (position_id,),
    ).fetchone()
    return _row_to_intent(row) if row is not None else None


EXIT_FOLLOWUP_HORIZONS_S = (30, 120, 300, 900, 3600, 10800, 43200)
EXIT_FOLLOWUP_VERSION = "v1"


def _schedule_exit_followups_in_tx(conn, position_id: str, action_token: str,
                                   token_qty_atomic: int, filled_at: str) -> None:
    filled = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
    for horizon_s in EXIT_FOLLOWUP_HORIZONS_S:
        followup_id = fingerprint({
            "version": EXIT_FOLLOWUP_VERSION,
            "action_token": action_token,
            "horizon_s": horizon_s,
        })
        due_at = (filled + timedelta(seconds=horizon_s)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO v6_paper_exit_followup_schedules "
            "(followup_id,position_id,action_token,horizon_s,due_at,token_qty_atomic,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (followup_id, position_id, action_token, horizon_s, due_at,
             _atomic_text(token_qty_atomic), filled_at),
        )


def _row_to_followup(row) -> ExitFollowupSchedule:
    return ExitFollowupSchedule(
        followup_id=row["followup_id"], position_id=row["position_id"],
        action_token=row["action_token"], horizon_s=int(row["horizon_s"]),
        due_at=row["due_at"], token_qty_atomic=int(row["token_qty_atomic"]),
        created_at=row["created_at"],
    )


def due_exit_followup(conn, now_iso: str) -> ExitFollowupSchedule | None:
    _aware_timestamp(now_iso, "followup_now")
    row = conn.execute(
        "SELECT s.* FROM v6_paper_exit_followup_schedules s "
        "LEFT JOIN v6_paper_exit_followup_results r ON r.followup_id=s.followup_id "
        "WHERE r.followup_id IS NULL AND s.due_at<=? ORDER BY s.due_at,s.followup_id LIMIT 1",
        (now_iso,),
    ).fetchone()
    return _row_to_followup(row) if row is not None else None


def _outcome_field(outcome, name, default=None):
    if isinstance(outcome, Mapping):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


def record_exit_followup_result(conn, followup_id: str, outcome, observed_at: str) -> bool:
    observed_at = _aware_timestamp(observed_at, "followup_observed_at")
    schedule = conn.execute(
        "SELECT * FROM v6_paper_exit_followup_schedules WHERE followup_id=?", (followup_id,),
    ).fetchone()
    if schedule is None:
        raise KeyError(f"no v6 exit followup: {followup_id}")
    raw_status = _outcome_field(outcome, "status")
    status = getattr(raw_status, "value", raw_status)
    healthy = status in (None, "healthy") and _outcome_field(outcome, "quote_out_atomic") is not None
    if healthy:
        status = "healthy"
        expected_qty = int(schedule["token_qty_atomic"])
        if isinstance(outcome, Mapping):
            fill = dict(outcome)
        else:
            fill = {
                name: getattr(outcome, name, None) for name in (
                    "token_qty_atomic", "quote_in_asset", "quote_in_atomic",
                    "quote_in_decimals", "quote_out_asset", "quote_out_atomic",
                    "quote_out_decimals", "quote_fingerprint", "quote_provider",
                    "quote_payload", "route_binding", "costs", "net_pnl_computable",
                    "quote_age_s", "unit_price", "observed_at")
            }
        _filled_at, evidence = _validate_exit_fill(fill, expected_qty)
        values = {
            "quote_out_asset": fill.get("quote_out_asset"),
            "quote_out_atomic": _atomic_text(fill.get("quote_out_atomic")),
            "quote_out_decimals": fill.get("quote_out_decimals"),
            "unit_price": fill.get("unit_price"), "quote_provider": fill.get("quote_provider"),
            "quote_fingerprint": fill.get("quote_fingerprint"),
            "quote_payload_json": evidence["quote_payload_json"],
            "route_binding_json": evidence["route_binding_json"],
            "costs_json": evidence["costs_json"], "detail_code": None,
        }
    else:
        if status not in {"route_absent", "provider_unavailable", "stale"}:
            raise ValueError(f"invalid followup quote status: {status!r}")
        provider = str(_outcome_field(outcome, "provider") or "").strip()
        detail = str(_outcome_field(outcome, "detail_code") or "").strip()
        if not provider or not detail:
            raise ValueError("followup quote failure requires provider and detail_code")
        values = {
            "quote_out_asset": None, "quote_out_atomic": None,
            "quote_out_decimals": None, "unit_price": None, "quote_provider": provider,
            "quote_fingerprint": None, "quote_payload_json": None,
            "route_binding_json": None, "costs_json": None, "detail_code": detail[:120],
        }
    content = fingerprint({
        "followup_id": followup_id, "status": status, "observed_at": observed_at, **values,
    })
    with _tx(conn):
        prior = conn.execute(
            "SELECT content_fingerprint FROM v6_paper_exit_followup_results WHERE followup_id=?",
            (followup_id,),
        ).fetchone()
        if prior is not None:
            if prior[0] != content:
                raise ImmutableConflict(f"followup {followup_id} redelivered with new content")
            return False
        conn.execute(
            "INSERT INTO v6_paper_exit_followup_results "
            "(followup_id,status,observed_at,quote_out_asset,quote_out_atomic,"
            "quote_out_decimals,unit_price,quote_provider,quote_fingerprint,quote_payload_json,"
            "route_binding_json,costs_json,detail_code,content_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (followup_id, status, observed_at, values["quote_out_asset"],
             values["quote_out_atomic"], values["quote_out_decimals"], values["unit_price"],
             values["quote_provider"], values["quote_fingerprint"],
             values["quote_payload_json"], values["route_binding_json"], values["costs_json"],
             values["detail_code"], content),
        )
        return True


def claim_retry(conn, intent_token: str, now: str) -> bool:
    """Atomically claim one retry slot and persist the next eligible time."""
    _aware_timestamp(now, "retry_at")
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    with _tx(conn):
        row = conn.execute(
            "SELECT attempt_count, next_attempt_at FROM v6_paper_action_intents "
            "WHERE action_token=? AND status='active'", (intent_token,),
        ).fetchone()
        if row is None:
            return False
        next_at = row["next_attempt_at"]
        if next_at is not None:
            next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
            if next_dt > now_dt:
                return False
        completed = int(row["attempt_count"]) + 1
        next_retry = (now_dt + timedelta(seconds=retry_delay_s(completed))).isoformat()
        updated = conn.execute(
            "UPDATE v6_paper_action_intents SET attempt_count=?, last_attempt_at=?, "
            "next_attempt_at=? WHERE action_token=? AND status='active'",
            (completed, now, next_retry, intent_token),
        ).rowcount
        return updated == 1


def record_intent_unfilled(conn, intent_token: str, reason: str, at: str, *,
                           quote_status: str = "provider_unavailable") -> bool:
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("unfilled reason must not be empty")
    _aware_timestamp(at, "unfilled_at")
    if quote_status not in {"route_absent", "provider_unavailable", "stale"}:
        raise ValueError("unfilled quote_status is invalid")
    with _tx(conn):
        updated = conn.execute(
            "UPDATE v6_paper_action_intents SET reason=?, last_quote_status=? "
            "WHERE action_token=? AND status='active'",
            (reason[:120], quote_status, intent_token),
        ).rowcount
        return updated == 1


def complete_intent_fill(conn, intent_token: str, modeled_fill) -> bool:
    """Apply a fresh modeled fill only while ``intent_token`` is still the active intent."""
    with _tx(conn):
        row = conn.execute(
            "SELECT i.*, p.status AS position_status, p.remaining_qty_atomic, "
            "p.engine_state_json FROM v6_paper_action_intents i "
            "JOIN v6_paper_positions p ON p.position_id=i.position_id "
            "WHERE i.action_token=?", (intent_token,),
        ).fetchone()
        if row is None or row["status"] != "active":
            return False
        if row["position_status"] != "open":
            raise StateConflict(
                f"position {row['position_id']} is {row['position_status']}, not open")
        current = _state_from_json(row["engine_state_json"])
        if current.active_intent is None or current.active_intent.token != intent_token:
            raise StateConflict("active intent row differs from current engine state")
        remaining = int(row["remaining_qty_atomic"])
        requested = min(
            remaining,
            max(1, (remaining * int(row["fraction_ppm"])) // FRACTION_PPM_SCALE),
        )
        filled_at, evidence = _validate_exit_fill(modeled_fill, requested)
        template = _state_from_json(row["state_if_filled_json"])
        after = replace(
            current,
            remaining_qty_atomic=remaining - requested,
            filled_tranches=template.filled_tranches,
            break_state=replace(
                current.break_state,
                tranches=template.break_state.tranches,
                awaiting=template.break_state.awaiting,
            ),
            active_intent=None,
            pending_action_token=None,
            last_action_outcome="filled",
        )
        conn.execute(
            "INSERT INTO v6_paper_fills (position_id, leg, action_token, token_qty_atomic, "
            "quote_in_asset, quote_in_atomic, quote_in_decimals, quote_out_asset, quote_out_atomic, "
            "quote_out_decimals, route_quote_fingerprint, quote_provider, quote_payload_json, "
            "route_binding_json, costs_json, net_pnl_computable, unit_price, quote_age_s, created_at) "
            "VALUES (?, 'action', ?, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["position_id"], intent_token, _atomic_text(requested),
             modeled_fill.get("quote_in_asset"),
             _atomic_text(modeled_fill.get("quote_in_atomic")),
             modeled_fill.get("quote_in_decimals"),
             modeled_fill.get("quote_out_asset"),
             _atomic_text(modeled_fill.get("quote_out_atomic")),
             modeled_fill.get("quote_out_decimals"), modeled_fill.get("quote_fingerprint"),
             modeled_fill.get("quote_provider"), evidence["quote_payload_json"],
             evidence["route_binding_json"], evidence["costs_json"],
             evidence["net_pnl_computable"], modeled_fill.get("unit_price"),
             modeled_fill.get("quote_age_s"), filled_at),
        )
        _schedule_exit_followups_in_tx(
            conn, row["position_id"], intent_token, requested, filled_at)
        new_remaining = remaining - requested
        closing = new_remaining <= DUST_ATOMIC
        terminal = (("exit_full" if row["action"] == V6Action.EXIT_FULL.value
                     else "derisk_closed") if closing else None)
        state_json = _state_to_json(after)
        conn.execute(
            "UPDATE v6_paper_positions SET remaining_qty_atomic=?, engine_state_json=?, "
            "engine_state_fingerprint=?, latest_action=?, status=?, terminal_reason=?, "
            "closed_at=?, updated_at=? WHERE position_id=?",
            (_atomic_text(new_remaining), state_json, fingerprint(state_json), row["action"],
             "closed" if closing else "open", terminal, filled_at if closing else None,
             filled_at, row["position_id"]),
        )
        conn.execute(
            "UPDATE v6_paper_action_intents SET status='filled', last_quote_status='filled', "
            "fill_state_before_hash=?, fill_state_after_hash=?, fill_state_delta_json=?, "
            "resolved_at=? WHERE action_token=? AND status='active'",
            (fingerprint(row["engine_state_json"]), fingerprint(state_json),
             canonical_json(_state_delta(current, after)), filled_at, intent_token),
        )
        conn.execute(
            "UPDATE v6_paper_decisions SET applied_outcome='filled',applied_at=? "
            "WHERE action_token=? AND applied_outcome='unfilled'",
            (filled_at, intent_token),
        )
        _verify_post_fill(conn, row["position_id"], new_remaining)
        return True


def _record_state_transition(conn, stored, before: V6PositionState, after: V6PositionState,
                             outcome: str, at: str) -> None:
    after_json = _state_to_json(after)
    conn.execute(
        "UPDATE v6_paper_decisions SET state_after_hash=?, state_delta_json=?, "
        "pending_state_json=NULL, applied_outcome=?, applied_at=? WHERE action_token=?",
        (fingerprint(after_json), canonical_json(_state_delta(before, after)), outcome, at,
         stored["action_token"]),
    )


def apply_action_fill(conn, decision: V6Decision, modeled_fill) -> V6PaperPosition:
    position_id = decision.state_if_filled.position_id
    token = decision.action_token
    with _tx(conn):
        stored, row = _persisted_decision(conn, decision, position_id)
        if decision.action is V6Action.HOLD:
            raise ValueError("HOLD decisions cannot receive modeled fills")
        if stored["applied_outcome"] is not None:
            if stored["applied_outcome"] == "filled":
                return _load_position(conn, position_id)
            raise ImmutableConflict(
                f"action {token} already applied as {stored['applied_outcome']}")
        intent = conn.execute(
            "SELECT status FROM v6_paper_action_intents WHERE action_token=?", (token,)
        ).fetchone()
        if intent is not None:
            if intent["status"] == "filled":
                return _load_position(conn, position_id)     # idempotent replay
            if intent["status"] != "active":
                raise ImmutableConflict(f"action {token} already resolved as {intent['status']}")
        else:
            raise StateConflict(f"action {token} has no active intent")
        if row["status"] != "open":
            raise StateConflict(f"position {position_id} is {row['status']}, not open")
        if fingerprint(row["engine_state_json"]) != stored["state_before_hash"]:
            raise StateConflict("stale decision: current engine state differs from state_before")
        before_state = _state_from_json(row["engine_state_json"])
        remaining = int(row["remaining_qty_atomic"])
        requested = min(
            remaining, max(1, (remaining * decision.fraction_ppm) // FRACTION_PPM_SCALE))
        if decision.state_if_filled.remaining_qty_atomic != remaining - requested:
            raise StateConflict(
                "stale decision: state_if_filled inconsistent with current inventory")
        filled_at, evidence = _validate_exit_fill(modeled_fill, requested)
        fill_qty = modeled_fill["token_qty_atomic"]
        new_remaining = remaining - requested
        conn.execute(
            "INSERT INTO v6_paper_fills (position_id, leg, action_token, token_qty_atomic, "
            "quote_in_asset, quote_in_atomic, quote_in_decimals, quote_out_asset, quote_out_atomic, "
            "quote_out_decimals, route_quote_fingerprint, quote_provider, quote_payload_json, "
            "route_binding_json, costs_json, net_pnl_computable, unit_price, quote_age_s, created_at) "
            "VALUES (?, 'action', ?, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, token, _atomic_text(fill_qty), modeled_fill.get("quote_in_asset"),
             _atomic_text(modeled_fill.get("quote_in_atomic")),
             modeled_fill.get("quote_in_decimals"), modeled_fill.get("quote_out_asset"),
             _atomic_text(modeled_fill.get("quote_out_atomic")),
             modeled_fill.get("quote_out_decimals"), modeled_fill.get("quote_fingerprint"),
             modeled_fill.get("quote_provider"), evidence["quote_payload_json"],
             evidence["route_binding_json"], evidence["costs_json"],
             evidence["net_pnl_computable"], modeled_fill.get("unit_price"),
             modeled_fill.get("quote_age_s"),
             filled_at))
        _schedule_exit_followups_in_tx(conn, position_id, token, fill_qty, filled_at)
        state_json = _state_to_json(decision.state_if_filled)
        closing = new_remaining <= DUST_ATOMIC
        terminal = ("exit_full" if decision.fraction_ppm >= FRACTION_PPM_SCALE
                    else "derisk_closed") if closing else None
        conn.execute(
            "UPDATE v6_paper_positions SET remaining_qty_atomic=?, engine_state_json=?, "
            "engine_state_fingerprint=?, latest_action=?, status=?, terminal_reason=?, "
            "closed_at=?, updated_at=? "
            "WHERE position_id=?",
            (_atomic_text(new_remaining), state_json, fingerprint(state_json), decision.action.value,
             "closed" if closing else "open", terminal, filled_at if closing else None,
             filled_at, position_id))
        conn.execute(
            "UPDATE v6_paper_action_intents SET status='filled', resolved_at=? "
            "WHERE action_token=?", (filled_at, token))
        _record_state_transition(
            conn, stored, before_state, decision.state_if_filled, "filled", filled_at)
        _verify_post_fill(conn, position_id, new_remaining)
    return _load_position(conn, position_id)


def record_action_unfilled(conn, decision: V6Decision, reason: str, at: str) -> V6PaperPosition:
    position_id = decision.state_if_unfilled.position_id
    token = decision.action_token
    if not str(reason or "").strip():
        raise ValueError("unfilled reason must not be empty")
    _aware_timestamp(at, "resolved_at")
    with _tx(conn):
        stored, row = _persisted_decision(conn, decision, position_id)
        if decision.action is V6Action.HOLD:
            raise ValueError("HOLD decisions cannot be recorded as unfilled")
        if stored["applied_outcome"] is not None:
            if stored["applied_outcome"] == "unfilled":
                intent = conn.execute(
                    "SELECT reason FROM v6_paper_action_intents WHERE action_token=?", (token,)
                ).fetchone()
                if intent is not None and intent["reason"] == reason:
                    return _load_position(conn, position_id)
            raise ImmutableConflict(
                f"action {token} already applied as {stored['applied_outcome']}")
        intent = conn.execute(
            "SELECT status, reason FROM v6_paper_action_intents WHERE action_token=?", (token,)
        ).fetchone()
        if intent is None:
            raise StateConflict(f"action {token} has no active intent")
        if intent["status"] != "active":
            raise ImmutableConflict(f"action {token} already resolved as {intent['status']}")
        if row["status"] != "open":
            raise StateConflict(f"position {position_id} is {row['status']}, not open")
        if fingerprint(row["engine_state_json"]) != stored["state_before_hash"]:
            raise StateConflict("stale decision: current engine state differs from state_before")
        before_state = _state_from_json(row["engine_state_json"])
        # An unfilled action changes NO accounting column — only engine state + the pending token.
        state_json = _state_to_json(decision.state_if_unfilled)
        conn.execute(
            "UPDATE v6_paper_action_intents SET reason=?, last_attempt_at=?, attempt_count="
            "attempt_count+1 WHERE action_token=? AND status='active'", (reason, at, token))
        conn.execute(
            "UPDATE v6_paper_positions SET engine_state_json=?, engine_state_fingerprint=?, "
            "latest_action=?, updated_at=? WHERE position_id=?",
            (state_json, fingerprint(state_json), decision.action.value, at, position_id))
        _record_state_transition(
            conn, stored, before_state, decision.state_if_unfilled, "unfilled", at)
    return _load_position(conn, position_id)


def _apply_hold_in_tx(conn, position_id, decision: V6Decision, stored, row, at: str) -> None:
    if decision.action is not V6Action.HOLD:
        raise ValueError("apply_hold_decision requires a HOLD decision")
    if stored["applied_outcome"] is not None:
        if stored["applied_outcome"] == "held":
            return
        raise ImmutableConflict(
            f"decision {decision.action_token} already applied as {stored['applied_outcome']}")
    if row["status"] != "open":
        raise StateConflict(f"position {position_id} is {row['status']}, not open")
    if fingerprint(row["engine_state_json"]) != stored["state_before_hash"]:
        raise StateConflict("stale HOLD: current engine state differs from state_before")
    before_state = _state_from_json(row["engine_state_json"])
    state_json = _state_to_json(decision.state_if_filled)
    conn.execute(
        "UPDATE v6_paper_positions SET engine_state_json=?, engine_state_fingerprint=?, "
        "latest_action='HOLD', updated_at=? WHERE position_id=?",
        (state_json, fingerprint(state_json), at, position_id))
    _record_state_transition(conn, stored, before_state, decision.state_if_filled, "held", at)


def apply_hold_decision(conn, decision: V6Decision, *, at: str) -> V6PaperPosition:
    """Apply the causal state successor of one persisted HOLD. No intent or fill is created."""
    position_id = decision.state_if_filled.position_id
    _aware_timestamp(at, "applied_at")
    with _tx(conn):
        stored, row = _persisted_decision(conn, decision, position_id)
        _apply_hold_in_tx(conn, position_id, decision, stored, row, at)
    return _load_position(conn, position_id)


def censor_position(conn, position_id, *, reason: str, at: str) -> V6PaperPosition:
    """Operator termination of a broken experiment position. Writes no fill and marks the row
    ``censored_operator`` so it is excluded from engine outcomes."""
    reason = str(reason or "").strip()
    if len(reason) < 8:
        raise ValueError("censor reason must be at least 8 characters")
    _aware_timestamp(at, "censored_at")
    with _tx(conn):
        row = conn.execute(
            "SELECT status, censor_reason FROM v6_paper_positions WHERE position_id=?",
                           (position_id,)).fetchone()
        if row is None:
            raise KeyError(f"no v6 paper position: {position_id}")
        if row["status"] == "censored_operator" and row["censor_reason"] == reason:
            return _load_position(conn, position_id)
        if row["status"] != "open":
            raise StateConflict(f"position {position_id} is {row['status']}, not open")
        conn.execute(
            "UPDATE v6_paper_positions SET status='censored_operator', censor_reason=?, "
            "censored_at=?, updated_at=? WHERE position_id=?", (reason, at, at, position_id))
    return _load_position(conn, position_id)


def censor_measurement_position(
    conn, position_id, *, status: str, reason: str, at: str
) -> V6PaperPosition:
    """Close an unfaithful measurement without pretending an operator intervened."""
    if status not in {"censored_migration", "censored_handoff_stale"}:
        raise ValueError(f"unsupported measurement censor status: {status}")
    reason = str(reason or "").strip()
    if len(reason) < 8:
        raise ValueError("censor reason must be at least 8 characters")
    _aware_timestamp(at, "censored_at")
    with _tx(conn):
        row = conn.execute(
            "SELECT status,censor_reason FROM v6_paper_positions WHERE position_id=?",
            (position_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no v6 paper position: {position_id}")
        if row["status"] == status and row["censor_reason"] == reason:
            return _load_position(conn, position_id)
        if row["status"] != "open":
            raise StateConflict(f"position {position_id} is {row['status']}, not open")
        conn.execute(
            "UPDATE v6_paper_positions SET status=?, censor_reason=?, censored_at=?, updated_at=? "
            "WHERE position_id=?", (status, reason, at, at, position_id))
    return _load_position(conn, position_id)


# ---- reads / reconstruction -------------------------------------------------------------------

def fill_count(conn, action_token) -> int:
    return conn.execute("SELECT COUNT(*) FROM v6_paper_fills WHERE action_token=?",
                       (action_token,)).fetchone()[0]


def open_positions(conn) -> list[V6PaperPosition]:
    rows = conn.execute("SELECT * FROM v6_paper_positions WHERE status='open' "
                        "ORDER BY position_id").fetchall()
    return [_row_to_position(conn, r) for r in rows]


def unapplied_decisions(conn) -> list[UnappliedV6Decision]:
    rows = conn.execute(
        "SELECT d.* FROM v6_paper_decisions d "
        "JOIN v6_paper_positions p ON p.position_id=d.position_id "
        "WHERE d.applied_outcome IS NULL AND p.status='open' "
        "ORDER BY d.decided_at, d.action_token").fetchall()
    unapplied = []
    for row in rows:
        intent = None
        if row["action"] != V6Action.HOLD.value:
            intent = conn.execute(
                "SELECT * FROM v6_paper_action_intents WHERE action_token=?",
                (row["action_token"],),
            ).fetchone()
        unapplied.append(UnappliedV6Decision(
            position_id=row["position_id"],
            decision=_row_to_decision(row, intent),
            decided_at=row["decided_at"],
        ))
    return unapplied


def reconstruct_position(conn, position_id) -> V6PaperPosition:
    """Rebuild inventory and engine state from immutable entry, decision, and fill rows."""
    row = conn.execute("SELECT * FROM v6_paper_positions WHERE position_id=?",
                       (position_id,)).fetchone()
    if row is None:
        raise KeyError(f"no v6 paper position: {position_id}")
    entry = conn.execute(
        "SELECT token_qty_atomic, entry_price, quote_in_asset, fill_ts "
        "FROM v6_paper_entries WHERE entry_id=?", (row["entry_id"],)).fetchone()
    entry_qty = int(entry["token_qty_atomic"])
    exited = _sum_action_fill_qty(conn, position_id)
    remaining = entry_qty - exited
    if remaining != int(row["remaining_qty_atomic"]):
        raise StateConflict(
            f"reconstruction mismatch for {position_id}: fills give {remaining}, "
            f"row says {row['remaining_qty_atomic']}")
    reconstructed_state = V6PositionState(
        position_id=position_id, remaining_qty_atomic=int(entry_qty),
        entry_unit_price=str(entry["entry_price"]), entry_quote_asset=entry["quote_in_asset"],
        opened_at=entry["fill_ts"], break_state=V6State())
    decision_rows = conn.execute(
        "SELECT rowid AS transition_rowid, * FROM v6_paper_decisions WHERE position_id=?",
        (position_id,),
    ).fetchall()
    transitions = []
    unapplied = []
    for decision_row in decision_rows:
        outcome = decision_row["applied_outcome"]
        fill_count_for_action = conn.execute(
            "SELECT COUNT(*) FROM v6_paper_fills WHERE action_token=?",
            (decision_row["action_token"],)).fetchone()[0]
        if outcome is None:
            if fill_count_for_action:
                raise StateConflict("unapplied decision has a modeled fill")
            unapplied.append(decision_row)
            continue
        if outcome not in ("held", "filled", "unfilled"):
            raise StateConflict(f"unknown applied decision outcome: {outcome!r}")
        allowed_fill_counts = ({1} if outcome == "filled" else
                               {0, 1} if outcome == "unfilled" else {0})
        if fill_count_for_action not in allowed_fill_counts:
            raise StateConflict(f"{outcome} decision has {fill_count_for_action} modeled fills")
        delta_json = decision_row["state_delta_json"]
        after_hash = decision_row["state_after_hash"]
        if delta_json is None or after_hash is None:
            raise StateConflict(
                f"decision {decision_row['action_token']} has no applied state transition")
        transitions.append((
            decision_row["state_before_hash"], after_hash, delta_json,
            f"decision {decision_row['action_token']}",
        ))
    intent_fills = conn.execute(
        "SELECT action_token, fill_state_before_hash, fill_state_after_hash, "
        "fill_state_delta_json FROM v6_paper_action_intents "
        "WHERE position_id=? AND status='filled'", (position_id,),
    ).fetchall()
    for intent_fill in intent_fills:
        if any(intent_fill[name] is None for name in (
                "fill_state_before_hash", "fill_state_after_hash", "fill_state_delta_json")):
            # Compatibility: old immediate fills are represented by their decision transition.
            decision = conn.execute(
                "SELECT applied_outcome FROM v6_paper_decisions WHERE action_token=?",
                (intent_fill["action_token"],),
            ).fetchone()
            if decision is None or decision["applied_outcome"] != "filled":
                raise StateConflict("filled intent has no reconstructable state transition")
            continue
        transitions.append((
            intent_fill["fill_state_before_hash"], intent_fill["fill_state_after_hash"],
            intent_fill["fill_state_delta_json"],
            f"intent fill {intent_fill['action_token']}",
        ))
    pending = list(transitions)
    while pending:
        before_hash = fingerprint(_state_to_json(reconstructed_state))
        candidates = [transition for transition in pending if transition[0] == before_hash]
        if len(candidates) != 1:
            raise StateConflict(
                f"state transition chain has {len(candidates)} successors for {before_hash}")
        transition = candidates[0]
        reconstructed_state = _apply_state_delta(reconstructed_state, transition[2])
        if fingerprint(_state_to_json(reconstructed_state)) != transition[1]:
            raise StateConflict(f"{transition[3]} state delta breaks reconstruction")
        pending.remove(transition)
    final_hash = fingerprint(_state_to_json(reconstructed_state))
    for decision_row in unapplied:
        if decision_row["state_before_hash"] != final_hash:
            raise StateConflict(
                f"unapplied decision {decision_row['action_token']} is stale")
    if reconstructed_state.remaining_qty_atomic != remaining:
        raise StateConflict(
            "reconstructed engine state inventory differs from reconstructed fills")
    reconstructed_json = _state_to_json(reconstructed_state)
    if row["engine_state_fingerprint"] != fingerprint(row["engine_state_json"]):
        raise StateConflict("mutable engine state fingerprint does not match engine state JSON")
    if row["engine_state_json"] != reconstructed_json:
        raise StateConflict("mutable engine state differs from decision reconstruction")
    return V6PaperPosition(
        entry_id=row["entry_id"], network_id=row["network_id"], mint=row["mint"],
        status=row["status"], entry_qty_atomic=entry_qty, remaining_qty_atomic=remaining,
        engine_state=reconstructed_state,
        terminal_reason=row["terminal_reason"], exited_qty_atomic=int(exited))


def position_timeline(conn, position_id) -> dict:
    obs = [dict(r) for r in conn.execute(
        "SELECT * FROM v6_paper_observations WHERE position_id=? ORDER BY observed_at, "
        "observation_id", (position_id,)).fetchall()]
    decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM v6_paper_decisions WHERE position_id=? ORDER BY rowid",
        (position_id,)).fetchall()]
    fills = [dict(r) for r in conn.execute(
        "SELECT * FROM v6_paper_fills WHERE position_id=? ORDER BY fill_id",
        (position_id,)).fetchall()]
    return {"observations": obs, "decisions": decisions, "fills": fills}


# ---- cursors ----------------------------------------------------------------------------------

def set_cursor(conn, name: str, value: str, at: str):
    with _tx(conn):
        conn.execute(
            "INSERT INTO v6_paper_cursors (name, value, corrected_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, corrected_at=excluded.corrected_at",
            (name, value, at))


def get_cursor(conn, name: str):
    row = conn.execute("SELECT value FROM v6_paper_cursors WHERE name=?", (name,)).fetchone()
    return row[0] if row is not None else None
