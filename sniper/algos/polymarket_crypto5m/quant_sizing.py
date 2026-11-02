"""Phase 2A — paper sizing tracks (pure, PAPER-ONLY).

S0 fixed stake; S1 frozen edge-buckets; S2 capped fractional Kelly. S2 sizes from a CONSERVATIVE
(lower-bound/shrunk) probability and the fee-inclusive entry cost, never full Kelly, always under hard caps
(per-market fraction, total open exposure). Stale/disagreeing/uncertain upstream -> caller passes a low
q_conservative -> size goes to zero. This is NOT real-money sizing advice.
"""
from __future__ import annotations


def fixed_stake(*, stake=10.0) -> float:
    """S0 — constant paper notional."""
    return float(stake)


def bucket_stake(net_edge, *, buckets) -> float:
    """S1 — frozen edge buckets. ``buckets`` = ascending list of (min_edge, stake); returns the stake of the
    highest bucket whose min_edge <= net_edge (0 if none). Thresholds frozen before forward activation."""
    stake = 0.0
    for min_edge, s in sorted(buckets, key=lambda b: b[0]):
        if net_edge is not None and net_edge >= min_edge:
            stake = float(s)
    return stake


def fractional_kelly_stake(*, q_conservative, cost_per_share, bankroll, fraction,
                           max_fraction_per_market, max_open_exposure, current_open_exposure) -> dict:
    """S2 — capped fractional Kelly for the binary payoff.

    A share costs ``cost_per_share`` and pays 1 on a win. Full-Kelly fraction of bankroll
    f* = (p - c) / (1 - c) for p > c, else 0. Apply the (sub-1) ``fraction`` multiplier, then clamp to the
    per-market cap and the remaining open-exposure budget. Returns stake + which constraint bound it.
    """
    p, c = float(q_conservative), float(cost_per_share)
    if c <= 0 or c >= 1 or p <= c:
        return {"stake": 0.0, "fraction_used": 0.0, "capped_by": "no_edge",
                "kelly_full": 0.0}
    kelly_full = (p - c) / (1.0 - c)
    fraction_used = kelly_full * float(fraction)
    stake_raw = fraction_used * float(bankroll)
    per_market_cap_val = float(max_fraction_per_market) * float(bankroll)
    open_cap_val = max(0.0, float(max_open_exposure) - float(current_open_exposure))

    final = min(stake_raw, per_market_cap_val, open_cap_val)
    if final == stake_raw:
        capped_by = "kelly"
    elif final == per_market_cap_val:
        capped_by = "per_market_cap"
    else:
        capped_by = "open_exposure_cap"
    return {"stake": round(final, 6), "fraction_used": round(fraction_used, 6),
            "capped_by": capped_by, "kelly_full": round(kelly_full, 6)}
