"""Regime-gated vol-floor selection for the crypto5m maker (campaign maker-regime-gated-v1).

Finding 2026-07-07 (docs/superpowers/specs/2026-07-07-crypto5m-maker-tournament-finding-regime-gated-design.md):
the maker's edge is not online learning, it is matching quote posture to market state.
On the forward common set (N=137) the honest tight vol floor (2e-5) won the QUIET / one-sided
windows (+3.60/w) while the inflated wide floor (1e-4) won the CONTESTED windows (+4.79/w).

This module is the deterministic 2-state switch: classify the live window as quiet vs contested
from the current up-fair and book spread (both causal — known at quote time), and pick the floor.

Bucketing matches the runner's own live classifier (maker_regime_state):
  odds mid_chop = up-fair in [0.40, 0.60];  spread healthy = spread <= 0.03.
QUIET  = up-fair OUTSIDE mid_chop AND spread healthy  -> tight/honest floor.
CONTESTED = everything else (incl. unknown fair/spread) -> wide/protective floor.

Unknown inputs fail to the CONTESTED (protective) floor on purpose: a missing fair/spread is
not evidence of a calm one-sided book, so we do not quote tight into it.
"""
from __future__ import annotations

import math
from typing import Optional

# runner live classifier boundaries (maker_regime_state._odds_bucket / _spread_state)
MID_CHOP_LOW = 0.40
MID_CHOP_HIGH = 0.60
SPREAD_HEALTHY_MAX = 0.03
# a book this wide is untradeable — protect regardless of odds (used by the continuous ramp)
SPREAD_UNTRADEABLE = 0.08


def is_quiet(fair: Optional[float], spread: Optional[float]) -> bool:
    """True when the window is quiet/one-sided with a healthy (tight) book.

    Quiet = up-fair outside the [0.40, 0.60] mid_chop band AND spread <= 0.03.
    Missing fair or spread -> not quiet (fail to the protective side).
    """
    if fair is None or spread is None:
        return False
    one_sided = fair < MID_CHOP_LOW or fair > MID_CHOP_HIGH
    healthy = spread <= SPREAD_HEALTHY_MAX
    return bool(one_sided and healthy)


def select_gated_floor(
    fair: Optional[float],
    spread: Optional[float],
    quiet_floor: float,
    contested_floor: float,
) -> float:
    """Pick the per-second vol floor for this window from the live regime.

    quiet_floor for quiet/one-sided/tight windows (harvest), contested_floor otherwise
    (protect). Both floors are passed in so the runner owns the values; this module owns
    only the classification.
    """
    return float(quiet_floor if is_quiet(fair, spread) else contested_floor)


def ramp_floor(
    fair: Optional[float],
    spread: Optional[float],
    honest_floor: float,
    wide_floor: float,
    x_lo: float,
    x_hi: float,
) -> float:
    """regime-gated-v2: continuous vol floor, log-interpolated by odds-extremity x=|fair-0.5|.

    wide_floor (protective) at x<=x_lo (contested), honest_floor (harvest) at x>=x_hi (one-sided),
    smooth log-space ramp between. Interpolation is in log space because the floors span ~5x and
    log is the natural scale for a variance floor.

    The result is always bounded within [min(anchors), max(anchors)] — no extrapolation beyond the
    two floors we have evidence for (spec 2026-07-07 §2, the counterfactual limit). We fail to
    wide_floor (protect; never quote tight into the unknown) on ANY of: unknown fair, unknown spread
    (crossed/locked/missing book -> _spread is None), or an untradeable book (spread > 0.08). This
    mirrors v1's is_quiet() exactly (which returns contested/wide when spread is None), so the
    v1-vs-v2 paired A/B differs ONLY in binary-vs-continuous, not in edge-case handling. (Unknown
    spread is ~9-17% of ticks, so this is load-bearing, not a nicety.)

    honest_floor and wide_floor must be > 0 (math.log); the runner validates this at flag parse.
    x_lo < x_hi is required by construction (the runner validates the flag); if passed equal or
    inverted, the >= x_hi branch wins first for x>=x_hi and the <= x_lo branch for x<=x_lo, and the
    guarded interior division cannot run because x_lo>=x_hi makes one of those branches always true.
    """
    if fair is None:
        return float(wide_floor)
    if spread is None or spread > SPREAD_UNTRADEABLE:
        return float(wide_floor)
    x = abs(fair - 0.5)
    if x <= x_lo:
        return float(wide_floor)
    if x >= x_hi:
        return float(honest_floor)
    t = (x - x_lo) / (x_hi - x_lo)
    log_floor = math.log(wide_floor) + t * (math.log(honest_floor) - math.log(wide_floor))
    return float(math.exp(log_floor))
