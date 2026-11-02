#!/usr/bin/env python3
"""Edge decomposition for the HL copy book — where the PnL lives + the consensus/crowding signal.
Read-only.  python3 scripts/hl_copy_decomp.py"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_decomp as D  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def _line(k, v):
    if v.get("n", 0) == 0:
        return f"  {str(k):<12} (no data)"
    return (f"  {str(k):<12} n={v['n']:<3} mean={v['mean']:>8.1f} median={v['median']:>8.1f} "
            f"sum={v['sum']:>9.1f} win%={v['win_rate'] * 100:>3.0f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HL copy edge decomposition")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM hl_copy_book WHERE status!='open' AND pnl_bps IS NOT NULL")]
    allrows = [dict(r) for r in conn.execute("SELECT * FROM hl_copy_book")]
    if not closed:
        print("no closed copies", file=sys.stderr)
        return 1
    now = max((r["exit_ts_ms"] or 0) for r in allrows) or 0

    print("═══ HL COPY — EDGE DECOMPOSITION ═══\n\nMAJOR vs ALT (closed):")
    for k, v in D.major_vs_alt(closed).items():
        print(_line(k, v))
    print("\nSIDE (closed):")
    for k, v in D.by_side(closed).items():
        print(_line(k, v))
    print("\nTOP COIN CONTRIBUTORS (by sum):")
    for t in D.top_contributors(closed, n=10):
        print(_line(t["coin"], t))
    print("\nCONSENSUS / CROWDING (concurrent same coin+side at entry):")
    ann = D.annotate_consensus(allrows, now_ms=now)
    for k, v in sorted(D.consensus_buckets([r for r in ann if r.get("pnl_bps") is not None]).items()):
        print(_line(k, v))
    print("\n(consensus 2-3 = smart-money agreement; 4+ = crowded/late. alt = thin book, worst fills.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
