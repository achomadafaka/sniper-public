"""Phase 2A — read-only quant shadow cohort report.

Aggregates the ``crypto5m_quant_*`` tables: campaign header, totals, and the evaluation that matters —
the RESIDUAL SPLIT. For resolved windows with an actionable prediction, it reports the hit rate overall
and split by whether the prediction AGREED with the market favorite (the C1-style favorite trade) or
DIFFERED from it (the genuine residual bet — where any real edge would live), plus a shadow net-edge
(paper, documented fee). Pure read; never writes. Shadow predictions only — no orders, no PnL claims.
"""
from __future__ import annotations

import json

from .quant_eval import wilson_ci

MIN_VERDICT_N = 50   # resolved paper bets before any edge read is trustworthy


def _campaign(conn):
    row = conn.execute(
        "SELECT * FROM crypto5m_quant_campaigns ORDER BY campaign_start_ts LIMIT 1").fetchone()
    if row is None:
        return None
    return {"campaign_id": row["campaign_id"], "config_hash": row["config_hash"],
            "model_version": row["model_version"], "policy_version": row["policy_version"],
            "git_commit": row["git_commit"], "campaign_start_ts": row["campaign_start_ts"]}


def _empty_agg():
    return {"n": 0, "hits": 0, "hit_rate": None, "ci": [None, None]}


def _finalize(agg):
    agg["hit_rate"] = (agg["hits"] / agg["n"]) if agg["n"] else None
    agg["ci"] = list(wilson_ci(agg["hits"], agg["n"]))
    return agg


def build_quant_report(conn) -> dict:
    campaign = _campaign(conn)
    totals = {
        "matrix": conn.execute("SELECT COUNT(*) FROM crypto5m_quant_matrix").fetchone()[0],
        "decisions": conn.execute("SELECT COUNT(*) FROM crypto5m_quant_decisions").fetchone()[0],
        "actionable": conn.execute(
            "SELECT COUNT(*) FROM crypto5m_quant_decisions WHERE actionable_side IS NOT NULL").fetchone()[0],
        "resolved": conn.execute(
            "SELECT COUNT(*) FROM crypto5m_quant_settlements WHERE winning_outcome IN ('Up','Down')"
        ).fetchone()[0],
    }

    matrices = {}
    for r in conn.execute("SELECT condition_id, checkpoint, matrix_json FROM crypto5m_quant_matrix"):
        try:
            matrices[(r["condition_id"], r["checkpoint"])] = json.loads(r["matrix_json"] or "{}")
        except (TypeError, ValueError):
            matrices[(r["condition_id"], r["checkpoint"])] = {}
    settled = {r["condition_id"]: r["winning_outcome"] for r in conn.execute(
        "SELECT condition_id, winning_outcome FROM crypto5m_quant_settlements "
        "WHERE winning_outcome IN ('Up','Down')")}

    overall, agree, differ = _empty_agg(), _empty_agg(), _empty_agg()
    net_edges = []
    c1_hits = c1_n = 0
    for d in conn.execute(
            "SELECT condition_id, checkpoint, actionable_side FROM crypto5m_quant_decisions "
            "WHERE actionable_side IS NOT NULL"):
        winner = settled.get(d["condition_id"])
        if winner is None:
            continue
        side = d["actionable_side"]
        won = (side == winner)
        overall["n"] += 1
        overall["hits"] += int(won)
        m = matrices.get((d["condition_id"], d["checkpoint"]), {})
        qm = m.get("q_market")
        if qm is not None:
            fav = "Up" if qm >= 0.5 else "Down"
            c1_n += 1
            c1_hits += int(fav == winner)              # C1 favorite on the same window
            bucket = agree if side == fav else differ
            bucket["n"] += 1
            bucket["hits"] += int(won)
        cost = m.get(f"{side.lower()}_fee_inclusive_cost_per_share")
        if cost is not None:
            net_edges.append((1.0 - cost) if won else (-cost))

    eval_block = {
        "n_resolved_actionable": overall["n"],
        "hits": overall["hits"],
        "hit_rate": (overall["hits"] / overall["n"]) if overall["n"] else None,
        "agree_market": _finalize(agree),
        "differ_market": _finalize(differ),
        "c1_n": c1_n, "c1_hits": c1_hits,
        "c1_hit_rate": (c1_hits / c1_n) if c1_n else None,
        "shadow_net_edge_mean": (sum(net_edges) / len(net_edges)) if net_edges else None,
    }

    recent = []
    for d in conn.execute(
            "SELECT condition_id, checkpoint, actionable_side, q_trade, net_edge FROM "
            "crypto5m_quant_decisions ORDER BY id DESC LIMIT 12"):
        m = matrices.get((d["condition_id"], d["checkpoint"]), {})
        winner = settled.get(d["condition_id"])
        won = (d["actionable_side"] == winner) if (winner and d["actionable_side"]) else None
        if not d["actionable_side"]:
            outcome = "no_trade"                       # evaluated, no positive edge -> no paper bet
        elif winner is None:
            outcome = "pending"                        # bet placed, window not resolved yet
        else:
            outcome = "win" if won else "loss"
        recent.append({"condition_id": d["condition_id"], "actionable_side": d["actionable_side"],
                       "q_market": m.get("q_market"), "q_trade": d["q_trade"], "net_edge": d["net_edge"],
                       "won": won, "outcome": outcome})

    n_windows = conn.execute("SELECT COUNT(DISTINCT condition_id) FROM crypto5m_quant_matrix").fetchone()[0]

    # latest window snapshot (plain-language "live now" card)
    lm = conn.execute("SELECT condition_id, checkpoint, matrix_json FROM crypto5m_quant_matrix "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    latest = None
    if lm:
        try:
            m = json.loads(lm["matrix_json"] or "{}")
        except (TypeError, ValueError):
            m = {}
        panel = m.get("panel", {}) or {}
        fusion = panel.get("fusion", {}) or {}
        latest = {
            "checkpoint": lm["checkpoint"], "q_market": m.get("q_market"),
            "q_ensemble": m.get("q_ensemble"), "q_trade": m.get("q_trade"),
            "predictive_uncertainty": m.get("predictive_uncertainty"),
            "actionable_side": m.get("actionable_side"),
            "n_sources": fusion.get("n_usable"), "fusion_quality": fusion.get("state_quality"),
            "structural_minus_market": panel.get("structural_minus_market"),
            "vol_regime": panel.get("vol_regime"), "session": panel.get("session"),
        }

    # plain-language edge verdict
    nres = eval_block["n_resolved_actionable"]
    differ = eval_block["differ_market"]
    if nres < MIN_VERDICT_N:
        edge_verdict = {"state": "gathering",
                        "text": f"Gathering data — {nres} of {MIN_VERDICT_N} resolved paper bets before "
                                f"any edge read is trustworthy."}
    else:
        hr = eval_block["hit_rate"]
        c1 = eval_block["c1_hit_rate"]
        beats_c1 = (hr is not None and c1 is not None and hr > c1)
        resid = (differ["n"] >= MIN_VERDICT_N and differ["ci"][0] is not None and differ["ci"][0] > 0.5)
        if beats_c1 and resid:
            edge_verdict = {"state": "edge", "text": f"Beating the favorite (hit {hr} vs C1 {c1}) with a "
                            f"significant residual edge (differ-bucket {differ['hit_rate']})."}
        elif beats_c1:
            edge_verdict = {"state": "weak", "text": f"Edges out C1 (hit {hr} vs {c1}) but no significant "
                            f"residual edge yet."}
        else:
            edge_verdict = {"state": "none", "text": f"No edge — tracks/loses to the market favorite "
                            f"(hit {hr} vs C1 {c1})."}

    status = (f"Scores every BTC 5-min window with fused signals; placed {totals['actionable']} paper bets "
              f"across {n_windows} evaluated windows ({totals['resolved']} resolved). No orders.")

    return {"campaign": campaign, "totals": totals, "n_windows": n_windows, "eval": eval_block,
            "latest": latest, "edge_verdict": edge_verdict, "status": status, "recent": recent,
            "fee_status": "conflict"}
