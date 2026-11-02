#!/usr/bin/env python3
"""Create a bounded, verified SQLite online backup of the main Sniper ledger."""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _fsync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _prune(backup_dir: Path, keep: int) -> list[str]:
    finished = sorted(
        [*backup_dir.glob("sniper_tracker-*.db"), *backup_dir.glob("sniper_tracker-*.db.gz")],
        key=lambda path: path.name,
        reverse=True,
    )
    removed = []
    for path in finished[keep:]:
        path.unlink()
        removed.append(path.name)
    return removed


def create_backup(source: Path, backup_dir: Path, *, keep: int = 2, compress: bool = True,
                  timestamp: str | None = None, max_wal_bytes: int = 512 << 20) -> dict:
    source = Path(source).expanduser().resolve()
    backup_dir = Path(backup_dir).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if keep < 1 or max_wal_bytes < 1:
        raise ValueError("keep and max_wal_bytes must be positive")
    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    raw_tmp = backup_dir / f"sniper_tracker-{stamp}.db.tmp"
    final = backup_dir / f"sniper_tracker-{stamp}.db"
    gzip_tmp = backup_dir / f"sniper_tracker-{stamp}.db.gz.tmp"
    if compress:
        final = backup_dir / f"sniper_tracker-{stamp}.db.gz"
    lock_path = backup_dir / ".daily_backup.lock"
    wal_path = Path(str(source) + "-wal")
    temporary_paths = (
        raw_tmp,
        gzip_tmp,
        Path(f"{raw_tmp}-wal"),
        Path(f"{raw_tmp}-shm"),
    )

    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("daily backup already running") from exc
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)

        def progress(_status, _remaining, _total):
            wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
            if wal_bytes > max_wal_bytes:
                raise RuntimeError(
                    f"backup aborted: WAL grew to {wal_bytes} bytes "
                    f"(limit {max_wal_bytes})")

        try:
            source_conn = sqlite3.connect(
                f"{source.as_uri()}?mode=ro", uri=True, timeout=60, isolation_level=None)
            destination = None
            try:
                destination = sqlite3.connect(raw_tmp, timeout=60)
                source_conn.backup(destination, pages=2048, progress=progress, sleep=0.05)
            finally:
                if destination is not None:
                    destination.close()
                source_conn.close()
            integrity = _integrity(raw_tmp)
            if integrity != "ok":
                raise RuntimeError(f"backup integrity_check failed: {integrity}")

            if compress:
                with raw_tmp.open("rb") as source_file, gzip.open(
                        gzip_tmp, "wb", compresslevel=6) as out:
                    shutil.copyfileobj(source_file, out, length=8 << 20)
                _fsync(gzip_tmp)
                os.replace(gzip_tmp, final)
            else:
                _fsync(raw_tmp)
                os.replace(raw_tmp, final)
            removed = _prune(backup_dir, keep)
            return {
                "path": str(final), "bytes": final.stat().st_size,
                "integrity": integrity, "removed": removed,
            }
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=root / "sniper_tracker.db")
    parser.add_argument("--backup-dir", type=Path, default=root / "data" / "backups" / "daily")
    parser.add_argument("--keep", type=int, default=2)
    parser.add_argument("--max-wal-mb", type=int, default=512)
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.dry_run:
        if not args.db.expanduser().resolve().is_file():
            raise FileNotFoundError(args.db)
        print(json.dumps({
            "dry_run": True, "db": str(args.db.expanduser().resolve()),
            "backup_dir": str(args.backup_dir.expanduser().resolve()), "keep": args.keep,
            "max_wal_mb": args.max_wal_mb,
        }, sort_keys=True))
        return 0
    result = create_backup(
        args.db, args.backup_dir, keep=args.keep, compress=not args.no_compress,
        max_wal_bytes=args.max_wal_mb << 20,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
