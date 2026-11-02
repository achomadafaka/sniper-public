"""Re-ask a coin the m5 volume floor just refused, once, if its tape wakes up inside 30 seconds.

OPERATOR, 2026-08-26: "if in 30 seconds or less the volume rises enough to clear it" enter; "if in
30 sec the volume doesnt rise we leave it alone".

THE PHYSICS, so nobody reads rarity as breakage. The floor is mcap-RELATIVE: a coin clears when its
five-minute volume reaches `mcap * 0.10`. A refusal at 0.25x the floor therefore needs roughly 7.5%
of the entire market cap to trade in the next half minute. That is a genuine explosion, not a
recovery, so this will fire RARELY and firing rarely is the design working. If it fired often the
floor would be mis-set, not this.

WHAT THE WINDOW ACTUALLY BUYS. DexScreener refreshes m5 on a ~30s cadence, so a 30s window buys
approximately ONE fresh read. Polling at 10s is about catching that refresh soon after it lands,
not about resolution. Truly live resolution would need a WS/tape subscription on the refused mint's
pool; that is the upgrade path and it is out of scope for v1.

THE WATCH HAS NO ADMISSION AUTHORITY. It is a delayed re-ask and nothing else:

  - It holds NO reservation, NO exposure slot, and NO capital while it waits. A reservation is
    created only inside the normal entry path, after re-injection.
  - On clearance the ORIGINAL event goes back through the SAME entry hook, so the WHOLE gate stack
    runs again — staleness, chase, first-seen, execution premium, ceiling, cluster, bankroll, gas.
    The chase and first-seen gates are specifically what stop this buying a top that formed during
    the 30 seconds it was waiting.
  - Clearance is judged with the SAME instrument that refused (`stats_fn`, DexScreener). Judging a
    refusal by one instrument and its reversal by another would make the arm unmeasurable and could
    admit on a disagreement between two data sources rather than on a real move.
  - BOTH sides are recomputed. The mcap moves too, so comparing a fresh volume against the stale
    mcap that produced the refusal would clear coins whose cap merely fell.

ONLY the m5 floor enqueues. Every other refusal — ceiling, chase, premium, bankroll, safety — is a
judgement that does not become wrong because 30 seconds passed, and re-asking those would be a
retry loop against a gate that meant it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

FLOOR_REASON = "m5_turnover_under_floor"

OUTCOME_CLEARED = "cleared"
OUTCOME_EXPIRED = "expired"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_DROPPED_CAPACITY = "dropped_capacity"


# Live watches, and the keys already spent. BOTH are needed: `enqueue` is idempotent while a watch
# exists, but a watch is removed BEFORE re-injection, so without a spent-key memory a re-injection
# that hits the floor again would enqueue a second watch and loop forever on the same event.
WATCHES = {}
_SPENT = {}
_SPENT_TTL_S = 3600.0


def watch_key(mint, origin_event_id):
    return (str(mint), str(origin_event_id or ""))


def _prune_spent(now):
    for key, stamp in list(_SPENT.items()):
        if float(now) - float(stamp) > _SPENT_TTL_S:
            _SPENT.pop(key, None)


def should_watch(reason, *, mcap, max_mcap):
    """True only for an m5-floor refusal on a coin inside the scope knob.

    An UNKNOWN mcap does not qualify. The scope exists because the operator judges the arm most
    useful on small coins, and admitting an unpriced coin would silently ignore that judgement.
    """
    if str(reason or "") != FLOOR_REASON:
        return False
    if not max_mcap or float(max_mcap) <= 0:
        return True
    try:
        return mcap is not None and 0 < float(mcap) <= float(max_mcap)
    except (TypeError, ValueError):
        return False


def enqueue(watches, *, event, mint, origin_event_id, refused_at, mcap, needed, now,
            window_s=30.0, poll_s=10.0, max_watches=4, network_id=None, coin_band=None):
    """Register one watch. Returns (enqueued, evicted_key).

    Idempotent per (mint, origin_event_id): the same refusal re-arriving does not stack.
    At capacity the OLDEST-EXPIRING watch is evicted — it has the least window left to be useful,
    and dropping the newest would make the queue a coin-flip on arrival order.
    """
    key = watch_key(mint, origin_event_id)
    _prune_spent(now)
    if key in watches or key in _SPENT:
        # Already watching, or already watched once. One refusal earns exactly one re-ask; a
        # re-injection that hits the floor again must NOT start the cycle over.
        return False, None
    _SPENT[key] = float(now)
    evicted = None
    if len(watches) >= int(max_watches):
        evicted = min(watches, key=lambda k: watches[k]["expires_at"])
        watches.pop(evicted, None)
        logger.warning("revival watch at capacity (%d); dropped %s to admit %s",
                       int(max_watches), evicted, key)
    watches[key] = {
        "event": event, "mint": str(mint), "origin_event_id": origin_event_id,
        "refused_at": refused_at, "refused_mcap": mcap, "refused_need": needed,
        "expires_at": float(now) + float(window_s), "next_poll_at": float(now) + float(poll_s),
        "poll_s": float(poll_s), "polls": 0, "network_id": network_id, "coin_band": coin_band,
    }
    return True, evicted


def clears_floor(stats, floor):
    """Recompute BOTH sides from one fresh read. (cleared, vol_m5, mcap).

    Missing either field is NOT clearance. The entry gate fails OPEN on absent volume data, but
    this is the opposite question: here the absence of evidence cannot be evidence of a revival.
    """
    try:
        vol = (stats or {}).get("volume_m5")
        mcap = (stats or {}).get("market_cap")
        if vol is None or not mcap or float(mcap) <= 0 or not floor or float(floor) <= 0:
            return False, vol, mcap
        return float(vol) >= float(mcap) * float(floor), float(vol), float(mcap)
    except (TypeError, ValueError):
        return False, None, None


def poll(watches, *, now, floor, stats_fn, reinject_fn, record_fn=None, superseded_fn=None,
         clears_fn=None):
    """One pass over the live watches. Returns a tally; never raises.

    Runs on the ordinary loop cadence, holds no lock, and touches no reservation. Every exit path
    removes the watch, so a coin is re-asked at most once per refusal.
    """
    tally = {"polled": 0, "cleared": 0, "expired": 0, "superseded": 0}

    def _finish(key, outcome, detail=None, watch=None):
        watches.pop(key, None)
        if record_fn is not None:
            try:
                record_fn(watch, outcome, detail)
            except Exception:
                logger.exception("revival watch could not record outcome %s", outcome)

    for key in list(watches):
        watch = watches.get(key)
        if watch is None:
            continue
        try:
            # A live signal on the same mint owns it. The watch must neither duplicate that entry
            # nor look like the thing that caused it.
            if superseded_fn is not None and superseded_fn(watch["mint"]):
                tally["superseded"] += 1
                _finish(key, OUTCOME_SUPERSEDED, "a live position or reservation took this mint",
                        watch)
                continue
            if float(now) >= float(watch["expires_at"]):
                tally["expired"] += 1
                _finish(key, OUTCOME_EXPIRED,
                        "window closed after %d poll(s)" % watch["polls"], watch)
                continue
            if float(now) < float(watch["next_poll_at"]):
                continue
            watch["next_poll_at"] = float(now) + watch["poll_s"]
            watch["polls"] += 1
            tally["polled"] += 1
            # The coin's own chain: a 0x mint read without it lands on whatever chain DexScreener
            # lists first, or on nothing.
            nid = watch.get("network_id")
            stats = stats_fn(watch["mint"], network_id=nid) if nid else stats_fn(watch["mint"])
            if clears_fn is None:
                cleared, vol, mcap = clears_floor(stats, floor)
            else:
                cleared = bool(clears_fn(watch, stats))
                vol = (stats or {}).get("volume_m5")
                mcap = (stats or {}).get("market_cap")
            if not cleared:
                continue
            tally["cleared"] += 1
            # With clears_fn the decision is the full volume gate, so a single `floor` is not the bar
            # (it printed "x 0.000" on every clear after 2026-09-12). Record what the read showed.
            detail = ("fresh read passed the refusing gate: vol_m5 %.0f, mcap %.0f" % (vol or 0.0, mcap or 0.0)
                      if clears_fn is not None else
                      "vol_m5 %.0f >= mcap %.0f x %.3f" % (vol or 0.0, mcap or 0.0, float(floor)))
            _finish(key, OUTCOME_CLEARED, detail, watch)
            # Re-ask through the NORMAL hook. Whatever it decides is the real decision; a refusal
            # here is the gate stack working, not the watch failing. The mark lets the hook judge
            # the re-ask's age against the watch window, not against the original freshness cap.
            try:
                setattr(watch["event"], "revival_reask", True)
            except Exception:
                pass
            reinject_fn(watch["event"])
        except Exception:
            logger.exception("revival watch pass failed for %s", key)
            watches.pop(key, None)
    return tally
