"""Phase 1A shadow-simulator persistence (dedicated crypto5m DB; idempotent; append-only decisions).

Adds the 9 sim tables alongside the Phase 0/0.5 capture tables (same DB, same configurable path via
``persistence.connect``). Decision inputs are immutable; feature/prediction/decision rows are
idempotent on a content key; skips/fees/settlements/eval runs are append-only. Supersede rather than
silently overwrite (a new row + a ``superseded`` marker, never an in-place identity change).
"""
from __future__ import annotations

import json

from . import fee_model as _fm


def _fe_int(v):
    """fees_enabled -> 0/1/None, treating the string "false" as False (not truthy)."""
    b = _fm.normalize_bool(v)
    return int(b) if b is not None else None

SIM_SCHEMA_VERSION = 1

SIM_TABLES = (
    "crypto5m_feature_snapshots", "crypto5m_fee_configs", "crypto5m_model_predictions",
    "crypto5m_shadow_decisions", "crypto5m_paper_entries", "crypto5m_paper_fills",
    "crypto5m_paper_skips", "crypto5m_paper_settlements", "crypto5m_evaluation_runs",
    "crypto5m_feature_registry",
)


def ensure_sim_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto5m_feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT UNIQUE,
            condition_id TEXT, market_id TEXT, window_start_ts INTEGER, window_end_ts INTEGER,
            checkpoint INTEGER, decision_ts INTEGER, feature_cutoff_ts INTEGER,
            feature_version TEXT, feature_status TEXT,
            price_to_beat REAL, price_to_beat_quality TEXT, price_to_beat_known_ts INTEGER,
            book_integrity_status TEXT, market_rule_hash TEXT, code_commit TEXT,
            receive_time_missing INTEGER,
            features_json TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_fee_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, fetched_ts INTEGER, fees_enabled INTEGER, taker_base_fee REAL,
            fee_rate REAL, fee_status TEXT, observed_fee_rate_bps REAL, decoder_version TEXT,
            source TEXT, raw_payload TEXT, superseded INTEGER DEFAULT 0, persist_ts INTEGER,
            fee_source TEXT, fee_exponent REAL, taker_only INTEGER, rounding_precision INTEGER,
            raw_source_payloads TEXT
        );
        CREATE TABLE IF NOT EXISTS crypto5m_model_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_key TEXT UNIQUE,
            feature_snapshot_id INTEGER, condition_id TEXT, checkpoint INTEGER,
            model_id TEXT, model_version TEXT, calibrated INTEGER, q_up REAL, q_down REAL,
            prediction_status TEXT, reason TEXT, training_cutoff_ts INTEGER, feature_version TEXT,
            persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_shadow_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_key TEXT UNIQUE,
            condition_id TEXT, token_id TEXT, window_start_ts INTEGER, checkpoint INTEGER,
            decision_ts INTEGER, feature_snapshot_id INTEGER, prediction_id INTEGER,
            book_hash TEXT, fee_config_id INTEGER, price_to_beat_quality TEXT,
            model_id TEXT, model_version TEXT, strategy_version TEXT,
            status TEXT, reason TEXT, chosen_side TEXT,
            expected_edge_per_share REAL, expected_net_pnl REAL, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_paper_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER, condition_id TEXT, token_id TEXT, side TEXT,
            shares REAL, total_cash_cost REAL, fee_inclusive_average_cost REAL,
            entry_ts INTEGER, status TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_paper_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER, fill_json TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_paper_skips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skip_key TEXT,
            condition_id TEXT, checkpoint INTEGER, decision_ts INTEGER,
            reason TEXT, detail TEXT, strategy_version TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_paper_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER, condition_id TEXT, winning_outcome TEXT, realized_pnl REAL,
            reconciliation_status TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_evaluation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts INTEGER, model_id TEXT, params_json TEXT, summary_json TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_feature_registry (
            feature_name TEXT, feature_family TEXT, feature_version TEXT,
            required_sources TEXT, minimum_history TEXT, time_scope TEXT,
            null_reason_vocab TEXT, decision_safe INTEGER,
            PRIMARY KEY (feature_name, feature_version)
        );
        """
    )
    # Additive migration for DBs created before Phase 1B fee reconciliation.
    _ensure_columns(conn, "crypto5m_fee_configs", _FEE_RECON_COLUMNS)
    # Skip idempotency: key new skip rows so a replay can't duplicate them (NULL skip_key on legacy
    # rows is fine — SQLite UNIQUE permits multiple NULLs, so old rows coexist).
    _ensure_columns(conn, "crypto5m_paper_skips", {"skip_key": "TEXT"})
    _ensure_columns(conn, "crypto5m_feature_snapshots", {"receive_time_missing": "INTEGER"})
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto5m_paper_skips_key "
                 "ON crypto5m_paper_skips(skip_key)")
    conn.commit()


_FEE_RECON_COLUMNS = {
    "fee_source": "TEXT", "fee_exponent": "REAL", "taker_only": "INTEGER",
    "rounding_precision": "INTEGER", "raw_source_payloads": "TEXT",
}


def _ensure_columns(conn, table: str, columns: dict) -> None:
    """Idempotently ADD COLUMN any of ``columns`` missing from ``table`` (never drops/alters existing)."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# --- idempotent writers (content-keyed) ------------------------------------------------------

def upsert_feature_snapshot(conn, snap: dict, *, now_ts: int) -> int:
    key = f"{snap['condition_id']}|{snap['checkpoint']}|{snap['feature_version']}"
    conn.execute(
        """INSERT INTO crypto5m_feature_snapshots
           (snapshot_key, condition_id, market_id, window_start_ts, window_end_ts, checkpoint,
            decision_ts, feature_cutoff_ts, feature_version, feature_status, price_to_beat,
            price_to_beat_quality, price_to_beat_known_ts, book_integrity_status, market_rule_hash,
            code_commit, receive_time_missing, features_json, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(snapshot_key) DO UPDATE SET
             market_id=excluded.market_id,
             window_start_ts=excluded.window_start_ts, window_end_ts=excluded.window_end_ts,
             decision_ts=excluded.decision_ts, feature_cutoff_ts=excluded.feature_cutoff_ts,
             feature_status=excluded.feature_status,
             price_to_beat=excluded.price_to_beat,
             price_to_beat_quality=excluded.price_to_beat_quality,
             price_to_beat_known_ts=excluded.price_to_beat_known_ts,
             book_integrity_status=excluded.book_integrity_status,
             market_rule_hash=excluded.market_rule_hash,
             code_commit=excluded.code_commit,
             features_json=excluded.features_json,
             receive_time_missing=excluded.receive_time_missing, persist_ts=excluded.persist_ts""",
        (key, snap["condition_id"], snap.get("market_id"), snap.get("window_start_ts"),
         snap.get("window_end_ts"), snap["checkpoint"], snap.get("decision_ts"),
         snap.get("feature_cutoff_ts"), snap["feature_version"], snap.get("feature_status"),
         snap.get("price_to_beat"), snap.get("price_to_beat_quality"),
         snap.get("price_to_beat_known_ts"), snap.get("book_integrity_status"),
         snap.get("market_rule_hash"), snap.get("code_commit"),
         int(bool(snap.get("receive_time_missing"))),
         json.dumps(snap.get("features", {})), now_ts),
    )
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_feature_snapshots WHERE snapshot_key=?",
                        (key,)).fetchone()[0]


def record_fee_config(conn, fee: dict, *, now_ts: int) -> int:
    cur = conn.execute(
        """INSERT INTO crypto5m_fee_configs
           (condition_id, fetched_ts, fees_enabled, taker_base_fee, fee_rate, fee_status,
            observed_fee_rate_bps, decoder_version, source, raw_payload, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (fee["condition_id"], fee.get("fetched_ts"), int(bool(fee.get("fees_enabled"))),
         fee.get("taker_base_fee"), fee.get("fee_rate"), fee.get("fee_status"),
         fee.get("observed_fee_rate_bps"), fee.get("decoder_version"), fee.get("source"),
         json.dumps(fee.get("raw_payload", {})), now_ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def seed_feature_registry(conn, registry: list) -> None:
    """Replace the feature registry rows (idempotent on feature_name+feature_version). Lists -> JSON."""
    for e in registry:
        conn.execute(
            """INSERT OR REPLACE INTO crypto5m_feature_registry
               (feature_name, feature_family, feature_version, required_sources, minimum_history,
                time_scope, null_reason_vocab, decision_safe)
               VALUES (?,?,?,?,?,?,?,?)""",
            (e["feature_name"], e["feature_family"], e["feature_version"],
             json.dumps(e["required_sources"]), e["minimum_history"], e["time_scope"],
             json.dumps(e["null_reason_vocab"]), int(bool(e["decision_safe"]))),
        )
    conn.commit()


def read_feature_registry(conn) -> list:
    rows = conn.execute(
        "SELECT feature_name, feature_family, feature_version, required_sources, minimum_history, "
        "time_scope, null_reason_vocab, decision_safe FROM crypto5m_feature_registry").fetchall()
    return [{"feature_name": r["feature_name"], "feature_family": r["feature_family"],
             "feature_version": r["feature_version"],
             "required_sources": json.loads(r["required_sources"] or "[]"),
             "minimum_history": r["minimum_history"], "time_scope": r["time_scope"],
             "null_reason_vocab": json.loads(r["null_reason_vocab"] or "[]"),
             "decision_safe": bool(r["decision_safe"])} for r in rows]


def record_fee_reconciliation(conn, fee: dict, *, now_ts: int) -> int:
    """Persist a ``reconcile_fee_sources`` result, populating the Phase 1B lineage columns plus the legacy
    fee_config columns (back-compat). ``raw_source_payloads`` is stored verbatim (JSON)."""
    raw = fee.get("raw_source_payloads", {}) or {}
    clob = raw.get("clob") if isinstance(raw, dict) else None
    fees_enabled = tbf = None
    if isinstance(clob, dict):
        fees_enabled = clob.get("fees_enabled", clob.get("feesEnabled"))
        tbf = clob.get("taker_base_fee", clob.get("takerBaseFee"))
    obs = raw.get("last_trade_fee_rate_bps") if isinstance(raw, dict) else None
    taker_only = fee.get("taker_only")
    raw_json = json.dumps(raw)
    cur = conn.execute(
        """INSERT INTO crypto5m_fee_configs
           (condition_id, fetched_ts, fees_enabled, taker_base_fee, fee_rate, fee_status,
            observed_fee_rate_bps, decoder_version, source, raw_payload, persist_ts,
            fee_source, fee_exponent, taker_only, rounding_precision, raw_source_payloads)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fee["condition_id"], fee.get("fetched_ts"),
         _fe_int(fees_enabled), tbf,
         fee.get("fee_rate"), fee.get("fee_status"), obs, fee.get("decoder_version"),
         fee.get("fee_source"), raw_json, now_ts,
         fee.get("fee_source"), fee.get("fee_exponent"),
         (int(bool(taker_only)) if taker_only is not None else None),
         fee.get("rounding_precision"), raw_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def upsert_prediction(conn, pred: dict, *, feature_snapshot_id: int, now_ts: int) -> int:
    key = f"{feature_snapshot_id}|{pred['model_id']}|{pred['model_version']}"
    conn.execute(
        """INSERT INTO crypto5m_model_predictions
           (prediction_key, feature_snapshot_id, condition_id, checkpoint, model_id, model_version,
            calibrated, q_up, q_down, prediction_status, reason, training_cutoff_ts, feature_version,
            persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(prediction_key) DO UPDATE SET
             q_up=excluded.q_up, q_down=excluded.q_down, prediction_status=excluded.prediction_status,
             reason=excluded.reason, persist_ts=excluded.persist_ts""",
        (key, feature_snapshot_id, pred.get("condition_id"), pred.get("checkpoint"),
         pred["model_id"], pred["model_version"], int(bool(pred.get("calibrated"))),
         pred.get("q_up"), pred.get("q_down"), pred.get("prediction_status"), pred.get("reason"),
         pred.get("training_cutoff_ts"), pred.get("feature_version"), now_ts),
    )
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_model_predictions WHERE prediction_key=?",
                        (key,)).fetchone()[0]


def upsert_decision(conn, dec: dict, *, now_ts: int) -> int:
    # params_sig (latency/stake/min_edge) is part of the key so a different-parameter replay run gets a
    # DISTINCT row instead of silently overwriting (decision content depends on those params).
    key = (f"{dec['condition_id']}|{dec['checkpoint']}|{dec.get('strategy_version')}|"
           f"{dec.get('model_id')}|{dec.get('params_sig', '')}")
    conn.execute(
        """INSERT INTO crypto5m_shadow_decisions
           (decision_key, condition_id, token_id, window_start_ts, checkpoint, decision_ts,
            feature_snapshot_id, prediction_id, book_hash, fee_config_id, price_to_beat_quality,
            model_id, model_version, strategy_version, status, reason, chosen_side,
            expected_edge_per_share, expected_net_pnl, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_key) DO UPDATE SET
             status=excluded.status, reason=excluded.reason, chosen_side=excluded.chosen_side,
             expected_edge_per_share=excluded.expected_edge_per_share,
             expected_net_pnl=excluded.expected_net_pnl, persist_ts=excluded.persist_ts""",
        (key, dec["condition_id"], dec.get("token_id"), dec.get("window_start_ts"), dec["checkpoint"],
         dec.get("decision_ts"), dec.get("feature_snapshot_id"), dec.get("prediction_id"),
         dec.get("book_hash"), dec.get("fee_config_id"), dec.get("price_to_beat_quality"),
         dec.get("model_id"), dec.get("model_version"), dec.get("strategy_version"),
         dec["status"], dec.get("reason"), dec.get("chosen_side"),
         dec.get("expected_edge_per_share"), dec.get("expected_net_pnl"), now_ts),
    )
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_shadow_decisions WHERE decision_key=?",
                        (key,)).fetchone()[0]


def record_skip(conn, *, condition_id, checkpoint, decision_ts, reason, detail="",
                strategy_version=None, now_ts) -> None:
    """Idempotent on (condition_id, checkpoint, strategy_version): a replay updates the row in place
    rather than appending a duplicate (one skip per decision point per strategy)."""
    key = f"{condition_id}|{checkpoint}|{strategy_version}"
    conn.execute(
        """INSERT INTO crypto5m_paper_skips
           (skip_key, condition_id, checkpoint, decision_ts, reason, detail, strategy_version, persist_ts)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(skip_key) DO UPDATE SET
             reason=excluded.reason, detail=excluded.detail, decision_ts=excluded.decision_ts,
             persist_ts=excluded.persist_ts""",
        (key, condition_id, checkpoint, decision_ts, reason, detail, strategy_version, now_ts),
    )
    conn.commit()


def record_evaluation_run(conn, *, run_ts, model_id, params: dict, summary: dict, now_ts) -> int:
    cur = conn.execute(
        """INSERT INTO crypto5m_evaluation_runs (run_ts, model_id, params_json, summary_json, persist_ts)
           VALUES (?,?,?,?,?)""",
        (run_ts, model_id, json.dumps(params), json.dumps(summary), now_ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def sim_counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SIM_TABLES}
