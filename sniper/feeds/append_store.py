"""Append-only JSONL feed store with payload-hash idempotency (Phase 1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from sniper.feeds.thesis_feed_types import FeedEvent


class JsonlAppendStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._seen: dict[str, set[str]] = {}

    def _path(self, source: str) -> Path:
        return self.root / f"{source}.jsonl"

    def _load_seen(self, source: str) -> set[str]:
        if source in self._seen:
            return self._seen[source]
        seen: set[str] = set()
        path = self._path(source)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    seen.add(json.loads(line)["payload_hash"])
        self._seen[source] = seen
        return seen

    def append(self, source: str, event: FeedEvent) -> bool:
        seen = self._load_seen(source)
        if event.payload_hash in seen:
            return False
        with self._path(source).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.as_dict(), sort_keys=True) + "\n")
        seen.add(event.payload_hash)
        return True

    def read(self, source: str) -> Iterator[dict]:
        path = self._path(source)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)
