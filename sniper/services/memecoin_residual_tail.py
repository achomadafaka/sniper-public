"""How much of a TP'd position could have been KEPT, priced exactly. MEASUREMENT ONLY.

THE DESIGN THIS EXISTS TO GRADE. A "recover principal, then trail the tail" exit sells only as much
as it takes to get the entry back and rides the remainder. Grading it needs one number the ledger
does not hold: the smallest fraction of the bag whose EXACT sale proceeds cover the principal. Call
it f. The residual is the rest, and its forward value is the only thing that decides whether the
design is worth building.

WHY IT CANNOT BE COMPUTED FROM A UNIT PRICE. Selling f of a bag does not yield f times the whole
bag's quote — price impact is convex in size, and on the coins this lane trades it is the dominant
term, not a correction. Deriving f by dividing principal by a unit price would understate it on
exactly the thin coins where the tail design would be tested hardest, and would report a residual
that never existed. So every probe here is an exact route quote for a real partial quantity.

LINEARITY MAY CHOOSE A PROBE. IT MAY NOT PRODUCE AN ANSWER. The search uses a linear guess to pick
where to look next, because that is just a search heuristic and a wrong guess costs one extra
probe. The value returned is always a quantity that was actually quoted. That distinction is the
whole reason this module exists rather than a division.

WHAT IT MAY NEVER DO — the post-exit doctrine (2026-07-27) applies unchanged: never change a
decision, gate, policy, inventory or funded state; never be read by an entry or exit path; never
substitute a chart price for a route; never report a number it did not measure.

COST. One resolve is at most `max_probes` exact quotes (default 6). At roughly 19 funded
take-profits a day that is about 114 additional Jupiter calls a day, on top of the follow-up
samples themselves. Small against the funded runner's own quote rate, and worth stating rather
than discovering: this runs in the runner process and therefore spends the FUNDED Jupiter key.
That is a deliberate choice at this volume, not an oversight.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

RESOLVED = "resolved"                  # f found and quoted exactly
UNREACHABLE = "unreachable"            # even the WHOLE bag does not cover principal
UNDETERMINED = "undetermined"          # probe budget spent without bracketing f
NO_ROUTE = "no_route"                  # the provider has no route at the sizes we asked

# A fraction interval narrower than this is not worth another quote: the residual it distinguishes
# is smaller than the fill noise on the exits this grades. Not a tuned constant — a stopping rule.
FRACTION_TOLERANCE = 0.02


def resolve_residual(*, open_qty_atomic, target_usdc, quote_out_fn, max_probes=6,
                     tolerance=FRACTION_TOLERANCE):
    """Smallest exactly-quoted fraction whose proceeds cover `target_usdc`.

    `quote_out_fn(atomic) -> usdc_out or None`. Every returned figure comes from a real quote.

    Returns a dict: status, fraction, residual_qty_atomic, sold_qty_atomic, sold_usdc, probes.
    `fraction` is the SOLD fraction; the residual is what is left. On any status other than
    RESOLVED the quantities are None — a partial answer here would be read as a measurement.
    """
    out = {"status": UNDETERMINED, "fraction": None, "residual_qty_atomic": None,
           "sold_qty_atomic": None, "sold_usdc": None, "probes": 0, "detail": None}
    try:
        total = int(open_qty_atomic)
        target = float(target_usdc)
    except (TypeError, ValueError):
        out["detail"] = "unusable quantity or target"
        return out
    if total <= 0 or target <= 0:
        out["detail"] = "non-positive quantity or target"
        return out

    probes = 0

    def probe(fraction):
        nonlocal probes
        atomic = int(total * fraction)
        if atomic <= 0:
            return None, None
        probes += 1
        try:
            usdc = quote_out_fn(atomic)
        except Exception:
            logger.exception("residual probe failed at fraction %.4f", fraction)
            return atomic, None
        return atomic, (float(usdc) if usdc is not None else None)

    # THE WHOLE BAG FIRST. It answers the only question that can end the search early -- whether f
    # exists at all -- and it is the quote most likely to have a route, so a None here is a
    # provider fact rather than a statement about small sizes.
    full_atomic, full_usdc = probe(1.0)
    out["probes"] = probes
    if full_usdc is None:
        out["status"] = NO_ROUTE
        out["detail"] = "no route for the whole bag"
        return out
    if full_usdc < target:
        # The position could not have recovered principal at ANY size. That is a real finding about
        # this close, not a failure: the tail design would have had nothing to keep.
        out["status"] = UNREACHABLE
        out["detail"] = "whole bag quotes %.4f against a %.4f target" % (full_usdc, target)
        return out

    lo, hi = 0.0, 1.0                  # lo never covers target; hi always does
    best = (1.0, full_atomic, full_usdc)
    while probes < max_probes and (hi - lo) > tolerance:
        # Linear guess, clamped inside the bracket. Convexity means the true f sits ABOVE the
        # linear estimate, so this biases toward the covering side and converges from a real quote.
        guess = target / full_usdc if full_usdc > 0 else hi
        mid = min(max(guess if lo < guess < hi else (lo + hi) / 2.0, lo + 1e-6), hi - 1e-6)
        atomic, usdc = probe(mid)
        if usdc is None:
            # A missing route at a SMALLER size than one that routed is a real market fact; stop
            # narrowing rather than invent the boundary.
            out["detail"] = "no route at fraction %.4f" % mid
            break
        if usdc >= target:
            hi, best = mid, (mid, atomic, usdc)
        else:
            lo = mid
        full_usdc = full_usdc if usdc < target else usdc   # keep the guess anchored on a coverer
    out["probes"] = probes
    fraction, sold_atomic, sold_usdc = best
    out.update(status=RESOLVED, fraction=fraction, sold_qty_atomic=sold_atomic,
               sold_usdc=sold_usdc, residual_qty_atomic=max(total - sold_atomic, 0))
    if out["residual_qty_atomic"] == 0:
        out["detail"] = "the whole bag was needed to cover principal"
    return out
