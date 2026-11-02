"""Read-only API for the Hyperliquid microstructure live-paper lane."""
from __future__ import annotations

import math
import os
import json
import sqlite3

from flask import Blueprint, jsonify

from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure.fusion import dynamic_exit_plan, source_votes, FusionContext
from sniper.algos.hl_microstructure.models import FeatureSnapshot


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(ROOT, "data", "hl_microstructure_paper.sqlite")
DISCLAIMER = (
    "Paper-only Hyperliquid microstructure research. Public market data only — no live orders, "
    "no private keys, no execution advice."
)


def _safe(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in v]
    return v


def _open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _summary(conn) -> dict:
    decisions = _table_count(conn, "hlm_decisions")
    snapshots = _table_count(conn, "hlm_feature_snapshots")
    raw_events = _table_count(conn, "hlm_raw_events")
    fills = _table_count(conn, "hlm_fills")
    open_pos = conn.execute("SELECT COUNT(*) FROM hlm_positions WHERE state='open'").fetchone()[0]
    closed_pos = conn.execute("SELECT COUNT(*) FROM hlm_positions WHERE state='closed'").fetchone()[0]
    pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM hlm_positions WHERE state='closed'").fetchone()[0]
    latest = conn.execute("SELECT MAX(decision_ts_ms) FROM hlm_decisions").fetchone()[0]
    return {
        "raw_events": raw_events,
        "snapshots": snapshots,
        "decisions": decisions,
        "fills": fills,
        "positions_open": int(open_pos or 0),
        "positions_closed": int(closed_pos or 0),
        "realized_pnl": float(pnl or 0),
        "latest_decision_ts_ms": latest,
    }


def _lanes(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT policy_id,
               COUNT(*) AS decisions,
               SUM(CASE WHEN status='entered' THEN 1 ELSE 0 END) AS entered,
               SUM(CASE WHEN status='held' THEN 1 ELSE 0 END) AS held,
               SUM(CASE WHEN status='exited' THEN 1 ELSE 0 END) AS exited,
               SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
               MAX(decision_ts_ms) AS latest_decision_ts_ms
        FROM hlm_decisions
        GROUP BY policy_id
        ORDER BY policy_id
    """).fetchall()
    open_by_policy = {
        r["policy_id"]: int(r["n"]) for r in conn.execute(
            "SELECT policy_id, COUNT(*) n FROM hlm_positions WHERE state='open' GROUP BY policy_id"
        ).fetchall()
    }
    pnl_by_policy = {
        r["policy_id"]: float(r["pnl"] or 0) for r in conn.execute(
            "SELECT policy_id, COALESCE(SUM(realized_pnl),0) pnl FROM hlm_positions "
            "WHERE state='closed' GROUP BY policy_id"
        ).fetchall()
    }
    return [
        {
            "policy_id": r["policy_id"],
            "decisions": int(r["decisions"] or 0),
            "entered": int(r["entered"] or 0),
            "held": int(r["held"] or 0),
            "exited": int(r["exited"] or 0),
            "skipped": int(r["skipped"] or 0),
            "open_positions": open_by_policy.get(r["policy_id"], 0),
            "realized_pnl": pnl_by_policy.get(r["policy_id"], 0.0),
            "latest_decision_ts_ms": r["latest_decision_ts_ms"],
        }
        for r in rows
    ]


def _rows(conn, sql: str, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]


def _reason_counts(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT reason, COUNT(*) AS n
        FROM hlm_decisions
        WHERE policy_id='fusion_radar' AND status='skipped'
        GROUP BY reason
        ORDER BY n DESC, reason
        LIMIT 12
    """).fetchall()]


def _radar(conn) -> list[dict]:
    try:
        latest = conn.execute("SELECT MAX(decision_ts_ms) FROM hlm_radar_snapshots").fetchone()[0]
    except sqlite3.Error:
        return []
    if latest is None:
        return []
    rows = conn.execute("""
        SELECT asset, rank, score, reasons_json, volume_usdc, open_interest, funding, premium,
               selected, decision_ts_ms
        FROM hlm_radar_snapshots
        WHERE decision_ts_ms=?
        ORDER BY rank ASC
        LIMIT 20
    """, (latest,)).fetchall()
    out = []
    for r in rows:
        try:
            reasons = json.loads(r["reasons_json"] or "[]")
        except json.JSONDecodeError:
            reasons = []
        out.append({
            "asset": r["asset"],
            "rank": int(r["rank"]),
            "score": float(r["score"] or 0),
            "reasons": reasons,
            "volume_usdc": float(r["volume_usdc"] or 0),
            "open_interest": float(r["open_interest"] or 0),
            "funding": r["funding"],
            "premium": r["premium"],
            "selected": bool(r["selected"]),
            "decision_ts_ms": r["decision_ts_ms"],
        })
    return out


def _decode_json(raw, fallback):
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _open_theses(conn) -> list[dict]:
    try:
        rows = conn.execute("""
            SELECT p.id, p.policy_id, p.asset, p.side, p.entry_price, p.notional_usdc,
                   p.opened_ts_ms, p.entry_thesis, p.source_votes_json, p.exit_plan_json,
                   d.reason, d.confidence, f.features_json,
                   f.feature_version, f.status as feature_status, f.quality_flags_json
            FROM hlm_positions p
            LEFT JOIN hlm_decisions d ON d.position_id=p.id AND d.status='entered'
            LEFT JOIN hlm_feature_snapshots f ON f.id=d.feature_snapshot_id
            WHERE p.state='open' AND p.policy_id='fusion_radar'
            GROUP BY p.id
            ORDER BY p.opened_ts_ms DESC
            LIMIT 20
        """).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        features = _decode_json(r["features_json"], {})
        flags = _decode_json(r["quality_flags_json"], [])
        snap = FeatureSnapshot(r["asset"], int(r["opened_ts_ms"] or 0), r["feature_version"] or "", features, r["feature_status"] or "unknown", flags)
        stored_votes = _decode_json(r["source_votes_json"], {})
        active_votes = stored_votes if stored_votes else {
            family: {"active": True, "side": side, "score": score}
            for family, (side, score) in source_votes(snap, FusionContext()).items()
        }
        votes = {}
        for family in ("flow", "book", "momentum", "funding", "premium", "catalyst", "liquidation", "expert"):
            data = active_votes.get(family)
            votes[family] = {
                "active": bool(data.get("active", data is not None)) if data else False,
                "side": data.get("side") if data else None,
                "score": data.get("score") if data else 0.0,
            }
        exit_plan = _decode_json(r["exit_plan_json"], {}) or dynamic_exit_plan(snap, str(r["side"]))
        thesis = r["entry_thesis"] or _thesis_text(str(r["policy_id"]), str(r["asset"]), str(r["side"]), str(r["reason"] or "paper_entry"))
        out.append({
            "position_id": int(r["id"]),
            "policy_id": r["policy_id"],
            "asset": r["asset"],
            "side": r["side"],
            "entry_price": r["entry_price"],
            "notional_usdc": r["notional_usdc"],
            "opened_ts_ms": r["opened_ts_ms"],
            "confidence": r["confidence"],
            "reason": r["reason"],
            "thesis": thesis,
            "source_votes": votes,
            "exit_plan": exit_plan,
        })
    return out


def _thesis_text(policy_id: str, asset: str, side: str, reason: str) -> str:
    if policy_id == "fusion_radar":
        return f"{asset} {side}: market radar selected it and multiple live pressure sources agreed ({reason})."
    return f"{asset} {side}: legacy paper lane entry ({reason})."


def _source_health(conn) -> dict:
    def latest(channel: str):
        try:
            return conn.execute(
                "SELECT MAX(receive_ts_ms) FROM hlm_raw_events WHERE channel=?",
                (channel,),
            ).fetchone()[0]
        except sqlite3.Error:
            return None

    expert_count = _table_count(conn, "hlm_expert_predictions")
    return {
        "book_flow": {
            "active": latest("l2Book") is not None and latest("trades") is not None,
            "latest_book_ts_ms": latest("l2Book"),
            "latest_trade_ts_ms": latest("trades"),
        },
        "market_context": {
            "active": latest("activeAssetCtx") is not None,
            "latest_ctx_ts_ms": latest("activeAssetCtx"),
        },
        "expert_lab": {
            "active": expert_count > 0,
            "predictions": expert_count,
        },
        "catalysts": {
            "active": False,
            "note": "optional context DB feed; idle unless local news rows match selected assets",
        },
        "liquidations": {
            "active": False,
            "note": "optional context DB feed; idle unless whale/liquidation rows match selected assets",
        },
    }


def _expert_health(conn) -> list[dict]:
    try:
        return P.expert_summary(conn, limit=16)
    except sqlite3.Error:
        return []


def _empty_payload(db_path: str, message: str) -> dict:
    return {
        "available": False,
        "db_path": db_path,
        "message": message,
        "summary": {
            "raw_events": 0, "snapshots": 0, "decisions": 0, "fills": 0,
            "positions_open": 0, "positions_closed": 0, "realized_pnl": 0,
            "latest_decision_ts_ms": None,
        },
        "lanes": [],
        "positions": [],
        "decisions": [],
        "fills": [],
        "skip_reasons": [],
        "radar": [],
        "open_theses": [],
        "source_health": {},
        "expert_health": [],
        "strategy_state": {},
        "paper_status": {"paper_trading_live": False, "real_money": False},
        "disclaimer": DISCLAIMER,
    }


def create_api_hl_microstructure_blueprint(ctx=None) -> Blueprint:
    bp = Blueprint("api_hl_microstructure", __name__)
    ctx = ctx or {}

    @bp.get("/api/hl-microstructure/status")
    def status():
        db_path = ctx.get("db_path") or os.getenv("HL_MICROSTRUCTURE_DB") or DEFAULT_DB
        if not os.path.exists(db_path):
            return jsonify(_empty_payload(db_path, "paper DB not found"))
        try:
            conn = _open_readonly(db_path)
            try:
                payload = {
                    "available": True,
                    "db_path": db_path,
                    "summary": _summary(conn),
                    "lanes": _lanes(conn),
                    "positions": _rows(conn, """
                        SELECT id, policy_id, asset, side, entry_price, quantity, notional_usdc,
                               leverage, entry_fee_usdc, realized_pnl, state, opened_ts_ms,
                               closed_ts_ms, close_reason
                        FROM hlm_positions
                        ORDER BY state='open' DESC, id DESC
                        LIMIT ?
                    """, 30),
                    "decisions": _rows(conn, """
                        SELECT id, policy_id, asset, action, side, reason, confidence, status,
                               decision_ts_ms, position_id, fee_rate
                        FROM hlm_decisions
                        WHERE policy_id='fusion_radar'
                        ORDER BY decision_ts_ms DESC, id DESC
                        LIMIT ?
                    """, 40),
                    "fills": _rows(conn, """
                        SELECT id, position_id, policy_id, asset, fill_type, side, price,
                               quantity, notional_usdc, fee_usdc, decision_ts_ms
                        FROM hlm_fills
                        ORDER BY decision_ts_ms DESC, id DESC
                        LIMIT ?
                    """, 20),
                    "skip_reasons": _reason_counts(conn),
                    "radar": _radar(conn),
                    "open_theses": _open_theses(conn),
                    "source_health": _source_health(conn),
                    "expert_health": _expert_health(conn),
                    "strategy_state": {
                        "hypothesis": "Short-horizon perp moves are more tradable when book flow, momentum, carry stress, and the live expert lab agree.",
                        "entry_rule": "Enter paper only when at least two source families point the same way and the visible spread is acceptable.",
                        "execution": "Paper taker fill through the visible book, fixed small notional, fee estimate, dynamic stop/target/time/signal-decay exits.",
                        "promotion_bar": "Not real-trade eligible until paper PnL, expert hit rates, latency, slippage, and source coverage survive live review.",
                    },
                    "paper_status": {
                        "paper_trading_live": _summary(conn)["latest_decision_ts_ms"] is not None,
                        "real_money": False,
                        "live_orders_enabled": False,
                    },
                    "disclaimer": DISCLAIMER,
                }
            finally:
                conn.close()
            return jsonify(_safe(payload))
        except sqlite3.Error as e:
            return jsonify(_empty_payload(db_path, f"paper DB unavailable: {e}"))

    return bp
