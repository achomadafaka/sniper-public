#!/usr/bin/env python3
"""WO-7 — execution tiering measurement read. 20 fills per variant before ANY claim.

Reads the dedicated memecoin_exit_execution_attempts table and reports per exit tier. Entry latency
is deliberately excluded. A variant with < 20 confirmed exits prints counts only — no comparison.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from statistics import median

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

MIN_FILLS = 20


def _connect_ro(path):
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load(db_path):
    c = _connect_ro(db_path)
    if c is None:
        return {}
    try:
        rows = c.execute(
            "SELECT COALESCE(tier_variant,'baseline') v, depth_first_rung_pct s, "
            "fee_paid_lamports f, total_ms l FROM memecoin_exit_execution_attempts "
            "WHERE status IN ('closed','derisked')").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        c.close()
    by = defaultdict(lambda: {"slip": [], "fee": [], "lat": []})
    for r in rows:
        b = by[r["v"]]
        if r["s"] is not None:
            b["slip"].append(float(r["s"]))
        if r["f"] is not None:
            b["fee"].append(float(r["f"]))
        if r["l"] is not None:
            b["lat"].append(float(r["l"]))
    return by


def render(by):
    lines = ["", "=" * 70, "  EXECUTION TIERING — measured fills per variant (read-only)", "=" * 70]
    if not by:
        lines += ["  No exec-latency fills recorded yet (no variant armed).",
                  "=" * 70, ""]
        return "\n".join(lines)
    for variant, d in sorted(by.items()):
        n = len(d["slip"]) or len(d["lat"])
        gate = "" if n >= MIN_FILLS else f"  [counts only: n={n}<{MIN_FILLS}]"
        ms = f"{median(d['slip']):.3f}" if d["slip"] else "n/a"
        mf = f"{median(d['fee']):.0f}" if d["fee"] else "n/a"
        ml = f"{median(d['lat']):.0f}" if d["lat"] else "n/a"
        lines.append(f"  {variant:24s} n={n:3d}  median_first_rung={ms}  "
                     f"median_fee_lamports={mf}  "
                     f"median_lat_ms={ml}{gate}")
    lines.append("  (compare variants only where both clear n>=20; no claim below that.)")
    lines += ["=" * 70, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Execution tiering measurement report (read-only).")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    from sniper.persistence.db_core import get_db_path
    print(render(load(args.db or get_db_path())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
