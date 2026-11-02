#!/usr/bin/env python3
"""Funded history: backfill the executable top and bottom, and clear the display-derived columns.

WHY (operator orders 367d4ad6 / 86d053ce, 2026-09-30). Funded price recording uses only the
high-cadence executable series. The runner records each funded position's executable top and bottom
for RECENT closes (the last 24 hours). This script does the rest of the history, off the runner's
process, and removes the display-derived `max_gain_pct` / `time_to_peak_s` from closed funded rows.

WHAT IT DOES, with --confirm:
1. Every closed funded position that has a positive-price exit_decision quote and no extremes row
   gets one in `memecoin_position_exec_extremes`.
2. Every closed funded row that holds `max_gain_pct` or `time_to_peak_s` has those values copied to
   `memecoin_position_display_extremes_backup` and then set to NULL, in one transaction per batch.
   A backup row is written once and never overwritten.

Open positions and paper rows are never touched. A second run writes nothing.

UNDO: --restore --confirm copies the backed-up values back. The backup table stays.

    python3 scripts/memecoin_exec_extremes_history.py                 # dry run: counts only
    python3 scripts/memecoin_exec_extremes_history.py --confirm
    python3 scripts/memecoin_exec_extremes_history.py --restore --confirm
The operator runs --confirm; nobody else.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _batches(ids, size):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", help="ledger path (default: the funded ledger)")
    ap.add_argument("--confirm", action="store_true", help="write (operator only)")
    ap.add_argument("--restore", action="store_true", help="copy the backed-up values back")
    ap.add_argument("--batch", type=int, default=200, help="positions per transaction")
    args = ap.parse_args(argv)

    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    from sniper.util.clock import real_utc_now_iso

    reader = get_readonly_connection(args.db)
    try:
        targets = m.exec_extremes_history_targets(reader)
    except sqlite3.OperationalError as exc:
        print("REFUSED: the extremes tables are not in this ledger (%s); deploy the runner release "
              "that creates them first." % exc)
        return 2
    finally:
        reader.close()
    print("closed funded positions: %d" % targets["closed_funded"])
    print("extremes rows: %d present, %d to write" % (targets["extremes_rows"], len(targets["to_backfill"])))
    print("no positive-price series: %d (these get no extremes row)" % targets["no_series"])
    print("display values: %d to back up and clear, %d backed up earlier"
          % (len(targets["to_clear"]), targets["backed_up"]))
    if not args.confirm:
        print("DRY RUN: nothing written. Add --confirm to write (operator only).")
        return 0

    writer = get_connection(args.db)
    try:
        if args.restore:
            restored = m.restore_funded_display_extremes(writer)
            writer.commit()
            print("restored %d closed funded rows from the backup; the backup table is unchanged" % restored)
            return 0
        now = real_utc_now_iso()
        written = 0
        for batch in _batches(targets["to_backfill"], max(1, args.batch)):
            written += m.backfill_exec_extremes(writer, batch, now)      # reads, then writes
            writer.commit()
        print("wrote %d extremes rows" % written)
        before = m.display_extremes_backup_count(writer)
        cleared = 0
        for batch in _batches(targets["to_clear"], max(1, args.batch)):
            cleared += sum(m.clear_funded_display_extremes(writer, pid, now) for pid in batch)
            writer.commit()                 # the backup and the clear of a batch commit together
        print("backed up %d, cleared %d" % (m.display_extremes_backup_count(writer) - before, cleared))
        return 0
    except Exception:
        writer.rollback()
        raise
    finally:
        writer.close()


if __name__ == "__main__":
    sys.exit(main())
