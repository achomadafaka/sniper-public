"""Phase 1D — forward paper execution + fee scenarios + settlement PnL.

The paper fill is fee-EXCLUSIVE: spend up to a fixed GROSS notional ($10) walking the SELECTED token's ask
ladder ascending; the acquired shares are frozen and do NOT change with the fee source. Fee is computed
separately per scenario over the frozen per-level fill (F0 gross, F1 documented 0.07 curve, F2 canonical
later). Settlement uses the Polymarket winning outcome (Up when Chainlink end >= start). Never infer a
token's ask from ``1 - opposite_price``.
"""
from __future__ import annotations

_EPS = 1e-9
DOCUMENTED_FEE_RATE = 0.07
FEE_ROUNDING = 5


def walk_ask_ladder(asks, *, gross_usdc, min_order_size) -> dict:
    """Walk a SELECTED token's ask ladder, spending up to ``gross_usdc`` (fee-exclusive). Requires the full
    gross to deploy within visible depth AND shares >= min_order_size; else no fill with a reason."""
    levels = sorted(([float(p), float(s)] for p, s in (asks or []) if s and float(s) > 0),
                    key=lambda x: x[0])
    if not levels:
        return {"filled": False, "reason": "insufficient_depth"}
    best_ask = levels[0][0]
    remaining = float(gross_usdc)
    shares = gross = 0.0
    consumed = []
    for price, size in levels:
        if remaining <= _EPS:
            break
        afford = remaining / price                 # gross spend (no fee folded in)
        take = min(size, afford)
        if take <= 0:
            continue
        cost = take * price
        shares += take
        gross += cost
        remaining -= cost
        consumed.append({"price": price, "shares": take})
    if remaining > _EPS:
        return {"filled": False, "reason": "insufficient_depth"}
    if shares < float(min_order_size):
        return {"filled": False, "reason": "below_min_order"}
    return {
        "filled": True, "reason": None, "shares": shares, "gross_trade_cost": gross,
        "best_ask": best_ask, "depth_weighted_fill_price": gross / shares,
        "slippage_vs_best_ask": gross / shares - best_ask,
        "levels": consumed, "levels_consumed": len(consumed),
    }


def documented_fee(levels, *, rate=DOCUMENTED_FEE_RATE) -> float:
    """Per-level taker fee ``shares · rate · price · (1-price)``, rounded to 5 dp per level then summed."""
    return sum(round(l["shares"] * rate * l["price"] * (1.0 - l["price"]), FEE_ROUNDING) for l in levels)


def fee_scenarios(levels, *, canonical_rate=None) -> dict:
    """Separate fee tracks over the SAME frozen fill. F0 gross (diagnostic, can't pass); F1 documented 0.07;
    F2 canonical when a per-market descriptor rate is supplied."""
    out = {"F0_gross": 0.0, "F1_documented": documented_fee(levels, rate=DOCUMENTED_FEE_RATE)}
    if canonical_rate is not None:
        out["F2_canonical"] = documented_fee(levels, rate=float(canonical_rate))
    return out


def pnl(*, shares, gross_trade_cost, fee, won) -> float:
    """$1-payout token: win -> shares - gross - fee; lose -> -gross - fee."""
    return (shares - gross_trade_cost - fee) if won else (-gross_trade_cost - fee)


def selected_won(selected_outcome, winning_outcome):
    """True/False if resolved (winning_outcome in Up/Down); None if unresolved/unknown."""
    if winning_outcome not in ("Up", "Down"):
        return None
    return selected_outcome == winning_outcome
