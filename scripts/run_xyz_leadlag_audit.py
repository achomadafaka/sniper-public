#!/usr/bin/env python3
"""Audit the xyz cross-asset lead-lag capture: does the HL `xyz` perp lag its real-world
underlying by more than the ~9bps round-trip taker fee when the underlying moves? Reuses
scan_audit.basis_direction_edge over the xyz scan_log DB. Read-only. Selection/entry labels,
never PnL.

Reads 'accruing' until enough live-basis movers accumulate (markets must be OPEN for the
reference to be live and basis to be logged — see run_xyz_leadlag_capture)."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import scan_audit as SA  # noqa: E402
from sniper.hyperlive import scan_log as SL  # noqa: E402

XYZ_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_xyz_scan_log.sqlite"
# equities/commodities move bigger and slower than crypto micro -> test out to 30 min.
HORIZONS_MS = (150_000, 300_000, 900_000, 1_800_000)
THRESHOLDS_BPS = (5.0, 20.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="xyz cross-asset lead-lag audit")
    ap.add_argument("--db-path", default=XYZ_DB)
    args = ap.parse_args(argv)
    if not os.path.exists(args.db_path):
        print(f"xyz scan log not found at {args.db_path} — capture hasn't run yet.")
        return 0
    conn = SL.connect(args.db_path)
    try:
        refreshes = conn.execute("SELECT COUNT(DISTINCT scan_ts) FROM hyperlive_scan_log").fetchone()[0]
        with_basis = conn.execute(
            "SELECT COUNT(*) FROM hyperlive_scan_log WHERE binance_basis_bps IS NOT NULL").fetchone()[0]
        print(f"xyz lead-lag log: {refreshes} refreshes  |  {with_basis} live-basis rows "
              f"(logged only while the underlying is OPEN)")
        for hz in HORIZONS_MS:
            for thr in THRESHOLDS_BPS:
                de = SA.basis_direction_edge(conn, horizon_ms=hz, basis_threshold_bps=thr, min_volume=0.0)
                if de["status"] != "ok":
                    print(f"  @{hz//1000:>4}s |basis|>={thr:>4.0f}bps: accruing (n={de['n']}/{de['min_n']})")
                else:
                    print(f"  @{hz//1000:>4}s |basis|>={thr:>4.0f}bps: aligned {de['mean_aligned_bps']:+7.1f}bps "
                          f"after-fee {de['after_fee_bps']:+7.1f}bps  (hit {de['hit_rate']:.0%}, t={de['t_stat']}, "
                          f"n={de['n']}) => {'EDGE' if de['edge'] else 'no edge'}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
