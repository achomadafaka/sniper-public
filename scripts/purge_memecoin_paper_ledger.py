#!/usr/bin/env python3
"""One-shot destructive purge of the paper ledger (spec 2026-07-30, Phase 1).

Every per-chain number computed before 2026-07-30 is a fill-model artifact: the
paper lab priced entries off a DexScreener mid and fell back to the fomo feed
where no pair existed, which on Robinhood 4663 produced 13,530 fomo-priced
entries and zero usable ones. This deletes that history so a clean evidence era
can start on executable route quotes.

Never touched:
  * real positions (execution_mode='real') — funded money history
  * memecoin_wallets — the roster, including arming state and exclusions
  * memecoin_events / memecoin_entry_snapshots — the signal and decision record.
    Snapshots are keyed by upstream_event_id, are written once per event before
    the arm fan-out, and log skips as well as opens. They are not paper results.
  * sqlite_sequence — see the seq guard below.
"""
import argparse
import sqlite3
import sys

_PAPER = "COALESCE(execution_mode,'paper')='paper'"

# Every table keyed to a position id. memecoin_pnl_repairs was missed originally and left 17
# rows pointing at deleted positions (audit 2026-07-30).
_DEPENDENTS = (("memecoin_fills", "position_id"), ("memecoin_pnl_repairs", "position_id"))


def purge_paper_ledger(conn, *, dry_run=False):
    """Delete every paper position and its fills. Returns a count report.

    Raises RuntimeError rather than deleting when a real position is still open,
    when the autoincrement sequence moved, or when rows survive the delete.
    """
    open_real = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=1").fetchone()[0]
    if open_real:
        raise RuntimeError(f"refusing to purge: {open_real} real position(s) still open")

    seq_before = _sequence(conn)
    report = {
        "positions": conn.execute(
            f"SELECT COUNT(*) FROM memecoin_positions WHERE {_PAPER}").fetchone()[0],
        "fills": conn.execute(
            "SELECT COUNT(*) FROM memecoin_fills WHERE position_id IN "
            f"(SELECT id FROM memecoin_positions WHERE {_PAPER})").fetchone()[0],
        "orphans_before": _orphan_count(conn),
        "real_kept": conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions "
            "WHERE execution_mode='real'").fetchone()[0],
        "wallets_kept": conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0],
        "seq_before": seq_before,
        "seq_after": seq_before,
    }
    if dry_run:
        return report

    # Post-conditions are checked INSIDE the transaction and the whole delete rolls back on
    # failure. Committing first (as this did originally) made the contract a lie: a tripped
    # assertion could not undo anything.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass
    try:
        # Dependents first, parents last. Not every dependent is created by
        # ensure_memecoin_tables (memecoin_pnl_repairs exists only where a repair has run),
        # so a missing table is skipped rather than aborting the purge.
        for table, column in _DEPENDENTS:
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE {column} IN "
                    f"(SELECT id FROM memecoin_positions WHERE {_PAPER})")
            except sqlite3.OperationalError:
                continue
        conn.execute(f"DELETE FROM memecoin_positions WHERE {_PAPER}")

        report["seq_after"] = _sequence(conn)
        if report["seq_after"] != seq_before:
            raise RuntimeError(
                f"sqlite_sequence changed {seq_before} -> {report['seq_after']}; id reuse would "
                "restart position ids below the V6 cursor and stop admission permanently")
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM memecoin_positions WHERE {_PAPER}").fetchone()[0]
        if remaining:
            raise RuntimeError(f"purge incomplete: {remaining} paper rows remain")
        orphans = _orphan_count(conn)
        if orphans:
            raise RuntimeError(f"purge left {orphans} orphaned dependent row(s)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def _orphan_count(conn):
    """Dependent rows pointing at a position id that no longer exists."""
    total = 0
    for table, column in _DEPENDENTS:
        try:
            total += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND {column} "
                "NOT IN (SELECT id FROM memecoin_positions)").fetchone()[0]
        except sqlite3.OperationalError:
            continue          # table absent in older schemas
    return total


def _sequence(conn):
    """Current AUTOINCREMENT high-water mark for memecoin_positions (0 when unset)."""
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()
    return int(row[0]) if row else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--apply", action="store_true", help="without this, dry-run only")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    report = purge_paper_ledger(conn, dry_run=not args.apply)
    print(f"[{'APPLIED' if args.apply else 'DRY-RUN'}] {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
