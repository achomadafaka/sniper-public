"""Append-only JSONL writer with post-rotation compression.

Writes plain text JSONL during active collection (crash-safe — worst case
loses the last partial line). On hourly rotation, the completed file is
gzipped and the original removed. This avoids the gzip-append corruption
bug where a crash mid-stream corrupts the entire file tail.
"""
from __future__ import annotations

import gzip
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from sniper.sim.schema import RawEvent
from sniper.sim.serialize import event_to_jsonl


class RawWriter:
    """Writes RawEvents to JSONL files, compresses on rotation.

    File layout during collection: {raw_dir}/{venue}/{YYYY-MM-DD}/{HH}.jsonl
    After rotation: {raw_dir}/{venue}/{YYYY-MM-DD}/{HH}.jsonl.gz
    One file per venue per hour. All markets batched together.
    """

    def __init__(self, raw_dir: str, venue: str, rotation_seconds: int = 3600):
        self._raw_dir = raw_dir
        self._venue = venue
        self._rotation_seconds = rotation_seconds
        self._current_hour: Optional[str] = None
        self._current_path: Optional[str] = None
        self._file = None

    def _hour_key(self) -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d/%H")

    def _compress_and_remove(self, path: str) -> None:
        """Gzip a completed JSONL file and remove the original."""
        gz_path = path + ".gz"
        try:
            with open(path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=1) as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(path)
        except OSError:
            pass  # If compression fails, keep the uncompressed file

    def _ensure_file(self):
        hour = self._hour_key()
        if self._file is not None and self._current_hour == hour:
            return self._file

        self.close()
        date_part, hour_part = hour.split("/")
        dir_path = os.path.join(self._raw_dir, self._venue, date_part)
        os.makedirs(dir_path, exist_ok=True)
        self._current_path = os.path.join(dir_path, f"{hour_part}.jsonl")
        self._file = open(self._current_path, "a", encoding="utf-8")
        self._current_hour = hour
        return self._file

    def write(self, event: RawEvent) -> None:
        f = self._ensure_file()
        line = event_to_jsonl(event)
        f.write(line + "\n")

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            # Compress the completed file
            if self._current_path and os.path.exists(self._current_path):
                self._compress_and_remove(self._current_path)
            self._file = None
            self._current_hour = None
