"""Compatibility shim for the archived legacy stream_low_buys.

Full legacy implementation:
archive/legacy/legacy-pipeline-2026-04-30/stream_low_buys.py
"""

from __future__ import annotations

from pathlib import Path


_ARCHIVED_PATH = (
    Path(__file__).resolve().parent
    / "archive"
    / "legacy"
    / "legacy-pipeline-2026-04-30"
    / "stream_low_buys.py"
)

exec(compile(_ARCHIVED_PATH.read_text(encoding="utf-8"), str(_ARCHIVED_PATH), "exec"), globals())
