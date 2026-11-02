"""Corrected UTC clock.

The host machine's wall clock runs materially fast/slow vs real UTC (measured ~+7h on this
box). Any age computed as `host_now - external_timestamp` (fomo/DexScreener times, which ARE
real UTC) is therefore wrong. We correct centrally: whenever we see a trusted real-UTC time
(a server `Date` response header), we persist the host->real offset; all consumers then use
`real_utc_now_iso()` instead of `datetime.now(utc)` when diffing against external times.

Same-source diffs (snapshot_ts vs snapshot_ts) don't need this — but using the corrected clock
everywhere keeps every stored timestamp on one real-UTC basis, so nothing is cross-clock.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from sniper.util.state_paths import state_path

_OFFSET_FILE = os.environ.get(
    "SNIPER_CLOCK_OFFSET_FILE", str(state_path("data", "clock_offset.json")))


def record_real_utc(real_epoch, path=None):
    """Persist offset = host_now - real_utc, given a trusted real-UTC epoch (e.g. from a
    server Date header). Best-effort; never raises into the caller."""
    path = path or _OFFSET_FILE
    try:
        offset = time.time() - float(real_epoch)
        if abs(offset) > 172800:   # >2 days is a malformed/stub Date header, not a wall-clock skew
            return
        # atomic: this file is rewritten ~every poll; a torn read would zero the offset and
        # briefly reintroduce the +7h skew into every age computation.
        from sniper.util.atomic import atomic_write_json
        atomic_write_json(path, {"offset_s": offset, "at": time.time()})
    except Exception:
        pass


def offset_s(path=None):
    path = path or _OFFSET_FILE
    try:
        with open(path) as fh:
            return float(json.load(fh).get("offset_s", 0.0))
    except Exception:
        return 0.0   # no correction learned yet -> behave as host clock


def real_utc_now(path=None):
    """Host now minus the learned offset -> real-UTC epoch (falls back to host now)."""
    return time.time() - offset_s(path)


def real_utc_now_iso(path=None):
    return datetime.fromtimestamp(real_utc_now(path), tz=timezone.utc).isoformat()
