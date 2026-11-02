#!/usr/bin/env python3
"""Purge old empty-decode receipts from data/memecoin_fill_timing.db (deploy stop window only).

Gamma a61a5bb27: 99.6% of memecoin_signal_receipts rows were empty decodes carrying a 7.1 KB roster
copy (15.5 GB of 17.8 GB). New rows no longer carry it; this removes the old ones.

  python3 scripts/prune_empty_receipts.py            # dry run: counts only (runner stopped)
  python3 scripts/prune_empty_receipts.py --confirm  # delete (runner must be stopped)

Both modes refuse while the runner heartbeat is younger than STOPPED_AFTER_S or unreadable. Keep the
fill_timing_resolver cron paused while it runs. It frees pages for reuse; reclaiming the file
size is a separate VACUUM in the same stop window.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STOPPED_AFTER_S = 120.0


def runner_live(heartbeat_age_s):
    """Unknown age counts as live: a purge must prove the writer is gone, not assume it."""
    return heartbeat_age_s is None or heartbeat_age_s <= STOPPED_AFTER_S


def heartbeat_age_s():
    from sniper.util.state_paths import state_path
    try:
        ts = float(json.loads(Path(state_path("data", "memecoin_runner.heartbeat")).read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(0.0, time.time() - ts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)
    from sniper.services import memecoin_fill_timing as ft
    age = heartbeat_age_s()
    print(f"runner heartbeat age: {age}")
    if runner_live(age):
        # Checked BEFORE the file is opened: even the dry run's COUNT(*) is a full scan of a
        # multi-GB live file, which must not run beside the drain thread (Gamma nit, 18c83d208).
        print("REFUSED: the runner looks live (heartbeat fresh or unreadable); stop it first")
        return 2
    conn = ft.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM memecoin_signal_receipts "
                         "WHERE decode_state='empty_decode_unknown'").fetchone()[0]
        print(f"empty-decode receipts: {n}")
        if not args.confirm:
            print("dry run; pass --confirm in the stop window to delete")
            return 0
        print(f"deleted {ft.prune_empty_receipts(conn)} rows")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
