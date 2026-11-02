#!/usr/bin/env python3
"""One-time OFFLINE drain of the PumpSwap keep-window backlog (operator via Alpha 2026-10-28: tape.db hit its 50 GB cap;
PS_ARCHIVE_AFTER_S went 7 d -> 2 d -> 6 h). Moves every ps_keep window that closed more than --after-s (default PS_ARCHIVE_AFTER_S) ago from
data/desk/pumpswap/tape.db to data/archive/pumpswap/keep-<day>.db.

It runs the universe worker's OWN Worker.archive_keep_ps (copy, commit, read the copy back, delete only rows read back
equal; a mismatch or failure leaves the window in tape.db and counts it), only with bigger batches, in a loop. The live
worker drains the same backlog at PS_PRUNE_BATCH rows per capture pass; this is for when that is too slow.

    ionice -c3 nice -n19 python3 scripts/archive_ps_keep_backlog.py [--confirm] [--batch 20000] [--base data/desk]

Dry run by default: prints the eligible windows and their row count, changes nothing. --confirm moves them.
Refuses while the universe worker (data/desk/universe.pid) is alive, and stops between batches if the watchdog restarts
it (stop the worker AND comment out its cron lines first; Alpha does crons). Readers may keep the tape open (WAL).
A stopped run is safe to re-run: a window's rows leave tape.db only after an equal archive copy, and INSERT OR IGNORE
skips rows already copied. The deleted rows' pages go to the tape's freelist: the file does not shrink, the cap's LIVE
size does (ps_live_bytes), and new rows reuse the pages.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _module(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def eligible(conn, cutoff):
    """(windows, rows) whose keep window closed before cutoff."""
    windows = conn.execute("SELECT pool, keep_from, keep_until FROM ps_keep WHERE keep_until < ?", (cutoff,)).fetchall()
    rows = sum(conn.execute("SELECT COUNT(*) FROM swaps_ps WHERE pool = ? AND received_at BETWEEN ? AND ?", w).fetchone()[0]
               for w in windows)
    return len(windows), rows


def drain(base, *, now, batch, after_s=None, alive=lambda: False, log=print):
    """Archive every eligible window with the worker's own code. Returns the worker's stats Counter."""
    W = _module("ps_universe_worker", "scripts/memecoin_desk_universe_worker.py")
    W.PS_PRUNE_BATCH = batch
    w = W.Worker.__new__(W.Worker)           # only the archive state; no capture, no sockets, no config
    w.base, w.files = Path(base), W.DayFiles(Path(base))
    w.ps_archive_conn = w.ps_archive_day = w.ps_archive_lock = None
    w.ps_archive_stuck, w.stats = set(), __import__("collections").Counter()
    conn = w.files.conn("ps_keep", now)
    after_s = W.PS_ARCHIVE_AFTER_S if after_s is None else after_s
    W.PS_ARCHIVE_AFTER_S = after_s                      # archive_keep_ps reads the module global: this drain's own copy
    cutoff, busy_run = now - after_s, 0
    try:
        while True:
            if alive():
                log("the universe worker is running again (watchdog?): stopped; re-run after stopping it")
                break
            left = conn.execute("SELECT COUNT(*) FROM ps_keep WHERE keep_until < ?", (cutoff,)).fetchone()[0]
            if left <= len(w.ps_archive_stuck):
                break
            before = (w.stats["ps_archived"], w.stats["ps_archived_windows"], len(w.ps_archive_stuck),
                      w.stats["ps_archive_busy"])
            w.archive_keep_ps(now)
            if before == (w.stats["ps_archived"], w.stats["ps_archived_windows"], len(w.ps_archive_stuck),
                          w.stats["ps_archive_busy"]):
                log("no progress on a pass: stopped")       # never spin
                break
            if w.stats["ps_archive_busy"] > before[3]:      # a reader holds the archive day file
                busy_run += 1
                if busy_run >= 30:
                    log("archive file busy for 30 passes: stopped")
                    break
                time.sleep(1)
            else:
                busy_run = 0
    finally:
        if w.ps_archive_conn is not None:
            w.ps_archive_conn.close()                       # the last day may still get windows: left writable
        if w.ps_archive_lock is not None:
            os.close(w.ps_archive_lock)                     # the day's seal lock is an fd, not the sqlite handle
        for c in w.files._open.values():
            c.commit()
            c.close()
    return w.stats


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=None, help="the desk data dir (default <state>/data/desk)")
    p.add_argument("--batch", type=int, default=20000)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--after-s", type=float, default=None, help="windows closed more than this ago (default PS_ARCHIVE_AFTER_S)")
    a = p.parse_args(argv)
    if a.batch < 1:
        raise SystemExit("--batch must be >= 1")
    if a.after_s is not None and not (a.after_s > 0 and a.after_s < float("inf")):
        raise SystemExit("--after-s must be a finite number of seconds > 0")
    from sniper.util.state_paths import state_path
    base = Path(a.base or state_path("data", "desk")).resolve()   # a relative --base breaks Path.as_uri (Delta 7e1ead403)
    tape = base / "pumpswap" / "tape.db"
    if not tape.exists():
        raise SystemExit("no tape at %s" % tape)
    M = _module("migrate_ps_tape", "scripts/migrate_ps_tape.py")
    pid_file = base / "universe.pid"
    alive = lambda: M.worker_alive(pid_file)
    if alive():
        raise SystemExit("the universe worker is running: stop it and comment out its cron lines first")
    W = _module("ps_universe_worker", "scripts/memecoin_desk_universe_worker.py")
    now = time.time()
    ro = sqlite3.connect(tape.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        after_s = W.PS_ARCHIVE_AFTER_S if a.after_s is None else a.after_s
        windows, rows = eligible(ro, now - after_s)
    finally:
        ro.close()
    print("eligible: %d keep windows, %d swaps_ps rows (closed before now - %d s)" % (windows, rows, after_s))
    if not a.confirm:
        print("dry run: nothing changed (re-run with --confirm)")
        return 0
    try:
        os.nice(19)
        import subprocess
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, capture_output=True)
    except OSError:
        pass
    stats = drain(base, now=now, batch=a.batch, after_s=after_s, alive=alive)
    print("archived rows=%d windows=%d mismatches=%d errors=%d busy=%d" % (
        stats["ps_archived"], stats["ps_archived_windows"], stats["ps_archive_mismatch"], stats["ps_archive_errors"],
        stats["ps_archive_busy"]))
    return 1 if stats["ps_archive_mismatch"] or stats["ps_archive_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
