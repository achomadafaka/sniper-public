#!/usr/bin/env python3
"""Snapshot the honest scorecard metrics to hl_scorecard_snapshots — run via cron for a trend.

Records realized mean + CI, total-book MTM, beta-neutral alpha + CI, and concentration for both
all-rows and out-of-sample (holdout) modes, so you can watch whether the edge CI tightens toward
or away from zero as the forward holdout accrues. Reuses the endpoint builder. Read-only vs HL.

    python3 scripts/hl_scorecard_snapshot.py           # take a snapshot now
    python3 scripts/hl_scorecard_snapshot.py --show     # print recent trend, no new snapshot
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.routes.api_hyperlive import _copy_scorecard_payload  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def take_snapshot(conn, *, now_ms: int) -> int:
    n = 0
    for holdout in (False, True):
        payload = _copy_scorecard_payload(holdout=holdout)
        if payload.get("available"):
            IS.save_scorecard_snapshot(conn, payload, ts_ms=now_ms, holdout=holdout)
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Snapshot the HL copy scorecard for a trend")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--show", action="store_true", help="print recent trend, take no snapshot")
    ap.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    if not args.show:
        now = args.now_ms if args.now_ms is not None else int(time.time() * 1000)
        n = take_snapshot(conn, now_ms=now)
        print(f"saved {n} scorecard snapshot(s) at {now}")
    print("\n═══ SCORECARD TREND (most recent) ═══")
    for mode in (False, True):
        rows = IS.get_scorecard_snapshots(conn, holdout=mode, limit=8)
        print(f"  [{'holdout/OOS' if mode else 'all rows'}]")
        for r in rows:
            print(f"    ts={r['ts_ms']} n={r['realized_n']} mean={r['realized_mean']} "
                  f"CI=[{r['ci_lo']},{r['ci_hi']}] {'spans0' if r['spans_zero'] else 'excl0'} "
                  f"total={r['total_bps']} alpha={r['alpha_mean']}")
        if not rows:
            print("    (none yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
