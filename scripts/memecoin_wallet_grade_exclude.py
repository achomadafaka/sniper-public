#!/usr/bin/env python3
"""Keep one closed funded position off its wallet's funded grade (operator 2026-10-02).

For a loss that was OUR fault (no usable exit route, a garbage quote, a bug), not the copied
wallet's. The ledger row is untouched and the loss still counts in every money total; only the
wallet's demote guardrail and funded-authority board stop counting it. Undo: delete the row from
memecoin_wallet_grade_exclusions.

    python3 scripts/memecoin_wallet_grade_exclude.py 68544 --reason "..."            # dry run
    python3 scripts/memecoin_wallet_grade_exclude.py 68544 --reason "..." --confirm
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("position_id", type=int)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--actor", default="operator")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    from sniper.persistence import db_memecoin as m
    from sniper.util.state_paths import state_path
    path = a.db or str(state_path("sniper_tracker.db"))
    if a.confirm:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row                # the ledger helpers read rows by name
        m._ensure_wallet_grade_exclusions(conn)      # readers need the table even before a deploy
        conn.commit()
    else:                                             # a dry run writes nothing, not even schema (Codex W10)
        from sniper.persistence.db_core import get_readonly_connection
        conn = get_readonly_connection(path)
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='memecoin_wallet_grade_exclusions'").fetchone() is not None
    pos = conn.execute("SELECT source_user_id, source_handle, network_id, ticker, realized_pnl "
                       "FROM memecoin_positions WHERE id=?", (a.position_id,)).fetchone()
    if pos is None:
        print("REFUSED: no position %s" % a.position_id)
        return 2
    uid, handle, net, ticker, pnl = pos
    before = (m.wallet_real_record(conn, uid, network_id=net) if has_table
              else "unknown: memecoin_wallet_grade_exclusions not created yet (--confirm creates it)")
    print("position %s %s %s on %s realized %s" % (a.position_id, ticker, handle, net, pnl))
    print("wallet grade on %s now: %s" % (net, before))
    if not a.confirm:
        print("DRY RUN: nothing written. Add --confirm to exclude it.")
        return 0
    try:
        m.exclude_from_wallet_grade(conn, a.position_id, reason=a.reason, actor=a.actor)
    except ValueError as exc:
        print("REFUSED: %s" % exc)
        return 2
    print("EXCLUDED. wallet grade on %s now: %s" % (net, m.wallet_real_record(conn, uid, network_id=net)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
