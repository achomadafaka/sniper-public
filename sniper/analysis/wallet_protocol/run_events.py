"""JSONL run-event emission for wallet protocol runners."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonlEmitter:
    """Append one structured JSON object per event."""

    def __init__(self, path: str | Path, *, run_id: str, wallet: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.wallet = wallet
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        row = {
            "run_id": self.run_id,
            "wallet": self.wallet,
            "event": event,
            "ts": int(time.time()),
            "payload": payload or {},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


__all__ = ["JsonlEmitter"]
