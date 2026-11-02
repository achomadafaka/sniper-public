#!/usr/bin/env python3
"""Hourly chart-truth collector. Measurement only — writes no funded decision surface.

    python3 scripts/memecoin_chart_truth_loop.py --once
    python3 scripts/memecoin_chart_truth_loop.py            # loop, one pass per hour

Cron install is the operator's; this script never installs itself.
"""
import sys as _sys, pathlib as _pathlib
_R = str(_pathlib.Path(__file__).resolve().parents[1])
if _R not in _sys.path:
    _sys.path.insert(0, _R)

import argparse
import logging
import time

from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence.db_memecoin_chart_truth import connect as connect_chart_truth
from sniper.services.memecoin_chart_truth import run_once
from sniper.util.clock import real_utc_now_iso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--interval-s", type=float, default=3600.0)
    ap.add_argument("--max-pools", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    while True:
        # own file for writes; READ-ONLY on the funded ledger for discovery and the yield signal
        conn = connect_chart_truth()
        ledger = get_readonly_connection()
        try:
            counters = run_once(conn, ledger, max_pools=args.max_pools)
            print("[%s] chart_truth %s" % (real_utc_now_iso(), counters), flush=True)
        except Exception:
            logging.exception("chart_truth pass failed")
        finally:
            conn.close()
            ledger.close()
        if args.once:
            return 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
