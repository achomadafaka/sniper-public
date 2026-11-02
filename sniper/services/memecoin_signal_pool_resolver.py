"""Resolve a signal's pool in the background, so the observation lanes stop failing open.

THE GAP THIS CLOSES. Mint -> pool resolution has been a consequence of HOLDING a coin, never a
precondition of entering one. Measured 2026-08-26:

    pool resolved before the position opened      0 of 99 funded entries
    attempts carrying pre-entry flow              5 of 2,019   (0.25%)
    volume-death shadow blind on                  31% of holds
    sell-share gate's volume evidence lane        almost never fires

One missing input, three consumers. This enqueues every Solana signal whose mint has no pool yet
and resolves it on a background thread, writing the SAME rows the pre-warm lane writes — so every
consumer lights up with no change to any of them.

WHAT IT WINS, AND WHAT IT DOES NOT. A funded entry completes in ~1.8s; a resolution costs ~1-3s of
RPC. So the FIRST signal on a brand-new mint usually still loses the race and every consumer keeps
failing open exactly as today. That case is NOT fixed here, and anyone quoting one coverage number
is quoting the wrong thing. What it does win: every subsequent signal on that mint, the revival
watch's 30-second window (long enough for resolution to land INSIDE it, so a clearance can carry
real buy/sell direction rather than unknown), and open-position tape coverage from seconds after
the open.

IT IS ENQUEUED AT OBSERVATION TIME, BEFORE THE GATES, INDEPENDENT OF THE OUTCOME. A refusal wants
the pool as much as a fill does — the whole point is to measure what we declined as well as what we
took, and the revival watch's re-ask is a refusal by construction.

BOUNDS, because a signal burst must never become an RPC burst. A rate cap per minute, an overflow
that drops the OLDEST pending mint with a counter, and a per-mint TTL so a hot mint enqueued on
every poll is resolved once. The RESEARCH key does this work — never the funded pool, never the
capture rotator, whose startup assertion depends on not sharing.

It runs on its own thread with its own connections and never touches the entry path.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

COUNTS = {"enqueued": 0, "resolved": 0, "unresolved": 0, "failed": 0,
          "dropped_full": 0, "dropped_rate": 0, "deduped": 0}

_LOCK = threading.Lock()
_PENDING = []          # FIFO of (mint, first_seen_iso)
_SEEN = {}             # mint -> monotonic stamp, the TTL dedup
_RECENT = []           # monotonic stamps of completed resolutions, for the rate bound


def reset_state():
    """Test hook. Production never calls this."""
    with _LOCK:
        _PENDING.clear()
        _SEEN.clear()
        _RECENT.clear()
        for k in COUNTS:
            COUNTS[k] = 0


def enqueue(mint, *, first_seen=None, now, ttl_s=900.0, max_pending=64, has_pool_fn=None):
    """Queue one mint for resolution. Returns True when it was newly queued.

    Cheap and synchronous — this is called from the observation path, so it does no I/O beyond an
    optional indexed pool lookup and never blocks.
    """
    if not mint:
        return False
    with _LOCK:
        stamp = _SEEN.get(mint)
        if stamp is not None and (float(now) - float(stamp)) < float(ttl_s):
            COUNTS["deduped"] += 1
            return False
    if has_pool_fn is not None:
        try:
            if has_pool_fn(mint):
                return False                 # already resolved; nothing to do
        except Exception:
            pass                             # an unreadable check is not a reason to skip
    with _LOCK:
        _SEEN[mint] = float(now)
        for key, when in list(_SEEN.items()):
            if float(now) - float(when) > float(ttl_s) * 4:
                _SEEN.pop(key, None)         # bounded: the dedup memory cannot grow forever
        if any(m == mint for m, _fs in _PENDING):
            return False
        if len(_PENDING) >= int(max_pending):
            dropped = _PENDING.pop(0)        # oldest first: it has waited longest and is stalest
            COUNTS["dropped_full"] += 1
            logger.warning("signal pool resolver queue full (%d); dropped %s",
                           int(max_pending), dropped[0])
        _PENDING.append((mint, first_seen))
        COUNTS["enqueued"] += 1
        return True


def _rate_ok(now, *, max_per_min):
    """True when another resolution fits inside the rate bound. Prunes as it goes."""
    cutoff = float(now) - 60.0
    while _RECENT and _RECENT[0] < cutoff:
        _RECENT.pop(0)
    return len(_RECENT) < int(max_per_min)


def take_next(now, *, max_per_min=10):
    """The next mint to resolve, or None when the queue is empty or the rate bound is reached."""
    with _LOCK:
        if not _PENDING:
            return None
        if not _rate_ok(now, max_per_min=max_per_min):
            COUNTS["dropped_rate"] += 1
            return None
        _RECENT.append(float(now))
        return _PENDING.pop(0)


def record_outcome(stats):
    """Fold one resolve_one_mint result into the counters."""
    with _LOCK:
        if stats.get("errors"):
            COUNTS["failed"] += 1
        elif stats.get("resolved"):
            COUNTS["resolved"] += 1
        else:
            COUNTS["unresolved"] += 1


def snapshot():
    """Heartbeat projection.

    THE HEARTBEAT ASSEMBLY IS A WHITELIST — this dict reaching the file requires the field to be
    named where the heartbeat is built. The RPC pool's `degraded` was added to a snapshot on
    2026-08-25 and silently dropped one layer up.
    """
    with _LOCK:
        out = dict(COUNTS)
        out["pending"] = len(_PENDING)
        return out
