"""Drift-corrected first-passage reachability for the crypto5m taker redesign (brain v2, SHADOW).

The driftless ev-reach gate is EV-neutral by construction: its signal terms are symmetric in z
(phi(-z)=phi(z)), so Up and Down get an identical `gain` — `conviction` enters only as a magnitude, never
a direction. The viability study confirmed it predicts nothing (rho 0.024, p 0.76). This module fixes the
*premise*: a MEASURED directional drift `mu` (from real flow) enters a Girsanov-corrected first-passage
model, which is asymmetric in the sign of the move — the only thing that can make reachability tradeable.

Pure compute, no I/O. Fail-closed (None on degenerate inputs; mu=0 collapses to the driftless baseline so
the model is never worse than today). SHADOW only — nothing here feeds a live decision in v1.
"""
from __future__ import annotations

import math

# Pre-registered constants (fixed before the holdout is seen; conservative so the model can't manufacture
# confidence). K_DRIFT ~ BTC's typical 1s log-return scale; SHRINK<1 = don't over-trust a noisy flow read.
K_DRIFT = 1e-4          # max |drift| per second before shrink (log-price units / s)
SHRINK = 0.5            # shrinkage on the measured drift
EMA_SLOPE_SCALE = 5e-4  # a "strong" EMA(10)-vs-EMA(30) relative slope, for standardizing ema_slope to ~[-1,1]
BANKABLE_MARGIN = 0.05  # odds gain that would make a scalp "bankable" — sets the favorable barrier b

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _phi(x):                       # standard normal PDF
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _Phi(x):                       # standard normal CDF
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def drift_estimate(*, tape_ofi=None, ema_slope=None, volume_dir=None, k_drift=K_DRIFT, shrink=SHRINK):
    """Measured BTC per-second log-price drift `mu` from the flow signals already on the desk. Each signal
    is normalized to ~[-1,1] and averaged; the result is scaled by k_drift*shrink and bounded by it. No
    signals present -> 0.0 (fail-closed to the driftless baseline)."""
    votes = []
    if tape_ofi is not None:
        votes.append(_clip(float(tape_ofi), -1.0, 1.0))                 # signed trade-tape OFI, already ~[-1,1]
    if volume_dir is not None:
        votes.append(_clip(2.0 * float(volume_dir), -1.0, 1.0))         # taker-buy share - 0.5, in [-0.5,0.5]
    if ema_slope is not None:
        votes.append(_clip(float(ema_slope) / EMA_SLOPE_SCALE, -1.0, 1.0))
    if not votes:
        return 0.0
    return k_drift * shrink * (sum(votes) / len(votes))


def touch_prob(b, sigma, tau, mu):
    """P(the favorable barrier `b`>0 log-price units away is touched before expiry `tau`) under drift `mu`,
    via the Girsanov-corrected reflection principle:
        Phi(-(b-mu*tau)/(sigma*sqrt(tau))) + exp(2*mu*b/sigma^2) * Phi(-(b+mu*tau)/(sigma*sqrt(tau)))
    At mu=0 this is the symmetric 2*Phi(-b/(sigma*sqrt(tau))). None on degenerate inputs; b<=0 -> 1.0
    (already at/past the favorable level)."""
    if b is None or sigma is None or tau is None or mu is None:
        return None
    if sigma <= 0 or tau <= 0:
        return None
    if b <= 0:
        return 1.0
    s = sigma * math.sqrt(tau)
    expo = _clip(2.0 * mu * b / (sigma * sigma), -700.0, 700.0)         # guard exp overflow on extreme inputs
    p = _Phi(-(b - mu * tau) / s) + math.exp(expo) * _Phi(-(b + mu * tau) / s)
    return _clip(p, 0.0, 1.0)


def expected_fav_move(z, sigma, tau, mu, t_hold):
    """First-order expected FAVORABLE odds move over `t_hold` under drift: delta * mu * t_hold, where
    delta = phi(z)/(sigma*sqrt(tau)) is odds gained per unit log-move. Signed (follows mu). This is the
    quantity validated against realized_mid_move. None on degenerate inputs; mu=0 -> 0 (no directional move).
    Linear approx — fine for a rank/correlation test (the sign and relative magnitude are what matter)."""
    if None in (z, sigma, tau, mu, t_hold):
        return None
    if sigma <= 0 or tau <= 0 or t_hold <= 0:
        return None
    delta = _phi(z) / (sigma * math.sqrt(tau))
    return delta * mu * t_hold


def max_hold_s(z, tau):
    """Gamma-theta breakeven max hold time = 4*tau/z^2, capped to `tau`. Derived from delta*sigma*sqrt(dt) >=
    theta*dt with delta=phi(z)/(sigma*sqrt(tau)), theta=phi(z)*|z|/(2*tau) — sigma CANCELS. Deeper |z| (theta
    burns faster) -> shorter hold. z~0 (theta ~0) -> capped to tau. Logged as a diagnostic; gates nothing in
    v1. None on degenerate tau."""
    if z is None or tau is None or tau <= 0:
        return None
    if z * z <= 1e-18:
        return tau
    return min(4.0 * tau / (z * z), tau)


def side_prediction(*, side, z, vol_per_s, seconds_left, mu_hat, t_hold, bankable_margin=BANKABLE_MARGIN):
    """Compose the primitives into the drift-reachability prediction for a CONFIRMED side (the row we log).
    The favorable drift is +mu_hat for Up, -mu_hat for Down (a down-move favors the Down token). `b` is the
    log-price distance for the side's odds to gain `bankable_margin` (b = margin/delta). Returns a dict with
    the signed expected move, touch prob, the driftless baseline (0), and the diagnostic max hold — or None
    on degenerate inputs (fail-closed)."""
    if None in (z, vol_per_s, seconds_left, mu_hat, t_hold) or vol_per_s <= 0 or seconds_left <= 0:
        return None
    mu_signed = mu_hat if side == "Up" else -mu_hat
    delta = _phi(z) / (vol_per_s * math.sqrt(seconds_left))            # odds per unit log-move (side-agnostic)
    if delta <= 0:
        return None
    b = bankable_margin / delta
    return {
        "mu_signed": mu_signed,
        "b_barrier": b,
        "touch_prob": touch_prob(b, vol_per_s, seconds_left, mu_signed),
        "exp_fav_move": expected_fav_move(z, vol_per_s, seconds_left, mu_signed, t_hold),
        "exp_fav_move_driftless": 0.0,
        "max_hold_s": max_hold_s(z, seconds_left),
    }
