"""Phase 1D — read-only forward-paper campaign report.

Aggregates the ``crypto5m_forward_*`` tables into a display dict: campaign manifest header, per-rule x
fee-scenario PnL/WR leaderboard, milestone progress, recent decisions, and settlement counts. Pure read —
never writes. Fee is unresolved (Gamma descriptor 0.07 vs realized 0), so PnL is reported under EVERY
recorded fee scenario and never collapsed to a single number; settlements with a fee/outcome conflict are
quarantined upstream (no fee_scenario rows) and so contribute zero resolved bets here.
"""
from __future__ import annotations

import json

MILESTONES = [200, 500, 1000]


def _campaign(conn):
    row = conn.execute(
        "SELECT * FROM crypto5m_forward_campaigns ORDER BY campaign_start_ts LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        manifest = json.loads(row["manifest_json"] or "{}")
    except (TypeError, ValueError):
        manifest = {}
    config = manifest.get("config") or {}
    return {
        "campaign_id": row["campaign_id"],
        "manifest_hash": row["manifest_hash"],
        "git_commit": row["git_commit"],
        "campaign_start_ts": row["campaign_start_ts"],
        "feature_version": row["feature_version"],
        "host": row["host"],
        "settlement_rule": config.get("settlement_rule"),
        "config": {
            "primary_checkpoint_s": config.get("primary_checkpoint_s"),
            "latency_ms": config.get("latency_ms"),
            "gross_notional_usdc": config.get("gross_notional_usdc"),
            "bankroll_usdc": config.get("bankroll_usdc"),
            "documented_fee_rate": config.get("documented_fee_rate"),
        },
    }


def _rule_ids(conn, campaign_id):
    if campaign_id is not None:
        rows = conn.execute(
            "SELECT DISTINCT rule_id FROM crypto5m_forward_rule_versions WHERE campaign_id=? "
            "ORDER BY rule_id", (campaign_id,)).fetchall()
        if rows:
            return [r["rule_id"] for r in rows]
    return [r["rule_id"] for r in
            conn.execute("SELECT DISTINCT rule_id FROM crypto5m_forward_decisions ORDER BY rule_id")]


def _scenario_stats(conn):
    """Per (rule_id, scenario): resolved bets (one fee row per settled entry), wins, total PnL."""
    out = {}
    for r in conn.execute(
            "SELECT rule_id, scenario, COUNT(DISTINCT entry_id) resolved, "
            "SUM(COALESCE(won,0)) wins, SUM(pnl) total_pnl "
            "FROM crypto5m_forward_fee_scenarios GROUP BY rule_id, scenario"):
        resolved = int(r["resolved"] or 0)
        wins = int(r["wins"] or 0)
        out.setdefault(r["rule_id"], {})[r["scenario"]] = {
            "resolved": resolved,
            "wins": wins,
            "win_rate": (wins / resolved) if resolved else None,
            "total_pnl": round(float(r["total_pnl"] or 0.0), 4),
        }
    return out


def build_forward_report(conn) -> dict:
    campaign = _campaign(conn)
    campaign_id = campaign["campaign_id"] if campaign else None

    dec_counts = {r["rule_id"]: r for r in conn.execute(
        "SELECT rule_id, COUNT(*) n, SUM(status='paper_entry') entries "
        "FROM crypto5m_forward_decisions GROUP BY rule_id")}
    resolved_by_rule = {r["rule_id"]: int(r["n"]) for r in conn.execute(
        "SELECT rule_id, COUNT(DISTINCT entry_id) n FROM crypto5m_forward_fee_scenarios GROUP BY rule_id")}
    scen_stats = _scenario_stats(conn)

    rules = []
    for rid in _rule_ids(conn, campaign_id):
        dc = dec_counts.get(rid)
        n_decisions = int(dc["n"]) if dc else 0
        n_entries = int(dc["entries"] or 0) if dc else 0
        rules.append({
            "rule_id": rid,
            "n_decisions": n_decisions,
            "n_entries": n_entries,
            "n_skips": n_decisions - n_entries,
            "n_resolved": resolved_by_rule.get(rid, 0),
            "scenarios": scen_stats.get(rid, {}),
        })

    totals = {
        "n_decisions": sum(r["n_decisions"] for r in rules),
        "n_entries": sum(r["n_entries"] for r in rules),
        "n_resolved": sum(r["n_resolved"] for r in rules),
        "by_scenario": {},
    }
    for r in rules:
        for scen, st in r["scenarios"].items():
            agg = totals["by_scenario"].setdefault(scen, {"resolved": 0, "wins": 0, "total_pnl": 0.0})
            agg["resolved"] += st["resolved"]
            agg["wins"] += st["wins"]
            agg["total_pnl"] += st["total_pnl"]
    for agg in totals["by_scenario"].values():
        agg["total_pnl"] = round(agg["total_pnl"], 4)
        agg["win_rate"] = (agg["wins"] / agg["resolved"]) if agg["resolved"] else None

    resolved = totals["n_resolved"]
    nxt = next((m for m in MILESTONES if resolved < m), None)
    milestones = {"targets": list(MILESTONES), "resolved": resolved, "next": nxt}

    recent = [dict(r) for r in conn.execute(
        "SELECT condition_id, rule_id, selected_outcome, status, reason, decision_ts "
        "FROM crypto5m_forward_decisions ORDER BY id DESC LIMIT 20")]

    s_rows = conn.execute(
        "SELECT winning_outcome, reconciliation_status FROM crypto5m_forward_settlements").fetchall()
    settlements = {
        "total": len(s_rows),
        "resolved": sum(1 for s in s_rows if s["winning_outcome"] in ("Up", "Down")),
        "quarantined": sum(1 for s in s_rows
                           if s["reconciliation_status"] == "conflict"
                           or s["winning_outcome"] not in ("Up", "Down")),
    }

    return {
        "campaign": campaign,
        "rules": rules,
        "totals": totals,
        "milestones": milestones,
        "recent_decisions": recent,
        "settlements": settlements,
        "fee_status": "conflict",
    }
