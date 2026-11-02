#!/usr/bin/env python3
"""Read the Solana exit sub-marks (ExitTiming sub_ms, 2026-09-28 Step 0), by exit reason.

Every funded Solana full exit (take-profit, stop, mirror, vol_death, manual) writes
memecoin_positions.exit_timing_json. Since the Step 0 deploy it carries `sub_ms`: claim_db,
balance_read, quote_call, usdc_pre_read / usdc_pre_wait, sign_call, signing_read /
signing_read_wait, swap_build, local_sign. `*_read` is a read's own wall time and `*_wait` is how
long the exit BLOCKED on it; with MEMECOIN_EXIT_OVERLAP_READS on, the gap between them is the time
the overlap hid. Single-rung exits only (sub-marks accumulate across rungs). Read-only.

    python3 scripts/research/exit_submarks_read.py --since 2026-09-28T10:00
"""
import argparse
import json
import sqlite3

LEDGER = "file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro"
KEYS = ("claim_db", "balance_read", "quote_call", "usdc_pre_read", "usdc_pre_wait", "sign_call",
        "swap_build", "local_sign", "signing_read", "signing_read_wait")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="closed_at lower bound (UTC ISO)")
    args = ap.parse_args()
    conn = sqlite3.connect(LEDGER, uri=True, timeout=5)
    rows = conn.execute(
        "SELECT close_exit_reason, exit_timing_json FROM memecoin_positions WHERE "
        "execution_mode='real' AND network_id='solana' AND closed_at >= ? "
        "AND exit_timing_json LIKE '%sub_ms%'", (args.since,)).fetchall()
    groups = {}
    for reason, raw in rows:
        t = json.loads(raw)
        if t.get("ladder_rungs") != 1:
            continue
        g = groups.setdefault(reason or "?", [])
        g.append(t)
    for reason in sorted(groups, key=lambda r: -len(groups[r])):
        g = groups[reason]
        print("%s  n=%d" % (reason, len(g)))
        for key in ("quote", "sign", "submit"):
            xs = [t["stages_ms"][key] for t in g if key in t.get("stages_ms", {})]
            print("  stage %-18s p50 %7.1f  p90 %7.1f" % (key, pct(xs, .5), pct(xs, .9)))
        for key in KEYS:
            xs = [t["sub_ms"][key] for t in g if key in t.get("sub_ms", {})]
            if xs:
                print("  sub   %-18s p50 %7.1f  p90 %7.1f  (n=%d)" % (key, pct(xs, .5), pct(xs, .9), len(xs)))


if __name__ == "__main__":
    main()
