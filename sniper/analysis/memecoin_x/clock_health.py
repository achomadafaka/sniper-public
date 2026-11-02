"""Clock-health gate for X capture. The corrected-clock helper (sniper.util.clock) silently
falls back to the host clock when no offset is learned; that is NOT sufficient for forward lead
evidence. This gate requires a present, finite, in-bounds, fresh offset before any X observation
timestamp may be trusted (clock_domain='observed_real_utc'). Otherwise the capture is marked
'legacy_unknown' and excluded from forward correlation."""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

from sniper.util import clock as _clock

MAX_OFFSET_AGE_S = 900          # 15 minutes
MAX_ABS_OFFSET_S = 172800       # 2 days (matches clock.record_real_utc sanity bound)


def _offset_file(path=None):
    return path or os.environ.get(
        "SNIPER_CLOCK_OFFSET_FILE", _clock._OFFSET_FILE)


def clock_health(path=None, now=None) -> dict:
    p = _offset_file(path)
    now = time.time() if now is None else now
    base = {"healthy": False, "offset_s": None, "offset_age_s": None, "reason": "missing"}
    try:
        with open(p) as fh:
            d = json.load(fh)
    except Exception:
        return base
    try:
        offset = float(d.get("offset_s"))
        at = float(d.get("at"))
    except (TypeError, ValueError):
        return {**base, "reason": "missing"}
    age = now - at
    out = {"healthy": False, "offset_s": offset, "offset_age_s": age, "reason": "ok"}
    if not (math.isfinite(offset) and math.isfinite(at)):
        return {**out, "reason": "non_finite"}
    if abs(offset) > MAX_ABS_OFFSET_S:
        return {**out, "reason": "out_of_bounds"}
    if age > MAX_OFFSET_AGE_S or age < -MAX_OFFSET_AGE_S:
        return {**out, "reason": "stale"}
    out["healthy"] = True
    return out


def x_observed_at_or_none(path=None, now=None):
    """Return corrected UTC from the exact offset snapshot accepted by clock_health."""
    host_now = time.time() if now is None else float(now)
    h = clock_health(path=path, now=host_now)
    if not h["healthy"]:
        return None, h
    observed = datetime.fromtimestamp(host_now - h["offset_s"], tz=timezone.utc)
    return observed.isoformat(), h
