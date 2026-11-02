#!/usr/bin/env python3
"""Gate 2 validation for the v3.3 flow-scaled-participation lever (DRAFT spec 2026-07-06).

Read-only. Tests whether EXOGENOUS market-wide taker flow, observed early in a window,
predicts the maker's realized PnL on fills made LATER in that same window — i.e. whether
a live engine could have used the signal causally. This replaces the endogenous
fills-per-window finding (fills are a component of PnL) with the market-wide print tape.

Pre-registered pass bar (do not move after looking at results):
  - >= 400 window-campaign observations spanning >= 2 days of prints
  - positive monotone tercile relationship (low < mid < high mean PnL/share)
  - low-flow tercile materially negative

Usage: python3 scripts/crypto5m_flow_validation.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics as st

CAPTURE_DB = "data/crypto5m_capture.sqlite"
EARLY_S = 60          # flow observation: prints in the window's first EARLY_S seconds
CAMPAIGNS = [
    ("regime-v1", "data/crypto5m/maker_v0.sqlite", "maker-regime-v1"),
    ("adaptive-v2", "data/crypto5m/maker_adaptive_v2.sqlite", "maker-adaptive-paper-v2"),
    ("adaptive-v3.1", "data/crypto5m/maker_adaptive_v3_1.sqlite", "maker-adaptive-paper-v3.1"),
    ("v3.2r", "data/crypto5m/maker_adaptive_v3_2r.sqlite", "maker-adaptive-paper-v3.2r"),
    ("v3.2f", "data/crypto5m/maker_adaptive_v3_2f.sqlite", "maker-adaptive-paper-v3.2f"),
]


def early_flow_by_condition() -> tuple[dict[str, dict], float]:
    """condition_id -> {'n': prints, 'notional': $} for prints in the first EARLY_S seconds
    of that condition's 5m window. Window start is derived from the print stream itself
    (min exchange_ts per condition rounded down to the 5m grid)."""
    c = sqlite3.connect(f"file:{CAPTURE_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    lo, hi = c.execute(
        "SELECT MIN(exchange_ts), MAX(exchange_ts) FROM crypto5m_book_events "
        "WHERE event_type='last_trade_price'").fetchone()
    span_days = ((hi or 0) - (lo or 0)) / 86_400_000.0
    flow: dict[str, dict] = {}
    for r in c.execute(
            "SELECT condition_id, exchange_ts, raw_payload FROM crypto5m_book_events "
            "WHERE event_type='last_trade_price'"):
        ts_s = r["exchange_ts"] / 1000.0
        ws = int(ts_s // 300) * 300
        if ts_s - ws > EARLY_S:
            continue
        try:
            p = json.loads(r["raw_payload"])
            notional = float(p["price"]) * float(p["size"])
        except (KeyError, ValueError, json.JSONDecodeError):
            notional = 0.0
        cell = flow.setdefault(r["condition_id"], {"n": 0, "notional": 0.0, "ws": ws})
        cell["n"] += 1
        cell["notional"] += notional
    c.close()
    return flow, span_days


def late_fill_outcomes(db: str, camp: str) -> dict[str, dict]:
    """condition_id -> realized PnL/share and $ on settled, non-hard-cut fills with
    ts > window_start + EARLY_S (the fills a flow gate could still have influenced)."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for f in c.execute(
            """SELECT f.condition_id cid, f.side, f.token, f.price, f.size,
                      w.winning_outcome wo
               FROM maker_fills f
               JOIN maker_windows w ON w.campaign_id=f.campaign_id
                 AND w.condition_id=f.condition_id AND w.window_start_ts=f.window_start_ts
               WHERE f.campaign_id=? AND w.settled=1 AND w.realized_pnl IS NOT NULL
                 AND f.fill_model!='hard-cut' AND f.ts > f.window_start_ts + ?""",
            (camp, EARLY_S)):
        won = 1.0 if (f["wo"] or "").lower() == (f["token"] or "").lower() else 0.0
        per_share = (won - f["price"]) if f["side"] == "bid" else (f["price"] - won)
        cell = out.setdefault(f["cid"], {"pnl": 0.0, "shares": 0.0})
        cell["pnl"] += per_share * f["size"]
        cell["shares"] += f["size"]
    c.close()
    return out


def spearman(x: list[float], y: list[float]) -> float:
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = rank(x), rank(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def main() -> None:
    flow, span_days = early_flow_by_condition()
    print(f"crypto5m flow-lever Gate 2 validation — early flow ({EARLY_S}s) vs later fill PnL")
    print(f"print span: {span_days:.2f} days ({len(flow)} windows with early prints)\n")

    pooled: list[tuple[float, float]] = []
    for name, db, camp in CAMPAIGNS:
        outcomes = late_fill_outcomes(db, camp)
        obs = [(flow[cid]["notional"], o["pnl"] / o["shares"])
               for cid, o in outcomes.items() if cid in flow and o["shares"] > 0]
        pooled.extend(obs)
        if len(obs) < 8:
            print(f"{name:14} n={len(obs):>4}  (too few to summarize)")
            continue
        obs.sort()
        rho = spearman([a for a, _ in obs], [b for _, b in obs])
        t = len(obs) // 3
        ters = [obs[:t], obs[t:2 * t], obs[2 * t:]]
        parts = "  ".join(
            f"{lab}={st.mean([b for _, b in seg]) * 100:+6.2f}c/sh(n={len(seg)})"
            for lab, seg in zip(("low", "mid", "high"), ters))
        print(f"{name:14} n={len(obs):>4} rho={rho:+.3f}  {parts}")

    print(f"\npooled window-campaign observations: {len(pooled)} (pass bar: >=400 AND >=2.0 days)")
    if len(pooled) >= 8:
        pooled.sort()
        rho = spearman([a for a, _ in pooled], [b for _, b in pooled])
        t = len(pooled) // 3
        ters = [pooled[:t], pooled[t:2 * t], pooled[2 * t:]]
        means = [st.mean([b for _, b in seg]) for seg in ters]
        print(f"pooled rho={rho:+.3f}  terciles: low={means[0]*100:+.2f}c  mid={means[1]*100:+.2f}c  high={means[2]*100:+.2f}c /share")
        monotone = means[0] < means[1] < means[2]
        # "materially negative" pinned to -0.5c/share (same number as the spec) so the
        # pre-registration is a real threshold, not any-negative.
        MATERIAL = -0.005
        if len(pooled) >= 400 and span_days >= 2.0:
            verdict = "PASS" if (monotone and means[0] < MATERIAL) else "FAIL"
            print(f"GATE 2 VERDICT: {verdict} (monotone={monotone}, low-tercile {means[0]*100:+.2f}c < {MATERIAL*100:.1f}c={means[0] < MATERIAL})")
        else:
            print("GATE 2: INSUFFICIENT DATA — accumulating; result above is NOT evidence.")


if __name__ == "__main__":
    main()
