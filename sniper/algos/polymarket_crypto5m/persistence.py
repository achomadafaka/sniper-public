"""Dedicated SQLite persistence for the crypto-5m capture spine.

A *separate* DB namespace (configurable path) — never the wallet-copy ``paper_trades`` DB and never a
production DB. Idempotent upserts / append-only event keys. Every time value is a caller-supplied
server-time integer (unix seconds/ms), never the local +7h-skewed clock. Raw payloads + code-version
lineage are stored so captures are auditable and replayable.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .models import CaptureGap, ChainlinkTick, MarketSpec, ReferenceObservation, Settlement

SCHEMA_VERSION = 1

_TABLES = (
    "crypto5m_markets", "crypto5m_market_rules", "crypto5m_book_events", "crypto5m_book_snapshots",
    "crypto5m_chainlink_ticks", "crypto5m_price_to_beat", "crypto5m_reference_observations",
    "crypto5m_settlements", "crypto5m_capture_gaps", "crypto5m_identity_changes",
    "crypto5m_book_reconciliation", "crypto5m_clock", "crypto5m_binance_observations",
)


def connect(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the capture DB (WAL). Pass ``check_same_thread=False`` for the multi-threaded capture
    loop — callers MUST serialize writes with a lock in that case (the CLI does)."""
    import os
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto5m_markets (
            condition_id TEXT PRIMARY KEY,
            event_id TEXT, market_id TEXT, slug TEXT, question TEXT, asset TEXT,
            series_id TEXT,
            window_start_ts INTEGER, window_end_ts INTEGER,
            up_token_id TEXT, down_token_id TEXT, outcomes TEXT,
            price_to_beat REAL,
            fees_enabled INTEGER, fee_type TEXT, taker_base_fee REAL, maker_base_fee REAL,
            tick_size REAL, min_order_size REAL,
            resolution_source TEXT, rule_text_hash TEXT, market_created_ts TEXT,
            active INTEGER, closed INTEGER, accepting_orders INTEGER, enable_order_book INTEGER,
            raw_payload TEXT, code_version TEXT, source_lineage TEXT,
            first_seen_ts INTEGER, last_updated_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_market_rules (
            condition_id TEXT, rule_text_hash TEXT, raw_rule_text TEXT, resolution_source TEXT,
            verdict_reason TEXT, rule_supported INTEGER, rule_contract_version INTEGER,
            recorded_ts INTEGER,
            PRIMARY KEY (condition_id, rule_text_hash)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_book_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT UNIQUE,
            condition_id TEXT, token_id TEXT, side TEXT, event_type TEXT,
            exchange_ts INTEGER, recv_ts INTEGER, persist_ts INTEGER,
            reconnect_generation INTEGER, quality_flags TEXT, raw_payload TEXT
        );
        CREATE TABLE IF NOT EXISTS crypto5m_book_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, token_id TEXT, side TEXT,
            bids TEXT, asks TEXT, best_bid REAL, best_ask REAL, spread REAL,
            book_hash TEXT, tick_size REAL, min_order_size REAL,
            exchange_ts INTEGER, recv_ts INTEGER, persist_ts INTEGER,
            source_event TEXT, quality_flags TEXT, reconnect_generation INTEGER, code_version TEXT
        );
        CREATE TABLE IF NOT EXISTS crypto5m_chainlink_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT UNIQUE,
            symbol TEXT, source_ts_ms INTEGER, server_ts_ms INTEGER, recv_ts INTEGER,
            value REAL, full_accuracy_value TEXT, is_snapshot INTEGER,
            window_start_ts INTEGER, raw_payload TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_price_to_beat (
            condition_id TEXT, candidate_rule TEXT,
            value TEXT, source_ts_ms INTEGER, recv_ts INTEGER, known_latency_ms INTEGER,
            quality TEXT, gamma_oracle REAL, abs_error TEXT, match INTEGER,
            code_version TEXT, persist_ts INTEGER,
            PRIMARY KEY (condition_id, candidate_rule)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_reference_observations (
            condition_id TEXT, source TEXT, checkpoint TEXT,
            observed_ts INTEGER, price REAL, code_version TEXT, persist_ts INTEGER,
            PRIMARY KEY (condition_id, source, checkpoint)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_settlements (
            condition_id TEXT PRIMARY KEY,
            winning_token_id TEXT, winning_outcome TEXT, resolution_ts TEXT,
            uma_resolution_status TEXT, outcome_prices TEXT, source_used TEXT,
            reconciliation_status TEXT, gamma_price_to_beat REAL,
            code_version TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_capture_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, window_start_ts INTEGER, gap_kind TEXT, detected_ts INTEGER,
            recovery_action TEXT, reconnect_generation INTEGER, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_identity_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, field TEXT, old_value TEXT, new_value TEXT, detected_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_book_reconciliation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, token_id TEXT, book_state_match INTEGER, book_divergence_levels INTEGER,
            book_divergence_duration_ms INTEGER, rest_reconciliation_ts INTEGER,
            quarantined_interval INTEGER, code_version TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_clock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clock_source TEXT, clock_offset_ms INTEGER, clock_last_verified_ts INTEGER,
            delivery_mode TEXT, staleness_ms INTEGER,
            persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_binance_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT UNIQUE,
            symbol TEXT, source_event_ts INTEGER, receive_ts INTEGER,
            price REAL, quantity REAL, quote_quantity REAL, trade_count INTEGER,
            delivery_source TEXT, raw_payload TEXT, persist_ts INTEGER
        );
        -- hot-path indexes for high-volume capture + per-window queries
        CREATE INDEX IF NOT EXISTS idx_c5m_book_events_cid_ts
            ON crypto5m_book_events (condition_id, exchange_ts);
        CREATE INDEX IF NOT EXISTS idx_c5m_book_events_token
            ON crypto5m_book_events (token_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_c5m_book_snapshots_token
            ON crypto5m_book_snapshots (condition_id, token_id);
        CREATE INDEX IF NOT EXISTS idx_c5m_chainlink_window
            ON crypto5m_chainlink_ticks (window_start_ts);
        """
    )
    # Additive migration for DBs created before per-window RTDS delivery/staleness recording.
    _clock_cols = {r[1] for r in conn.execute("PRAGMA table_info(crypto5m_clock)").fetchall()}
    for _name, _decl in (("delivery_mode", "TEXT"), ("staleness_ms", "INTEGER")):
        if _name not in _clock_cols:
            conn.execute(f"ALTER TABLE crypto5m_clock ADD COLUMN {_name} {_decl}")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _flags_json(flags) -> str:
    return json.dumps([getattr(f, "value", f) for f in (flags or [])])


# --- markets / rules -------------------------------------------------------------------------

def upsert_market(conn, spec: MarketSpec, *, code_version: str, source_lineage: str,
                  now_ts: int) -> list:
    """Insert or operationally-update a market. Immutable identity fields are NEVER overwritten on
    conflict; an identity change is recorded to ``crypto5m_identity_changes`` and returned as
    ``[(field, old, new), ...]`` (empty when new or unchanged). Only operational fields update.
    """
    from . import identity as _identity
    existing = get_market(conn, spec.condition_id)
    changes = _identity.diff_identity(existing, spec) if existing else []
    for field, old, new in changes:
        record_identity_change(conn, condition_id=spec.condition_id, field=field,
                               old_value=old, new_value=new, now_ts=now_ts)

    conn.execute(
        """
        INSERT INTO crypto5m_markets (
            condition_id, event_id, market_id, slug, question, asset, series_id,
            window_start_ts, window_end_ts, up_token_id, down_token_id, outcomes, price_to_beat,
            fees_enabled, fee_type, taker_base_fee, maker_base_fee, tick_size, min_order_size,
            resolution_source, rule_text_hash, market_created_ts,
            active, closed, accepting_orders, enable_order_book,
            raw_payload, code_version, source_lineage, first_seen_ts, last_updated_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(condition_id) DO UPDATE SET
            question=excluded.question,
            price_to_beat=COALESCE(excluded.price_to_beat, price_to_beat),
            fees_enabled=excluded.fees_enabled, fee_type=excluded.fee_type,
            taker_base_fee=excluded.taker_base_fee, maker_base_fee=excluded.maker_base_fee,
            tick_size=excluded.tick_size, min_order_size=excluded.min_order_size,
            active=excluded.active, closed=excluded.closed,
            accepting_orders=excluded.accepting_orders, enable_order_book=excluded.enable_order_book,
            raw_payload=excluded.raw_payload, code_version=excluded.code_version,
            last_updated_ts=excluded.last_updated_ts
        """,
        (spec.condition_id, spec.event_id, spec.market_id, spec.slug, spec.question, spec.asset,
         spec.series_id, spec.window_start_ts, spec.window_end_ts, spec.up_token_id,
         spec.down_token_id, json.dumps(list(spec.outcomes)), spec.price_to_beat,
         int(spec.fees_enabled), spec.fee_type, spec.taker_base_fee, spec.maker_base_fee,
         spec.tick_size, spec.min_order_size, spec.resolution_source, spec.rule_text_hash,
         spec.market_created_ts, int(spec.active), int(spec.closed), int(spec.accepting_orders),
         int(spec.enable_order_book), json.dumps(spec.raw_payload), code_version, source_lineage,
         now_ts, now_ts),
    )
    conn.commit()
    return changes


def record_identity_change(conn, *, condition_id, field, old_value, new_value, now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_identity_changes
           (condition_id, field, old_value, new_value, detected_ts) VALUES (?,?,?,?,?)""",
        (condition_id, field, str(old_value), str(new_value), now_ts),
    )
    conn.commit()


def get_market(conn, condition_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM crypto5m_markets WHERE condition_id=?",
                       (condition_id,)).fetchone()
    return dict(row) if row else None


def record_rule(conn, *, condition_id, rule_text, rule_text_hash, resolution_source, verdict,
                now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_market_rules
           (condition_id, rule_text_hash, raw_rule_text, resolution_source, verdict_reason,
            rule_supported, rule_contract_version, recorded_ts)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(condition_id, rule_text_hash) DO NOTHING""",
        (condition_id, rule_text_hash, rule_text, resolution_source,
         verdict.reason.value, int(verdict.rule_supported), verdict.rule_contract_version, now_ts),
    )
    conn.commit()


# --- book events / snapshots -----------------------------------------------------------------

def append_book_event(conn, *, condition_id, token_id, side, event_type, exchange_ts, recv_ts,
                      reconnect_generation, quality_flags, raw_payload, now_ts,
                      commit: bool = True) -> bool:
    import hashlib
    payload_json = json.dumps(raw_payload, sort_keys=True, default=str)
    # idempotency keys on the EXCHANGE event identity (+ reconnect generation), NOT local recv_ts —
    # replaying the same exchange event with a new receive time must not insert a duplicate.
    event_key = hashlib.sha256(
        f"{token_id}|{event_type}|{exchange_ts}|{reconnect_generation}|{payload_json}".encode()
    ).hexdigest()
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto5m_book_events
           (event_key, condition_id, token_id, side, event_type, exchange_ts, recv_ts, persist_ts,
            reconnect_generation, quality_flags, raw_payload)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (event_key, condition_id, token_id, side, event_type, exchange_ts, recv_ts, now_ts,
         reconnect_generation, _flags_json(quality_flags), payload_json),
    )
    if commit:  # pass commit=False to batch high-volume inserts; caller commits once
        conn.commit()
    return cur.rowcount > 0


def write_snapshot(conn, *, condition_id, token_id, side, bids, asks, best_bid, best_ask, spread,
                   book_hash, tick_size, min_order_size, exchange_ts, recv_ts, source_event,
                   quality_flags, reconnect_generation, code_version, now_ts) -> int:
    cur = conn.execute(
        """INSERT INTO crypto5m_book_snapshots
           (condition_id, token_id, side, bids, asks, best_bid, best_ask, spread, book_hash,
            tick_size, min_order_size, exchange_ts, recv_ts, persist_ts, source_event,
            quality_flags, reconnect_generation, code_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (condition_id, token_id, side, json.dumps(bids), json.dumps(asks), best_bid, best_ask,
         spread, book_hash, tick_size, min_order_size, exchange_ts, recv_ts, now_ts, source_event,
         _flags_json(quality_flags), reconnect_generation, code_version),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- chainlink ticks / price-to-beat ---------------------------------------------------------

def append_chainlink_tick(conn, tick: ChainlinkTick, *, window_start_ts=None,
                          raw_payload=None) -> bool:
    dedup_key = f"{tick.symbol}|{tick.source_ts_ms}|{tick.full_accuracy_value or tick.value}|{int(tick.is_snapshot)}"
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto5m_chainlink_ticks
           (dedup_key, symbol, source_ts_ms, server_ts_ms, recv_ts, value, full_accuracy_value,
            is_snapshot, window_start_ts, raw_payload, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (dedup_key, tick.symbol, tick.source_ts_ms, tick.server_ts_ms, tick.recv_ts, tick.value,
         tick.full_accuracy_value, int(tick.is_snapshot), window_start_ts,
         json.dumps(raw_payload) if raw_payload is not None else None, tick.recv_ts),
    )
    conn.commit()
    return cur.rowcount > 0


def record_price_to_beat(conn, *, condition_id, candidate_rule, value, source_ts_ms, recv_ts,
                         known_latency_ms, quality, gamma_oracle, abs_error, match, code_version,
                         now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_price_to_beat
           (condition_id, candidate_rule, value, source_ts_ms, recv_ts, known_latency_ms, quality,
            gamma_oracle, abs_error, match, code_version, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(condition_id, candidate_rule) DO UPDATE SET
            value=excluded.value, source_ts_ms=excluded.source_ts_ms, recv_ts=excluded.recv_ts,
            known_latency_ms=excluded.known_latency_ms, quality=excluded.quality,
            gamma_oracle=excluded.gamma_oracle, abs_error=excluded.abs_error, match=excluded.match,
            code_version=excluded.code_version, persist_ts=excluded.persist_ts""",
        (condition_id, candidate_rule, str(value) if value is not None else None, source_ts_ms,
         recv_ts, known_latency_ms, getattr(quality, "value", quality), gamma_oracle,
         str(abs_error) if abs_error is not None else None,
         None if match is None else int(match), code_version, now_ts),
    )
    conn.commit()


# --- settlement / reference / gaps -----------------------------------------------------------

def record_settlement(conn, s: Settlement, *, code_version, now_ts) -> None:
    # Label integrity: terminal states are sticky. A conflict is never auto-overwritten; a resolved
    # winner is never downgraded to unresolved; two DIFFERENT winners quarantine to conflict. Only a
    # first write, a same-winner refresh, or an upgrade from unresolved proceeds to the upsert.
    existing = conn.execute(
        "SELECT winning_outcome, reconciliation_status FROM crypto5m_settlements WHERE condition_id=?",
        (s.condition_id,)).fetchone()
    if existing is not None:
        ex_winner, ex_status = existing["winning_outcome"], existing["reconciliation_status"]
        if ex_status == "conflict":
            return
        if ex_winner is not None:
            if s.winning_outcome is None:
                return
            if s.winning_outcome != ex_winner:
                conn.execute(
                    "UPDATE crypto5m_settlements SET winning_token_id=NULL, winning_outcome=NULL, "
                    "reconciliation_status='conflict', persist_ts=? WHERE condition_id=?",
                    (now_ts, s.condition_id))
                conn.commit()
                return
    conn.execute(
        """INSERT INTO crypto5m_settlements
           (condition_id, winning_token_id, winning_outcome, resolution_ts, uma_resolution_status,
            outcome_prices, source_used, reconciliation_status, gamma_price_to_beat,
            code_version, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(condition_id) DO UPDATE SET
            winning_token_id=excluded.winning_token_id, winning_outcome=excluded.winning_outcome,
            resolution_ts=excluded.resolution_ts, uma_resolution_status=excluded.uma_resolution_status,
            outcome_prices=excluded.outcome_prices, source_used=excluded.source_used,
            reconciliation_status=excluded.reconciliation_status,
            gamma_price_to_beat=excluded.gamma_price_to_beat, persist_ts=excluded.persist_ts""",
        (s.condition_id, s.winning_token_id, s.winning_outcome, s.resolution_ts,
         s.uma_resolution_status, json.dumps(list(s.outcome_prices)), s.source_used,
         s.reconciliation_status, s.gamma_price_to_beat, code_version, now_ts),
    )
    conn.commit()


def record_reference_observation(conn, obs: ReferenceObservation, *, code_version, now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_reference_observations
           (condition_id, source, checkpoint, observed_ts, price, code_version, persist_ts)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(condition_id, source, checkpoint) DO UPDATE SET
            observed_ts=excluded.observed_ts, price=excluded.price, persist_ts=excluded.persist_ts""",
        (obs.condition_id, obs.source, obs.checkpoint, obs.observed_ts, obs.price, code_version,
         now_ts),
    )
    conn.commit()


def record_gap(conn, g: CaptureGap, *, now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_capture_gaps
           (condition_id, window_start_ts, gap_kind, detected_ts, recovery_action,
            reconnect_generation, persist_ts)
           VALUES (?,?,?,?,?,?,?)""",
        (g.condition_id, g.window_start_ts, g.gap_kind, g.detected_ts, g.recovery_action,
         g.reconnect_generation, now_ts),
    )
    conn.commit()


def record_book_reconciliation(conn, *, condition_id, token_id, book_state_match,
                               book_divergence_levels, book_divergence_duration_ms,
                               rest_reconciliation_ts, quarantined_interval, code_version,
                               now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_book_reconciliation
           (condition_id, token_id, book_state_match, book_divergence_levels,
            book_divergence_duration_ms, rest_reconciliation_ts, quarantined_interval,
            code_version, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (condition_id, token_id, int(bool(book_state_match)), book_divergence_levels,
         book_divergence_duration_ms, rest_reconciliation_ts, int(bool(quarantined_interval)),
         code_version, now_ts),
    )
    conn.commit()


def record_clock(conn, *, clock_source, clock_offset_ms, clock_last_verified_ts, now_ts,
                 delivery_mode=None, staleness_ms=None) -> None:
    conn.execute(
        """INSERT INTO crypto5m_clock
           (clock_source, clock_offset_ms, clock_last_verified_ts, delivery_mode, staleness_ms,
            persist_ts) VALUES (?,?,?,?,?,?)""",
        (getattr(clock_source, "value", clock_source), clock_offset_ms, clock_last_verified_ts,
         getattr(delivery_mode, "value", delivery_mode), staleness_ms, now_ts),
    )
    conn.commit()


def record_binance_observation(conn, obs, *, now_ts) -> None:
    """Persist a BinanceObservation (predictor lane). Idempotent on dedup_key
    (symbol|source_event_ts|delivery_source) so a replayed/duplicate frame can't double-count."""
    conn.execute(
        """INSERT OR IGNORE INTO crypto5m_binance_observations
           (dedup_key, symbol, source_event_ts, receive_ts, price, quantity, quote_quantity,
            trade_count, delivery_source, raw_payload, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (obs.dedup_key, obs.symbol, obs.source_event_ts, obs.receive_ts, obs.price, obs.quantity,
         obs.quote_quantity, obs.trade_count, obs.delivery_source, json.dumps(obs.raw), now_ts),
    )
    conn.commit()


def counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLES}


def db_size_bytes(conn) -> int:
    """Approximate DB size from SQLite page accounting. Checkpoints WAL first so uncommitted-to-main
    pages in the -wal file are counted (otherwise size under-reports during active capture)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:  # noqa: BLE001 - checkpoint is best-effort for a size estimate
        pass
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(page_count) * int(page_size)
