"""Write row dicts to date-partitioned Parquet files."""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class ParquetTableWriter:
    """Accumulates rows in memory, writes to Parquet on flush().

    Layout: {base_dir}/{table_name}/{YYYY-MM-DD}.parquet
    """

    def __init__(self, base_dir: str):
        self._base_dir = base_dir
        self._buffers: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    def append(self, table_name: str, date: str, rows: list[dict[str, Any]]) -> None:
        key = (table_name, date)
        self._buffers[key].extend(rows)

    def flush(self) -> dict[str, int]:
        """Write all buffered rows to Parquet. Returns {table_name: rows_written}."""
        counts: dict[str, int] = defaultdict(int)
        for (table_name, date), rows in self._buffers.items():
            if not rows:
                continue
            dir_path = os.path.join(self._base_dir, table_name)
            os.makedirs(dir_path, exist_ok=True)
            file_path = os.path.join(dir_path, f"{date}.parquet")

            table = pa.Table.from_pylist(rows)

            if os.path.exists(file_path):
                existing = pq.read_table(file_path)
                table = pa.concat_tables([existing, table], promote_options="default")

            pq.write_table(table, file_path)
            counts[table_name] += len(rows)

        self._buffers.clear()
        return dict(counts)
