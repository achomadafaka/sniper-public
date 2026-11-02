"""SQLite persistence for Hyperliquid microstructure live-paper decisions."""
from __future__ import annotations

import json
import os
import sqlite3

from sniper.algos.hl_microstructure import CODE_VERSION
from sniper.algos.hl_microstructure.experts import ExpertSignal
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision
from sniper.algos.hl_microstructure.radar import RadarCandidate


def connect(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hlm_raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT UNIQUE,
            channel TEXT NOT NULL,
            asset TEXT,
            exchange_ts_ms INTEGER,
            receive_ts_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            code_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hlm_feature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            decision_ts_ms INTEGER NOT NULL,
            feature_version TEXT NOT NULL,
            status TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL,
            features_json TEXT NOT NULL,
            code_version TEXT NOT NULL,
            UNIQUE(asset, decision_ts_ms, feature_version)
        );
        CREATE TABLE IF NOT EXISTS hlm_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_id TEXT NOT NULL,
            asset TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('long','short')),
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            notional_usdc REAL NOT NULL,
            leverage REAL NOT NULL DEFAULT 1,
            entry_fee_usdc REAL NOT NULL DEFAULT 0,
            exit_fee_usdc REAL NOT NULL DEFAULT 0,
            realized_pnl REAL,
            state TEXT NOT NULL CHECK(state IN ('open','closed')),
            opened_ts_ms INTEGER NOT NULL,
            closed_ts_ms INTEGER,
            close_reason TEXT NOT NULL DEFAULT '',
            entry_thesis TEXT NOT NULL DEFAULT '',
            source_votes_json TEXT NOT NULL DEFAULT '{}',
            exit_plan_json TEXT NOT NULL DEFAULT '{}',
            code_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hlm_positions_open
            ON hlm_positions(policy_id, asset, state);
        CREATE TABLE IF NOT EXISTS hlm_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_id TEXT NOT NULL,
            asset TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT,
            reason TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            decision_ts_ms INTEGER NOT NULL,
            feature_snapshot_id INTEGER,
            position_id INTEGER,
            fee_rate REAL NOT NULL,
            code_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hlm_decisions_policy_ts
            ON hlm_decisions(policy_id, asset, decision_ts_ms);
        CREATE TABLE IF NOT EXISTS hlm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            policy_id TEXT NOT NULL,
            asset TEXT NOT NULL,
            fill_type TEXT NOT NULL CHECK(fill_type IN ('entry','exit')),
            side TEXT NOT NULL,
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            notional_usdc REAL NOT NULL,
            fee_usdc REAL NOT NULL,
            exchange_ts_ms INTEGER,
            decision_ts_ms INTEGER NOT NULL,
            code_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hlm_radar_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_ts_ms INTEGER NOT NULL,
            asset TEXT NOT NULL,
            rank INTEGER NOT NULL,
            score REAL NOT NULL,
            reasons_json TEXT NOT NULL,
            volume_usdc REAL NOT NULL,
            open_interest REAL NOT NULL,
            funding REAL,
            premium REAL,
            selected INTEGER NOT NULL DEFAULT 1,
            code_version TEXT NOT NULL,
            UNIQUE(decision_ts_ms, asset)
        );
        CREATE INDEX IF NOT EXISTS idx_hlm_radar_latest
            ON hlm_radar_snapshots(decision_ts_ms DESC, rank);
        CREATE TABLE IF NOT EXISTS hlm_expert_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            expert_id TEXT NOT NULL,
            family TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('long','short')),
            confidence REAL NOT NULL,
            reliability REAL NOT NULL,
            horizon_ms INTEGER NOT NULL,
            decision_ts_ms INTEGER NOT NULL,
            due_ts_ms INTEGER NOT NULL,
            entry_mid REAL NOT NULL,
            rationale TEXT NOT NULL,
            label_side TEXT,
            realized_return REAL,
            result INTEGER,
            settled_ts_ms INTEGER,
            code_version TEXT NOT NULL,
            UNIQUE(asset, expert_id, decision_ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_hlm_expert_due
            ON hlm_expert_predictions(asset, due_ts_ms, result);
        CREATE INDEX IF NOT EXISTS idx_hlm_expert_perf
            ON hlm_expert_predictions(expert_id, settled_ts_ms);
    """)
    _ensure_column(conn, "hlm_positions", "entry_thesis", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "hlm_positions", "source_votes_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "hlm_positions", "exit_plan_json", "TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def record_raw_event(conn, *, channel: str, asset: str | None, exchange_ts_ms: int | None,
                     receive_ts_ms: int, payload: dict) -> int | None:
    key = f"{channel}|{asset or ''}|{exchange_ts_ms or ''}|{json.dumps(payload, sort_keys=True)}"
    try:
        cur = conn.execute(
            """INSERT INTO hlm_raw_events
               (event_key, channel, asset, exchange_ts_ms, receive_ts_ms, payload_json, code_version)
               VALUES (?,?,?,?,?,?,?)""",
            (key, channel, asset, exchange_ts_ms, receive_ts_ms, json.dumps(payload, sort_keys=True), CODE_VERSION),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def record_feature_snapshot(conn, snapshot: FeatureSnapshot) -> int:
    cur = conn.execute(
        """INSERT INTO hlm_feature_snapshots
           (asset, decision_ts_ms, feature_version, status, quality_flags_json, features_json, code_version)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(asset, decision_ts_ms, feature_version) DO UPDATE SET
             status=excluded.status,
             quality_flags_json=excluded.quality_flags_json,
             features_json=excluded.features_json,
             code_version=excluded.code_version
           RETURNING id""",
        (snapshot.asset, snapshot.decision_ts_ms, snapshot.feature_version, snapshot.status,
         json.dumps(snapshot.quality_flags), json.dumps(snapshot.features, sort_keys=True), CODE_VERSION),
    )
    row = cur.fetchone()
    conn.commit()
    return int(row["id"])


def record_decision(conn, decision: PolicyDecision, snapshot: FeatureSnapshot, *,
                    feature_snapshot_id: int | None, status: str, reason: str | None = None,
                    position_id: int | None = None, fee_rate: float = 0.0,
                    action: str | None = None, side: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO hlm_decisions
           (policy_id, asset, action, side, reason, confidence, status, decision_ts_ms,
            feature_snapshot_id, position_id, fee_rate, code_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (decision.policy_id, snapshot.asset, action or decision.action, side if side is not None else decision.side,
         reason or decision.reason, float(decision.confidence), status, snapshot.decision_ts_ms,
         feature_snapshot_id, position_id, float(fee_rate), CODE_VERSION),
    )
    conn.commit()
    return cur.lastrowid


def open_position(conn, *, policy_id: str, asset: str, side: str, entry_price: float,
                  quantity: float, notional_usdc: float, leverage: float, fee_usdc: float,
                  opened_ts_ms: int, entry_thesis: str = "", source_votes: dict | None = None,
                  exit_plan: dict | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO hlm_positions
           (policy_id, asset, side, entry_price, quantity, notional_usdc, leverage,
            entry_fee_usdc, state, opened_ts_ms, entry_thesis, source_votes_json,
            exit_plan_json, code_version)
           VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?)""",
        (policy_id, asset, side, entry_price, quantity, notional_usdc, leverage,
         fee_usdc, opened_ts_ms, entry_thesis or "",
         json.dumps(source_votes or {}, sort_keys=True),
         json.dumps(exit_plan or {}, sort_keys=True),
         CODE_VERSION),
    )
    conn.commit()
    return cur.lastrowid


def record_fill(conn, *, position_id: int, policy_id: str, asset: str, fill_type: str,
                side: str, price: float, quantity: float, notional_usdc: float,
                fee_usdc: float, exchange_ts_ms: int | None, decision_ts_ms: int) -> int:
    cur = conn.execute(
        """INSERT INTO hlm_fills
           (position_id, policy_id, asset, fill_type, side, price, quantity, notional_usdc,
            fee_usdc, exchange_ts_ms, decision_ts_ms, code_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (position_id, policy_id, asset, fill_type, side, price, quantity, notional_usdc,
         fee_usdc, exchange_ts_ms, decision_ts_ms, CODE_VERSION),
    )
    conn.commit()
    return cur.lastrowid


def open_position_for(conn, *, policy_id: str, asset: str):
    return conn.execute(
        "SELECT * FROM hlm_positions WHERE policy_id=? AND asset=? AND state='open' ORDER BY id DESC LIMIT 1",
        (policy_id, asset),
    ).fetchone()


def close_position(conn, *, position_id: int, exit_fee_usdc: float, realized_pnl: float,
                   closed_ts_ms: int, reason: str) -> None:
    conn.execute(
        """UPDATE hlm_positions
           SET state='closed', exit_fee_usdc=?, realized_pnl=?, closed_ts_ms=?, close_reason=?
           WHERE id=?""",
        (exit_fee_usdc, realized_pnl, closed_ts_ms, reason, position_id),
    )
    conn.commit()


def record_radar_snapshot(conn, *, decision_ts_ms: int, candidates: list[RadarCandidate]) -> None:
    for c in candidates:
        conn.execute(
            """INSERT INTO hlm_radar_snapshots
               (decision_ts_ms, asset, rank, score, reasons_json, volume_usdc, open_interest,
                funding, premium, selected, code_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(decision_ts_ms, asset) DO UPDATE SET
                 rank=excluded.rank,
                 score=excluded.score,
                 reasons_json=excluded.reasons_json,
                 volume_usdc=excluded.volume_usdc,
                 open_interest=excluded.open_interest,
                 funding=excluded.funding,
                 premium=excluded.premium,
                 selected=excluded.selected,
                 code_version=excluded.code_version""",
            (
                int(decision_ts_ms),
                c.asset,
                int(c.rank),
                float(c.score),
                json.dumps(c.reasons),
                float(c.volume_usdc),
                float(c.open_interest),
                c.funding,
                c.premium,
                1 if c.selected else 0,
                CODE_VERSION,
            ),
        )
    conn.commit()


def record_expert_predictions(conn, snapshot: FeatureSnapshot, signals: list[ExpertSignal]) -> None:
    mid = _float((snapshot.features or {}).get("mid"))
    if mid is None or mid <= 0:
        return
    for s in signals:
        conn.execute(
            """INSERT OR IGNORE INTO hlm_expert_predictions
               (asset, expert_id, family, side, confidence, reliability, horizon_ms,
                decision_ts_ms, due_ts_ms, entry_mid, rationale, code_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.asset,
                s.expert_id,
                s.family,
                s.side,
                float(s.confidence),
                float(s.reliability),
                int(s.horizon_ms),
                int(snapshot.decision_ts_ms),
                int(snapshot.decision_ts_ms) + int(s.horizon_ms),
                mid,
                s.rationale,
                CODE_VERSION,
            ),
        )
    conn.commit()


def settle_due_expert_predictions(conn, *, asset: str, decision_ts_ms: int, mid: float) -> int:
    mid_f = _float(mid)
    if mid_f is None or mid_f <= 0:
        return 0
    rows = conn.execute(
        """SELECT id, side, entry_mid
           FROM hlm_expert_predictions
           WHERE asset=? AND result IS NULL AND due_ts_ms <= ?""",
        (asset, int(decision_ts_ms)),
    ).fetchall()
    settled = 0
    for r in rows:
        entry = _float(r["entry_mid"])
        if entry is None or entry <= 0:
            continue
        realized = (mid_f - entry) / entry
        label = "long" if realized >= 0 else "short"
        result = 1 if r["side"] == label else 0
        conn.execute(
            """UPDATE hlm_expert_predictions
               SET label_side=?, realized_return=?, result=?, settled_ts_ms=?
               WHERE id=?""",
            (label, realized, result, int(decision_ts_ms), int(r["id"])),
        )
        settled += 1
    conn.commit()
    return settled


def expert_reliability(conn, *, lookback: int = 500) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT expert_id,
                  COUNT(*) AS n,
                  AVG(result) AS accuracy,
                  AVG(CASE WHEN side=label_side THEN ABS(realized_return) ELSE -ABS(realized_return) END) AS avg_signed_return
           FROM (
               SELECT *
               FROM hlm_expert_predictions
               WHERE result IS NOT NULL
               ORDER BY settled_ts_ms DESC, id DESC
               LIMIT ?
           )
           GROUP BY expert_id""",
        (int(lookback),),
    ).fetchall()
    return {
        r["expert_id"]: {
            "n": int(r["n"] or 0),
            "accuracy": float(r["accuracy"] or 0.0),
            "avg_signed_return": float(r["avg_signed_return"] or 0.0),
        }
        for r in rows
    }


def expert_summary(conn, *, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """SELECT expert_id,
                  family,
                  COUNT(*) AS settled,
                  AVG(result) AS accuracy,
                  AVG(CASE WHEN side=label_side THEN ABS(realized_return) ELSE -ABS(realized_return) END) AS avg_signed_return,
                  MAX(settled_ts_ms) AS latest_settled_ts_ms
           FROM hlm_expert_predictions
           WHERE result IS NOT NULL
           GROUP BY expert_id, family
           ORDER BY settled DESC, accuracy DESC, expert_id
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [
        {
            "expert_id": r["expert_id"],
            "family": r["family"],
            "settled": int(r["settled"] or 0),
            "accuracy": float(r["accuracy"] or 0.0),
            "avg_signed_return": float(r["avg_signed_return"] or 0.0),
            "latest_settled_ts_ms": r["latest_settled_ts_ms"],
        }
        for r in rows
    ]


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
