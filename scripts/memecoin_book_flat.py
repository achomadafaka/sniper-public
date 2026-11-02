#!/usr/bin/env python3
"""Exit 0 when the funded book is flat: no open real position and no reserved pending entry.

Usage: memecoin_book_flat.py [--dip-watches] [db_path]   (default: <repo>/sniper_tracker.db)

--dip-watches (the deploy waiter and memecoin_deploy_pinned.sh; Alpha 2026-10-01, after the 15:29Z
deploy cut a live Degen dip watch): a live dip-retry row also keeps the book not flat. A dip row
(memecoin_entry_retry_queue.dip_json) is live until its deadline + DIP_MARGIN_S. Past its deadline
run_hits ends the row with no decision, so it can no longer sign; a row still there after the margin
(runner down, lane stuck) is printed as stale and does NOT block, or it would hold every deploy,
the fixing one included; so is a row whose signal time is more than the margin in the future. Shadow-chain rows block too, for at most their window: telling them apart
needs the runner's env, which this read-only check does not load. An unreadable deadline raises.
deploy_memecoin_runtime.sh --require-flat does NOT pass the flag: it checks after its kill switch,
when a dip hit is refused anyway, and a live row there would fail the deploy mid-cutover.

The deploy waiter (scripts/memecoin_wait_flat_then_deploy.sh) and scripts/memecoin_deploy_pinned.sh
gate on this exit code. Any read failure (missing DB, missing table) raises and exits non-zero, so a
waiter never deploys on a book it could not read. A Long Hold position is open, so it keeps the book
not flat; that is deliberate and is the operator's call, not this script's.

Moved from a session scratchpad on tmpfs, where the 2026-09-24 21:55Z reboot erased it and the next
waiter died on a missing path with nothing deployed.
"""
import contextlib
import json
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "sniper_tracker.db"
DIP_MARGIN_S = 30.0   # a decision begun at the deadline has reserved (pending blocks) or ended by then


def book_counts(db_path):
    """(open real positions, reserved pending entries) from a read-only connection."""
    with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        open_real = conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1"
        ).fetchone()[0]
        reserved = conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'"
        ).fetchone()[0]
    return open_real, reserved


def dip_watches(db_path, now=None):
    """(live dip rows, stale dip row ids) from a read-only connection. Raises on a row whose
    deadline cannot be read: an unknown window is never treated as closed."""
    now = time.time() if now is None else now
    live, stale = 0, []
    with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT fomo_event_id, dip_json FROM memecoin_entry_retry_queue "
                            "WHERE dip_json IS NOT NULL").fetchall()
    for fid, dip_json in rows:
        try:
            dip = json.loads(dip_json)
            deadline, signal_at = float(dip["deadline"]), float(dip["signal_at"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("dip row %s: deadline unreadable (%r)" % (fid, exc)) from exc
        # A signal stamped in the future (or in milliseconds) would hold the waiter until it gives
        # up: stale, printed, not live (Gamma LOW on 67fab47ff).
        if signal_at - DIP_MARGIN_S <= now < deadline + DIP_MARGIN_S:
            live += 1
        else:
            stale.append(fid)
    return live, stale


def main(argv):
    args = argv[1:]
    with_dips = "--dip-watches" in args
    args = [a for a in args if a != "--dip-watches"]
    db_path = args[0] if args else DEFAULT_DB
    open_real, reserved = book_counts(db_path)
    if not with_dips:
        print("book: funded open %d pending %d" % (open_real, reserved))
        return 0 if open_real == 0 and reserved == 0 else 1
    live, stale = dip_watches(db_path)
    print("book: funded open %d pending %d dip watches %d" % (open_real, reserved, live))
    for fid in stale:
        print("stale dip row %s: outside its signal - %.0f s .. deadline + %.0f s, not blocking"
              % (fid, DIP_MARGIN_S, DIP_MARGIN_S))
    return 0 if open_real == 0 and reserved == 0 and live == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
