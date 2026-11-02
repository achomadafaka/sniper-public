#!/usr/bin/env python3
"""Mark every open intel paper position. Cron every 5 minutes.

The mark series IS the measurement, so it must accrue whether or not anyone is
looking at the UI — which is why this is a cron and not a refresh-on-view. Cost is
about three HTTP calls per run regardless of book size.

Plan: docs/superpowers/plans/2026-07-29-intel-paper-testing.md (Task 6)
Cron: */5 * * * * cd <repo> && python3 scripts/intel_paper_mark_job.py >> logs/intel_paper_mark.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

# Runnable as a bare script from cron, which does not put the repo on sys.path.
# Omitting this is what silently disabled scripts/intel_gate_read.py (audit F-11).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.persistence import intel_paper  # noqa: E402
from sniper.services.intel_paper_mark import mark_cycle  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("intel-paper-mark")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mark open intel paper positions")
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify wiring and open-position count without any network call")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    intel_paper.ensure_schema(conn)

    if args.dry_run:
        logger.info("dry-run: %d open position(s)", len(intel_paper.open_positions(conn)))
        return 0

    try:
        logger.info("marked %s", mark_cycle(conn))
    except Exception:
        # Never fail a cron run on a transient API problem — it would mail on every poll.
        logger.exception("mark cycle failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
