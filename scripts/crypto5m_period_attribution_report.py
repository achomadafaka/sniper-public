#!/usr/bin/env python3
"""crypto5m_period_attribution_report.py — deterministic read-only gate report.

Pre-registered analysis code for the learner tournament
(docs/superpowers/specs/2026-07-06-crypto5m-learner-tournament-preregistration.md).
Written and frozen BEFORE the forward sample accrued so the gate read has no
flexible-analysis wiggle room. Run ONLY at the gate. No cron. No UI. No writes.

Output: one row per campaign_pair x paired settled window (CSV to reports/), then
grouped summaries per pre-registered bucket dimension printed to stdout.

All timestamps are engine/Binance time (window_start_ts). The host wall clock runs
~+7h fast — this script never calls time.time() for anything that matters; even the
output filename is stamped from the DATA's newest window.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CAPTURE_DB = os.path.join(_ROOT, "data", "crypto5m_capture.sqlite")
MAKER_V0_DB = os.path.join(_ROOT, "data", "crypto5m", "maker_v0.sqlite")
C5M = os.path.join(_ROOT, "data", "crypto5m")

# Tournament comparison set — mirrors the pre-registration doc. (campaign_a, campaign_b):
# delta is always a − b.
DEFAULT_PAIRS = (
    ("maker-adaptive-paper-v3.2f-frozen", os.path.join(C5M, "maker_adaptive_v3_2f_frozen.sqlite"),
     "maker-adaptive-paper-v3.2f", os.path.join(C5M, "maker_adaptive_v3_2f.sqlite")),
    ("maker-adaptive-paper-v3.2f-frozen", os.path.join(C5M, "maker_adaptive_v3_2f_frozen.sqlite"),
     "maker-regime-v1", MAKER_V0_DB),
    ("maker-adaptive-paper-v3.2f", os.path.join(C5M, "maker_adaptive_v3_2f.sqlite"),
     "maker-regime-v1", MAKER_V0_DB),
    ("maker-adaptive-paper-v3.2r", os.path.join(C5M, "maker_adaptive_v3_2r.sqlite"),
     "maker-regime-v1", MAKER_V0_DB),
)

GROUP_DIMS = ("jump_bucket", "vol_bucket", "print_bucket", "odds_bucket",
              "spread_bucket", "arm_bucket", "winner_inventory_bucket")


def _conn(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _settled_windows(c, campaign):
    return {
        int(r["window_start_ts"]): r
        for r in c.execute(
            """SELECT condition_id, window_start_ts, realized_pnl, up_inv, down_inv, winning_outcome
               FROM maker_windows WHERE settled=1 AND campaign_id=? AND realized_pnl IS NOT NULL""",
            (campaign,),
        )
    }


def _jump_share(c, campaign, ws):
    """Registered jump-share source: maker_regime_decisions.regime_id='jump' (same source as
    the codex exploratory proof; exists for ALL campaigns incl. control). Fallback to the v3
    policy-action context_key vol field when regime decisions are retention-pruned (7d)."""
    try:
        r = c.execute(
            """SELECT COUNT(*) n, SUM(CASE WHEN regime_id='jump' THEN 1 ELSE 0 END) j
               FROM maker_regime_decisions WHERE campaign_id=? AND window_start_ts=?""",
            (campaign, ws),
        ).fetchone()
        if r and r["n"]:
            return r["j"] / r["n"], "regime_decisions"
    except sqlite3.OperationalError:
        pass
    try:
        r = c.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN context_key LIKE '%|jump|%' THEN 1 ELSE 0 END) j
               FROM crypto5m_maker_v3_policy_actions
               WHERE campaign_id=? AND window_start_ts=?""",
            (campaign, ws),
        ).fetchone()
        if r and r["n"]:
            return r["j"] / r["n"], "policy_actions_fallback"
    except sqlite3.OperationalError:
        pass
    return None, None


def _policy_mix(c, campaign, ws):
    """(arm_mix, risk_mix) from v3 policy actions; ({},{}) for non-v3 campaigns."""
    try:
        rows = c.execute(
            """SELECT quote_action_arm, risk_state
               FROM crypto5m_maker_v3_policy_actions
               WHERE campaign_id=? AND window_start_ts=?""",
            (campaign, ws),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}, {}
    arms: dict = {}
    risks: dict = {}
    for r in rows:
        arms[r["quote_action_arm"]] = arms.get(r["quote_action_arm"], 0) + 1
        risks[r["risk_state"]] = risks.get(r["risk_state"], 0) + 1
    return arms, risks


def _fills(c, campaign, cid):
    """(fill_shares, claimed_edge, n_fills_with_quote) — claimed edge via quote_id -> fair."""
    shares = 0.0
    claimed = None
    n_quoted = 0
    try:
        rows = c.execute(
            """SELECT f.side, f.price, f.size, q.fair
               FROM maker_fills f LEFT JOIN maker_quotes q ON q.id = f.quote_id
               WHERE f.campaign_id=? AND f.condition_id=?""",
            (campaign, cid),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0.0, None, 0
    for r in rows:
        size = float(r["size"] or 0.0)
        shares += size
        if r["fair"] is None:
            continue
        n_quoted += 1
        edge = (float(r["fair"]) - float(r["price"])) * size
        if str(r["side"]).lower() in ("sell", "ask"):
            edge = -edge
        claimed = (claimed or 0.0) + edge
    return shares, claimed, n_quoted


def _posted_and_spread_and_odds(c, campaign, ws):
    """(posted_shares, avg_spread, mean_up_fair) from maker_quotes; Nones after retention prune."""
    try:
        r = c.execute(
            """SELECT SUM(CASE WHEN status='posted' THEN size END) posted,
                      AVG(CASE WHEN best_ask IS NOT NULL AND best_bid IS NOT NULL
                               THEN best_ask - best_bid END) spread,
                      AVG(CASE WHEN token='up' THEN fair END) up_fair
               FROM maker_quotes WHERE campaign_id=? AND window_start_ts=?""",
            (campaign, ws),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None, None
    if not r:
        return None, None, None
    # each field independently: zero POSTED rows must not blank spread/odds, which any
    # quote snapshot (incl. pulled) can still supply
    return (float(r["posted"]) if r["posted"] is not None else None,
            float(r["spread"]) if r["spread"] is not None else None,
            float(r["up_fair"]) if r["up_fair"] is not None else None)


def _binance_window(cap, ws):
    """(vol, range_frac) over [ws, ws+300] from capture binance observations; Nones if absent."""
    if cap is None:
        return None, None
    try:
        rows = cap.execute(
            """SELECT source_event_ts, price FROM crypto5m_binance_observations
               WHERE source_event_ts >= ? AND source_event_ts < ? AND price > 0
               ORDER BY source_event_ts""",
            (ws * 1000, (ws + 300) * 1000),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    prices = [float(r["price"]) for r in rows]
    if len(prices) < 3:
        return None, None
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0]
    mu = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1))
    rng = (max(prices) - min(prices)) / (sum(prices) / len(prices))
    return vol, rng


def _prints(cap, cid, ws):
    """(count, notional) of last_trade_price prints for this market in-window.
    Zero-vs-pruned disambiguation: if the capture has ANY book event for this market
    in-window, zero prints is a genuine 0; if it has none at all, retention pruned the
    window (or capture was down) and both fields are None."""
    if cap is None or not cid:
        return None, None
    try:
        any_events = cap.execute(
            """SELECT 1 FROM crypto5m_book_events
               WHERE condition_id=? AND exchange_ts >= ? AND exchange_ts < ? LIMIT 1""",
            (cid, ws * 1000, (ws + 300) * 1000),
        ).fetchone()
        if not any_events:
            return None, None
        rows = cap.execute(
            """SELECT raw_payload FROM crypto5m_book_events
               WHERE condition_id=? AND event_type='last_trade_price'
                 AND exchange_ts >= ? AND exchange_ts < ?""",
            (cid, ws * 1000, (ws + 300) * 1000),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    count = len(rows)
    notional = 0.0
    for r in rows:
        try:
            p = json.loads(r["raw_payload"])
            notional += float(p.get("price") or 0.0) * float(p.get("size") or 0.0)
        except (ValueError, TypeError):
            pass
    return count, round(notional, 2)


def pair_rows(conn_a, camp_a, conn_b, camp_b, cap):
    """One dict per paired settled window (matched on window_start_ts). Pure given connections."""
    wa = _settled_windows(conn_a, camp_a)
    wb = _settled_windows(conn_b, camp_b)
    out = []
    for ws in sorted(wa.keys() & wb.keys()):
        ra, rb = wa[ws], wb[ws]
        pnl_a, pnl_b = float(ra["realized_pnl"]), float(rb["realized_pnl"])
        jump, jump_source = _jump_share(conn_a, camp_a, ws)
        if jump is None:
            jump, jump_source = _jump_share(conn_b, camp_b, ws)
        arms, risks = _policy_mix(conn_a, camp_a, ws)
        if not arms:
            arms, risks = _policy_mix(conn_b, camp_b, ws)
        posted, spread, up_fair = _posted_and_spread_and_odds(conn_a, camp_a, ws)
        fill_a, claimed, _nq = _fills(conn_a, camp_a, ra["condition_id"])
        fill_b, claimed_b, _ = _fills(conn_b, camp_b, rb["condition_id"])
        vol, rng = _binance_window(cap, ws)
        pc, pn = _prints(cap, ra["condition_id"], ws)
        win = str(ra["winning_outcome"] or "").lower()
        up_inv, down_inv = float(ra["up_inv"] or 0.0), float(ra["down_inv"] or 0.0)
        winner_inv = up_inv if win == "up" else down_inv if win == "down" else None
        loser_inv = down_inv if win == "up" else up_inv if win == "down" else None
        out.append({
            "window_start_ts": ws,
            "campaign_a": camp_a, "campaign_b": camp_b,
            "pnl_a": round(pnl_a, 2), "pnl_b": round(pnl_b, 2),
            "delta": round(pnl_a - pnl_b, 2),
            "jump_share": round(jump, 4) if jump is not None else None,
            "jump_source": jump_source,
            "jump_bucket": (None if jump is None else
                            "calm" if jump == 0 else "moderate" if jump < 0.10 else "high"),
            "binance_vol": round(vol, 8) if vol is not None else None,
            "binance_range": round(rng, 6) if rng is not None else None,
            "print_count": pc, "print_notional": pn,
            "avg_spread": round(spread, 4) if spread is not None else None,
            "mean_up_fair": round(up_fair, 4) if up_fair is not None else None,
            "odds_bucket": (None if up_fair is None else
                            "mid" if 0.35 <= up_fair <= 0.65 else "extreme"),
            "arm_mix": json.dumps(arms, sort_keys=True),
            "risk_mix": json.dumps(risks, sort_keys=True),
            "base_small_share": (round(arms.get("base_small", 0) / sum(arms.values()), 4)
                                 if arms else None),
            "posted_shares": round(posted, 2) if posted is not None else None,
            "fill_shares_a": round(fill_a, 2), "fill_shares_b": round(fill_b, 2),
            "winner_inventory": round(winner_inv, 2) if winner_inv is not None else None,
            "loser_inventory": round(loser_inv, 2) if loser_inv is not None else None,
            "claimed_edge": round(claimed, 2) if claimed is not None else None,
            "claimed_minus_realized": (round(claimed - pnl_a, 2)
                                       if claimed is not None else None),
            "claimed_edge_b": round(claimed_b, 2) if claimed_b is not None else None,
            "claimed_minus_realized_b": (round(claimed_b - pnl_b, 2)
                                         if claimed_b is not None else None),
        })
    return out


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else None


def bucketize(rows):
    """Add sample-relative buckets (median/tercile splits) — deterministic given the rows."""
    vols = [r["binance_vol"] for r in rows if r["binance_vol"] is not None]
    v_lo = _median([v for v in vols if v <= _median(vols)]) if vols else None
    v_hi = _median([v for v in vols if v >= _median(vols)]) if vols else None
    p_med = _median([r["print_count"] for r in rows if r["print_count"] is not None])
    s_med = _median([r["avg_spread"] for r in rows if r["avg_spread"] is not None])
    a_med = _median([r["base_small_share"] for r in rows if r["base_small_share"] is not None])
    w_med = _median([r["winner_inventory"] for r in rows if r["winner_inventory"] is not None])
    for r in rows:
        v = r["binance_vol"]
        r["vol_bucket"] = (None if v is None or v_lo is None else
                           "low" if v <= v_lo else "high" if v >= v_hi else "mid")
        r["print_bucket"] = (None if r["print_count"] is None or p_med is None else
                             "high" if r["print_count"] > p_med else "low")
        r["spread_bucket"] = (None if r["avg_spread"] is None or s_med is None else
                              "wide" if r["avg_spread"] > s_med else "tight")
        r["arm_bucket"] = (None if r["base_small_share"] is None or a_med is None else
                           "prior_heavy" if r["base_small_share"] >= a_med else "drifted")
        r["winner_inventory_bucket"] = (None if r["winner_inventory"] is None or w_med is None else
                                        "high" if r["winner_inventory"] > w_med else "low")
    return rows


def group_summary(rows, dim):
    groups: dict = {}
    for r in rows:
        key = r.get(dim)
        if key is None:
            key = "n/a"
        g = groups.setdefault(key, {"n": 0, "delta_sum": 0.0, "a_sum": 0.0, "b_sum": 0.0})
        g["n"] += 1
        g["delta_sum"] += r["delta"]
        g["a_sum"] += r["pnl_a"]
        g["b_sum"] += r["pnl_b"]
    out = {}
    for k, g in sorted(groups.items()):
        out[k] = {"n": g["n"], "delta_mean": round(g["delta_sum"] / g["n"], 3),
                  "delta_total": round(g["delta_sum"], 2),
                  "a_mean": round(g["a_sum"] / g["n"], 3), "b_mean": round(g["b_sum"] / g["n"], 3)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forward-anchor", type=int, default=None,
                    help="only windows with window_start_ts >= this ENGINE-time anchor "
                         "(the gate read passes the comparison's registered anchor)")
    ap.add_argument("--capture-db", default=CAPTURE_DB)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cap = _conn(args.capture_db) if os.path.exists(args.capture_db) else None
    all_rows = []
    for camp_a, db_a, camp_b, db_b in DEFAULT_PAIRS:
        if not (os.path.exists(db_a) and os.path.exists(db_b)):
            print(f"[skip] {camp_a} vs {camp_b}: db missing", file=sys.stderr)
            continue
        ca, cb = _conn(db_a), _conn(db_b)
        rows = pair_rows(ca, camp_a, cb, camp_b, cap)
        if args.forward_anchor:
            rows = [r for r in rows if r["window_start_ts"] >= args.forward_anchor]
        all_rows.extend(bucketize(rows))
        ca.close(); cb.close()

    if not all_rows:
        print("no paired settled windows", file=sys.stderr)
        return 1

    newest = max(r["window_start_ts"] for r in all_rows)
    stamp = datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d")
    out_path = args.out or os.path.join(_ROOT, "reports",
                                        f"crypto5m_period_attribution_{stamp}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    keys = list(all_rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"wrote {len(all_rows)} pair-window rows -> {out_path}\n")

    pairs = sorted({(r["campaign_a"], r["campaign_b"]) for r in all_rows})
    for pa, pb in pairs:
        rows = [r for r in all_rows if r["campaign_a"] == pa and r["campaign_b"] == pb]
        print(f"=== {pa}  minus  {pb}  (n={len(rows)}) ===")
        for dim in GROUP_DIMS:
            summary = group_summary(rows, dim)
            cells = "  ".join(f"{k}: n={v['n']} Δ={v['delta_mean']:+.2f}/w"
                              for k, v in summary.items())
            print(f"  {dim:26s} {cells}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
