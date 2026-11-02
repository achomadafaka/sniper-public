#!/usr/bin/env python3
"""Maintain the HL watch cohort and paper copy target tier.

Public read-only research bookkeeping over local SQLite. No keys, no orders.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.hyperlive import watch_lifecycle as WL  # noqa: E402


DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Maintain HL watch lifecycle and paper copy targets")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--max-watch-targets", type=int, default=5)
    args = ap.parse_args(argv)

    conn = IS.connect(args.db_path)
    try:
        summary = WL.maintain_watchlist(
            conn, now_ms=int(time.time() * 1000), max_watch_targets=args.max_watch_targets)
        print(summary)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
