#!/usr/bin/env python3
"""Audit the liquidation-cascade magnet edge: conditioned on a NEAR + BIG liquidation cluster,
does price move toward it by more than the ~9bps round-trip taker fee? Reuses
cluster_audit.cluster_cascade_edge over the hyperlive_cluster_history time series. Read-only;
24/7 crypto; selection/entry labels, never PnL.

Reads 'accruing' until enough near+big-cluster signals accumulate (the poller must run a while
to build the time series — there is no historical liquidation data to backfill)."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import persistence as P  # noqa: E402
from sniper.hyperlive import cluster_audit as CA  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
# NOTE: cluster notional is a SMALL-SAMPLE estimate (poller harvests ~300 addresses, a sliver
# of HL's book), so thresholds are calibrated to the SAMPLED scale, not true market notional.
# The dose-response across distance/size is the signal; absolute dollars are relative.
HORIZONS_MS = (180_000, 360_000, 900_000)            # 3 / 6 / 15 min
DISTANCE_BPS = (50.0, 100.0, 150.0)                  # nearer cluster = stronger magnet (dose)
NOTIONAL = (250_000.0, 1_000_000.0)                  # bigger sampled cluster = stronger (dose)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Liquidation-cascade magnet edge audit")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    args = ap.parse_args(argv)
    if not os.path.exists(args.db_path):
        print(f"engine db not found at {args.db_path} — cluster poller hasn't run yet.")
        return 0
    conn = P.connect(args.db_path)
    try:
        try:
            polls = conn.execute("SELECT COUNT(DISTINCT ts_ms) FROM hyperlive_cluster_history").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM hyperlive_cluster_history").fetchone()[0]
        except Exception:
            print("hyperlive_cluster_history not present yet — restart the cluster poller to begin logging.")
            return 0
        print(f"cluster history: {polls} polls  |  {rows} (asset, poll) signals")
        for hz in HORIZONS_MS:
            for dist in DISTANCE_BPS:
                for notl in NOTIONAL:
                    de = CA.cluster_cascade_edge(conn, horizon_ms=hz, max_distance_bps=dist, min_notional=notl)
                    tag = f"@{hz//60000:>2}min dist<={dist:>4.0f}bps notl>=${notl/1e6:.2f}M"
                    if de["status"] != "ok":
                        print(f"  {tag}: accruing (n={de['n']}/{de['min_n']})")
                    else:
                        print(f"  {tag}: aligned {de['mean_aligned_bps']:+7.1f}bps after-fee {de['after_fee_bps']:+7.1f}bps "
                              f"(hit {de['hit_rate']:.0%}, t={de['t_stat']}, n={de['n']}) => "
                              f"{'EDGE' if de['edge'] else 'no edge'}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
