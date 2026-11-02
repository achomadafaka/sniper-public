"""Offline shadow-simulator replay + evaluation (reads ONLY the captured DB — no live APIs).

For each market window and each checkpoint: materialize a known-at-time feature snapshot, run the
model, run the FOK-backed shadow decision engine, persist, and reconcile the final outcome. In Phase
1A (boundary unlocked / diagnostic models) every decision is ``shadow_prediction`` or
``blocked_decision`` — no paper entries. The evaluation summary distinguishes data-availability /
execution-feasibility / model-availability / boundary-readiness / paper-activation-readiness; it
reports NO strategy ROI from diagnostic or blocked decisions.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import decision as D
from . import features as FE
from . import price_to_beat_resolver as P2B
from . import sim_persistence as SP

_SCALE = Decimal(10) ** 18


def _ticks(conn, window_start, window_end):
    rows = conn.execute(
        "SELECT source_ts_ms, recv_ts, value, full_accuracy_value FROM crypto5m_chainlink_ticks "
        "WHERE source_ts_ms BETWEEN ? AND ? ORDER BY source_ts_ms",
        ((window_start - 120) * 1000, window_end * 1000)).fetchall()
    # dedup by source_ts_ms preferring the exact (full_accuracy) value so duplicate-timestamp ticks
    # (snapshot backfill + delta) can't inject a spurious 0/large log-return into realized vol.
    by_src = {}
    for r in rows:
        price = float(Decimal(r["full_accuracy_value"]) / _SCALE) if r["full_accuracy_value"] else r["value"]
        if price is None:
            continue
        cur = by_src.get(r["source_ts_ms"])
        if cur is None or (not cur["_exact"] and r["full_accuracy_value"]):
            by_src[r["source_ts_ms"]] = {"source_ts_ms": r["source_ts_ms"], "recv_ts": r["recv_ts"],
                                         "price": price, "_exact": bool(r["full_accuracy_value"])}
    out = [{"source_ts_ms": t["source_ts_ms"], "recv_ts": t["recv_ts"], "price": t["price"]}
           for t in sorted(by_src.values(), key=lambda t: t["source_ts_ms"])]
    return out


def _latest_book(conn, token_id, decision_ts):
    row = conn.execute(
        "SELECT * FROM crypto5m_book_snapshots WHERE token_id=? AND recv_ts<=? "
        "ORDER BY recv_ts DESC LIMIT 1", (token_id, decision_ts)).fetchone()
    if not row:
        return None
    flags = json.loads(row["quality_flags"] or "[]")
    integrity = "quarantined" if "book_quarantined" in flags else (
        "stale" if "book_stale" in flags else "usable")
    return {"best_bid": row["best_bid"], "best_ask": row["best_ask"],
            "asks": json.loads(row["asks"] or "[]"), "bids": json.loads(row["bids"] or "[]"),
            "book_ts": row["exchange_ts"], "recv_ts": row["recv_ts"], "book_hash": row["book_hash"],
            "book_integrity_status": integrity}


def _fee(conn, condition_id):
    row = conn.execute("SELECT fee_status, fee_rate, id FROM crypto5m_fee_configs "
                       "WHERE condition_id=? ORDER BY id DESC LIMIT 1", (condition_id,)).fetchone()
    if not row:
        return {"fee_status": "fee_unknown", "fee_rate": None}, None
    return {"fee_status": row["fee_status"], "fee_rate": row["fee_rate"]}, row["id"]


def _winner(conn, condition_id):
    row = conn.execute("SELECT winning_outcome, reconciliation_status FROM crypto5m_settlements "
                       "WHERE condition_id=? LIMIT 1", (condition_id,)).fetchone()
    return (row["winning_outcome"], row["reconciliation_status"]) if row else (None, None)


BINANCE_SYMBOL = "BTCUSDT"          # the crypto5m market is BTC; never mix another symbol's ticks in


def _binance_obs(conn, window_start_ts, window_end_ts, *, symbol=BINANCE_SYMBOL):
    """Window Binance observations (predictor lane) for family C/D; fetched slightly before the window
    start to cover the 120s lookback, and SYMBOL-FILTERED so an ETH tick can't become BTC's price. The
    feature layer re-applies the known-at-decision cutoff."""
    lo = (int(window_start_ts) - 130) * 1000
    hi = int(window_end_ts) * 1000
    rows = conn.execute(
        "SELECT symbol, source_event_ts, receive_ts, price, quantity, quote_quantity, trade_count, "
        "delivery_source, raw_payload FROM crypto5m_binance_observations "
        "WHERE symbol = ? AND source_event_ts BETWEEN ? AND ? ORDER BY source_event_ts",
        (symbol, lo, hi)).fetchall()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["raw_payload"]) if r["raw_payload"] else {}
        except (ValueError, TypeError):
            raw = {}
        out.append({"symbol": r["symbol"], "source_event_ts": r["source_event_ts"],
                    "receive_ts": r["receive_ts"], "price": r["price"], "quantity": r["quantity"],
                    "quote_quantity": r["quote_quantity"], "trade_count": r["trade_count"],
                    "delivery_source": r["delivery_source"], "raw": raw})
    return out


def _activation_blockers(conn) -> list:
    blockers = []
    if P2B.LOCKED_BOUNDARY_RULE is None:
        blockers.append("boundary_rule_unvalidated (LOCKED_BOUNDARY_RULE is None)")
    cal = conn.execute("SELECT COUNT(*) FROM crypto5m_model_predictions WHERE calibrated=1").fetchone()[0]
    if cal == 0:
        blockers.append("no calibrated probability model")
    known = conn.execute("SELECT COUNT(*) FROM crypto5m_fee_configs WHERE fee_status='known'").fetchone()[0]
    if known == 0:
        blockers.append("no market with fee_status=known")
    return blockers


def _exec_ts(feature_ts, latency_ms):
    """Execution (fill) time = decision time + modeled latency, in seconds, WITHOUT sub-second
    truncation. ``latency_ms // 1000`` collapsed 250ms / 999ms to 0 (identical to no latency); a float
    division keeps the latency faithful so the FOK fill can reach a sub-second-later book when one exists.
    Book snapshots are second-granular today, so this is behaviourally inert on current data but correct
    when finer book data arrives — never silently drops modeled latency."""
    return feature_ts + latency_ms / 1000.0


def replay(conn, *, model, checkpoints=FE.CHECKPOINTS, latency_ms=0, stake_usdc=10.0, min_edge=0.0,
           market_limit=None, window_start=None, window_end=None, code_commit="", now_ts=0) -> dict:
    SP.ensure_sim_schema(conn)
    q = "SELECT * FROM crypto5m_markets"
    conds, args = [], []
    if window_start is not None:
        conds.append("window_start_ts >= ?"); args.append(window_start)
    if window_end is not None:
        conds.append("window_end_ts <= ?"); args.append(window_end)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY window_start_ts"
    if market_limit:
        q += f" LIMIT {int(market_limit)}"
    markets = conn.execute(q, args).fetchall()

    s = {"windows": 0, "snapshots": 0, "predictions": 0, "settlement_covered": 0,
         "by_status": {}, "by_reason": {}, "snapshot_status_by_checkpoint": {}}
    for m in markets:
        s["windows"] += 1
        ticks = _ticks(conn, m["window_start_ts"], m["window_end_ts"])
        bobs = _binance_obs(conn, m["window_start_ts"], m["window_end_ts"])
        fee, fee_id = _fee(conn, m["condition_id"])           # per-condition; reused for every checkpoint
        winner, _recon = _winner(conn, m["condition_id"])
        if winner:
            s["settlement_covered"] += 1
        params_sig = f"{latency_ms}|{stake_usdc}|{min_edge}"
        for cp in checkpoints:
            feature_ts = m["window_start_ts"] + cp                 # decision-time cutoff (NO latency)
            exec_ts = _exec_ts(feature_ts, latency_ms)             # execution time (the FILL book)
            # FEATURES use the decision-time book (<= cp); the FOK FILL uses the execution book
            # (<= cp+latency). Conflating them would leak a future book into the benchmark feature.
            up_feat = _latest_book(conn, m["up_token_id"], feature_ts)
            down_feat = _latest_book(conn, m["down_token_id"], feature_ts)
            up_fill = _latest_book(conn, m["up_token_id"], exec_ts)
            down_fill = _latest_book(conn, m["down_token_id"], exec_ts)
            # family-G flow: the book as-of FLOW_LOOKBACK_S before the decision (recv_ts <= that instant,
            # so strictly known-at-decision — no look-ahead into the flow window).
            flow_prior_ts = feature_ts - FE.FLOW_LOOKBACK_S
            up_prior = _latest_book(conn, m["up_token_id"], flow_prior_ts)
            down_prior = _latest_book(conn, m["down_token_id"], flow_prior_ts)
            snap = FE.build_feature_snapshot(
                condition_id=m["condition_id"], market_id=m["market_id"],
                window_start_ts=m["window_start_ts"], window_end_ts=m["window_end_ts"], checkpoint=cp,
                chainlink_ticks=ticks, up_book=up_feat, down_book=down_feat, code_commit=code_commit,
                market_rule_hash=m["rule_text_hash"], binance_obs=bobs, fee=fee,
                binance_symbol=BINANCE_SYMBOL, up_book_prior=up_prior, down_book_prior=down_prior)
            fid = SP.upsert_feature_snapshot(conn, snap, now_ts=now_ts)
            s["snapshots"] += 1
            s["snapshot_status_by_checkpoint"].setdefault(cp, {})
            s["snapshot_status_by_checkpoint"][cp][snap["feature_status"]] = \
                s["snapshot_status_by_checkpoint"][cp].get(snap["feature_status"], 0) + 1

            pred = model.predict_up_probability(snap["features"])
            pid = SP.upsert_prediction(conn, {
                "condition_id": m["condition_id"], "checkpoint": cp, "model_id": pred.model_id,
                "model_version": pred.model_version, "calibrated": pred.calibrated, "q_up": pred.q_up,
                "q_down": pred.q_down, "prediction_status": pred.prediction_status,
                "reason": pred.reason, "feature_version": snap["feature_version"]},
                feature_snapshot_id=fid, now_ts=now_ts)
            s["predictions"] += 1

            dec = D.evaluate(
                features=snap, prediction=pred, fee=fee, up_asks=(up_fill or {}).get("asks"),
                down_asks=(down_fill or {}).get("asks"), up_token_id=m["up_token_id"],
                down_token_id=m["down_token_id"], stake_usdc=stake_usdc,
                min_order_size=m["min_order_size"] or 5, min_edge=min_edge,
                up_book_integrity_status=(up_fill or {}).get("book_integrity_status", "usable"),
                down_book_integrity_status=(down_fill or {}).get("book_integrity_status", "usable"))
            SP.upsert_decision(conn, {
                "condition_id": m["condition_id"], "token_id": dec.get("token_id"),
                "window_start_ts": m["window_start_ts"], "checkpoint": cp, "decision_ts": exec_ts,
                "feature_snapshot_id": fid, "prediction_id": pid, "params_sig": params_sig,
                "book_hash": (up_fill or {}).get("book_hash"), "fee_config_id": fee_id,
                "price_to_beat_quality": snap["price_to_beat_quality"], "model_id": pred.model_id,
                "model_version": pred.model_version, "strategy_version": dec["strategy_version"],
                "status": dec["status"], "reason": dec.get("reason"),
                "chosen_side": dec.get("chosen_side"),
                "expected_edge_per_share": dec.get("expected_edge_per_share"),
                "expected_net_pnl": dec.get("expected_net_pnl")}, now_ts=now_ts)
            s["by_status"][dec["status"]] = s["by_status"].get(dec["status"], 0) + 1
            if dec.get("reason"):
                s["by_reason"][dec["reason"]] = s["by_reason"].get(dec["reason"], 0) + 1
            if dec["status"] in ("blocked_decision", "paper_no_trade"):
                SP.record_skip(conn, condition_id=m["condition_id"], checkpoint=cp,
                               decision_ts=exec_ts, reason=dec.get("reason"),
                               strategy_version=dec["strategy_version"], now_ts=now_ts)

    s["paper_entries"] = s["by_status"].get("paper_entry", 0)
    s["diagnostic_scoring"] = score_predictions(conn)
    s["paper_activation_blockers"] = _activation_blockers(conn)
    SP.record_evaluation_run(conn, run_ts=now_ts, model_id=model.model_id,
                             params={"latency_ms": latency_ms, "stake_usdc": stake_usdc,
                                     "min_edge": min_edge, "checkpoints": list(checkpoints)},
                             summary=s, now_ts=now_ts)
    return s


def score_predictions(conn) -> dict:
    """DIAGNOSTIC scoring of recorded shadow forecasts against the final settlement winner — per model:
    Brier + hit-rate over (q_up, label). Settlement-conflict rows are excluded (winner untrusted).
    This is NOT strategy performance (the models are uncalibrated and no entry fired)."""
    rows = conn.execute(
        "SELECT p.model_id, p.q_up, s.winning_outcome FROM crypto5m_model_predictions p "
        "JOIN crypto5m_settlements s ON s.condition_id = p.condition_id "
        "WHERE p.q_up IS NOT NULL AND s.winning_outcome IS NOT NULL "
        "AND s.reconciliation_status != 'conflict'").fetchall()
    agg = {}
    for r in rows:
        label = 1.0 if r["winning_outcome"] == "Up" else 0.0
        a = agg.setdefault(r["model_id"], {"n": 0, "_brier": 0.0, "_hits": 0})
        a["n"] += 1
        a["_brier"] += (r["q_up"] - label) ** 2
        a["_hits"] += int((r["q_up"] >= 0.5) == (label == 1.0))
    return {mid: {"n": a["n"], "brier": (a["_brier"] / a["n"]) if a["n"] else None,
                  "hit_rate": (a["_hits"] / a["n"]) if a["n"] else None,
                  "label": "diagnostic, uncalibrated, NOT strategy performance"}
            for mid, a in agg.items()}


def format_summary(s: dict) -> str:
    lines = [
        "=== Polymarket crypto-5m — Phase 1A shadow-sim evaluation ===",
        "(diagnostic, uncalibrated — NOT strategy performance; no ROI from blocked/diagnostic)",
        f"windows           : {s['windows']}   settlement-covered: {s['settlement_covered']}",
        f"feature snapshots : {s['snapshots']}   model predictions: {s['predictions']}",
        f"paper entries     : {s.get('paper_entries', 0)}  (expected 0 in Phase 1A)",
        "decisions by status:",
    ]
    for st, n in sorted(s["by_status"].items()):
        lines.append(f"  - {st}: {n}")
    lines.append("decisions by reason:")
    for r, n in sorted(s["by_reason"].items()):
        lines.append(f"  - {r}: {n}")
    if s.get("diagnostic_scoring"):
        lines.append("diagnostic forecast scoring (uncalibrated, NOT strategy performance):")
        for mid, sc in s["diagnostic_scoring"].items():
            lines.append(f"  - {mid}: n={sc['n']} brier={sc['brier']} hit_rate={sc['hit_rate']}")
    lines.append("paper activation blockers:")
    for b in s.get("paper_activation_blockers") or ["none"]:
        lines.append(f"  - {b}")
    return "\n".join(lines)
