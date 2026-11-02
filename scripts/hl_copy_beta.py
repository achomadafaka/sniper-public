#!/usr/bin/env python3
"""Beta-neutral alpha, done properly — per-coin beta to BTC, not the naive beta=1.

The scorecard's headline alpha neutralizes against BTC assuming beta=1. But the edge is LONG ALTS,
and alts are high-beta to BTC, so beta=1 under-removes market exposure and inflates the residual
alpha. This estimates each traded coin's beta from 1h candles and recomputes market-neutral alpha,
side-by-side with the naive version. If alpha collapses, the "skill" was mostly long-alt beta.

    python3 scripts/hl_copy_beta.py            # all closed trades
    python3 scripts/hl_copy_beta.py --holdout  # out-of-sample only
Read-only; paper."""
from __future__ import annotations

import argparse
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_scorecard as SC  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.markets import hyperliquid_api as HL  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
MARKET = "BTC"


def _gross_bps(side, entry, exit_):
    g = (float(exit_) - float(entry)) / float(entry) * 1e4
    return -g if side == "short" else g


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-coin beta-neutral alpha for the HL copy book")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    wc = "status!='open' AND pnl_bps IS NOT NULL AND entry_mark>0 AND exit_mark>0"
    if args.holdout:
        wc += " AND is_holdout=1"
    closed = [dict(r) for r in conn.execute(f"SELECT * FROM hl_copy_book WHERE {wc}")]
    conn.close()
    if not closed:
        print("no closed trades", file=sys.stderr)
        return 1

    t0 = min(r["detect_ts_ms"] for r in closed)
    t1 = max(r["exit_ts_ms"] for r in closed)
    btc = HL.fetch_candles(MARKET, "1h", t0 - 3600000, t1 + 3600000)
    if not btc:
        print("no BTC candles", file=sys.stderr)
        return 1

    coins = sorted({r["coin"].upper() for r in closed})
    betas = {}
    for coin in coins:
        if coin == MARKET:
            betas[coin] = 1.0
            continue
        try:
            cc = HL.fetch_candles(coin, "1h", t0 - 3600000, t1 + 3600000)
            m, a = SC.aligned_returns(btc, cc)
            b = SC.estimate_beta(m, a)
            betas[coin] = b if b is not None else 1.0
        except Exception:
            betas[coin] = 1.0

    gross, beta_naive, beta_pc = [], [], []
    for r in closed:
        g = _gross_bps(r["side"], r["entry_mark"], r["exit_mark"])
        p0 = SC.price_at(btc, r["detect_ts_ms"])
        p1 = SC.price_at(btc, r["exit_ts_ms"])
        if not p0 or not p1:
            continue
        mret = (p1 - p0) / p0
        sign = 1.0 if r["side"] == "long" else -1.0
        gross.append(g)
        beta_naive.append(sign * mret * 1e4)                       # beta=1
        beta_pc.append(sign * betas[r["coin"].upper()] * mret * 1e4)  # per-coin beta

    naive = SC.beta_neutral(gross, beta_naive)
    pc = SC.beta_neutral(gross, beta_pc)
    bvals = [betas[c] for c in coins if c != MARKET]
    print("═══ PER-COIN BETA-NEUTRAL ALPHA (vs naive beta=1) ═══")
    print(f"  coins={len(coins)}  median coin-beta to BTC = {st.median(bvals) if bvals else 1.0:.2f}  "
          f"(alts high-beta => naive beta=1 under-removes market)")
    for label, r in (("naive beta=1", naive), ("per-coin beta", pc)):
        a = r["alpha"]
        print(f"  {label:<15} gross={r['gross_mean']:+.1f}  beta_share={r['beta_share_pct']}%  "
              f"alpha={a['mean']:+.1f}bps  CI=[{a['ci_lo']:+.1f},{a['ci_hi']:+.1f}]  "
              f"{'SPANS 0' if a['spans_zero'] else 'excludes 0'}")
    print("\n  If per-coin alpha CI now spans 0, the headline 'skill' was mostly long-alt beta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
