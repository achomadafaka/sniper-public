#!/usr/bin/env python3
"""Backfill target fill + detection-lag on the CURRENTLY-OPEN copy book, and report the lag tax.

Capture only runs on new opens (post-restart). For the ~170 already-open mirrors we can still
recover the target's own fill (entryPx) and size from clearinghouse state right now, and compute:
  * lag_bps      — our entry_mark vs the target's fill = the polling-lag tax we already eat
  * target_notional — the target's conviction (for the size/conviction filter)
Historical entry-time depth/funding can't be recovered; this is the recoverable subset.

    python3 scripts/hl_copy_backfill.py            # backfill + lag report
    python3 scripts/hl_copy_backfill.py --report   # report only (no writes)
Paper only; read-only against HL."""
from __future__ import annotations

import argparse
import os
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_capture as CC  # noqa: E402
from sniper.hyperlive import follow_sim as FS  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.markets import hyperliquid_api as HL  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def backfill_open(conn, *, fetch_state) -> int:
    """Stamp target_entry_px/szi/notional/lag_bps on open mirrors the target still holds (same
    side). fetch_state(wallet)->clearinghouseState. Returns rows updated. Pure of network via inj."""
    open_rows = [dict(r) for r in conn.execute("SELECT * FROM hl_copy_book WHERE status='open'")]
    by_target = defaultdict(list)
    for r in open_rows:
        by_target[r["target"]].append(r)
    updated = 0
    for target, rows in by_target.items():
        try:
            pos = FS.positions_map(fetch_state(target))
        except Exception:
            continue
        for r in rows:
            p = pos.get(r["coin"])
            if not p or p.get("side") != r["side"]:
                continue
            tpx = p.get("entry_px")
            szi = p.get("szi")
            if not tpx:
                continue
            notional = abs(szi) * tpx if szi else None
            lag = CC.lag_bps(r["side"], r["entry_mark"], tpx)
            conn.execute(
                "UPDATE hl_copy_book SET target_entry_px=?, target_szi=?, target_notional=?, "
                "lag_bps=COALESCE(lag_bps, ?) WHERE id=?",
                (tpx, szi, round(notional, 2) if notional else None, lag, r["id"]))
            updated += 1
    conn.commit()
    return updated


def lag_report(conn) -> dict:
    lags = [r["lag_bps"] for r in conn.execute(
        "SELECT lag_bps FROM hl_copy_book WHERE lag_bps IS NOT NULL")]
    notionals = [r["target_notional"] for r in conn.execute(
        "SELECT target_notional FROM hl_copy_book WHERE target_notional IS NOT NULL")]
    out = {"lag_n": len(lags), "notional_n": len(notionals)}
    if lags:
        out.update(lag_mean_bps=round(st.mean(lags), 2), lag_median_bps=round(st.median(lags), 2),
                   lag_p90_bps=round(sorted(lags)[int(0.9 * len(lags))], 2),
                   lag_pct_positive=round(100 * sum(1 for x in lags if x > 0) / len(lags), 1))
    if notionals:
        out.update(target_notional_median=round(st.median(notionals), 0),
                   target_notional_p90=round(sorted(notionals)[int(0.9 * len(notionals))], 0))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill target fill + report detection-lag tax")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--report", action="store_true", help="report only, no writes")
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    if not args.report:
        n = backfill_open(conn, fetch_state=HL.fetch_clearinghouse_state)
        print(f"backfilled target fill + lag on {n} open mirrors")
    rep = lag_report(conn)
    print("═══ DETECTION-LAG TAX (open book) ═══")
    print(f"  lag: n={rep['lag_n']}  mean={rep.get('lag_mean_bps')}bps  median={rep.get('lag_median_bps')}  "
          f"p90={rep.get('lag_p90_bps')}  %positive={rep.get('lag_pct_positive')}")
    print(f"  target conviction: median notional=${rep.get('target_notional_median')}  "
          f"p90=${rep.get('target_notional_p90')}  (n={rep['notional_n']})")
    print("  (lag_bps>0 = we entered worse than the target's own fill — the cost of polling lag.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
