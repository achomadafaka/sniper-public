#!/usr/bin/env python3
"""Discover candidate winning Hyperliquid PERP traders — the HL analog of
discover_rising_wallets.py. Pulls the public HL leaderboard, ranks by recent ROI (return on
account value) inside a TRADEABLE account-size band, requires sustained (all-time positive)
performance, surfaces mid-size risers (not $50M whales, not dust). Read-only discovery; output
feeds HL wallet analysis + strategy research. NOT live trading.

HL specifics vs Polymarket: ROI is return on ACCOUNT VALUE (leverage-adjusted), not pnl/volume;
a strong perp trader shows 15-100%+ MONTHLY roi on a mid account. Like Polymarket, huge accounts
churning at ~1-2% ROI are not copyable edge — recent ROI on modest size is the discriminator."""
import sys, os, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import hyperliquid_api as HL

ACCT_MIN, ACCT_MAX = 50_000.0, 5_000_000.0     # tradeable: not dust, not whale
MONTH_ROI_MIN = 0.15                           # 15%+ monthly return on account value
MONTH_PNL_MIN = 20_000.0


def main():
    rows = HL.fetch_leaderboard()
    print(f"leaderboard rows: {len(rows)}", file=sys.stderr)
    cand = []
    for r in rows:
        av = r["account_value"]
        m = r["month"]; a = r["all_time"]; wk = r["week"]
        if not (ACCT_MIN <= av <= ACCT_MAX):
            continue
        # ACTUAL account return (the HL `roi` field is leverage-distorted: huge % on a tiny
        # deployed base). month_pnl/account_value is the honest, comparable return.
        acct_ret = m["pnl"] / av if av else 0.0
        if acct_ret < MONTH_ROI_MIN or m["pnl"] < MONTH_PNL_MIN:
            continue
        if a["pnl"] <= 0:                       # sustained, not a one-month fluke
            continue
        if wk["roi"] < -0.05:                   # drop accounts currently BLOWING UP (hot month, crashing week)
            continue
        cand.append({"wallet": r["wallet"], "name": r["name"], "account_value": round(av),
                     "acct_return": round(acct_ret, 3), "month_pnl": round(m["pnl"]),
                     "month_vlm": round(m["vlm"]), "week_roi": round(wk["roi"], 3),
                     "all_time_pnl": round(a["pnl"])})
    # rank by ACTUAL account return (recent skill), tie-break sustained all-time PnL
    cand.sort(key=lambda c: (c["acct_return"], c["all_time_pnl"]), reverse=True)
    print(f"{'wallet':44}{'name':16}{'acct$':>11}{'moRet%':>8}{'moPnL':>10}{'wkROI':>7}{'allPnL':>11}")
    print("-" * 108)
    for c in cand[:30]:
        print(f"{c['wallet']:44}{(c['name'] or '')[:15]:16}{c['account_value']:>11,}"
              f"{c['acct_return']*100:>7.0f}%{c['month_pnl']:>10,}{c['week_roi']*100:>6.0f}%{c['all_time_pnl']:>11,}")
    out = os.path.join(ROOT, "runs", "discovery_hl_candidates.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"band": {"acct": [ACCT_MIN, ACCT_MAX], "month_roi_min": MONTH_ROI_MIN},
                   "candidates": cand}, f, indent=2)
    print(f"\nwrote {out} ({len(cand)} candidates in band)")


if __name__ == "__main__":
    main()
