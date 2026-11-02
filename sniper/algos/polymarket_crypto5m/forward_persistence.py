"""Phase 1D — forward paper persistence. Dedicated tables (NOT the wallet-copy paper tables), append-only,
versioned. A campaign is FROZEN: re-starting it with a different manifest hash is refused. Decisions are
idempotent on (campaign, condition, rule, checkpoint) so the harness is restart-safe.
"""
from __future__ import annotations

import json

FORWARD_TABLES = (
    "crypto5m_forward_campaigns", "crypto5m_forward_rule_versions", "crypto5m_forward_decisions",
    "crypto5m_forward_entries", "crypto5m_forward_skips", "crypto5m_forward_fee_scenarios",
    "crypto5m_forward_settlements", "crypto5m_forward_rule_results", "crypto5m_forward_milestone_reports",
)


class FrozenCampaignError(Exception):
    """Raised when a campaign is re-started with a manifest that differs from the frozen original."""


def ensure_forward_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto5m_forward_campaigns (
            campaign_id TEXT PRIMARY KEY, campaign_start_ts INTEGER, git_commit TEXT,
            manifest_hash TEXT, manifest_json TEXT, feature_version TEXT, host TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_rule_versions (
            campaign_id TEXT, rule_id TEXT, rule_version TEXT, manifest_hash TEXT,
            definition_json TEXT, start_ts INTEGER,
            PRIMARY KEY (campaign_id, rule_id, rule_version)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT UNIQUE,
            campaign_id TEXT, condition_id TEXT, rule_id TEXT, rule_version TEXT, checkpoint INTEGER,
            decision_ts INTEGER, selected_outcome TEXT, status TEXT, reason TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER UNIQUE,
            campaign_id TEXT, condition_id TEXT, rule_id TEXT, selected_token_id TEXT, selected_outcome TEXT,
            decision_ts INTEGER, feature_cutoff_ts INTEGER, fill_book_ts INTEGER, modeled_latency_ms INTEGER,
            best_ask REAL, depth_weighted_fill_price REAL, gross_trade_cost REAL, shares REAL,
            levels_consumed INTEGER, levels_json TEXT, book_hash TEXT, book_age_ms INTEGER, spread REAL,
            slippage_vs_best_ask REAL, book_integrity_status TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_skips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER UNIQUE,
            campaign_id TEXT, condition_id TEXT, rule_id TEXT, reason TEXT, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_fee_scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_key TEXT UNIQUE,
            entry_id INTEGER, campaign_id TEXT, condition_id TEXT, rule_id TEXT, scenario TEXT,
            fee REAL, pnl REAL, won INTEGER, persist_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_settlements (
            campaign_id TEXT, condition_id TEXT, winning_outcome TEXT, reconciliation_status TEXT,
            persist_ts INTEGER, PRIMARY KEY (campaign_id, condition_id)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_rule_results (
            campaign_id TEXT, rule_id TEXT, scenario TEXT, n_eligible INTEGER, n_entries INTEGER,
            wins INTEGER, total_pnl REAL, updated_ts INTEGER,
            PRIMARY KEY (campaign_id, rule_id, scenario)
        );
        CREATE TABLE IF NOT EXISTS crypto5m_forward_milestone_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, milestone INTEGER,
            report_json TEXT, persist_ts INTEGER
        );
        """
    )
    conn.commit()


def start_campaign(conn, *, campaign_id, manifest, manifest_hash, git_commit, now_ts,
                   feature_version=None, host=None) -> None:
    """Register (or re-attach to) a campaign. Frozen: a different manifest_hash for the same id is refused."""
    row = conn.execute("SELECT manifest_hash FROM crypto5m_forward_campaigns WHERE campaign_id=?",
                       (campaign_id,)).fetchone()
    if row is not None:
        if row["manifest_hash"] != manifest_hash:
            raise FrozenCampaignError(
                f"campaign {campaign_id} is frozen at {row['manifest_hash']}, refusing {manifest_hash}")
        return
    conn.execute(
        """INSERT INTO crypto5m_forward_campaigns
           (campaign_id, campaign_start_ts, git_commit, manifest_hash, manifest_json, feature_version,
            host, persist_ts) VALUES (?,?,?,?,?,?,?,?)""",
        (campaign_id, now_ts, git_commit, manifest_hash, json.dumps(manifest), feature_version, host, now_ts))
    for rule_id, defn in (manifest.get("rules") or {}).items():
        conn.execute(
            """INSERT OR IGNORE INTO crypto5m_forward_rule_versions
               (campaign_id, rule_id, rule_version, manifest_hash, definition_json, start_ts)
               VALUES (?,?,?,?,?,?)""",
            (campaign_id, rule_id, manifest.get("rule_version", "v0"), manifest_hash,
             json.dumps(defn), now_ts))
    conn.commit()


def record_decision(conn, *, campaign_id, condition_id, rule_id, rule_version, checkpoint, decision_ts,
                    selected_outcome, status, reason, now_ts) -> int:
    """Idempotent on (campaign, condition, rule, checkpoint) — one decision per market/rule, restart-safe."""
    key = f"{campaign_id}|{condition_id}|{rule_id}|{checkpoint}"
    conn.execute(
        """INSERT INTO crypto5m_forward_decisions
           (decision_key, campaign_id, condition_id, rule_id, rule_version, checkpoint, decision_ts,
            selected_outcome, status, reason, persist_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_key) DO NOTHING""",
        (key, campaign_id, condition_id, rule_id, rule_version, checkpoint, decision_ts,
         selected_outcome, status, reason, now_ts))
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_forward_decisions WHERE decision_key=?", (key,)).fetchone()[0]


def record_entry(conn, *, decision_id, campaign_id, condition_id, rule_id, selected_token_id,
                 selected_outcome, decision_ts, feature_cutoff_ts, fill_book_ts, modeled_latency_ms,
                 best_ask, depth_weighted_fill_price, gross_trade_cost, shares, levels_consumed, levels,
                 book_hash, book_age_ms, spread, slippage_vs_best_ask, book_integrity_status, now_ts) -> int:
    conn.execute(
        """INSERT OR IGNORE INTO crypto5m_forward_entries
           (decision_id, campaign_id, condition_id, rule_id, selected_token_id, selected_outcome,
            decision_ts, feature_cutoff_ts, fill_book_ts, modeled_latency_ms, best_ask,
            depth_weighted_fill_price, gross_trade_cost, shares, levels_consumed, levels_json, book_hash,
            book_age_ms, spread, slippage_vs_best_ask, book_integrity_status, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, campaign_id, condition_id, rule_id, selected_token_id, selected_outcome, decision_ts,
         feature_cutoff_ts, fill_book_ts, modeled_latency_ms, best_ask, depth_weighted_fill_price,
         gross_trade_cost, shares, levels_consumed, json.dumps(levels), book_hash, book_age_ms, spread,
         slippage_vs_best_ask, book_integrity_status, now_ts))
    conn.commit()
    return conn.execute("SELECT id FROM crypto5m_forward_entries WHERE decision_id=?", (decision_id,)).fetchone()[0]


def record_skip(conn, *, decision_id, campaign_id, condition_id, rule_id, reason, now_ts) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO crypto5m_forward_skips
           (decision_id, campaign_id, condition_id, rule_id, reason, persist_ts) VALUES (?,?,?,?,?,?)""",
        (decision_id, campaign_id, condition_id, rule_id, reason, now_ts))
    conn.commit()


def record_fee_scenario(conn, *, entry_id, campaign_id, condition_id, rule_id, scenario, fee, pnl, won,
                        now_ts) -> None:
    key = f"{entry_id}|{scenario}"
    conn.execute(
        """INSERT OR IGNORE INTO crypto5m_forward_fee_scenarios
           (scenario_key, entry_id, campaign_id, condition_id, rule_id, scenario, fee, pnl, won, persist_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (key, entry_id, campaign_id, condition_id, rule_id, scenario, fee, pnl,
         (int(won) if won is not None else None), now_ts))
    conn.commit()


def pending_settlements(conn, campaign_id, *, checkpoint) -> list:
    """Markets that have been decided but lack a FINAL settlement (no settlement row, or one quarantined as
    unresolved/conflict). Lets a restarted harness resume settling in-flight windows instead of orphaning
    the in-memory tracking. Reconstructs the window from the stored decision_ts: window_start = decision_ts
    - checkpoint, slug = ``btc-updown-5m-{window_start}``, window_end = window_start + 300."""
    rows = conn.execute(
        """SELECT d.condition_id AS condition_id, MIN(d.decision_ts) AS decision_ts
           FROM crypto5m_forward_decisions d
           LEFT JOIN crypto5m_forward_settlements s
             ON s.campaign_id = d.campaign_id AND s.condition_id = d.condition_id
           WHERE d.campaign_id = ?
             AND COALESCE(s.winning_outcome, '') NOT IN ('Up', 'Down')
           GROUP BY d.condition_id""",
        (campaign_id,)).fetchall()
    out = []
    for r in rows:
        ws = int(r["decision_ts"]) - int(checkpoint)
        out.append({
            "condition_id": r["condition_id"],
            "slug": f"btc-updown-5m-{ws}",
            "window_start_ts": ws,
            "window_end_ts": ws + 300,
        })
    return out


def record_settlement(conn, *, campaign_id, condition_id, winning_outcome, reconciliation_status,
                      now_ts) -> None:
    conn.execute(
        """INSERT INTO crypto5m_forward_settlements
           (campaign_id, condition_id, winning_outcome, reconciliation_status, persist_ts)
           VALUES (?,?,?,?,?)
           ON CONFLICT(campaign_id, condition_id) DO UPDATE SET
             winning_outcome=excluded.winning_outcome,
             reconciliation_status=excluded.reconciliation_status, persist_ts=excluded.persist_ts""",
        (campaign_id, condition_id, winning_outcome, reconciliation_status, now_ts))
    conn.commit()
