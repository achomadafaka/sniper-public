"""Read API for the intel paper book.

GET touches SQLite only — never the network. Marks are written by the */5 cron
(`scripts/intel_paper_mark_job.py`), so the request path stays free of external
calls no matter how many people have the tab open. The single exception is the
explicit operator-triggered refresh below.

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import datetime as dt
import logging

from flask import Blueprint, jsonify, request

from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

STALE_AFTER_S = 900


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()


def _with_realized(rows: list[dict]) -> list[dict]:
    """Attach realized PnL to closed rows using the shared sign convention.

    Delegates to `pnl_pct`, which inverts for HL shorts — computing this inline would
    report a profitable short as a loss.
    """
    from sniper.services.intel_paper_mark import pnl_pct

    out = []
    for r in rows:
        entry, close = r.get("entry_price"), r.get("close_price")
        if entry and close is not None and float(entry) > 0:
            pct = pnl_pct(r.get("direction", ""), float(entry), float(close))
            r["realized_pnl_pct"] = pct
            r["realized_pnl_usdc"] = pct / 100.0 * float(r.get("size_usdc") or 0.0)
        else:
            # Distinguish unknown from zero — a closed row missing a price has no
            # computable result and must not read as break-even.
            r["realized_pnl_pct"] = None
            r["realized_pnl_usdc"] = None
        out.append(r)
    return out


def _realized_summary(closed: list[dict]) -> dict:
    """Aggregate realized results. Rows with no computable PnL are excluded, not zeroed."""
    scored = [c for c in closed if c.get("realized_pnl_pct") is not None]
    wins = [c for c in scored if c["realized_pnl_pct"] > 0]
    return {
        "n": len(scored),
        "n_uncomputable": len(closed) - len(scored),
        "pnl_usdc": round(sum(c["realized_pnl_usdc"] for c in scored), 2) if scored else None,
        "avg_pnl_pct": round(sum(c["realized_pnl_pct"] for c in scored) / len(scored), 2)
                       if scored else None,
        "wins": len(wins),
        "losses": len(scored) - len(wins),
    }


def create_api_intel_paper_blueprint(ctx) -> Blueprint:
    bp = Blueprint("api_intel_paper", __name__)

    @bp.get("/api/intel-paper/positions")
    def positions():
        venue = request.args.get("venue")
        if venue not in (None, "", "pm", "hl"):
            return jsonify({"items": [], "count": 0, "error": "bad venue"}), 400
        try:
            items = intel_paper.open_positions(ctx["conn"], venue or None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("intel paper positions failed")
            return jsonify({"items": [], "count": 0, "available": False,
                            "error": str(exc)}), 200

        # Closed positions are returned alongside the open ones. Without them a closed
        # position simply vanishes from the UI and its realized PnL — the entire point
        # of the paper book — becomes invisible.
        try:
            closed = _with_realized(intel_paper.closed_positions(ctx["conn"], venue or None))
        except Exception:  # noqa: BLE001
            logger.exception("intel paper closed positions failed")
            closed = []

        # Age of the FRESHEST mark across the shown positions. Reported rather than
        # hidden: a stale mark must never present as a current price (audit F-02).
        ages = [a for a in (_age_s(i.get("last_mark_ts")) for i in items) if a is not None]
        newest = min(ages) if ages else None
        return jsonify({
            "items": items,
            "count": len(items),
            "closed": closed,
            "closed_count": len(closed),
            "realized": _realized_summary(closed),
            "venue": venue or "all",
            "available": True,
            "mark_age_s": newest,
            "stale": (newest is None or newest > STALE_AFTER_S),
            "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        })

    @bp.post("/api/intel-paper/positions/<int:position_id>/close")
    def close(position_id: int):
        """Close a position at its last mark.

        A position closes manually or on resolution — there is no time-based rule.
        HL perps never resolve, so this is their only exit.
        """
        try:
            rows = [p for p in intel_paper.open_positions(ctx["conn"])
                    if p["position_id"] == position_id]
            if not rows:
                return jsonify({"ok": False, "error": "position not open"}), 404
            px = rows[0].get("last_mark_price")
            if px is None:
                # Closing with no observed price would invent one.
                return jsonify({"ok": False,
                                "error": "position has no mark yet — refresh first"}), 400
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            intel_paper.close_position(ctx["conn"], position_id, px, now, "manual")
            return jsonify({"ok": True, "position_id": position_id, "close_price": px})
        except Exception as exc:  # noqa: BLE001
            logger.exception("manual close failed for %s", position_id)
            return jsonify({"ok": False, "error": str(exc)}), 200

    @bp.post("/api/intel-paper/refresh")
    def refresh():
        """Operator-triggered mark cycle — the only endpoint permitted network calls."""
        from sniper.services.intel_paper_mark import mark_cycle
        try:
            return jsonify({"ok": True, "written": mark_cycle(ctx["conn"])})
        except Exception as exc:  # noqa: BLE001
            logger.exception("manual mark refresh failed")
            return jsonify({"ok": False, "error": str(exc)}), 200

    return bp
