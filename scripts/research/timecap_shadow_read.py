#!/usr/bin/env python3
"""Measurement-only 60 s time-cap shadow on funded Solana and 4663 positions
(spec docs/superpowers/specs/2026-09-30-memecoin-timecap-shadow-design.md).

For every funded position, what closing at the first exact whole-balance sell quote at or after
open + T seconds would have returned, against what the position actually returned. The quotes are
the ones the funded exit rail already takes each sweep and already persists
(memecoin_route_series, seam 'exit_decision'); this reads them and fetches nothing. The funded exit
is not touched.

Read-only on the ledger (get_readonly_connection), indexed by position id, no network call.

    python3 scripts/research/timecap_shadow_read.py --since 2026-09-30T00:00:00+00:00   # forward read
    python3 scripts/research/timecap_shadow_read.py --since 2026-09-13T00:00:00+00:00 --retro
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CHAINS = ("solana", "4663")
PRIMARY_T = 60.0
SECONDARY_T = (30.0, 120.0, 300.0)
# The shadow quote must be the rail's own read of the moment, not a later one: a quote more than
# this long after open + T is not "exiting at T", and the position is counted as unmeasured.
MAX_QUOTE_LAG_S = 10.0
MIN_N = 50                                   # the pre-registered read waits for this many per chain


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def positions(conn, since, until=None):
    """Closed funded positions on the shadow chains opened in [since, until), manual entries out."""
    sql = ("SELECT id, network_id, signal_source, source_handle, mint, entry_notional, opened_at, "
           "opened_recorded_at, closed_at, realized_pnl, exit_reason FROM memecoin_positions "
           "WHERE execution_mode='real' "
           "AND opened_at >= ? AND closed_at IS NOT NULL AND realized_pnl IS NOT NULL "
           "AND COALESCE(signal_source,'') != 'manual'")
    args = [since]
    if until:
        sql += " AND opened_at < ?"
        args.append(until)
    return [dict(r) for r in conn.execute(sql + " ORDER BY opened_at", args)
            if r["network_id"] in CHAINS]


def shadow(conn, pos, t_cap):
    """(status, delta_usd). delta = shadow proceeds - actual proceeds, where actual proceeds are
    entry_notional + realized_pnl.

    T counts from `opened_recorded_at`, the FILL (Gamma post-review, 2026-09-30): that is when a
    live cap would start its clock. `opened_at` is the decision, about 2 s earlier on Solana and 5 s
    earlier on 4663.
      closed_before_cap  the rail closed first: the cap changes nothing (delta 0)
      measured           an exact exit_decision quote within MAX_QUOTE_LAG_S after fill + T
      partial_before_cap any exit fill before T (derisk, partial_exit, mirror_exit, ...): the
                         whole-balance quote is not the whole book
      no_fill_stamp      no opened_recorded_at: no clock to count T from (absent, not zero)
      unmeasured         open at T with no quote in the window (the rail was blind)"""
    opened = _ts(pos.get("opened_recorded_at"))
    if opened is None:
        return "no_fill_stamp", None
    hold = (_ts(pos["closed_at"]) - opened).total_seconds()
    if hold <= t_cap:
        return "closed_before_cap", 0.0
    at = opened.timestamp() + t_cap
    if conn.execute("SELECT 1 FROM memecoin_fills WHERE position_id=? AND kind != 'entry' "
                    "AND created_at <= ? LIMIT 1",
                    (pos["id"], datetime.fromtimestamp(at, opened.tzinfo).isoformat())).fetchone():
        return "partial_before_cap", None
    lo = datetime.fromtimestamp(at, opened.tzinfo).isoformat()
    hi = datetime.fromtimestamp(at + MAX_QUOTE_LAG_S, opened.tzinfo).isoformat()
    row = conn.execute("SELECT expected_usdc FROM memecoin_route_series WHERE position_id=? "
                       "AND seam='exit_decision' AND observed_at >= ? AND observed_at <= ? "
                       "ORDER BY observed_at LIMIT 1", (pos["id"], lo, hi)).fetchone()
    if row is None or row[0] is None or float(row[0]) <= 0:
        return "unmeasured", None
    actual = float(pos["entry_notional"]) + float(pos["realized_pnl"])
    return "measured", float(row[0]) - actual


def exit_cost_frac(conn, rows):
    """Mean of (realised proceeds / last exit_decision quote within MAX_QUOTE_LAG_S before the
    close) - 1 over these positions: what a quoted exit loses on the way to a fill. None when no
    close has such a quote."""
    fracs = []
    for r in rows:
        # One exit fill only: a position closed in several sells has no single quote-to-fill gap.
        exits = conn.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? "
                             "AND kind != 'entry'", (r["id"],)).fetchone()[0]
        if exits != 1:
            continue
        closed = _ts(r["closed_at"])
        lo = datetime.fromtimestamp(closed.timestamp() - MAX_QUOTE_LAG_S, closed.tzinfo).isoformat()
        q = conn.execute("SELECT expected_usdc FROM memecoin_route_series WHERE position_id=? "
                         "AND seam='exit_decision' AND observed_at >= ? AND observed_at <= ? "
                         "ORDER BY observed_at DESC LIMIT 1", (r["id"], lo, r["closed_at"])).fetchone()
        if q and q[0] and float(q[0]) > 0:
            fracs.append((float(r["entry_notional"]) + float(r["realized_pnl"])) / float(q[0]) - 1.0)
    return statistics.fmean(fracs) if fracs else None


def read(conn, rows, t_cap):
    out = {}
    for chain in CHAINS:
        rs = [r for r in rows if r["network_id"] == chain]
        status = defaultdict(int)
        deltas, adjusted, notional = [], [], 0.0
        cost = exit_cost_frac(conn, rs)
        for r in rs:
            st, d = shadow(conn, r, t_cap)
            status[st] += 1
            if d is not None:
                deltas.append(d)
                notional += float(r["entry_notional"])
                if st == "measured" and cost is not None:
                    # the shadow exit pays the same quote-to-fill cost the real exits paid
                    actual = float(r["entry_notional"]) + float(r["realized_pnl"])
                    adjusted.append((d + actual) * (1.0 + cost) - actual)
                else:
                    adjusted.append(d)
        measured = [d for d in deltas if d != 0.0]
        out[chain] = {
            "positions": len(rs), "status": dict(status),
            "actual_pnl": round(sum(float(r["realized_pnl"]) for r in rs), 2),
            "shadow_delta_usd": round(sum(deltas), 2),
            "shadow_delta_pct_of_notional": round(100 * sum(deltas) / notional, 2) if notional else None,
            "exit_cost_mean_pct": round(100 * cost, 3) if cost is not None else None,
            "shadow_delta_after_exit_cost_usd": round(sum(adjusted), 2) if cost is not None else None,
            "measured_delta_median_usd": round(statistics.median(measured), 2) if measured else None,
            "measured_better_share": (round(sum(d > 0 for d in measured) / len(measured), 3)
                                      if measured else None),
            "unmeasured_share": round(status["unmeasured"] / len(rs), 3) if rs else None,
            "ready": len(rs) >= MIN_N}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--retro", action="store_true", help="also split the window in chronological halves")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(args.db or str(state_path("sniper_tracker.db")))
    rows = positions(conn, args.since, args.until)
    result = {"since": args.since, "until": args.until, "primary_t_s": PRIMARY_T,
              "primary": read(conn, rows, PRIMARY_T),
              "secondary": {int(t): read(conn, rows, t) for t in SECONDARY_T}}
    if args.retro and rows:
        split = sorted(r["opened_at"] for r in rows)[len(rows) // 2]
        result["split_at"] = split
        result["H1"] = read(conn, [r for r in rows if r["opened_at"] < split], PRIMARY_T)
        result["H2"] = read(conn, [r for r in rows if r["opened_at"] >= split], PRIMARY_T)
    print(json.dumps(result, indent=1))
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    sys.exit(main())
