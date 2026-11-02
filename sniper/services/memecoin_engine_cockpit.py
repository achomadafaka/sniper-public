"""WO-12 — the operator's cockpit: SEE the engine think, per open engine position.

Read-only over the exit-state DB (market-state snapshots + candidate decisions) plus the ledger
(open-position set). No authority, no money-path write. Powers the canary dashboard surface and is a
pure function so it can be tested without Flask.
"""
from __future__ import annotations

import json


def _open_real_positions(ledger_conn):
    try:
        out = []
        for row in ledger_conn.execute(
                "SELECT id, mint, ticker, network_id, entry_price, entry_notional, "
                "executable_peak_price, opened_at, derisked_at, funded_contract_json "
                "FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' ORDER BY id"):
            pos = dict(row)
            try:
                policy = (json.loads(pos.get("funded_contract_json") or "{}").get("policy") or {})
            except (ValueError, TypeError):
                continue
            if policy.get("policy_id") != "engine-envelope-v1":
                continue
            try:
                catastrophe = float(policy["catastrophe_pct"])
                if not 0 <= catastrophe <= 100:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                catastrophe = None
            pos["_catastrophe_pct"] = catastrophe
            out.append(pos)
        return out
    except Exception:
        return []


def _latest_snapshot(exit_conn, position_id):
    r = exit_conn.execute(
        "SELECT state, nominal_price, executable_price, executable_peak, drawdown, realized_vol, "
        "net_flow_quote, reserve_quote, state_json, computed_at "
        "FROM memecoin_exit_market_state WHERE position_id=? ORDER BY computed_at DESC LIMIT 1",
        (position_id,)).fetchone()
    return dict(r) if r else None


def _recent_decisions(exit_conn, position_id, limit=10):
    rows = exit_conn.execute(
        "SELECT decided_at, candidate_action, candidate_reason_codes_json, proposed_fraction, "
        "outcome, executable_confirmed, no_action_reason, future_context_json "
        "FROM memecoin_exit_candidate_decisions WHERE position_id=? "
        "ORDER BY decided_at DESC LIMIT ?", (position_id, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["reason_codes"] = json.loads(d.pop("candidate_reason_codes_json") or "[]")
        except (ValueError, TypeError):
            d["reason_codes"] = []
        try:
            d["future_context"] = json.loads(d.pop("future_context_json") or "null")
        except (ValueError, TypeError):
            d["future_context"] = None
        out.append(d)
    return out


def _distance_to_catastrophe(snapshot, position):
    """Fractional PnL distance above the position's frozen catastrophe threshold."""
    if not snapshot:
        return None, "state_unavailable"
    px = snapshot.get("executable_price")
    if px is None:
        return None, "exact_unavailable"
    entry = (position or {}).get("entry_price")
    catastrophe = (position or {}).get("_catastrophe_pct")
    try:
        px, entry, catastrophe = float(px), float(entry), float(catastrophe)
    except (TypeError, ValueError):
        return None, "policy_unavailable"
    if entry <= 0:
        return None, "entry_unavailable"
    return ((px - entry) / entry) + catastrophe / 100.0, "exact"


def engine_cockpit(exit_conn, ledger_conn, *, decisions_limit=10):
    """Per open real position: current state, last N decisions with reason codes, the snapshot values
    behind the latest one, outcome stamps, and distance-to-catastrophe. Read-only."""
    out = []
    for pos in _open_real_positions(ledger_conn):
        snap = _latest_snapshot(exit_conn, pos["id"]) if exit_conn is not None else None
        state_extra = {}
        if snap and snap.get("state_json"):
            try:
                sj = json.loads(snap["state_json"])
                state_extra = {"prior_low": sj.get("prior_low"),
                               "arrival_rate": sj.get("arrival_rate"),
                               "depth_pull": sj.get("depth_pull_flag")}
            except (ValueError, TypeError):
                pass
        distance, distance_status = _distance_to_catastrophe(snap, pos)
        out.append({
            "position_id": pos["id"], "mint": pos.get("mint"), "ticker": pos.get("ticker"),
            "network_id": pos.get("network_id"),
            "entry_price": pos.get("entry_price"), "derisked_at": pos.get("derisked_at"),
            "catastrophe_pct": pos.get("_catastrophe_pct"),
            "state": (snap or {}).get("state"),
            "snapshot": None if not snap else {
                "computed_at": snap.get("computed_at"),
                "nominal_price": snap.get("nominal_price"),
                "executable_price": snap.get("executable_price"),
                "executable_peak": snap.get("executable_peak"),
                "drawdown": snap.get("drawdown"),
                "realized_vol": snap.get("realized_vol"),
                "net_flow_quote": snap.get("net_flow_quote"),
                **state_extra,
            },
            "distance_to_catastrophe": distance,
            "distance_status": distance_status,
            "recent_decisions": _recent_decisions(exit_conn, pos["id"], decisions_limit)
                                 if exit_conn is not None else [],
        })
    return out
