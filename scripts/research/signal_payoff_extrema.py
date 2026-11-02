"""Exact minimum and maximum of the barrier-policy payoff over every admissible entry and every
within-slot ordering (signal-setup pre-registration section 3.2). Pure functions, no data, no I/O.

A slot is an unordered set of net returns, one per admissible state, computed for one entry. The policy
exits at the first visited state with net >= take (take) or <= -stop (stop), else at the end mark.
Unknown, non-finite or empty inputs are never skipped: they raise `Uncomputable`, and the caller scores
the signal at the support bounds (`censored_extrema`).
"""
import math


class Uncomputable(ValueError):
    """An input needed for an exact extremum is missing, empty or not finite."""


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def net(proceeds, spend, fixed):
    """Net return. Zero proceeds are a valid measured value; a non-positive or non-finite spend is not."""
    if not (_finite(spend) and spend > 0) or not (_finite(fixed) and fixed >= 0) or not (_finite(proceeds) and proceeds >= 0):
        raise Uncomputable("net(): spend must be finite > 0, fixed finite >= 0, proceeds finite >= 0")
    return (proceeds - spend - fixed) / spend


def _states(states, what):
    if not isinstance(states, (list, tuple)) or not states:
        raise Uncomputable("%s: empty state set" % what)
    if not all(_finite(n) for n in states):
        raise Uncomputable("%s: a state is missing or not finite" % what)
    return states


def policy_extrema_for_entry(slots, end_slot, take, stop):
    """(P_lo, P_hi, ambiguous) for one entry. `slots` is the ordered list of state-net sets after the
    entry slot; `end_slot` the set at the end mark. take is e.g. 0.20, stop e.g. 0.12 (positive)."""
    if not (_finite(take) and take > 0 and _finite(stop) and stop > 0):
        raise Uncomputable("take and stop must be finite and > 0")
    if not isinstance(slots, (list, tuple)) or not slots:
        raise Uncomputable("slots must be a non-empty sequence (the horizon spans many slots after entry)")
    for i, states in enumerate(slots):
        # validate each slot only when the scan REACHES it: unknown states after a proven exit are irrelevant
        _states(states, "slot %d" % i)
        cross = [n for n in states if n >= take or n <= -stop]
        if cross:
            has_take = any(n >= take for n in cross)
            has_stop = any(n <= -stop for n in cross)
            # a stop state is below a take state, so min is the lowest stop (else the lowest take); max the
            # highest take (else the highest stop)
            return min(cross), max(cross), has_take and has_stop
    _states(end_slot, "end slot")          # no crossing: the end set is required
    return min(end_slot), max(end_slot), False


def policy_extrema(entries, take=0.20, stop=0.12):
    """`entries` is a list of (slots, end_slot) pairs, one per admissible entry state. The extrema are
    min over entries of P_lo(e) and max over entries of P_hi(e). Raises Uncomputable on no entries or any
    unusable input. Returns (P_lo, P_hi, ambiguous)."""
    if not entries:
        raise Uncomputable("no admissible entry state")
    out = [policy_extrema_for_entry(slots, end, take, stop) for slots, end in entries]
    return min(o[0] for o in out), max(o[1] for o in out), any(o[2] for o in out)


def lower_support(spend, fixed):
    if not (_finite(spend) and spend > 0 and _finite(fixed) and fixed >= 0):
        raise Uncomputable("lower_support(): spend must be finite > 0, fixed finite >= 0")
    return -(spend + fixed) / spend


def censored_extrema(entries, spend, fixed, take=0.20, stop=0.12):
    """The extrema, or, when they are uncomputable, the support bounds: (lower_support, +inf, False,
    True). The last element says the signal was censored."""
    try:
        lo, hi, amb = policy_extrema(entries, take, stop)
        return lo, hi, amb, False
    except Uncomputable:
        return lower_support(spend, fixed), math.inf, False, True
