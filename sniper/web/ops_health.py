"""Pure operational-health classifiers used by the web status endpoint."""

from __future__ import annotations

import os


def usable_cpu_count() -> int | None:
    """Return CPUs this process may use, preferring Linux affinity limits."""
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass

    count = os.cpu_count()
    return int(count) if count and count > 0 else None


def classify_load(
    load_1min: float,
    cpu_count: int | None,
    load_5min: float | None = None,
) -> tuple[float, str | None]:
    """Classify load against usable CPU capacity.

    High load is only a real problem when it PERSISTS, the same rule the swap check already uses.
    A funded-runner redeploy restarts about ten workers at once, and one research query can pin a
    core for minutes; both push the 1-minute average over the threshold for a minute or two on a
    box that is otherwise 90%+ idle. Flagging that marked a healthy host DEGRADED (observed
    2026-09-22: 1-min 17.94 with 32 CPUs, 92-95% idle in vmstat, back to 6.92 minutes later).

    So a spike needs the 5-minute average to agree before it becomes an issue. `load_5min=None`
    keeps the old single-sample behaviour for callers that have only the one number.
    """
    threshold = round(max(1.0, cpu_count * 0.9), 2) if cpu_count else 3.0
    sustained = load_5min is None or load_5min > threshold
    issue = f"load_high:{load_1min}" if load_1min > threshold and sustained else None
    return threshold, issue
