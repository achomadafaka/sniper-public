"""Reachability / expected-odds-move EV gate for the Polymarket BTC 5m scalper.

The directional read says WHICH way BTC is moving; this module says whether a correct read can actually
MOVE THE ODDS more than it costs, given how far the binary is from target (moneyness z) and how much time
is left. A deep-underdog near expiry barely responds to a move and bleeds to 0 (theta) — direction
confirmed, odds unreachable. This gate rejects it.

Math (the driftless diffusion the engine already runs; z = ln(now/start)/(vol*sqrt(t_left))):
    delta = phi(z)/(vol*sqrt(t_left))           odds gained per unit BTC log-move
    theta = phi(z)*|z|/(2*t_left)               odds bled per second of time passing
    E[signed log move over t_hold] ~ k*conviction*vol*sqrt(t_hold)
    EV(t_hold) = A*sqrt(t_hold) - B*t_hold - cost   A = phi(z)*k*conviction/sqrt(t_left), B = theta
EV is concave in t_hold -> t* = (A/2B)^2, CAPPED to the window. cost = spread + fee. Pure / no I/O.

LIVE PATH USES THE CAPPED EV(t_hold) ONLY — never the phi(z)/|z| interior closed-form (div-by-zero at ATM).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class EVConfig:
    k: float = 0.10                    # shrinkage / strictness — biased LOW (EV* ~ k^2; low = strict = safe)
    documented_fee_rate: float = 0.07  # ALWAYS logged -> ev_007/passed_007 (real-money-readiness overlay)
    realized_fee_rate: float = 0.0     # ALWAYS logged -> ev_realfee/passed_realfee (paper cost = spread)
    gate_on_realized: bool = True      # v1: LIVE `passed` follows the realized-fee EV (paper fee is 0); the
                                       # 0.07 overlay stays logged but does NOT gate. False = gate on 0.07.
    t_min: float = 5.0                 # floor on modeled hold (s)
    exit_buffer_s: float = 20.0        # never model holding past window_close - this (mirrors MIN_SECONDS_LEFT)
    t_ceiling_s: float = 30.0          # cap on modeled hold — bounds linear-theta/const-sigma approx; low = strict
    price_floor: float = 0.05          # catastrophic price-band guardrail (the EV gate is the operative band)
    price_ceiling: float = 0.95


def normal_pdf(z: float) -> float:
    return INV_SQRT_2PI * math.exp(-0.5 * z * z)


def probit(p):
    """Inverse normal CDF (Acklam's rational approximation). LOGGING ONLY (market-probit-z). None outside (0,1)."""
    if p is None or not (0.0 < p < 1.0):
        return None
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def _blank(reason):
    return {"passed": False, "passed_007": False, "passed_realfee": False, "ev_007": None, "ev_realfee": None,
            "delta": None, "theta": None, "t_hold": None, "t_star": None, "gain": None, "gate_stat": None,
            "spread": None, "cost_007": None, "cost_realfee": None, "reject_reason": reason}


def entry_ev(*, z, vol_per_s, seconds_left, conviction, side_ask, side_bid, cfg: EVConfig = EVConfig()):
    """Expected-odds-move EV for entering `side` THIS tick, per share, in odds (price) units.
    LIVE decision = `passed` (== passed_realfee when gate_on_realized=True, else passed_007).
    Both fee columns are always returned; `*_realfee` feeds the cost-bound/maker verdict, `*_007` the overlay.
    Fail-closed: degenerate inputs -> passed=False + reject_reason (never divides by zero/None)."""
    if (z is None or vol_per_s is None or vol_per_s <= 0 or seconds_left is None or seconds_left <= 0
            or conviction is None or side_ask is None or side_bid is None
            or not math.isfinite(z)):
        return _blank("degenerate_inputs")
    if not (cfg.price_floor <= side_ask <= cfg.price_ceiling):
        return _blank("price_band")
    upper = min(seconds_left - cfg.exit_buffer_s, cfg.t_ceiling_s)
    if upper < cfg.t_min:
        return _blank("no_hold_room")

    phi = normal_pdf(z)
    absz = abs(z)
    A = phi * cfg.k * conviction / math.sqrt(seconds_left)
    B = phi * absz / (2.0 * seconds_left)                       # = |theta|
    # EV(t)=A*sqrt(t)-B*t is concave; t*=(A/2B)^2. z->0 => B->0 => t*->inf => the cap binds (no div-by-zero;
    # we evaluate the CAPPED EV, never the phi(z)/|z| interior form).
    t_star = math.inf if (B <= 0 or absz < 1e-9) else (A / (2.0 * B)) ** 2
    t_hold = min(max(t_star, cfg.t_min), upper)
    gain = A * math.sqrt(t_hold) - B * t_hold
    spread = max(0.0, side_ask - side_bid)

    def _cost(fee_rate):
        return spread + fee_rate * side_ask * (1.0 - side_ask)

    cost_007 = _cost(cfg.documented_fee_rate)
    cost_real = _cost(cfg.realized_fee_rate)
    ev_007 = gain - cost_007
    ev_real = gain - cost_real
    gate_stat = (conviction * conviction * phi / absz) if absz > 1e-9 else math.inf  # sec 2.2 diagnostic only
    delta = phi / (vol_per_s * math.sqrt(seconds_left))
    passed_007 = ev_007 > 0
    passed_realfee = ev_real > 0
    passed = passed_realfee if cfg.gate_on_realized else passed_007   # LIVE decision (v1: realized fee)
    return {"passed": passed, "passed_007": passed_007, "passed_realfee": passed_realfee,
            "ev_007": round(ev_007, 6), "ev_realfee": round(ev_real, 6),
            "delta": round(delta, 6), "theta": round(B, 8), "t_hold": round(t_hold, 3),
            "t_star": (None if math.isinf(t_star) else round(t_star, 3)), "gain": round(gain, 6),
            "gate_stat": (None if math.isinf(gate_stat) else round(gate_stat, 6)),
            "spread": round(spread, 6), "cost_007": round(cost_007, 6), "cost_realfee": round(cost_real, 6),
            "reject_reason": None if passed else "ev_negative"}
