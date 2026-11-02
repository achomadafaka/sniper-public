#!/usr/bin/env python3
"""Per-wallet edge-location breakdown for Hyperliquid traders — the perp analog of
wallet breakdown scripts. From a wallet's fills (closed round-trips): per-COIN and per-SIDE
(long/short) win rate / profit factor / realized PnL, PnL concentration by coin, and a recent-vs-
older split (is the edge improving or decaying?). Tells us WHERE a clean trader's edge lives
before any strategy validation. Default: runs the CLEAN set from screen_hl_clean.json; or pass a wallet
address. Read-only. NOT live trading."""
import sys, os, json
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import hyperliquid_api as HL


def _f(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def _stats(pnls):
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p < 0]
    gl = -sum(losses)
    return {"n": len(pnls), "wr": round(len(wins) / len(pnls), 2),
            "pf": round(sum(wins) / gl, 2) if gl > 0 else 999.0,
            "realized": round(sum(pnls), 0)}


def breakdown(wallet):
    fills = HL.fetch_user_fills(wallet)
    closes = [f for f in fills if str(f.get("dir", "")).startswith("Close")]
    if not closes:
        return None
    closes.sort(key=lambda f: int(f.get("time") or 0))
    by_coin = defaultdict(list); by_side = defaultdict(list)
    for f in closes:
        pnl = _f(f.get("closedPnl"))
        by_coin[f.get("coin")].append(pnl)
        by_side["Long" if "Long" in str(f.get("dir", "")) else "Short"].append(pnl)
    mid = len(closes) // 2
    older = _stats([_f(f.get("closedPnl")) for f in closes[:mid]])
    recent = _stats([_f(f.get("closedPnl")) for f in closes[mid:]])
    coin_real = {c: sum(p) for c, p in by_coin.items()}
    total_real = sum(coin_real.values()) or 1.0
    top_coin = max(coin_real.items(), key=lambda kv: kv[1])
    return {"coins": {c: _stats(p) for c, p in by_coin.items()},
            "sides": {s: _stats(p) for s, p in by_side.items()},
            "older": older, "recent": recent,
            "top_coin": top_coin[0], "top_coin_share": round(top_coin[1] / total_real, 2)}


def _line(label, s):
    if not s:
        return f"    {label:14} —"
    return f"    {label:14} n={s['n']:>4} WR={s['wr']:.2f} PF={s['pf']:>6.2f} realized=${s['realized']:>12,.0f}"


def main():
    if len(sys.argv) > 1:
        wallets = [sys.argv[1].lower()]
    else:
        wallets = [c["wallet"] for c in json.load(open(os.path.join(ROOT, "runs", "screen_hl_clean.json")))["clean"]]
    for w in wallets:
        b = breakdown(w)
        print(f"\n=== {w} ===")
        if not b:
            print("  no closed round-trips"); continue
        print(f"  top coin: {b['top_coin']} ({b['top_coin_share']:.0%} of realized PnL)  "
              f"| trajectory older->recent:")
        print(_line("older half", b["older"])); print(_line("recent half", b["recent"]))
        print("  by side:")
        for s in ("Long", "Short"):
            print(_line(s, b["sides"].get(s)))
        print("  top coins by |realized|:")
        top = sorted(b["coins"].items(), key=lambda kv: -abs(kv[1]["realized"]))[:6]
        for c, s in top:
            print(_line(c, s))


if __name__ == "__main__":
    main()
