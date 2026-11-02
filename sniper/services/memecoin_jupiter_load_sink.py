"""Durable per-minute Jupiter load buckets (spec 2026-08-30-jupiter-load-persistence-design).

`JUPITER_LOAD` in `solana_real_seams` counts quotes, 429s, errors and auth failures in PROCESS
MEMORY. The runner restarted twice on 2026-08-30, so that day's funded rate-limit record is
unrecoverable, and "is the funded key actually holding?" cannot be answered after any restart.

This is the durable sink behind those counters. It changes no counting logic and gates nothing.

TWO QUANTITIES, NOT ONE, AND THE DISTINCTION IS THE WHOLE POINT:
  * `host_429`     -- per HOST ATTEMPT (`_note_host_429`, inside the failover loop). PRESSURE.
  * `rate_limited` -- per QUOTE (`solana_real_seams.py:637`, only when every host failed). A LOST
                      DECISION.
A quote that 429s on `api` and succeeds on `lite-api` is +1 pressure and +0 lost decision, so
`host_429 >= rate_limited` always. Summing them together is meaningless: different denominators.
Recording both at one seam would inflate the persisted rate by the failover fan-out, and a rate
that is wrong but looks authoritative is worse than no instrument at all.

A PER-MINUTE ACCUMULATOR, NOT A DIFF of the cumulative counter: `JUPITER_LOAD` counts since
process start, so differencing it goes NEGATIVE across exactly the restart this exists to survive.

Follows `memecoin_route_series`: the hot path only increments a dict under a short lock -- no I/O,
no sleep, no DB handle -- and the flush runs in the MAINTENANCE pass. This preserves the
funded-first asymmetry at `solana_real_seams.py:477`; a funded preview must never queue behind
bookkeeping. Every entry point is fail-soft, matching the existing contract that a counter must
never be able to refuse a funded quote.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

FIELDS = ("quotes", "host_429", "rate_limited", "errors", "auth_failed")
# Quote-scoped events know their role but not a single host (the terminal seam sits outside the
# host loop). They carry this sentinel rather than inventing an attribution.
QUOTE_SCOPE = "_quote"
# Bounded so a stuck flush cannot grow the process without limit. ~4 rows/minute in normal
# operation, so this is roughly 17 hours of unflushed buckets.
MAX_BUCKETS = 4096

COUNTS = {"recorded": 0, "flushed": 0, "dropped_full": 0, "errors": 0}
_BUCKETS: dict[tuple[str, str, str], dict[str, int]] = {}
_LOCK = threading.Lock()


def reset():
    """Test hook only."""
    with _LOCK:
        _BUCKETS.clear()
        for k in COUNTS:
            COUNTS[k] = 0


def _minute(now):
    """'2026-08-30T15:00:11.123Z' -> '2026-08-30T15:00'. Returns None if unusable."""
    try:
        text = str(now)
        return text[:16] if len(text) >= 16 and text[10] in "T " else None
    except Exception:
        return None


def note(field, *, role=None, host=None, now=None):
    """Increment one bucket. NEVER raises -- a counter must not refuse a funded quote."""
    try:
        if field not in FIELDS:
            return
        minute = _minute(now)
        if minute is None:
            return
        key = (minute,
               str(role) if role else "unknown",
               str(host) if host else QUOTE_SCOPE)
        with _LOCK:
            slot = _BUCKETS.get(key)
            if slot is None:
                if len(_BUCKETS) >= MAX_BUCKETS:
                    COUNTS["dropped_full"] += 1      # overflow is counted, never silent
                    return
                slot = _BUCKETS[key] = {f: 0 for f in FIELDS}
            slot[field] += 1
            COUNTS["recorded"] += 1
    except Exception:
        pass


def snapshot():
    """Copy of the current buckets, for tests and the heartbeat."""
    with _LOCK:
        return {k: dict(v) for k, v in _BUCKETS.items()}


def flush(conn, *, write_fn=None, now=None, limit=500):
    """Persist CLOSED minute buckets. Called from the MAINTENANCE pass, never a quote path.

    The current minute is left in place: a quote in flight across the boundary must not be
    written as a short bucket and then read as a reconciliation discrepancy.

    Rows are taken off the buffer BEFORE the write, so a failed write loses that batch rather
    than retrying forever against a broken table. Losing an observation is acceptable here in a
    way that blocking a funded quote is not.
    """
    current = _minute(now)
    if current is None:
        return 0
    with _LOCK:
        closed = sorted(k for k in _BUCKETS if k[0] < current)[:int(limit)]
        if not closed:
            return 0
        batch = [dict(ts_minute=k[0], role=k[1], host=k[2], **_BUCKETS.pop(k)) for k in closed]
    try:
        if write_fn is None:
            from sniper.persistence import db_memecoin as m
            write_fn = m.record_jupiter_load
        write_fn(conn, batch)
        with _LOCK:
            COUNTS["flushed"] += len(batch)
        return len(batch)
    except Exception:
        with _LOCK:
            COUNTS["errors"] += 1
        logger.exception("jupiter load flush failed; %d bucket(s) lost", len(batch))
        return 0
