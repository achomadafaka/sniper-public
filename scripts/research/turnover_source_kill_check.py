#!/usr/bin/env python3
"""Run the kill-criterion check for the turnover x source-size conjunction gate.

See docs/superpowers/specs/2026-09-08-turnover-source-conjunction-kill-criterion.md. Prints
RUNNING or KILL, with the reason, from the exact query in that document's section 4. Requires
--activation-timestamp -- the recorded value from that document's section 6. Refuses to guess it.

Read-only. No writes, no admission-path change, no setting change. Turning the gate off after a
KILL verdict is a separate, manual, operator action.

    python3 scripts/research/turnover_source_kill_check.py --activation-timestamp 2026-09-08T20:00:00+00:00
"""
import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "sniper_tracker.db"

FAST_FLOOR_MIN_N = 10
FAST_FLOOR_RETURN_PCT = -15.0
STANDARD_FLOOR_MIN_N = 20
STANDARD_FLOOR_RETURN_PCT = 0.0

QUERY = """
SELECT realized_pnl, entry_notional
FROM memecoin_positions
WHERE execution_mode = 'real'
  AND is_open = 0
  AND signal_source = 'follow_activity'
  AND network_id IN ('solana', '4663')
  AND opened_at >= ?
  AND realized_pnl IS NOT NULL
  AND entry_notional IS NOT NULL AND entry_notional > 0
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--activation-timestamp", required=True,
                     help="ISO timestamp from the kill-criterion doc's section 6")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(QUERY, (args.activation_timestamp,)).fetchall()

    n = len(rows)
    total_pnl = sum(r[0] for r in rows)
    total_stake = sum(r[1] for r in rows)
    ret_pct = (total_pnl / total_stake * 100.0) if total_stake else None

    print(f"population: closed real follow_activity, solana+4663, opened_at >= "
          f"{args.activation_timestamp}")
    print(f"n_closes={n}  total_pnl=${total_pnl:.2f}  total_stake=${total_stake:.2f}  "
          f"stake_weighted_return_pct={ret_pct if ret_pct is not None else 'n/a'}")

    if n == 0:
        print("\nRUNNING -- no closes yet, no trigger can fire")
        return

    ex_largest = None
    if n >= STANDARD_FLOOR_MIN_N:
        largest = max(rows, key=lambda r: r[0])
        rest_pnl = total_pnl - largest[0]
        rest_stake = total_stake - largest[1]
        ex_largest = (rest_pnl / rest_stake * 100.0) if rest_stake else None
        print(f"return_pct_ex_largest_trade={ex_largest if ex_largest is not None else 'n/a'} "
              f"(largest trade: pnl=${largest[0]:.2f} stake=${largest[1]:.2f})")

    if n >= FAST_FLOOR_MIN_N and ret_pct is not None and ret_pct <= FAST_FLOOR_RETURN_PCT:
        print(f"\nKILL -- fast floor: n={n}>={FAST_FLOOR_MIN_N} and "
              f"return={ret_pct:.2f}%<={FAST_FLOOR_RETURN_PCT}%")
        return
    if n >= STANDARD_FLOOR_MIN_N and ret_pct is not None and ret_pct < STANDARD_FLOOR_RETURN_PCT:
        print(f"\nKILL -- standard floor: n={n}>={STANDARD_FLOOR_MIN_N} and "
              f"return={ret_pct:.2f}%<{STANDARD_FLOOR_RETURN_PCT}%")
        return
    if n >= STANDARD_FLOOR_MIN_N and ex_largest is not None and ex_largest < 0.0:
        print(f"\nKILL -- single-trade fragility: n={n}>={STANDARD_FLOOR_MIN_N} and "
              f"return_ex_largest={ex_largest:.2f}%<0%")
        return

    print("\nRUNNING -- no kill trigger fired")


if __name__ == "__main__":
    main()
