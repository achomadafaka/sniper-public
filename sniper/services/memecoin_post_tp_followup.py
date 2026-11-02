"""Price the tail we stop watching after a funded take-profit. MEASUREMENT ONLY.

THE QUESTION THIS EXISTS TO ANSWER. `executable_peak_price` stops ratcheting when we exit, so the
executable tail of a TP'd position reads as +27.8% median / +47.4% max / nothing at or above +50%
(n=44) — and that is an artefact of when we stopped looking, not a measurement of what the coin
did next. The post-exit tail is UNMEASURED, not proven absent. A laddered take-profit is only worth
building if that tail is real, so it has to be measured before it is designed around.

IT MUST BE THE ROUTE, NOT THE CHART. The chart has lied by two orders of magnitude on exactly this
population: TRASHY showed a +212.6% nominal peak against a +16.1% executable one, AGENTS +242.9%
against +15.0%, and four of the six coins with that divergence went on to lose 35-52%. So each
sample is an exit quote for the exact quantity we sold, priced the same way the executable preview
prices a live position. A chart mid here would manufacture the very tail we are trying to test for.

WHAT IT MAY NEVER DO (the V6 post-exit doctrine, 2026-07-27, applied to the funded lane):
  - never change a decision, gate, policy, inventory, position, or any funded state
  - never be read by an entry or exit path; it is evidence for a later study and nothing else
  - never backfill a historical close: a quote taken today cannot reconstruct the executable route
    of a past exit, so only closes seen after this lands get schedules
  - never substitute a chart price for a missing route; a failure is stored as a typed status
  - never overwrite a recorded result; a restart may retry an unfinished schedule only
  - never label a late sample by its target horizon; `elapsed_s` carries what actually happened

BUDGET. Four horizons per TP close. Funded take-profits ran 12 on 2026-08-25 and 33 across 08-24/25,
so this is roughly 48-70 quotes a day, one at a time, against a provider the runner already calls
several times a minute for live marks. It is not a meaningful load, and the one-outstanding rule
means a backlog drains slowly rather than bursting.

THE QUANTITY IS RECONSTRUCTED, AND THAT IS DEFENSIBLE HERE. The exit fill stores a float human
quantity, so the atomic amount is recomputed as qty x 10^decimals rather than read exactly. The
usual prohibition on that conversion (memecoin balances exceed 2^53 and the float round-trip
mis-sizes a real sell) is about SELLING: being a few atomic units over what you hold reverts the
transaction. Nothing here is ever signed. A relative error around 1e-16 does not move a price
measurement, and the alternative — touching the exit path to persist an exact atomic quantity —
would put a measurement concern inside the money path.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The status vocabulary. A failure is a typed outcome, never an absent row and never a chart price.
STATUS_HEALTHY = "healthy"
STATUS_ROUTE_ABSENT = "route_absent"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"


def atomic_from_qty(qty, decimals):
    """Human quantity -> atomic units as an int, or None when either input is unusable."""
    try:
        if qty is None or decimals is None:
            return None
        value = int(round(float(qty) * (10 ** int(decimals))))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def schedule_new_closes(conn, *, since_iso, now_iso, decimals_fn, limit=50,
                        exit_reasons=("real_tp",), horizons=None):
    """Create schedules for funded closes at/after `since_iso` (take-profit by default; the
    source-sell mirror passes its own reason and a 600 s horizon). Returns a tally.

    Reads closed positions after the fact, so nothing here can delay or fail a real exit.
    """
    from sniper.persistence import db_memecoin as m
    tally = {"seen": 0, "scheduled": 0, "skipped_no_qty": 0}
    try:
        rows = m.recent_real_tp_closes(conn, since_iso=since_iso, limit=limit,
                                       exit_reasons=tuple(exit_reasons))
    except Exception:
        logger.exception("post-TP follow-up could not scan closes")
        return tally
    for row in rows:
        tally["seen"] += 1
        try:
            decimals = decimals_fn(row["mint"])
            atomic = atomic_from_qty(row.get("exit_qty"), decimals)
            if atomic is None:
                # Unknown decimals or an unusable quantity. A schedule we cannot price exactly is
                # worse than no schedule: it would produce samples for the wrong size.
                tally["skipped_no_qty"] += 1
                continue
            tally["scheduled"] += m.schedule_post_tp_followups(
                conn, position_id=row["position_id"], network_id=row.get("network_id"),
                mint=row["mint"], closed_at=row["closed_at"], token_qty_atomic=str(atomic),
                token_decimals=decimals, exit_usdc=row.get("exit_usdc"), now_iso=now_iso,
                **({"horizons": tuple(horizons)} if horizons else {}))
        except Exception:
            logger.exception("post-TP follow-up could not schedule position %s",
                             row.get("position_id"))
    return tally


def _elapsed_s(closed_at, observed_at):
    from datetime import datetime
    try:
        a = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def quote_due_followup(conn, *, now_iso, exit_quote_fn, slippage_pct=1.0, limit=1, horizons=None):
    """Quote at most `limit` due schedules. Returns the list of recorded tallies; never raises.

    ONE AT A TIME by default. A backlog after downtime must drain gently rather than burst against
    the provider the live marks share.
    """
    from sniper.persistence import db_memecoin as m
    out = []
    try:
        due = m.due_post_tp_followups(conn, now_iso, limit=limit, horizons=horizons)
    except Exception:
        logger.exception("post-TP follow-up could not read the due queue")
        return out
    for job in due:
        status, quote_usdc, unit, detail = STATUS_PROVIDER_UNAVAILABLE, None, None, None
        try:
            atomic = int(job["token_qty_atomic"])
            quote = exit_quote_fn(job["mint"], atomic, slippage_pct)
            if not quote:
                # No route is a REAL observation about the tail, not a provider fault: the coin
                # cannot be sold at this size right now. Typed, and distinct from unavailable.
                status, detail = STATUS_ROUTE_ABSENT, "no route for the sold quantity"
            else:
                quote_usdc = int(quote.get("outAmount") or 0) / 1e6
                status = STATUS_HEALTHY
                unit = (quote_usdc / (atomic / (10 ** int(job["token_decimals"]))))\
                    if job.get("token_decimals") is not None and atomic else None
        except Exception as exc:
            status, detail = STATUS_PROVIDER_UNAVAILABLE, type(exc).__name__
            logger.exception("post-TP follow-up quote failed for %s", job.get("followup_id"))
        try:
            m.record_post_tp_result(
                conn, followup_id=job["followup_id"], position_id=job["position_id"],
                horizon_s=job["horizon_s"], status=status, observed_at=now_iso,
                elapsed_s=_elapsed_s(job.get("closed_at"), now_iso),
                quote_out_usdc=quote_usdc, unit_price=unit, detail=detail)
            out.append({"followup_id": job["followup_id"], "status": status,
                        "usdc": quote_usdc, "exit_usdc": job.get("exit_usdc")})
        except Exception:
            logger.exception("post-TP follow-up could not record %s", job.get("followup_id"))
    return out


def post_tp_pass(conn, *, since_iso, now_iso, decimals_fn, exit_quote_fn, limit=1,
                 exit_reasons=("real_tp",), horizons=None):
    """One combined pass: schedule new closes, then quote the oldest due sample. Never raises."""
    tally = {"scheduled": 0, "quoted": 0, "results": []}
    try:
        sched = schedule_new_closes(conn, since_iso=since_iso, now_iso=now_iso,
                                    decimals_fn=decimals_fn, exit_reasons=exit_reasons,
                                    horizons=horizons)
        tally["scheduled"] = sched.get("scheduled", 0)
        results = quote_due_followup(conn, now_iso=now_iso, exit_quote_fn=exit_quote_fn,
                                     limit=limit, horizons=horizons)
        tally["quoted"] = len(results)
        tally["results"] = results
    except Exception:
        logger.exception("post-TP follow-up pass failed")
    return tally
