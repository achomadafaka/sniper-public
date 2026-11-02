"""Phase 2A — quant shadow cohort persistence (separate from forward/sim/capture tables).

Records, per market window at each decision time, the full price-comparison matrix + a SHADOW decision
(prediction only — no paper entry, no order), then settles on the real Polymarket outcome. A campaign is
FROZEN on its config hash (a model/policy/sizing change is a NEW campaign version, never an edit), and
matrix/decision rows are idempotent on content keys so the runner is restart-safe. ``pending_settlements``
re-tracks decided-but-unsettled (or quarantined) windows after a restart.
"""
from __future__ import annotations

import hashlib
import json

QUANT_TABLES = (
    "crypto5m_quant_campaigns", "crypto5m_quant_matrix", "crypto5m_quant_decisions",
    "crypto5m_quant_settlements",
)


class QuantFrozenError(Exception):
    """Raised when a campaign is re-started with a config that differs from the frozen original."""


def _config_hash(config) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ensure_quant_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto5m_quant_campaigns (
            campaign_id TEXT PRIMARY KEY, campaign_start_ts INTEGER, git_commit TEXT,
            config_hash TEXT, config_json TEXT, model_version TEXT, policy_version TEXT,
            host TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_quant_matrix (
            id INTEGER PRIMARY KEY AUTOINCREMENT, matrix_key TEXT UNIQUE,
            campaign_id TEXT, condition_id TEXT, checkpoint INTEGER, decision_ts INTEGER,
            matrix_json TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_quant_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT UNIQUE,
            campaign_id TEXT, condition_id TEXT, checkpoint INTEGER, decision_ts INTEGER,
            actionable_side TEXT, q_trade REAL, net_edge REAL, status TEXT, reason TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_quant_settlements (
            campaign_id TEXT, condition_id TEXT, winning_outcome TEXT, reconciliation_status TEXT,
            persist_ts INTEGER, PRIMARY KEY (campaign_id, condition_id)
        );
        """
    )
    conn.commit()


def start_campaign(conn, *, campaign_id, config, git_commit, now_ts, model_version=None,
                   policy_version=None, host=None) -> None:
    """Register (or re-attach to) a shadow campaign. Frozen: a different config hash for the same id is
    refused (declare a new version instead)."""
    ch = _config_hash(config)
    row = conn.execute("SELECT config_hash FROM crypto5m_quant_campaigns WHERE campaign_id=?",
                       (campaign_id,)).fetchone()
    if row is not None:
        if row["config_hash"] != ch:
            raise QuantFrozenError(
                f"campaign {campaign_id} is frozen at {row['config_hash']}, refusing {ch}")
        return
    conn.execute(
        """INSERT INTO crypto5m_quant_campaigns
           (campaign_id, campaign_start_ts, git_commit, config_hash, config_json, model_version,
            policy_version, host, persist_ts) VALUES (?,?,?,?,?,?,?,?,?)""",
        (campaign_id, now_ts, git_commit, ch, json.dumps(config), model_version, policy_version, host,
         now_ts))
    conn.commit()


def record_matrix(conn, *, campaign_id, condition_id, checkpoint, decision_ts, matrix, now_ts) -> int:
    key = f"{campaign_id}|{condition_id}|{checkpoint}"
    conn.execute(
        """INSERT INTO crypto5m_quant_matrix
           (matrix_key, campaign_id, condition_id, checkpoint, decision_ts, matrix_json, persist_ts)
           VALUES (?,?,?,?,?,?,?) ON CONFLICT(matrix_key) DO NOTHING""",
        (key, campaign_id, condition_id, checkpoint, decision_ts, json.dumps(matrix), now_ts))
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_quant_matrix WHERE matrix_key=?", (key,)).fetchone()[0]


def record_decision(conn, *, campaign_id, condition_id, checkpoint, decision_ts, actionable_side, q_trade,
                    net_edge, status, reason, now_ts) -> int:
    key = f"{campaign_id}|{condition_id}|{checkpoint}"
    conn.execute(
        """INSERT INTO crypto5m_quant_decisions
           (decision_key, campaign_id, condition_id, checkpoint, decision_ts, actionable_side, q_trade,
            net_edge, status, reason, persist_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_key) DO NOTHING""",
        (key, campaign_id, condition_id, checkpoint, decision_ts, actionable_side, q_trade, net_edge,
         status, reason, now_ts))
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_quant_decisions WHERE decision_key=?", (key,)).fetchone()[0]


def record_settlement(conn, *, campaign_id, condition_id, winning_outcome, reconciliation_status,
                      now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_quant_settlements
           (campaign_id, condition_id, winning_outcome, reconciliation_status, persist_ts)
           VALUES (?,?,?,?,?)
           ON CONFLICT(campaign_id, condition_id) DO UPDATE SET
             winning_outcome=excluded.winning_outcome,
             reconciliation_status=excluded.reconciliation_status, persist_ts=excluded.persist_ts""",
        (campaign_id, condition_id, winning_outcome, reconciliation_status, now_ts))
    conn.commit()


def pending_settlements(conn, campaign_id, *, checkpoint) -> list:
    """Decided markets lacking a FINAL (Up/Down) settlement — for restart recovery. Reconstructs the window
    from the stored decision_ts (window_start = decision_ts - checkpoint)."""
    rows = conn.execute(
        """SELECT d.condition_id AS condition_id, MIN(d.decision_ts) AS decision_ts
           FROM crypto5m_quant_decisions d
           LEFT JOIN crypto5m_quant_settlements s
             ON s.campaign_id = d.campaign_id AND s.condition_id = d.condition_id
           WHERE d.campaign_id = ?
             AND COALESCE(s.winning_outcome, '') NOT IN ('Up', 'Down')
           GROUP BY d.condition_id""",
        (campaign_id,)).fetchall()
    out = []
    for r in rows:
        ws = int(r["decision_ts"]) - int(checkpoint)
        out.append({"condition_id": r["condition_id"], "slug": f"btc-updown-5m-{ws}",
                    "window_start_ts": ws, "window_end_ts": ws + 300})
    return out
