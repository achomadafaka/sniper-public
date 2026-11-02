#!/usr/bin/env python3
"""WO-10 — WAL health check for the ops cron. REPORTS, never mitigates.

Distinct from scripts/wal_checkpoint.py (which checkpoints — the old "bounce" reflex). This one
diagnoses: for each engine DB it prints WAL size, whether a PASSIVE checkpoint is stalled (frames the
checkpointer can't reclaim), and the IDENTITY of every process holding the -wal open — so ops sees
WHICH long-lived reader is pinning the snapshot instead of guessing and bouncing everything.

Root cause (reproduced 2026-07-25, tests/test_wal_pinning.py): a connection sitting with an OPEN
transaction pins the WAL; a TRUNCATE returns busy=1 and the WAL grows unbounded. The fix landed in
get_readonly_connection (autocommit); this check catches any remaining writer-side offender fast.

Exit 0 = all WALs healthy; 1 = at least one over the warn bound or stalled.

    python3 scripts/wal_health.py                 # one page, all engine DBs
    crontab: */10 * * * * cd <repo> && .venv/bin/python3 scripts/wal_health.py >> logs/wal_health.log
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

WARN_MB = float(os.getenv("WAL_WARN_MB", "100"))


def _state_dbs():
    """The DBs the engine touches, anchored at the state root (never CWD)."""
    from sniper.util.state_paths import state_root
    root = state_root()
    return {
        "main": os.getenv("DB_PATH", str(root / "sniper_tracker.db")),
        "ticks": os.getenv("MEMECOIN_TICKS_DB", str(root / "data" / "memecoin_ticks.db")),
        "exit_state": os.getenv("MEMECOIN_EXIT_STATE_DB", str(root / "data" / "memecoin_exit_state.db")),
        "sol_tape": os.getenv("MEMECOIN_SOL_TAPE_DB", str(root / "data" / "memecoin_sol_tape.db")),
    }


def _wal_mb(db):
    wal = db + "-wal"
    return os.path.getsize(wal) / (1024 * 1024) if os.path.exists(wal) else 0.0


def _holders(target):
    """PIDs (with process name) holding an fd open on `target` — the pinner identity. Linux /proc,
    no lsof dependency; unreadable /proc entries (other users) are skipped."""
    target = os.path.realpath(target)
    out = []
    proc = Path("/proc")
    if not proc.exists():
        return out
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fddir = pid_dir / "fd"
        try:
            for fd in fddir.iterdir():
                try:
                    if os.path.realpath(str(fd)) == target:
                        try:
                            name = (pid_dir / "comm").read_text().strip()
                        except OSError:
                            name = "?"
                        out.append(f"{pid_dir.name}:{name}")
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


def _stalled_frames(db):
    """A PASSIVE checkpoint never blocks; if it leaves frames behind (log > checkpointed) a reader is
    pinning the WAL. Returns (log_pages, checkpointed_pages) or None if the DB can't be opened."""
    if not os.path.exists(db):
        return None
    try:
        c = sqlite3.connect(db, timeout=2, isolation_level=None)
        c.execute("PRAGMA busy_timeout=1000")
        r = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        c.close()
        return (int(r[1]), int(r[2])) if r else None
    except sqlite3.Error:
        return None


def main():
    unhealthy = False
    print(f"{'db':11s} {'wal_MB':>8s}  {'stalled':>8s}  holders")
    print("-" * 72)
    for label, db in _state_dbs().items():
        if not os.path.exists(db):
            print(f"{label:11s} {'—':>8s}  {'—':>8s}  (no DB)")
            continue
        mb = _wal_mb(db)
        st = _stalled_frames(db)
        # -wal holders that are NOT this short-lived process are the candidate pinners
        holders = [h for h in _holders(db + "-wal") if not h.startswith(f"{os.getpid()}:")]
        stalled = (st[0] - st[1]) if st else 0
        flag = ""
        if mb >= WARN_MB:
            flag += " WAL>bound"
            unhealthy = True
        if st and stalled > 0 and holders:
            flag += " PINNED"
            unhealthy = True
        print(f"{label:11s} {mb:8.1f}  {stalled:8d}  {', '.join(holders) or '-'}{flag}")
    print("-" * 72)
    print("VERDICT: " + ("WARN — a WAL is bloated or pinned (see holders above)" if unhealthy
                         else "OK — all WALs within bound, no pinners"))
    return 1 if unhealthy else 0


if __name__ == "__main__":
    sys.exit(main())
