"""Simulate exit rails over one price series. Pure, honest about its price space, and censoring-aware.

THE SHARED CONTRACT. This is consumed by the exit-rail replay AND by Gamma's refused-mint capture,
so a counterfactual computed on a coin we refused means the same thing as one computed on a coin we
held. That is the whole reason it is a named component rather than logic inside either caller.

FOUR PROPERTIES, each pinned by a test, each load-bearing:

1. PURE. No I/O, no clock, no DB. The same series and policy always give the same result. A
   simulator that can read anything else can read the future by accident.

2. PRICE-SPACE HONEST. It never converts between spaces and it LABELS which one it was handed. The
   funded book's chart prices and its route prices differ by a −33% to +76% spread (n=69), so a
   chart series silently evaluated against a route-priced policy produces a confident wrong answer.
   The model refuses to guess which it has; the caller declares it and the result carries it.

3. CENSORING-AWARE. A gap wider than the policy's shortest reaction time returns `undetermined` for
   that position. This is NOT the same as nothing firing, and conflating them is a documented
   defect class in this codebase — three instances in one week. `undetermined` means we cannot tell;
   `no_trigger` means we watched and nothing happened. A study that pools them reports a policy as
   inactive when the truth is that it was unobservable.

4. NO FORWARD LEAKAGE. A decision at observation i reads only observations <= i. Enforced by the
   loop shape rather than by discipline: the peak is accumulated as the loop advances and never
   computed over the whole series.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Outcome vocabulary. `UNDETERMINED` and `NO_TRIGGER` are DIFFERENT FACTS.
TP = "tp"
STOP = "stop"
TRAIL = "trail"
MAX_HOLD = "max_hold"
NO_TRIGGER = "no_trigger"           # observed to the end of the series; nothing fired
UNDETERMINED = "undetermined"       # a gap too wide to simulate through; we cannot tell

# Price spaces. The caller declares one; the model never infers it.
SPACE_CHART = "chart"
SPACE_ROUTE = "route"


@dataclass(frozen=True)
class RailsPolicy:
    tp_pct: float = 25.0
    tp_fraction: float = 1.0            # <1.0 is a ladder: sell this much, ride the rest
    stop_pct: float = 35.0
    trail_arm_pct: float = 0.0          # 0 = off
    trail_pct: float = 0.0
    max_hold_s: float = 780.0

    # A TOLERANCE, NOT A CADENCE. The distinction is the whole bug: at 1.0x the threshold is exactly
    # one nominal interval, and real observation stamps are wall-clock, so consecutive gaps land at
    # cadence +/- jitter and about half exceed it. Over a 162-observation series at least one always
    # does, so EVERY series censored and `undetermined` became the default rather than the
    # exception — the model could not distinguish the case it exists to distinguish.
    #
    # Measured 2026-08-26 (Gamma, reproduced independently here), flat series, 200 seeds, censoring
    # only:
    #     multiplier   jitter-only (want 0%)   one dropped poll (want 100%)
    #     1.0x              100%                        100%     <- censors everything
    #     1.2x                0%                        100%
    #     1.5x                0%                        100%     <- midpoint of the safe window
    #     1.9x                0%                        100%
    #     2.0x                0%                         51%     <- starts missing real gaps
    #     2.5x                0%                          0%
    # Anything in [1.2, 1.9] is correct; 1.5 is furthest from both failure modes. Below the window
    # the model cries censored at ordinary jitter; above it, a genuinely dropped poll slips through
    # and a rail that could have fired inside the hole is silently resolved past it.
    GAP_TOLERANCE_MULT = 1.5

    def shortest_reaction_s(self, cadence_hint_s):
        """The widest gap that can still be simulated through. Wider, and a rail could have fired
        inside it unseen, so the position is undetermined rather than resolved on the far side."""
        return float(cadence_hint_s) * self.GAP_TOLERANCE_MULT


@dataclass(frozen=True)
class RailsResult:
    reason: str
    price_space: str
    exit_t: float | None = None
    exit_price: float | None = None
    return_pct: float | None = None     # fraction-weighted when the policy is a ladder
    trigger_index: int | None = None    # the observation that caused it, for tracing
    detail: str | None = None
    legs: tuple = field(default_factory=tuple)   # ((fraction, price, reason), ...)

    @property
    def determined(self):
        return self.reason != UNDETERMINED


def rails_outcome(series, policy, *, price_space, entry_price, cadence_hint_s=5.0):
    """Simulate `policy` over `series`. Returns a RailsResult; never raises on shape.

    `series` is ordered `(t_seconds_from_open, price)`. `price_space` is declared by the caller and
    carried on the result — this function does not know and must not guess which space it holds.

    `cadence_hint_s` is a scalar, or one value PER OBSERVATION when the producer varied its cadence
    deliberately. A sequence of the wrong length is refused rather than padded: a silently
    misaligned hint censors the wrong observations and looks like a data property.
    """
    if price_space not in (SPACE_CHART, SPACE_ROUTE):
        return RailsResult(UNDETERMINED, str(price_space),
                           detail="price space must be declared, never inferred")
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        entry = 0.0
    if entry <= 0:
        return RailsResult(UNDETERMINED, price_space, detail="no usable entry price")
    # A PER-OBSERVATION CADENCE, because one number cannot describe a series whose cadence changes
    # ON PURPOSE. A throttled collector runs fast while the funded book is empty and slow while a
    # position is open, so a single series can contain two legitimate cadences. Held against one
    # hint, either the slow segment reads as a continuous outage (censoring a healthy series) or the
    # fast segment gets the slow tolerance (letting a real dropout resolve past a hole). Spacing
    # alone cannot separate "the cadence changed" from "we missed polls" — those are the two facts
    # the censor exists to distinguish — so the cadence has to be RECORDED by the producer, not
    # inferred here. Scalar hints behave exactly as before.
    hints = list(cadence_hint_s) if isinstance(cadence_hint_s, (list, tuple)) else None
    raw = list(series or ())
    if hints is not None and len(hints) != len(raw):
        return RailsResult(UNDETERMINED, price_space,
                           detail="cadence hint has %d entries for %d observations"
                                  % (len(hints), len(raw)))
    points, cadences = [], []
    for i, (t, p) in enumerate(raw):
        if p is None or float(p) <= 0:
            continue
        points.append((float(t), float(p)))
        cadences.append(float(hints[i]) if hints is not None else float(cadence_hint_s))
    if not points:
        return RailsResult(UNDETERMINED, price_space, detail="empty series")

    # AT A CADENCE BOUNDARY THE WIDER TOLERANCE WINS. The gap from the last fast observation into
    # the first slow one is legitimate under the SLOW regime, and the reverse gap is legitimate
    # under the slow one too. Taking only the arriving observation's hint would censor every
    # slow -> fast transition for being slow, which is the boundary a caller-side splitter gets
    # wrong and the reason this lives in the model.
    max_gaps = [policy.shortest_reaction_s(max(cadences[max(i - 1, 0)], c))
                for i, c in enumerate(cadences)]
    max_gap = max_gaps[0]
    # ZERO DISABLES A RAIL. This is the funded contract's convention, not a local choice:
    # `memecoin_funded_policy` writes `trail_giveback_pct: policy.trail_pct or None`, and the mcap
    # gate documents "A floor of 0 is OFF, matching the ceiling's convention". Without these two
    # guards a zero threshold RESOLVED at the entry price instead of standing down — `tp_pct=0`
    # returned TP at t=0 for +0.0%, and `stop_pct=0` returned STOP the same way. That is not a
    # harmless default: `memecoin_exit_rail_replay.baseline_policy` builds a RailsPolicy straight
    # from the position's live funded policy, and the band policies (`funded-nano-v2`,
    # `funded-micro-v2`, `funded-mid-v1`) all carry tp_pct=0.0, so every one of their baselines
    # would have booked a fabricated break-even exit on the first observation.
    tp_on = float(policy.tp_pct) > 0.0
    stop_on = float(policy.stop_pct) > 0.0
    tp_at = entry * (1.0 + float(policy.tp_pct) / 100.0)
    stop_at = entry * (1.0 - float(policy.stop_pct) / 100.0)
    frac = max(0.0, min(1.0, float(policy.tp_fraction)))

    legs, peak, prev_t, armed = [], entry, 0.0, False
    remaining = 1.0

    for i, (t, price) in enumerate(points):
        # CENSORING FIRST. A gap wide enough to hide a trigger makes everything after it a guess,
        # so the position is undetermined rather than resolved on the far side of the hole.
        max_gap = max_gaps[i]
        if (t - prev_t) > max_gap:
            return RailsResult(UNDETERMINED, price_space, trigger_index=i,
                               detail="gap %.1fs at t=%.1f exceeds the %.1fs reaction window"
                                      % (t - prev_t, t, max_gap),
                               legs=tuple(legs))
        prev_t = t
        peak = max(peak, price)                     # accumulated forward only; never over the whole
        if stop_on and remaining > 0 and price <= stop_at:
            legs.append((remaining, price, STOP))
            return _finish(legs, entry, price_space, t, price, i, STOP)
        if tp_on and remaining == 1.0 and price >= tp_at:
            legs.append((frac, price, TP))
            remaining -= frac
            # A PARTIAL TP MAKES THE TRAIL ELIGIBLE, NOT ARMED. Arming here unconditionally let a
            # ladder skip its own arm threshold: TP at +10% with trail_arm_pct=+50% trailed out at
            # +5% on a peak that never passed +10%. When an arm threshold is set, the peak check
            # below owns arming. Only trail_arm_pct <= 0 means "armed as soon as there is a
            # remainder", which matches the funded band policies (trail_arm=0.0).
            if float(policy.trail_arm_pct) <= 0.0:
                armed = True
            if remaining <= 0:
                return _finish(legs, entry, price_space, t, price, i, TP)
            continue                                # ladder: the remainder rides on
        if remaining > 0 and remaining < 1.0 and policy.trail_pct > 0:
            if not armed and peak >= entry * (1.0 + float(policy.trail_arm_pct) / 100.0):
                armed = True
            if armed and price <= peak * (1.0 - float(policy.trail_pct) / 100.0):
                legs.append((remaining, price, TRAIL))
                return _finish(legs, entry, price_space, t, price, i, TRAIL)
        if t >= float(policy.max_hold_s):
            legs.append((remaining, price, MAX_HOLD))
            return _finish(legs, entry, price_space, t, price, i, MAX_HOLD)

    last_t, last_price = points[-1]
    if last_t < float(policy.max_hold_s) - max_gaps[-1]:
        # The series stops before the policy's own horizon. We did not watch to the end, so we
        # cannot say nothing fired.
        return RailsResult(UNDETERMINED, price_space, trigger_index=len(points) - 1,
                           detail="series ends at %.1fs, inside the %.1fs horizon"
                                  % (last_t, policy.max_hold_s),
                           legs=tuple(legs))
    legs.append((remaining, last_price, NO_TRIGGER))
    return _finish(legs, entry, price_space, last_t, last_price, len(points) - 1, NO_TRIGGER)


def _finish(legs, entry, price_space, t, price, index, reason):
    total = 0.0
    for fraction, leg_price, _r in legs:
        total += float(fraction) * (float(leg_price) / entry - 1.0) * 100.0
    return RailsResult(reason, price_space, exit_t=t, exit_price=price,
                       return_pct=total, trigger_index=index, legs=tuple(legs))
