#!/usr/bin/env python3
"""Fills-based skill screen for Hyperliquid trader candidates — the perp analog of
screen_rising_mechanics.py. The leaderboard ROI is leverage-distorted, so skill is assessed from
the wallet's actual round-trips: enough sample, profit factor > 1, PnL NOT from one lucky
leveraged hit (concentration), and a real win rate. Reads runs/discovery_hl_candidates.json;
one userFills call per wallet. Read-only. NOT live trading.

HL fills carry `dir` (Open/Close Long/Short) + `closedPnl` (realized on close) + `fee`. A 'Close'
fill is a realized round-trip leg. Mirrors the Polymarket lesson: many independent wins = skill;
few dominant hits = leveraged variance."""
import sys, os, json, statistics
from collections import Counter, defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import hyperliquid_api as HL

TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CAND_PATH = os.path.join(ROOT, "runs", "discovery_hl_candidates.json")


def _f(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def screen_wallet(wallet):
    try:
        fills = HL.fetch_user_fills(wallet)
    except Exception as e:  # noqa: BLE001
        return {"err": type(e).__name__}
    closes = [f for f in fills if str(f.get("dir", "")).startswith("Close")]
    if len(closes) < 5:
        return {"err": "too_few_closes", "n_fills": len(fills)}
    pnls = [_f(f.get("closedPnl")) for f in closes]
    fees = sum(_f(f.get("fee")) for f in fills)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins); gross_loss = -sum(losses)
    realized = sum(pnls) - fees
    n = len(closes)
    coins = Counter(f.get("coin") for f in closes)
    # dominant-coin WR: per-coin pnl, pick the coin with the most realized PnL, compute its WR.
    # WR~1.0 on the dominant coin = mechanical grid/MM (books closedPnl as wins), NOT a directional
    # edge — the breakdown exposed 0x906d31b2's 96%-ETH-at-WR-1.0 that the aggregate WR 0.60 hid.
    coin_pnls = defaultdict(list)
    for f in closes:
        coin_pnls[f.get("coin")].append(_f(f.get("closedPnl")))
    dom = max(coin_pnls.items(), key=lambda kv: sum(kv[1]))
    dom_pnls = dom[1]
    dom_wr = round(sum(1 for p in dom_pnls if p > 0) / len(dom_pnls), 2) if dom_pnls else 0.0
    dom_share = round(sum(dom_pnls) / realized, 2) if realized > 0 else 999.0
    longs = sum(1 for f in closes if "Long" in str(f.get("dir", "")))
    max_loss = -min(losses) if losses else 0.0
    # loss-tail fragility: worst single loss vs net realized PnL. >1 means one bad trade already
    # wiped (or would wipe) all net profit — martingale/grid tail risk even at a high win rate.
    loss_tail = round(max_loss / realized, 2) if realized > 0 else 999.0
    return {
        "n_closed": n, "n_fills": len(fills),
        "win_rate": round(len(wins) / n, 3),
        "realized_pnl": round(realized, 0),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else 999.0,
        "top_trade_share": round(max(wins) / gross_win, 3) if gross_win > 0 else 1.0,
        "max_loss": round(max_loss, 0), "loss_tail": loss_tail,
        "n_coins": len(coins),
        "dom_coin": dom[0], "dom_wr": dom_wr, "dom_share": dom_share,
        "long_share": round(longs / n, 2),
        "median_pnl": round(statistics.median(pnls), 0),
    }


def verdict(m):
    # CLEAN: enough round-trips, makes money (profit factor), NOT one lucky leveraged hit, real WR.
    if "err" in m:
        return "ERR"
    ok = (m["n_closed"] >= 30 and m["profit_factor"] >= 1.2 and m["top_trade_share"] <= 0.40
          and m["realized_pnl"] > 0 and 0.35 <= m["win_rate"] <= 0.95
          and m["loss_tail"] <= 1.0          # worst single loss must not exceed net realized PnL
          and m["dom_wr"] <= 0.90)           # dominant coin WR~1.0 = grid/MM, not a directional edge
    return "CLEAN" if ok else "skip"


def main():
    cands = json.load(open(CAND_PATH))["candidates"][:TOP_N]
    print(f"{'wallet':44}{'nClose':>7}{'WR':>6}{'PF':>6}{'topShr':>7}{'lossTail':>9}{'domCoin':>8}{'domWR':>6}{'long%':>6}{'realPnL':>11}  verdict")
    print("-" * 134)
    clean = []
    for c in cands:
        m = screen_wallet(c["wallet"])
        if "err" in m:
            print(f"{c['wallet']:44}  {m['err']}")
            continue
        v = verdict(m)
        print(f"{c['wallet']:44}{m['n_closed']:>7}{m['win_rate']:>6.2f}{m['profit_factor']:>6.2f}"
              f"{m['top_trade_share']:>7.2f}{m['loss_tail']:>9.2f}{str(m['dom_coin'])[:7]:>8}{m['dom_wr']:>6.2f}"
              f"{m['long_share']*100:>5.0f}%{m['realized_pnl']:>11,.0f}  {v}")
        if v == "CLEAN":
            clean.append({**c, **m})
    out = os.path.join(ROOT, "runs", "screen_hl_clean.json")
    with open(out, "w") as f:
        json.dump({"clean": clean}, f, indent=2)
    print(f"\nCLEAN HL traders: {len(clean)} -> {out}")
    for e in clean:
        print(f"  {e['wallet']}  acct=${e['account_value']:,} moRet={e['acct_return']*100:.0f}% "
              f"nClose={e['n_closed']} WR={e['win_rate']:.2f} PF={e['profit_factor']} topShr={e['top_trade_share']}")


if __name__ == "__main__":
    main()
