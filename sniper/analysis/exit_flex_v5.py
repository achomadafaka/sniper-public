"""WO-5 — v5 flexibility: conviction-scaled fractions, per-band profiles, horizon-unit windows.

Pure registered-constant upgrades. They remain default-OFF and promotion-gated, but the selected
v5 proposer consumes them in both the funded fallback and hot lane. No helper here mutates state.

Every constant is [inherited] from measured data or [bound] by construction — nothing fitted.
"""
from __future__ import annotations
import math


# [inherited/mechanical] The registered signal target is five minutes. Live features stay in
# elapsed seconds so chain cadence cannot silently change the horizon.
TARGET_HORIZON_S = 300.0
VOL_HORIZON_UNITS = 1.0
ARRIVAL_HORIZON_UNITS = 0.2

# [inherited] The pre-v5 funded partial was exactly half. These are explicit per-rule policy inputs,
# not a missing-volatility fallback shared with unrelated actions. WO-7 may re-register them.
LP_MATERIAL_DERISK_FRACTION = 0.5
TIME_DERISK_FRACTION = 0.5


def vol_reference_ratio(realized_vol, trail_pct):
    """Parameter-free saturating vol-vs-reference strength in [0, 1].

    For x = realized_vol / live_trail_distance, x/(1+x) stays monotonic without pinning every
    action at 1 once the policy's blowoff gate x>=1 has fired. None/invalid inputs -> None.
    """
    try:
        t = float(trail_pct)
        v = float(realized_vol)
    except (TypeError, ValueError):
        return None
    if not (t > 0) or v < 0 or not math.isfinite(t) or math.isnan(v):
        return None
    if math.isinf(v):
        return 1.0
    raw = v / (t / 100.0)
    return raw / (1.0 + raw)


def conviction_scaled_fraction(strength_ratio, *, lo=0.25, hi=0.75):
    """DERISK/TAKE_PROFIT fraction scaled by signal strength instead of the hard 0.5.

    [bound] to [lo, hi] = [0.25, 0.75] by construction. Parameter-free: a linear map of the
    already-computed vol-vs-reference ratio, so a stronger signal banks a bigger slice.

    WO-5B: `None` ratio (no strength evidence) returns None — NOT the old 0.5 midpoint. A sizing
    input never falls back across rules; a rule with no size proposes no partial (the caller defers).
    """
    if strength_ratio is None:
        return None
    r = max(0.0, min(1.0, float(strength_ratio)))
    return lo + (hi - lo) * r


# WO-5B — four action-specific sizers. Each requires ONLY its own evidence and returns None when it
# is missing (no proposal, never a fabricated midpoint). The registered modulation inputs (exec-peak
# drawdown, arrival exhaustion, route fragility, remaining depth, risk persistence) are accepted so
# WO-7 replay can fit their weighting; until it does, the conservative form is today's registered
# fraction. By doctrine none of these can ever RAISE the fraction — monotonic risk reduction only.

def size_take_profit(*, strength_ratio, exec_gain=None, drawdown_from_exec_peak=None,
                     arrival_exhaustion=None):
    """TP banks into strength. Required evidence: `strength_ratio` (vol-vs-trail). Missing -> None."""
    return conviction_scaled_fraction(strength_ratio)


def size_vol_derisk(*, strength_ratio, route_degraded=False, remaining_depth_fraction=None,
                    risk_persistence_s=None):
    """vol-expansion DERISK. Required evidence: `strength_ratio`. Missing -> None."""
    return conviction_scaled_fraction(strength_ratio)


def size_time_derisk(*, registered_fraction=None):
    """time-DERISK sizes from its OWN registered fraction (never a vol fallback). Absent registration
    -> the conservative TIME_DERISK_FRACTION until WO-7 registers a per-band value."""
    return TIME_DERISK_FRACTION if registered_fraction is None else float(registered_fraction)


def size_lp_derisk(*, removed_fraction=None, registered_fraction=None):
    """material LP-removal DERISK (catastrophic CUTs are full exits, never sized). Conservative =
    LP_MATERIAL_DERISK_FRACTION until WO-7 registers a per-chain value."""
    return LP_MATERIAL_DERISK_FRACTION if registered_fraction is None else float(registered_fraction)


def clamp_partial_fraction(proposed, *, slippage_cap_max=None, remaining_budget=None):
    """Final partial size = min(proposed, max-fraction-inside-slippage-cap, remaining partial budget),
    skipping any bound the executor could not measure. A spent budget (0.0) returns None so the
    caller cannot emit an unexecutable zero-size action. `proposed` None -> None."""
    if proposed is None:
        return None
    try:
        out = float(proposed)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or not 0.0 < out <= 1.0:
        return None
    for bound in (slippage_cap_max, remaining_budget):
        if bound is not None:
            try:
                value = float(bound)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                return None
            out = min(out, value)
    return out if out > 0.0 else None


def route_health(controller_state, *, gone_streak=None):
    """WO-5B route-degradation feed: (route_degraded, route_gone) from the WO-5A controller columns.

    `route_degraded` is the reducer's degradation onset (set from a degraded whole-bag quote or a
    route failure). `route_gone` requires a REGISTERED consecutive-failure streak (`gone_streak`,
    from WO-7) — absent it the feed can never declare the route gone, so it stays conservative and
    the LP classifier keeps today's behavior."""
    degraded = controller_state.route_degradation_since is not None
    gone = (gone_streak is not None
            and controller_state.route_failure_streak >= int(gone_streak))
    return (degraded or gone), gone


# [inherited] measured MEDIAN time-to-peak per coin band, from memecoin_positions.time_to_peak_s
# (query 2026-07-25, n in the prereg). The "3h" prior was 4-8x too long: nano/small peak in ~21-25
# min, mid/large in ~41-46 min. Edge-duration derisk windows scale to THIS, per band, not a uniform prior.
BAND_TIME_TO_PEAK_S = {
    "nano<1M": 1524,
    "small1-5M": 1250,
    "mid5-30M": 2457,
    "large>=30M": 2787,
    "unknown": 2457,          # conservative: the slower mid-cap median when the band is unknown
}

# Funded contracts freeze operational bands at <50K/<250K/<1M/1-2M. The measured v5 cohorts use
# <1M/1-5M/5-30M/30M+, so translate by interval containment before lookup. Without this adapter every
# funded band missed the table and silently received the unknown/mid5-30M fallback.
FUNDED_BAND_PROFILE = {
    "assumed_nano": "nano<1M",
    "nano": "nano<1M",
    "micro": "nano<1M",
    "small": "nano<1M",
    "mid": "small1-5M",
}


def market_cap_band(entry_mcap):
    """The preregistered research cohort for a point-in-time entry market cap."""
    try:
        value = float(entry_mcap)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    if value < 1_000_000:
        return "nano<1M"
    if value < 5_000_000:
        return "small1-5M"
    if value < 30_000_000:
        return "mid5-30M"
    return "large>=30M"


def band_edge_duration_s(coin_band):
    """Per-band edge-duration [inherited] from the measured time-to-peak median. Nano coins get a
    SHORTER derisk window than mid caps because they peak sooner (measured, not assumed)."""
    profile = FUNDED_BAND_PROFILE.get(coin_band, coin_band)
    return BAND_TIME_TO_PEAK_S.get(profile, BAND_TIME_TO_PEAK_S["unknown"])
