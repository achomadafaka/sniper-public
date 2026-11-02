"""Track which raw files have been compacted — prevents double-processing."""
from __future__ import annotations

import json
import os


class CompactManifest:
    """Persistent set of processed .jsonl.gz filenames."""

    def __init__(self, path: str):
        self._path = path
        self._processed: set[str] = set()
        if os.path.exists(path):
            with open(path, "r") as f:
                self._processed = set(json.load(f))

    def is_processed(self, filename: str) -> bool:
        return filename in self._processed

    def should_process(self, filename: str) -> bool:
        """Returns True only for unprocessed .jsonl.gz files."""
        if not filename.endswith(".jsonl.gz"):
            return False
        return not self.is_processed(filename)

    def mark_processed(self, filename: str) -> None:
        self._processed.add(filename)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(sorted(self._processed), f)
