#!/usr/bin/env python3
"""Export ``signal_history`` to a deterministic parquet snapshot.

Phase 3 Workstream E. The snapshot is used later by replay/backfill work
so historical score-bearing rows survive the position_signals split.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


REQUIRED_COLUMNS: tuple[str, ...] = (
    "position_insider_score",
    "position_speculator_score",
    "position_classification",
    "entry_anchor_ts",
)


def default_snapshot_path(today: dt.date | None = None) -> Path:
    day = today or dt.date.today()
    return Path("archive") / "snapshots" / f"{day.isoformat()}-signal-history.parquet"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [str(row[1]) for row in rows]


def _validate_columns(columns: Iterable[str]) -> None:
    colset = set(columns)
    missing = [c for c in REQUIRED_COLUMNS if c not in colset]
    if missing:
        raise RuntimeError(
            "signal_history missing required columns: " + ", ".join(missing)
        )


def export_signal_history(
    *,
    db_path: str | Path,
    output_path: str | Path | None = None,
    compression: str = "zstd",
) -> Path:
    """Export all ``signal_history`` rows to parquet.

    Rows are ordered by ``id`` and the DataFrame index is not persisted, so
    rerunning over the same DB contents produces the same bytes.
    """
    db_path = Path(db_path)
    output = Path(output_path) if output_path is not None else default_snapshot_path()
    output.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        columns = _table_columns(conn, "signal_history")
        if not columns:
            raise RuntimeError("signal_history table does not exist")
        _validate_columns(columns)

        selected = ", ".join(_quote_ident(c) for c in columns)
        df = pd.read_sql_query(
            f"SELECT {selected} FROM signal_history ORDER BY id",
            conn,
        )
    finally:
        conn.close()

    table = pa.Table.from_pandas(df, preserve_index=False)
    tmp = output.with_name(output.name + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression=compression,
        version="2.6",
        use_dictionary=True,
    )
    os.replace(tmp, output)
    return output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.getenv("SNIPER_DB_PATH", "sniper_tracker.db"),
        help="SQLite DB path. Defaults to SNIPER_DB_PATH or sniper_tracker.db.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output parquet path. Defaults to archive/snapshots/<today>-signal-history.parquet.",
    )
    parser.add_argument(
        "--compression",
        default="zstd",
        help="Parquet compression codec passed to pyarrow.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = export_signal_history(
        db_path=args.db,
        output_path=args.output,
        compression=args.compression,
    )
    df = pd.read_parquet(path)
    print(f"snapshot={path}")
    print(f"rows={len(df)}")
    print(f"sha256={sha256_file(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
