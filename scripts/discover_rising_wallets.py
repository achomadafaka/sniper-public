#!/usr/bin/env python3
"""Discover FRESH, RISING sports wallets — small/mid size, high ROI, recent momentum — instead
of the big established whales (which churn, straddle, or move markets you can't follow).

Funnel: Polymarket SPORTS leaderboard, WEEK + MONTH, deep (offset 0..300), ranked by ROI
(PnL/volume) inside a TRADEABLE volume band, with recent (week) momentum, excluding all-time
whales. ROI is the discriminator: a $122k-PnL / $162k-vol wallet (75% ROI) is a sharp picker; a
$1.8M-PnL / $135M-vol wallet (1.3% ROI) is a churner with no copyable edge. Read-only discovery
feeding dossiers. NOT live trading."""
import sys, os, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import polymarket_api as P

VOL_MIN, VOL_MAX = 50_000.0, 5_000_000.0      # tradeable band: not noise, not whale/churner
ROI_MIN = 0.20                                # efficient picker floor (20%+)
PNL_MIN = 20_000.0                            # meaningful, not luck-noise


def board(sess, period, n=300):
    rows = {}
    for off in range(0, n, 50):
        try:
            r = P.fetch_leaderboard_rankings(sess, category="SPORTS", time_period=period,
                                             order_by="PNL", limit=50, offset=off)
        except Exception as e:  # noqa: BLE001
            print(f"  {period} off{off} ERR {type(e).__name__}", file=sys.stderr); break
        if not r:
            break
        for x in r:
            w = (x.get("wallet") or "").lower()
            if w:
                rows[w] = {"pnl": float(x.get("official_pnl") or 0), "vol": float(x.get("official_volume") or 0),
                           "name": x.get("username") or ""}
    return rows


def main():
    sess = P.build_session()
    month = board(sess, "MONTH"); week = board(sess, "WEEK")
    # all-time whales to exclude (already arrived; copying them is pointless)
    whales = set()
    for off in (0, 50, 100):
        for x in P.fetch_leaderboard_rankings(sess, category="OVERALL", time_period="ALL", limit=50, offset=off):
            whales.add((x.get("wallet") or "").lower())
    print(f"month={len(month)} week={len(week)} whales={len(whales)}", file=sys.stderr)

    cand = []
    for w, m in month.items():
        roi = m["pnl"] / m["vol"] if m["vol"] else 0.0
        if not (VOL_MIN <= m["vol"] <= VOL_MAX):
            continue
        if roi < ROI_MIN or m["pnl"] < PNL_MIN:
            continue
        if w in whales:
            continue
        wk = week.get(w)
        wk_pnl = wk["pnl"] if wk else 0.0
        momentum = wk_pnl / m["pnl"] if m["pnl"] else 0.0      # frac of month PnL earned this week
        cand.append({"wallet": w, "name": m["name"], "month_pnl": round(m["pnl"]),
                     "vol": round(m["vol"]), "roi": round(roi, 3),
                     "week_pnl": round(wk_pnl), "week_active": wk is not None,
                     "momentum": round(momentum, 2)})

    # rank: efficient AND recently active first; ROI primary, week-presence + momentum tie-break
    cand.sort(key=lambda c: (c["week_active"], c["roi"], c["momentum"]), reverse=True)
    print(f"\n{'wallet':44}{'name':18}{'month_pnl':>11}{'vol':>11}{'roi':>7}{'wk_pnl':>10}{'mom':>6}")
    print("-" * 110)
    for c in cand[:30]:
        print(f"{c['wallet']:44}{(c['name'] or '')[:17]:18}{c['month_pnl']:>11,}{c['vol']:>11,}"
              f"{c['roi']*100:>6.0f}%{c['week_pnl']:>10,}{c['momentum']:>6.2f}")
    out = os.path.join(ROOT, "runs", "discovery_rising_candidates.json")
    with open(out, "w") as f:
        json.dump({"band": {"vol": [VOL_MIN, VOL_MAX], "roi_min": ROI_MIN, "pnl_min": PNL_MIN},
                   "candidates": cand}, f, indent=2)
    print(f"\nwrote {out} ({len(cand)} candidates in band)")


if __name__ == "__main__":
    main()
