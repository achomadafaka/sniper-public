#!/usr/bin/env python3
"""Remove non-informative unknown holder history in bounded ID ranges."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def repair_history(db_path: Path, *, apply: bool = False, batch_size: int = 100_000,
                   vacuum: bool = False) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=60)
    try:
        unknown_before = int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_holder_evidence_history "
            "WHERE evidence_state='unknown'",
        ).fetchone()[0])
        if not apply:
            return {
                "unknown_before": unknown_before,
                "deleted": 0,
                "unknown_after": unknown_before,
            }

        max_id = int(conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM memecoin_holder_evidence_history",
        ).fetchone()[0])
        deleted = 0
        lower = 0
        while lower < max_id:
            upper = min(max_id, lower + batch_size)
            cursor = conn.execute(
                "DELETE FROM memecoin_holder_evidence_history "
                "WHERE id>? AND id<=? AND evidence_state='unknown'",
                (lower, upper),
            )
            deleted += max(0, int(cursor.rowcount))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            lower = upper

        unknown_after = int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_holder_evidence_history "
            "WHERE evidence_state='unknown'",
        ).fetchone()[0])
        if vacuum:
            conn.execute("VACUUM")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        return {
            "unknown_before": unknown_before,
            "deleted": deleted,
            "unknown_after": unknown_after,
        }
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true", help="perform deletion")
    parser.add_argument("--batch-size", type=int, default=100_000)
    parser.add_argument("--vacuum", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    print(json.dumps(repair_history(
        args.db, apply=args.apply, batch_size=args.batch_size, vacuum=args.vacuum,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
