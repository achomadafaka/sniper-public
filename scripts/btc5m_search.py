#!/usr/bin/env python3
"""Multi-asset, walk-forward FEATURE SEARCH for the 5m mean-reversion algo — the honest hunt for an
edge across {BTC, ETH, SOL} x {base meanrev, volume-confirmed} x params, parallelized across cores.

Same discipline as the single backtest: 1-bar execution lag, realistic (maker) costs, features
recomputed per fold (no leakage), SELECTION on TRAIN only, OOS aggregated over walk-forward folds and
REPORTED (worst-fold surfaced). Reports edge-or-no-edge straight; writes runs/btc5m/search_result.json.

Paper/research only — never trades. Usage: python3 scripts/btc5m_search.py [--days 45] [--assets BTC,ETH,SOL]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.btc5m import data as D, signals as S, search as SR  # noqa: E402

RUNS = os.path.join(ROOT, "runs", "btc5m")
ASSET_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
COST = {"taker_fee": 0.0002, "slippage": 0.0}   # maker preset (limit-order fade entries)


def _grid() -> list[tuple]:
    g = []
    for n in (20, 36):
        for e in (2.0, 2.5, 3.0):
            for x in (0.3, 0.5):
                g.append(("meanrev_zscore", {"n": n, "entry": e, "exit_z": x}))
    for n in (20, 36):
        for e in (2.0, 2.5):
            for mvz in (1.0, 2.0):
                for mv in (None, 0.004):
                    g.append(("meanrev_confirmed",
                              {"n": n, "entry": e, "exit_z": 0.5, "min_vol_z": mvz, "max_vol": mv, "vol_n": n}))
    return g


# --- worker globals (bars cached once per process via initializer) ---
_BARS: dict = {}


def _init(symbols: list[str]):
    import db
    conn = db.get_connection()
    for s in symbols:
        _BARS[s] = D.load_bars(conn, s)


def _eval(task: tuple) -> dict:
    symbol, signame, params, k = task
    r = SR.evaluate_config(_BARS[symbol], S.SIGNALS[signame], params, cost=COST, k=k)
    r["asset"] = symbol
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--assets", default="BTC,ETH,SOL")
    ap.add_argument("--folds", type=int, default=4)
    args = ap.parse_args(argv)
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    symbols = [ASSET_SYMBOL[a] for a in assets if a in ASSET_SYMBOL]

    from scripts.btc5m_backtest import backfill
    for sym in symbols:
        n = len(backfill(sym, args.days))
        print(f"  {sym}: {n} bars cached", file=sys.stderr)

    grid = _grid()
    tasks = [(sym, name, params, args.folds) for sym in symbols for name, params in grid]
    workers = max(1, min(16, (os.cpu_count() or 4) - 2))
    print(f"sweeping {len(tasks)} configs ({len(grid)}/asset x {len(symbols)} assets) on {workers} workers...",
          file=sys.stderr)

    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(symbols,)) as ex:
        results = list(ex.map(_eval, tasks))
    dt = time.perf_counter() - t0
    print(f"swept {len(results)} configs in {dt:.1f}s ({1000*dt/max(1,len(results)):.0f} ms/config)\n",
          file=sys.stderr)

    by_asset = {}
    for sym in symbols:
        rs = [r for r in results if r["asset"] == sym]
        best = SR.select_best(rs, min_trades=60)        # >=60 across folds (45d sample is modest)
        by_asset[sym] = best
        rs.sort(key=lambda r: r["train_sharpe"], reverse=True)
        print(f"=== {sym} (top 3 by TRAIN sharpe) ===")
        for r in rs[:3]:
            print(f"  {r['signal']:<17} {json.dumps(r['params'])[:46]:<46} "
                  f"trShrp={r['train_sharpe']:+.2f} oosShrp={r['oos_sharpe']:+.2f} "
                  f"oosMin={r['oos_sharpe_min']:+.2f} oosPF={r['oos_pf']:.2f} oosN={r['oos_trades']}")
        if best:
            print(f"  -> TRAIN-selected: {best['signal']} {best['params']} | "
                  f"OOS {'CONFIRMED' if best['confirmed'] else 'NOT confirmed'} "
                  f"(oosShrp={best['oos_sharpe']:+.2f} worst={best['oos_sharpe_min']:+.2f} PF={best['oos_pf']:.2f})")
        else:
            print("  -> no config cleared the TRAIN gate")
        print()

    confirmed = {s: b for s, b in by_asset.items() if b and b["confirmed"]}
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, "search_result.json"), "w") as f:
        json.dump({"assets": assets, "days": args.days, "folds": args.folds, "cost": COST,
                   "by_asset": by_asset, "n_confirmed": len(confirmed),
                   "note": "selection on train; OOS reported. stamp post-hoc (Binance server time)."},
                  f, indent=2)
    if confirmed:
        print(f"EDGE FOUND on {len(confirmed)} asset(s): {', '.join(confirmed)} — forward-test before trusting.")
    else:
        print("NO EDGE: no asset/config cleared the OOS gate after costs+lag (honest null — 5m crypto "
              "mean-reversion is efficient under realistic execution). Framework reusable for more "
              "assets/features/data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
