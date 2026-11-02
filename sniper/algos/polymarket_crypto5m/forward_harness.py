"""Phase 1D — forward harness decision logic (pure). Build the known-at-time decision context from the
per-token books + Binance momentum, run all 5 frozen rules, and execute the SELECTED side on ITS OWN ask
ladder (never 1 - opposite). Book-quality gates produce a blocked record. The live loop (discover ->
schedule 240s -> snapshot -> decide -> persist -> settle) lives in scripts/run_polymarket_crypto5m_forward_paper.py.
"""
from __future__ import annotations

import json

from . import forward_paper as FP
from . import forward_persistence as FPP
from . import forward_rules as FR

NEAR_TOP_LEVELS = 3


def _mid(book):
    b = (book or {}).get("best_bid")
    a = (book or {}).get("best_ask")
    return ((float(b) + float(a)) / 2.0) if (b is not None and a is not None) else None


def _near_top_notional(book, side_key):
    levels = (book or {}).get(side_key) or []
    if not levels:
        return None
    return sum(float(p) * float(s) for p, s in levels[:NEAR_TOP_LEVELS])


def _spread(book):
    b = (book or {}).get("best_bid")
    a = (book or {}).get("best_ask")
    return (float(a) - float(b)) if (b is not None and a is not None) else None


def build_context(*, binance_now, binance_30s_ago, up_book, down_book) -> dict:
    """Assemble the known-at-time decision context the rules consume. condition_id is added by decide_market."""
    return {
        "binance_price_now": binance_now,
        "binance_price_30s_ago": binance_30s_ago,
        "up_midpoint": _mid(up_book),
        "down_midpoint": _mid(down_book),
        "up_bid_notional": _near_top_notional(up_book, "bids"),
        "up_ask_notional": _near_top_notional(up_book, "asks"),
        "down_bid_notional": _near_top_notional(down_book, "bids"),
        "down_ask_notional": _near_top_notional(down_book, "asks"),
    }


def decide_market(*, condition_id, ctx, up_book, down_book, up_token, down_token, decision_ts, fill_book_ts,
                  gross, min_order, latency_ms, book_quality="usable", book_age_ms=None) -> list:
    """Run every frozen rule for one market. Returns one record per rule with status ∈
    {paper_entry, paper_no_trade, blocked}. An entry carries the fill executed on the selected token's asks."""
    records = []
    for rid in FR.RULE_IDS:
        side, reason = FR.evaluate(rid, {**ctx, "condition_id": condition_id})
        if side is None:
            records.append({"rule_id": rid, "selected_outcome": None, "status": "paper_no_trade",
                            "reason": reason})
            continue
        if book_quality != "usable":
            records.append({"rule_id": rid, "selected_outcome": side, "status": "blocked",
                            "reason": f"book_{book_quality}"})
            continue
        token, book = (up_token, up_book) if side == "Up" else (down_token, down_book)
        fill = FP.walk_ask_ladder((book or {}).get("asks"), gross_usdc=gross, min_order_size=min_order)
        if not fill["filled"]:
            records.append({"rule_id": rid, "selected_outcome": side, "status": "paper_no_trade",
                            "reason": fill["reason"]})
            continue
        records.append({
            "rule_id": rid, "selected_outcome": side, "status": "paper_entry", "reason": reason,
            "selected_token_id": token, "fill": fill, "decision_ts": decision_ts, "fill_book_ts": fill_book_ts,
            "modeled_latency_ms": latency_ms, "spread": _spread(book),
            "book_hash": (book or {}).get("book_hash"), "book_age_ms": book_age_ms,
            "book_integrity_status": book_quality,
        })
    return records


def persist_records(conn, *, campaign_id, condition_id, rule_version, checkpoint, decision_ts, records,
                    now_ts) -> None:
    """Persist each rule's decision + (entry | skip). Idempotent via record_decision's decision_key."""
    for rec in records:
        did = FPP.record_decision(
            conn, campaign_id=campaign_id, condition_id=condition_id, rule_id=rec["rule_id"],
            rule_version=rule_version, checkpoint=checkpoint, decision_ts=decision_ts,
            selected_outcome=rec.get("selected_outcome"), status=rec["status"], reason=rec.get("reason"),
            now_ts=now_ts)
        if rec["status"] == "paper_entry":
            f = rec["fill"]
            FPP.record_entry(
                conn, decision_id=did, campaign_id=campaign_id, condition_id=condition_id,
                rule_id=rec["rule_id"], selected_token_id=rec.get("selected_token_id"),
                selected_outcome=rec["selected_outcome"], decision_ts=decision_ts,
                feature_cutoff_ts=decision_ts, fill_book_ts=rec.get("fill_book_ts"),
                modeled_latency_ms=rec.get("modeled_latency_ms"), best_ask=f["best_ask"],
                depth_weighted_fill_price=f["depth_weighted_fill_price"],
                gross_trade_cost=f["gross_trade_cost"], shares=f["shares"],
                levels_consumed=f["levels_consumed"], levels=f["levels"], book_hash=rec.get("book_hash"),
                book_age_ms=rec.get("book_age_ms"), spread=rec.get("spread"),
                slippage_vs_best_ask=f["slippage_vs_best_ask"],
                book_integrity_status=rec.get("book_integrity_status"), now_ts=now_ts)
        else:
            FPP.record_skip(conn, decision_id=did, campaign_id=campaign_id, condition_id=condition_id,
                            rule_id=rec["rule_id"], reason=rec.get("reason"), now_ts=now_ts)


def settle_market(conn, *, campaign_id, condition_id, winning_outcome, reconciliation_status, now_ts,
                  canonical_rate=None) -> None:
    """Record the settlement and, for each entry of this market, compute the fee scenarios + PnL. Quarantine
    (no formal PnL) when unresolved/conflict — the entry stays, but no fee_scenario rows are written."""
    FPP.record_settlement(conn, campaign_id=campaign_id, condition_id=condition_id,
                          winning_outcome=winning_outcome, reconciliation_status=reconciliation_status,
                          now_ts=now_ts)
    if reconciliation_status == "conflict" or winning_outcome not in ("Up", "Down"):
        return
    for e in conn.execute("SELECT * FROM crypto5m_forward_entries WHERE campaign_id=? AND condition_id=?",
                          (campaign_id, condition_id)).fetchall():
        won = FP.selected_won(e["selected_outcome"], winning_outcome)
        levels = json.loads(e["levels_json"] or "[]")
        for scen, fee in FP.fee_scenarios(levels, canonical_rate=canonical_rate).items():
            p = FP.pnl(shares=e["shares"], gross_trade_cost=e["gross_trade_cost"], fee=fee, won=won)
            FPP.record_fee_scenario(conn, entry_id=e["id"], campaign_id=campaign_id,
                                    condition_id=condition_id, rule_id=e["rule_id"], scenario=scen,
                                    fee=fee, pnl=p, won=won, now_ts=now_ts)
