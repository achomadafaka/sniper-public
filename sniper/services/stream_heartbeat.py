"""Shared helpers for the realtime stream heartbeat file."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

STREAM_HEARTBEAT_PATH = Path(
    os.getenv("STREAM_HEARTBEAT_PATH", "/tmp/sniper_stream_heartbeat.json")
).expanduser()


def read_stream_heartbeat(path: str | Path | None = None) -> dict[str, Any]:
    """Read the latest persisted stream heartbeat, if present."""
    heartbeat_path = Path(path).expanduser() if path else STREAM_HEARTBEAT_PATH
    try:
        return json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def write_stream_heartbeat(
    *,
    last_ws_message_ts: float | None = None,
    last_meta_refresh_ts: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically persist stream heartbeat timestamps for cross-process health checks."""
    heartbeat_path = Path(path).expanduser() if path else STREAM_HEARTBEAT_PATH
    payload = read_stream_heartbeat(heartbeat_path)
    if last_ws_message_ts is not None:
        payload["last_ws_message_ts"] = float(last_ws_message_ts)
    if last_meta_refresh_ts is not None:
        payload["last_meta_refresh_ts"] = float(last_meta_refresh_ts)
    payload["updated_at"] = time.time()
    payload["pid"] = os.getpid()

    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=str(heartbeat_path.parent),
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(payload, tmp, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, heartbeat_path)
    return payload
