#!/usr/bin/env python3
"""Backfill BTC 5m bars from Binance and sweep the signal/param grid with a train/OOS split.

Honest, cost-adjusted (1-bar execution lag; maker/taker fee preset). Selection is on TRAIN ONLY —
gates + ranks by train Sharpe subject to (profit_factor>1.1, max_dd<=0.25, n_trades>=30) — then
reports the chosen config's OOS metrics UNTOUCHED. Ranking by the OOS slice you report is selection
bias (it makes the holdout in-sample to the picker), so we never do that. Writes the chosen config
to runs/btc5m/best_config.json. Read-only on Binance; caches to btc5m_bars.

Usage: python3 scripts/btc5m_backtest.py [--days 45] [--symbol BTCUSDT]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.btc5m import data as D, signals as S, backtest as BT  # noqa: E402

RUNS = os.path.join(ROOT, "runs", "btc5m")

GRID = [
    ("ema_crossover", {"fast": f, "slow": s}) for f, s in [(5, 20), (8, 34), (12, 26), (20, 50)]
] + [
    ("donchian_breakout", {"n": n}) for n in [12, 20, 36, 55]
] + [
    ("meanrev_zscore", {"n": n, "entry": e, "exit_z": x})
    for n in [20, 36] for e in [1.5, 2.0, 2.5] for x in [0.3, 0.5]
]


def backfill(symbol: str, days: int) -> list[dict]:
    import db
    conn = db.get_connection()
    sess = __import__("requests").Session()
    now = D.server_time_ms(sess)
    start = now - days * 24 * 60 * 60 * 1000
    cursor, all_new = start, 0
    while cursor < now:
        page = D.fetch_klines(symbol, start_ms=cursor, end_ms=now, limit=D.MAX_LIMIT, session=sess)
        if not page:
            break
        cursor = page[-1]["open_ms"] + D.INTERVAL_MS                  # advance from the raw last bar
        closed = [b for b in page if int(b["close_ms"]) <= now]       # drop Binance's in-progress candle
        all_new += D.save_bars(conn, closed)                          # never cache an unclosed bar (partial OHLC)
        print(f"  fetched to {page[-1]['open_ms']} (+{len(closed)})", file=sys.stderr)
        time.sleep(0.2)
    # Defensive tail-trim: a stale in-progress bar from a pre-fix run could linger in the cache; never
    # let an unclosed bar contaminate the last train/OOS bar.
    bars = [b for b in D.load_bars(conn, symbol) if int(b["close_ms"]) <= now]
    print(f"backfilled {all_new} new; {len(bars)} closed bars", file=sys.stderr)
    return bars


# Cost presets. 'maker' = resting limit-order entries (you provide liquidity); realistic for
# mean-reversion fading. 'taker' = market orders (crosses the spread); realistic for breakout/trend.
# NOTE: maker assumes the limit order fills at the band — live fill rate is < 100% (adverse
# selection), so forward results will be below backtest. The forward harness measures real fills.
COST = {"maker": {"taker_fee": 0.0002, "slippage": 0.0}, "taker": {"taker_fee": 0.0005, "slippage": 0.0002}}


def evaluate(bars: list[dict], cost: dict) -> list[dict]:
    train, oos = BT.split(bars, 0.7)
    rows = []
    for name, params in GRID:
        fn = S.SIGNALS[name]
        tr = BT.backtest(train, fn(train, **params), **cost)         # execution_lag=1 default (honest fill)
        oo = BT.backtest(oos, fn(oos, **params), **cost)
        rows.append({"signal": name, "params": params,
                     "train_sharpe": tr["sharpe"], "train_ret": tr["total_return"],
                     "train_pf": tr["profit_factor"], "train_dd": tr["max_drawdown"],
                     "train_trades": tr["n_trades"],
                     "oos_sharpe": oo["sharpe"], "oos_ret": oo["total_return"],
                     "oos_pf": oo["profit_factor"], "oos_dd": oo["max_drawdown"],
                     "oos_trades": oo["n_trades"], "oos_wr": oo["win_rate"]})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--cost-model", choices=["maker", "taker"], default="maker")
    args = ap.parse_args(argv)

    cost = COST[args.cost_model]
    bars = backfill(args.symbol, args.days)
    if len(bars) < 500:
        print("not enough bars", file=sys.stderr)
        return 1
    rows = evaluate(bars, cost)
    # Select on TRAIN ONLY (gate + rank), then report that single config's OOS untouched. Ranking by
    # the OOS slice you report is selection bias — it makes the holdout in-sample to the picker.
    rows.sort(key=lambda r: r["train_sharpe"], reverse=True)
    print(f"\n{'signal':<18} {'params':<34} {'tr_shrp':>8} {'tr_pf':>6} {'oos_shrp':>9} {'oos_ret':>9} "
          f"{'oos_pf':>7} {'oos_dd':>7} {'oos_n':>6} {'oos_wr':>7}")
    for r in rows:
        print(f"{r['signal']:<18} {json.dumps(r['params']):<34} {r['train_sharpe']:>8.2f} "
              f"{r['train_pf']:>6.2f} {r['oos_sharpe']:>9.2f} {100*r['oos_ret']:>8.2f}% {r['oos_pf']:>7.2f} "
              f"{100*r['oos_dd']:>6.1f}% {r['oos_trades']:>6} {100*r['oos_wr']:>6.1f}%")

    gated = [r for r in rows if r["train_pf"] > 1.1 and r["train_dd"] <= 0.25
             and r["train_trades"] >= 30 and r["train_sharpe"] > 0]
    os.makedirs(RUNS, exist_ok=True)
    best = gated[0] if gated else None     # chosen purely on train; its OOS row is the honest check
    with open(os.path.join(RUNS, "best_config.json"), "w") as f:
        json.dump({"best": best, "cost_model": args.cost_model, "cost": cost, "symbol": args.symbol,
                   "days": args.days, "n_bars": len(bars),
                   "caveat": ("maker model assumes limit-order fills at the band; live fill rate < "
                              "100% (adverse selection). OOS is one ~12d regime — forward-test before trusting."),
                   "evaluated_at_note": "stamp post-hoc; never trust local clock",
                   "all": rows[:10]}, f, indent=2)
    if best:
        confirmed = best["oos_pf"] > 1.1 and best["oos_dd"] <= 0.25 and best["oos_sharpe"] > 0
        print(f"\nTRAIN-SELECTED: {best['signal']} {best['params']} "
              f"(train Sharpe {best['train_sharpe']:.2f}, PF {best['train_pf']:.2f})")
        print(f"  OOS check: Sharpe {best['oos_sharpe']:.2f}, ret {100*best['oos_ret']:.2f}%, "
              f"PF {best['oos_pf']:.2f}, dd {100*best['oos_dd']:.1f}%, {best['oos_trades']} trades "
              f"-> {'HOLDS out-of-sample' if confirmed else 'DOES NOT confirm OOS (likely no edge)'}")
    else:
        print("\nNO SIGNAL CLEARS THE TRAIN GATES (honest null — costs eat the edge after a realistic "
              "1-bar execution lag). Framework + live paper surface still stand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
