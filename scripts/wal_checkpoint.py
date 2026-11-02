#!/usr/bin/env python3
"""
WAL checkpoint maintenance script.

Default strategy: PASSIVE only. It checkpoints available frames without waiting
for readers and is safe to run from cron while live services write.

Operator reclaim strategy (`--reclaim`): PASSIVE, then RESTART/TRUNCATE when the
WAL remains over the configured bounds. RESTART/TRUNCATE can hold the checkpoint
lock while waiting and must not run as routine live maintenance.

Usage:
    python3 scripts/wal_checkpoint.py             # live-safe PASSIVE one-shot
    python3 scripts/wal_checkpoint.py --reclaim   # explicit operator reclaim
    crontab: */3 * * * * cd /home/sniper/sniper && .venv/bin/python3 scripts/wal_checkpoint.py
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / os.getenv("DB_PATH", "sniper_tracker.db")
WAL_WARN_MB = float(os.getenv("WAL_WARN_MB", "100"))       # warn above this
WAL_RESTART_MB = float(os.getenv("WAL_RESTART_MB", "150")) # escalate to RESTART
WAL_TRUNCATE_MB = float(os.getenv("WAL_TRUNCATE_MB", "100")) # try TRUNCATE (must stay below 200MB ops/health alert)
# The size the WAL file is truncated back to when a checkpoint resets it. Matches
# `db_core.get_connection`, so every writer and this maintenance script agree on one bound.
WAL_SIZE_LIMIT_MB = float(os.getenv("WAL_SIZE_LIMIT_MB", "16"))


def _wal_mb(wal_path: Path) -> float:
    return wal_path.stat().st_size / (1024 * 1024) if wal_path.exists() else 0.0


def _checkpoint(conn, mode: str, timeout: int = 30) -> tuple[int, int, int]:
    conn.execute(f"PRAGMA busy_timeout={timeout * 1000}")
    row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    return row  # (blocked, total_pages, checkpointed_pages)


def checkpoint_db(DB_PATH: Path, *, reclaim: bool = False) -> int:
    now_str = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if not DB_PATH.exists():
        print(f"[{now_str()}] ERROR: DB not found at {DB_PATH}")
        return 1

    db_mb = DB_PATH.stat().st_size / (1024 * 1024)
    wal_path = Path(str(DB_PATH) + "-wal")
    wal_before = _wal_mb(wal_path)

    print(f"[{now_str()}] PRE  — DB: {db_mb:.0f}MB, WAL: {wal_before:.1f}MB")

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    # SET THE LIMIT ON THIS CONNECTION, OR THE FILE NEVER SHRINKS. A checkpoint resets the WAL's
    # write pointer; it does NOT truncate the file. Only `journal_size_limit` does that, and it is
    # a PER-CONNECTION setting that applies to the connection performing the reset. This script
    # opened a bare connection, so every run checkpointed 100% of frames and left the file at its
    # high-water mark — the log shows `pages: 117929/117929 | blocked=0` beside `WAL: 463.4MB` and
    # then warned about the size it had just declined to reclaim, every three minutes.
    # `db_core.get_connection` has always set this; the maintenance script that exists to bound the
    # WAL was the one connection that did not. The comment further down already assumed it was in
    # force ("journal_size_limit then truncates on the reset").
    conn.execute(f"PRAGMA journal_size_limit={int(WAL_SIZE_LIMIT_MB * 1024 * 1024)};")

    # Step 1: PASSIVE — never blocks
    blocked, total, done = _checkpoint(conn, "PASSIVE")
    wal_after = _wal_mb(wal_path)
    print(
        f"[{now_str()}] PASSIVE — WAL: {wal_after:.1f}MB | "
        f"pages: {done}/{total} | blocked={blocked}"
    )

    # RESTART/TRUNCATE are operator-only. They can hold the checkpoint lock while waiting,
    # which delayed live writers and even blocked an armed-runner restart during the 2026-07-27
    # WAL incident. Cron/default maintenance must return after PASSIVE.
    if reclaim and wal_after > WAL_RESTART_MB:
        blocked2, total2, done2 = _checkpoint(conn, "RESTART", timeout=15)
        wal_after = _wal_mb(wal_path)
        print(
            f"[{now_str()}] RESTART — WAL: {wal_after:.1f}MB | "
            f"pages: {done2}/{total2} | blocked={blocked2}"
        )

    # Step 3: TRUNCATE if WAL still large. RETRY loop (2026-07-18): a single 30s attempt loses to a
    # reader that pins across the whole window, so the WAL drifted past the 200MB ops_health warn
    # between cron runs ("fixed itself" only once a later run caught a clear window). Retry TRUNCATE
    # up to ~25s of 1s attempts — the funded runner's pins are intermittent (per-poll), so a gap
    # almost always opens; journal_size_limit then truncates on the reset.
    if reclaim and wal_after > WAL_TRUNCATE_MB:
        import time as _time
        blocked3 = 1
        for _ in range(25):
            blocked3, total3, done3 = _checkpoint(conn, "TRUNCATE", timeout=2)
            if blocked3 == 0:
                break
            _time.sleep(1)
        wal_after = _wal_mb(wal_path)
        print(
            f"[{now_str()}] TRUNCATE — WAL: {wal_after:.1f}MB | "
            f"pages: {done3}/{total3} | blocked={blocked3}"
        )

    conn.close()

    db_mb_after = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"[{now_str()}] POST — DB: {db_mb_after:.0f}MB, WAL: {wal_after:.1f}MB")

    if wal_after > WAL_WARN_MB and (blocked != 0 or done < total):
        print(f"[{now_str()}] WARN — WAL still {wal_after:.0f}MB (>{WAL_WARN_MB:.0f}MB threshold)")
        return 2

    return 0


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reclaim",
        action="store_true",
        help="allow blocking RESTART/TRUNCATE escalation; operator use only",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    # ticks DB added 2026-07-24: its 22MB WAL on a 1.2GB DB went uncheckpointed because only the
    # main DB was covered; the funded tape writes there continuously.
    # sol tape DB added 2026-07-24 (own-DB split): the Solana pull tape now writes its own file.
    # exit-state DB added 2026-07-24 (P0-3 own-DB split): the exit-state worker's snapshots,
    # candidate decisions, and RPC-backfilled block_times now write their own file too.
    root = Path(__file__).resolve().parent.parent
    # P2 (Codex round 2): resolve via the SAME state-root/env-override functions the code uses —
    # script-relative paths made a pinned release checkpoint its own discarded release-local files
    # instead of the real state-root DBs, and ignored MEMECOIN_*_DB overrides.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sniper.persistence.db_ticks import sol_tape_db_path, ticks_db_path
    from sniper.persistence.db_exit_state import exit_state_db_path
    dbs = [DB_PATH, Path(ticks_db_path()), Path(sol_tape_db_path()), Path(exit_state_db_path())]
    return max(checkpoint_db(db, reclaim=args.reclaim) for db in dbs if db.exists())


if __name__ == "__main__":
    sys.exit(main())
