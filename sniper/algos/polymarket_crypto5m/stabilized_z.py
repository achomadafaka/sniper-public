"""Stabilized, market-regularized probability for crypto5m brain v2 (pure; no I/O, no cross-poll state).

Diffusion stays primary; the market regularizes via a z-space blend whose weight DROPS in sharp regimes.
The live weight is the pinned prior (below). The harness can refit a weight function offline from the logged
components -- the live path uses the prior, which is why it must be anchored, not arbitrary."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m.live_desk import diffusion_z
from sniper.algos.polymarket_crypto5m.reachability import probit

D_REF = 2.0      # reference divergence (fast = 2x slow) -> a clear sharp regime
S_REF = 0.01     # reference spread (median observed crypto5m spread)
EPS = 1e-4       # market_mid clamp bound for probit


def w_prior(divergence, spread, *, d_ref=D_REF, s_ref=S_REF):
    """Pinned precision-style weight on OUR model. var_ours=(div/d_ref)^2, var_market=(spread/s_ref)^2 ->
    w = var_market/(var_ours+var_market). Anchored: w=0.5 at (d_ref, s_ref). No free scale knob."""
    d = max(0.0, float(divergence)) / d_ref
    s = max(0.0, float(spread)) / s_ref if spread is not None else 0.0
    var_ours, var_market = d * d, s * s
    denom = var_ours + var_market
    if denom <= 0:
        return 1.0     # both degenerate -> trust ours (calm, tight)
    return var_market / denom


def jump_cap_decay(ticks_since_jump, *, cap_floor=0.15, decay_s=8):
    """Effective CAP on the weight after a jump: cap_floor at the jump, linearly relaxing to 1.0 over decay_s
    (un-cede). Pure function of ticks_since_jump (seconds since the most recent in-window jump)."""
    if ticks_since_jump is None:
        return 1.0
    t = max(0, int(ticks_since_jump))
    if t >= decay_s:
        return 1.0
    return cap_floor + (1.0 - cap_floor) * (t / decay_s)


def stabilized_z(btc_now, btc_start, seconds_left, regime_out, market_mid, spread):
    """z_v2 = w*z_ours + (1-w)*z_market (z-space). z_ours uses the jump-excluded vol_slow.
    Our inputs degenerate -> None. Market thin/missing -> z_v2 = z_ours (weight 1). Fail-closed."""
    if not regime_out:
        return None
    z_ours = diffusion_z(btc_now, btc_start, regime_out.get("vol_slow"), seconds_left)
    if z_ours is None:
        return None
    div = regime_out.get("divergence")
    jflag = regime_out.get("jump_flag")
    base = {"z_ours": z_ours, "divergence": div, "jump_flag": jflag}

    if market_mid is None:
        return {**base, "z_v2": z_ours, "z_market": None, "weight": 1.0}
    mid = min(1.0 - EPS, max(EPS, float(market_mid)))
    z_market = probit(mid)
    if z_market is None:
        return {**base, "z_v2": z_ours, "z_market": None, "weight": 1.0}

    w = min(w_prior(div, spread), jump_cap_decay(regime_out.get("ticks_since_jump")))
    z_v2 = w * z_ours + (1.0 - w) * z_market
    return {**base, "z_v2": z_v2, "z_market": z_market, "weight": w}
