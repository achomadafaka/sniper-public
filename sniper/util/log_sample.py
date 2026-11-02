"""Per-key sampling for repeated warnings.

A provider episode repeats the same warning every tick: 2026-08-14 produced ~3,500 lines EACH of
"budget exhausted", "INFRA failure", and "provider rate-limited" in one day's runner log. At that
volume the log costs disk and, worse, buries the one NEW line that says something different.

The first occurrence of a key always logs — an operator tailing the log sees the episode START
immediately. Repeats inside the window are counted, and the next emitted line carries the count,
so nothing is silently dropped; the log says "this happened 240 more times" instead of saying it
240 more times.
"""
from __future__ import annotations

import time

_last = {}      # key -> (last_emit_monotonic, suppressed_count)


def log_sampled(log_fn, key, msg, *args, interval_s=60.0, clock=time.monotonic):
    """Emit `log_fn(msg, *args)` now if `key` has not logged within `interval_s`; otherwise count.

    When an emit happens after suppression, the message is suffixed with the suppressed count.
    `log_fn` is a bound logger method (logger.warning / logger.error), so level and site stay at
    the caller.
    """
    now = clock()
    last, suppressed = _last.get(key, (None, 0))
    if last is not None and now - last < interval_s:
        _last[key] = (last, suppressed + 1)
        return False
    if suppressed:
        msg = msg + " (+%d suppressed in the last %.0fs)"
        args = (*args, suppressed, now - last if last is not None else interval_s)
    _last[key] = (now, 0)
    log_fn(msg, *args)
    return True
