#!/usr/bin/env python3
"""Count funded positions whose exit was APPROVED but never executed.

WHY THIS GATE EXISTS. Position 48565 (TITAN, 4663) on 2026-09-02:

    18:51:14  stop breaches
    18:51:19  the rail CONFIRMS it at -41.5% and ALLOWS the sale, projecting $40.84
    18:53:02  a deploy sends SIGTERM, mid-breach, with that sale still unexecuted
    18:53:10  the new process starts
    18:55:27  it finally sells at -81.3% for $13.04

The rail was correct and on time. $27.80 of a $58.08 loss happened AFTER we had decided to
sell, and a deploy landed in the middle of it. Killing the runner while a funded position
holds an approved, unexecuted exit converts a decided loss into a larger one.

THE PREDICATE. A position that is open and funded, whose most recent LOSS-rail audit row says
`allowed=1`, and which has no exit execution attempt since that row. `real_tp` is deliberately
NOT counted: a delayed take-profit costs opportunity, and blocking a deploy on one would stall
releases for a state that is not losing money.

Prints one integer to stdout, the way `pending_reservations` does. A missing table prints 0
rather than raising: this gate must never be the reason a deploy cannot proceed when the
schema predates it.
"""
from __future__ import annotations

import sqlite3
import sys

LOSS_REASONS = ("b_stop", "catastrophe")


def approved_unexecuted_exits(conn, *, loss_reasons=LOSS_REASONS):
    """[(position_id, ticker, network_id, reason, approved_at)] for each stuck approved exit."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"memecoin_positions", "memecoin_exit_trigger_audit"} <= have:
        return []
    marks = "memecoin_exit_execution_attempts" in have
    placeholders = ",".join("?" for _ in loss_reasons)
    rows = conn.execute(
        "SELECT p.id, p.ticker, p.network_id, a.reason, MAX(a.checked_at) AS approved_at "
        "FROM memecoin_positions p "
        "JOIN memecoin_exit_trigger_audit a ON a.position_id = p.id "
        "WHERE p.execution_mode='real' AND p.closed_at IS NULL "
        f"  AND a.allowed=1 AND a.reason IN ({placeholders}) "
        "GROUP BY p.id, a.reason", tuple(loss_reasons)).fetchall()
    out = []
    for pid, ticker, network_id, reason, approved_at in rows:
        if marks:
            done = conn.execute(
                "SELECT 1 FROM memecoin_exit_execution_attempts "
                "WHERE position_id=? AND attempted_at>=? LIMIT 1",
                (pid, approved_at)).fetchone()
            if done:
                continue          # the executor already acted on this approval
            # A BAG THAT CANNOT SELL IS NOT MID-EXIT (2026-09-04). This gate exists to stop a
            # deploy landing BETWEEN a decision and its execution -- TITAN 48565, where $27.80 of a
            # $58.08 loss happened after we had decided to sell. It was never meant to block on a
            # position the executor has already tried and failed to sell.
            #
            # It did, permanently, because the runner RE-APPROVES every sweep: FROGE 50786 had its
            # b_stop approved at 20:03:33 with its last attempt at 20:01:40, and each new sweep
            # pushed the approval past the newest attempt again. Any position whose sell reverts on
            # chain therefore blocks EVERY deploy forever -- which is exactly when a fix is most
            # wanted. FROGE's sell reverts because its only route to native runs through a hooked
            # pool that refuses us; no retry and no smaller size changes that.
            #
            # So: the executor has ACTED if it attempted recently and the attempt is terminal
            # `stuck`. That is a different fact from "no attempt at all", which still blocks.
            tried = conn.execute(
                "SELECT status, attempted_at FROM memecoin_exit_execution_attempts "
                "WHERE position_id=? ORDER BY attempted_at DESC LIMIT 1",
                (pid,)).fetchone()
            if tried and str(tried[0] or "") == "stuck":
                continue
        out.append((pid, ticker, network_id, reason, approved_at))
    return out


def main(argv):
    if len(argv) < 2:
        print("usage: memecoin_deploy_exit_drain_gate.py <db> [--list]", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{argv[1]}?mode=ro", uri=True)
    try:
        stuck = approved_unexecuted_exits(conn)
    except sqlite3.Error:
        print(0)                  # an unreadable ledger must not block a release
        return 0
    finally:
        conn.close()
    if "--list" in argv:
        for pid, ticker, net, reason, when in stuck:
            print(f"{pid}\t{ticker}\t{net}\t{reason}\t{when}", file=sys.stderr)
    print(len(stuck))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
