"""Response envelope for all MCP tool outputs."""
import time
from datetime import datetime, timezone
from typing import Any


def make_envelope(
    data: Any,
    *,
    data_source: str,
    bias_warning: str | None = None,
    last_write_ts: float | None = None,
) -> dict:
    """Wrap tool output in a standard envelope with metadata."""
    now = time.time()
    # Preserve sub-second freshness so "fresh but real" timestamps don't collapse to 0.0.
    freshness = max(now - last_write_ts, 0.0) if last_write_ts is not None else 0.0
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": data_source,
        "freshness_seconds": freshness,
        "bias_warning": bias_warning,
        "data": data,
    }
