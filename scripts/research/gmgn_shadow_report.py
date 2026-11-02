#!/usr/bin/env python3
"""Compare GMGN facts against our own. Read-only, no network.

    python3 scripts/research/gmgn_shadow_report.py [--hours 24]

Per chain: price agreement with a DexScreener mark on the SAME mint within 5 s and request latency.
Failed GMGN reads are not persisted, so this report cannot estimate coverage or failure rate.

The rows are selected by coins we held. A pair is one successful GMGN read and one valid Dex mark;
unpaired rows stay visible rather than being treated as zero or a failed Dex read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sniper.util.envfiles import load_env_files                          # noqa: E402
from sniper.util.state_paths import state_root, state_path               # noqa: E402
load_env_files(str(state_root()))

from sniper.persistence.db_core import get_readonly_connection           # noqa: E402

PAIR_WINDOW_S = 5


def paired_dex_mark(c, chain, mint, ts):
    """Nearest valid Dex mid inside the narrow comparison window, or None."""
    row = c.execute(
        "SELECT mid, liquidity FROM memecoin_position_marks "
        " WHERE chain_id=? AND mint=? AND source IN ('fastlane_mid','dexscreener') "
        " AND mid>0 AND COALESCE(quality,'')!='quarantined_spike' "
        " AND ABS(strftime('%s', sweep_ts) - strftime('%s', ?)) <= ? "
        " ORDER BY ABS(strftime('%s', sweep_ts) - strftime('%s', ?)) LIMIT 1",
        (chain, mint, ts, PAIR_WINDOW_S, ts)).fetchone()
    return tuple(row) if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    c = get_readonly_connection(str(state_path("sniper_tracker.db")))
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "memecoin_gmgn_shadow" not in tables:
        print("memecoin_gmgn_shadow does not exist — the shadow has never run. "
              "ABSENT READ, not a zero result.")
        return 1
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)).isoformat()
    rows = c.execute(
        "SELECT chain_id, mint, ts, price, liquidity, rtt_ms FROM memecoin_gmgn_shadow "
        " WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    print(f"# GMGN shadow — {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    print(f"window: {args.hours:g} h, since {since}")
    print(f"gmgn rows: {len(rows)}")
    if not rows:
        print("\nNo rows. Either the shadow knob is off or no real position has been open.")
        return 0

    by_chain = {}
    for chain, mint, ts, price, _liq, rtt in rows:
        d = by_chain.setdefault(chain, {"n": 0, "mints": set(), "rtt": [],
                                        "paired": 0, "unpaired": 0, "ratios": []})
        d["n"] += 1
        d["mints"].add(mint)
        if rtt is not None:
            d["rtt"].append(rtt)
        mark = paired_dex_mark(c, chain, mint, ts)
        if not mark:
            d["unpaired"] += 1
            continue
        d["paired"] += 1
        mark_mid, _mark_liq = mark
        if price and mark_mid and mark_mid > 0:
            d["ratios"].append(float(price) / float(mark_mid))

    print(f"\n{'chain':<9} {'rows':>5} {'mints':>5} {'paired':>7} {'unpaired':>8} "
          f"{'abs diff p50':>12} {'abs diff p90':>12} {'rtt p50':>8} {'rtt p90':>8}")
    for chain in sorted(by_chain):
        d = by_chain[chain]
        r = sorted(d["ratios"])
        diffs = sorted(abs(x - 1.0) * 100.0 for x in r)
        rt = sorted(d["rtt"])
        print(f"{chain:<9} {d['n']:>5} {len(d['mints']):>5} {d['paired']:>7} "
              f"{d['unpaired']:>8} "
              f"{(f'{statistics.median(diffs):.1f}%' if diffs else '-'):>12} "
              f"{(f'{diffs[len(diffs)*9//10]:.1f}%' if diffs else '-'):>12} "
              f"{(f'{rt[len(rt)//2]:.0f}ms' if rt else '-'):>8} "
              f"{(f'{rt[len(rt)*9//10]:.0f}ms' if rt else '-'):>8}")

    print("\n## How to read this")
    print("  Pairs are successful GMGN reads with a valid DexScreener mid within 5 s.")
    print("  `unpaired` means no near-time Dex mark; it does not prove Dex was blind.")
    print("  Absolute price differences include pool-selection and timing effects; neither source")
    print("  is assumed correct. Repeated reads of one mint are not independent market samples.")
    print("  GMGN failures are not counted: 403/429/timeout writes no row. Check worker logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
