#!/usr/bin/env python3
"""WO-8A Stage A gate — is there enough GENUINELY out-of-sample data to confirm the 5m signal?

The predictive study (commit 6128a5fe, run 2026-07-24) used ALL 4663 tape data up to its build time.
Its only survivor was fwd_ret_5m WITHOUT the span floor, weak (per-pool 0.15). Confirming it on
out-of-sample pools requires pools whose ENTIRE history POSTDATES the study window — a pool whose
first tick is after the cutoff has all its history after it. Below 100 such pools the confirmation
cannot run: STOP and say so (WO-8A preamble). This gate is re-runnable — as the funded runner ingests
new pools, rerun it; when it reports >=100, Stage A's walk-forward confirmation may proceed.

READ-ONLY. Reports a count and PASS/STOP. Never touches the money path.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The study's data window end = the tape's max observed_at at its 2026-07-24 build (assets written
# 20:19-21:10; last tick 20:06:10). A genuinely-OOS pool must begin AFTER this.
STUDY_WINDOW_END = "2026-07-24T20:06:10+00:00"
MIN_OOS_POOLS = 100
CHAIN = "4663"
MIN_SWAP_BLOCKS = 200      # [inherited] the study's per-pool density floor — an OOS pool must clear it too


def count_oos_pools(db_path, *, cutoff=STUDY_WINDOW_END, chain=CHAIN, min_blocks=MIN_SWAP_BLOCKS):
    if not os.path.exists(db_path):
        return None
    c = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    try:
        max_obs = c.execute("SELECT MAX(observed_at) FROM pool_ticks WHERE chain_id=?",
                            (chain,)).fetchone()[0]
        # pools whose EARLIEST tick is after the study window AND that clear the density floor
        row = c.execute(
            "SELECT COUNT(*) FROM (SELECT pool FROM pool_ticks WHERE chain_id=? "
            "GROUP BY pool HAVING MIN(observed_at) > ? "
            "AND COUNT(DISTINCT block_no) >= ?)",
            (chain, cutoff, min_blocks)).fetchone()
        qualifying = int(row[0])
        # also the raw count without the density floor, for context
        raw = c.execute(
            "SELECT COUNT(*) FROM (SELECT pool FROM pool_ticks WHERE chain_id=? "
            "GROUP BY pool HAVING MIN(observed_at) > ?)", (chain, cutoff)).fetchone()[0]
    finally:
        c.close()
    return {"max_observed_at": max_obs, "cutoff": cutoff, "qualifying_oos_pools": qualifying,
            "raw_oos_pools": int(raw), "min_blocks": min_blocks}


def render(result):
    if result is None:
        return "No tape DB — cannot evaluate the OOS gate."
    q = result["qualifying_oos_pools"]
    lines = ["", "=" * 66, "  WO-8A OOS POOL GATE (Stage A prerequisite)", "=" * 66,
             f"  study window end (cutoff): {result['cutoff']}",
             f"  tape max observed_at:      {result['max_observed_at']}",
             f"  OOS pools (first tick > cutoff): raw={result['raw_oos_pools']}, "
             f"clearing {result['min_blocks']}-block floor={q}",
             f"  gate: need >= {MIN_OOS_POOLS} qualifying OOS pools"]
    if q >= MIN_OOS_POOLS:
        lines.append("  VERDICT: PASS — Stage A confirmation may proceed on these OOS pools.")
    else:
        lines.append(f"  VERDICT: STOP — only {q} qualifying OOS pools (< {MIN_OOS_POOLS}). The tape "
                     "has not accrued enough post-study data. WO-8B stays BLOCKED. Rerun as it grows.")
    lines += ["=" * 66, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="WO-8A OOS pool gate (read-only).")
    ap.add_argument("--db", default=None)
    ap.add_argument("--cutoff", default=STUDY_WINDOW_END)
    args = ap.parse_args()
    from sniper.persistence.db_ticks import ticks_db_path
    result = count_oos_pools(args.db or ticks_db_path(), cutoff=args.cutoff)
    print(render(result))
    return 0 if (result and result["qualifying_oos_pools"] >= MIN_OOS_POOLS) else 2


if __name__ == "__main__":
    sys.exit(main())
