#!/usr/bin/env python3
"""Honest scorecard for the paper HL copy book — the one command that shows DEPLOYABLE truth.

Reads hl_copy_book, marks the open book to live prices, and prints: total-book PnL (not the
censored realized-only view), bootstrap CIs (is the edge > 0?), concentration (does one trade
carry it?), beta-neutral alpha (skill vs market), per-wallet ranking, size-decay from captured
depth (does it survive real fills?), and the funding/spread/lag haircuts. Read-only; paper only.

    python3 scripts/hl_copy_scorecard.py                 # full board
    python3 scripts/hl_copy_scorecard.py --holdout-only  # out-of-sample rows only
    python3 scripts/hl_copy_scorecard.py --no-beta       # skip the BTC-candle fetch
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_scorecard as SC  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.markets import hyperliquid_api as HL  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
SIZES = (1000, 5000, 10000, 25000, 50000, 100000)


def _fmt(v, suffix="", nd=1):
    return "n/a" if v is None else f"{v:,.{nd}f}{suffix}"


def render(rep: dict) -> str:
    L = []
    r = rep["realized"]
    L.append("═══ HL PAPER COPY — HONEST SCORECARD ═══")
    L.append(f"\nREALIZED (n={r['n']})  mean={_fmt(r['mean'])}bps  median={_fmt(r['median'])}  "
             f"winsor={_fmt(r['winsor_mean'])}  win%={_fmt(100 * r['win_rate'], nd=0)}")
    L.append(f"  95% CI on mean = [{_fmt(r['ci_lo'])}, {_fmt(r['ci_hi'])}]bps  "
             f"-> {'SPANS 0 (undecidable)' if r['spans_zero'] else 'excludes 0'}")
    c = rep["concentration"]
    L.append(f"  concentration: top1={_fmt(100 * c['top1_share'] if c['top1_share'] else None, nd=0)}%  "
             f"top5={_fmt(100 * c['top5_share'] if c['top5_share'] else None, nd=0)}% of realized sum")
    tb = rep["total_book"]
    L.append(f"\nTOTAL BOOK (realized+open MTM): {_fmt(tb['total_bps'])}bps over "
             f"{tb['realized_n'] + tb['open_n']} positions  "
             f"[realized {_fmt(tb['realized_sum_bps'])} + open MTM {_fmt(tb['open_mtm_sum_bps'])}]")
    L.append(f"  open MTM mean={_fmt(tb['open_mtm_mean_bps'])}bps  "
             f"(realized-only mean overstates when winners realize + losers linger)")
    cen = rep["censoring"]
    L.append(f"  censoring check: winners hold {_fmt(cen['winners_hold_median_h'])}h "
             f"vs losers {_fmt(cen['losers_hold_median_h'])}h")
    if rep.get("beta_neutral", {}).get("n"):
        b = rep["beta_neutral"]
        a = b["alpha"]
        L.append(f"\nBETA-NEUTRAL (n={b['n']})  gross={_fmt(b['gross_mean'])}  "
                 f"beta_share={_fmt(b['beta_share_pct'], nd=0)}%  "
                 f"alpha={_fmt(a['mean'])}bps  CI=[{_fmt(a['ci_lo'])},{_fmt(a['ci_hi'])}] "
                 f"-> {'SPANS 0' if a.get('spans_zero') else 'excludes 0'}")
    h = rep["haircuts"]
    L.append(f"\nHAIRCUTS (captured): funding={_fmt(h['funding_mean_bps'])}bps (n={h['funding_n']})  "
             f"entry_spread={_fmt(h['entry_spread_mean_bps'])}bps (n={h['entry_spread_n']})  "
             f"lag_tax={_fmt(h['lag_mean_bps'])}bps (n={h['lag_n']})")
    sd = rep["size_decay"]
    L.append(f"\nSIZE-DECAY  ({sd['coverage']}):")
    if sd["rows"]:
        L.append(f"  {'size$':>9} {'feasible%':>10} {'slip_rt':>9} {'real_mean':>10}")
        for row in sd["rows"]:
            L.append(f"  {row['size_usd']:>9,} {_fmt(row['feasible_pct'], nd=0):>10} "
                     f"{_fmt(row['mean_slippage_bps']):>9} {_fmt(row['mean_real_bps']):>10}")
    else:
        L.append("  (no captured depth yet — accrues after the runner restart)")
    pw = rep["per_wallet"]
    L.append(f"\nPER-WALLET (n>=5 closes), ranked by mean bps:")
    L.append(f"  {'wallet':<14}{'n':>4}{'mean':>9}{'median':>9}{'win%':>6}{'CI_lo':>9}{'CI_hi':>9}")
    for w in pw:
        L.append(f"  {str(w['wallet'])[:12]:<14}{w['n']:>4}{_fmt(w['mean']):>9}{_fmt(w['median']):>9}"
                 f"{_fmt(100 * w['win_rate'], nd=0):>6}{_fmt(w['ci_lo']):>9}{_fmt(w['ci_hi']):>9}")
    return "\n".join(L)


def load_rows(conn, *, holdout_only: bool):
    where_closed = "status!='open' AND pnl_bps IS NOT NULL"
    where_open = "status='open'"
    if holdout_only:
        where_closed += " AND is_holdout=1"
        where_open += " AND is_holdout=1"
    closed = [dict(r) for r in conn.execute(f"SELECT * FROM hl_copy_book WHERE {where_closed}")]
    open_rows = [dict(r) for r in conn.execute(f"SELECT * FROM hl_copy_book WHERE {where_open}")]
    return closed, open_rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Honest scorecard for the HL paper copy book")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--holdout-only", action="store_true", help="only is_holdout=1 (out-of-sample) rows")
    ap.add_argument("--no-beta", action="store_true", help="skip the BTC-candle fetch")
    ap.add_argument("--market-coin", default="BTC")
    ap.add_argument("--fee-bps", type=float, default=9.0)
    args = ap.parse_args(argv)

    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    closed, open_rows = load_rows(conn, holdout_only=args.holdout_only)
    if not closed and not open_rows:
        print("no copy rows found", file=sys.stderr)
        return 1
    marks = HL.fetch_marks()
    candles = None
    asset_candles = None
    if not args.no_beta and closed:
        t0 = min(r["detect_ts_ms"] for r in closed)
        t1 = max((r["exit_ts_ms"] or r["detect_ts_ms"]) for r in closed)
        try:
            candles = HL.fetch_candles(args.market_coin, "1h", t0 - 3600000, t1 + 3600000)
            asset_candles = {}
            for coin in sorted({str(row["coin"]) for row in closed}):
                if coin == args.market_coin:
                    asset_candles[coin] = candles
                else:
                    asset_candles[coin] = HL.fetch_candles(
                        coin, "1h", t0 - 3600000, t1 + 3600000
                    )
        except Exception as e:  # noqa: BLE001
            print(f"(beta series unavailable: {e})", file=sys.stderr)
            candles = asset_candles = None
    rep = SC.build_report(closed, open_rows, marks=marks, market_candles=candles,
                          asset_candles=asset_candles, sizes=SIZES, fee_bps=args.fee_bps)
    print(render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
