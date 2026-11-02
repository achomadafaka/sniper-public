"""Live Wallet Signals board v2 — read-only. Joins the analyzer's canonical reads
(`wallet_analysis_latest`) with each wallet's current open positions (`live_positions`) and serves
them in explainable LANES for the Signals UI:

  directional_edge  — established / emerging directional edge
  fresh_watch       — fresh_high_surprise: thin-history, information-advantage SHAPE, UNVERIFIED
                      (carries the survivorship warning; NEVER the word "insider")
  algo              — structural_non_directional (arb/MM/basket) — positions are not directional picks

Each row leads with the WHY (the read's one-line headline) and links to the wallet's analyzer page.
Favorites are pinned; soft-deleted wallets are excluded. The analyzer has not proven live edge —
these are IN-SAMPLE reads; only a separately-authored strategy can validate a thesis out of sample.
No insider_score, no overall score, no copy advice / sizing.
"""
from __future__ import annotations

import datetime as dt
import json

from flask import Blueprint, jsonify, request

from sniper.persistence import db_wallet_signals as POS
from sniper.persistence import db_wallet_reads as RD
from sniper.persistence import db_wallet_flags as FL
from sniper.analysis.market_taxonomy import league_matches

DISCLAIMER = ("Explainable lanes over analyzer-backed reads — IN-SAMPLE, NOT a score, advice, "
              "expected PnL, or sizing. fresh_watch is UNVERIFIED (high false-positive shape); the "
              "next step is a separately-authored derived strategy with its own paper validation.")

# primary_verdict -> board lane. Verdicts not listed are NOT surfaced unless the wallet is favorited.
_LANE = {
    "established_directional_edge": "directional_edge",
    "emerging_directional_evidence": "directional_edge",
    "fresh_high_surprise": "fresh_watch",
    "fresh_high_conviction": "fresh_watch",   # came-from-nowhere big-conviction whale — UNVERIFIED
    "structural_non_directional": "algo",
}
_LANE_ORDER = {"directional_edge": 0, "fresh_watch": 1, "algo": 2, "other": 3}
_STRATEGY_ROUTES = {"strategy_candidate", "expedited_review"}
_STRATEGY_VERDICTS = {
    "established_directional_edge",
    "emerging_directional_evidence",
    "fresh_high_conviction",
    "fresh_high_surprise",
}


def _strategy_candidate(route: str | None, verdict: str | None) -> bool:
    if route in _STRATEGY_ROUTES:
        return True
    if route in {"monitor", "ignore"}:
        return False
    return verdict in _STRATEGY_VERDICTS


def _domains(read: dict) -> list:
    try:
        return list((json.loads(read.get("edge_location_json") or "{}") or {}).get("domains") or [])
    except (ValueError, TypeError):
        return []


def _warnings(read: dict) -> list:
    try:
        return list(json.loads(read.get("warnings_json") or "[]") or [])
    except (ValueError, TypeError):
        return []


def _domain_match(league: str | None, domains: list) -> bool:
    return league_matches(league, domains)


def _build_rows(conn, *, lane_filter: str | None = None) -> tuple[list, dict]:
    reads = RD.all_latest(conn)
    if not reads:
        return [], {}
    positions = POS.open_positions(conn)
    flags = FL.flags_for(conn, list({p["wallet"] for p in positions}))
    out = []
    for p in positions:
        w = p["wallet"]
        f = flags.get(w, {})
        if f.get("deleted"):
            continue
        read = reads.get(w)
        if not read:
            continue  # un-analyzed wallet — not on the board until it has a read
        verdict = read.get("primary_verdict")
        lane = _LANE.get(verdict)
        is_fav = bool(f.get("favorite"))
        if lane is None and not is_fav:
            continue  # unsupported verdict, not favorited -> not surfaced
        lane = lane or "other"
        domains = _domains(read)
        out.append({
            "wallet": w, "condition_id": p["condition_id"], "outcome_index": p["outcome_index"],
            "outcome": p.get("outcome"), "title": p.get("title"), "event_slug": p.get("event_slug"),
            "league": p.get("league"), "size": p.get("size"), "avg_price": p.get("avg_price"),
            "cur_price": p.get("cur_price"), "current_value": p.get("current_value"),
            "cash_pnl": p.get("cash_pnl"),
            "lane": lane, "primary_verdict": verdict,
            "archetype": read.get("archetype"), "intent": read.get("intent"),
            "headline": read.get("headline"),               # the WHY, shown first
            "edge_domains": domains, "edge_domain_match": _domain_match(p.get("league"), domains),
            "warnings": _warnings(read) if lane == "fresh_watch" else [],
            "is_favorite": is_fav,
            "strategy_candidate": _strategy_candidate(read.get("operational_route"), verdict),
            "validation_required": True,
        })
    if lane_filter:
        out = [r for r in out if r["lane"] == lane_filter]
    # favorites pinned, then lane order, then value desc
    out.sort(key=lambda x: (0 if x["is_favorite"] else 1,
                            _LANE_ORDER.get(x["lane"], 3), -float(x.get("current_value") or 0)))
    counts: dict = {}
    for r in out:
        counts[r["lane"]] = counts.get(r["lane"], 0) + 1
    return out, counts


def create_api_live_signals_blueprint(ctx) -> Blueprint:
    bp = Blueprint("api_live_signals", __name__)

    def _now():
        return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    @bp.get("/api/live-wallet-signals")
    def board():
        rows, counts = _build_rows(ctx["conn"], lane_filter=request.args.get("lane"))
        return jsonify({"count": len(rows), "items": rows, "lanes": counts,
                        "disclaimer": DISCLAIMER, "updated_at": _now()})

    @bp.get("/api/live-wallet-signals/strategy-candidates")
    def strategy_candidates():
        rows, _ = _build_rows(ctx["conn"])
        rows = [r for r in rows if r["strategy_candidate"]]
        return jsonify({"count": len(rows), "items": rows,
                        "disclaimer": DISCLAIMER + " Read-only proposal for derived strategy design.",
                        "updated_at": _now()})

    return bp
