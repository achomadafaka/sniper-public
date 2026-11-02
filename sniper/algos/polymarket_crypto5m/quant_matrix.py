"""Phase 2A — per-decision price-comparison matrix (pure).

Assembles, at one decision time, every probability estimate alongside the EXECUTABLE token prices, so the
only economically actionable edge is explicit:

    net edge = uncertainty-adjusted probability  -  fee-inclusive executable cost per share  -  safety margin

Executable prices reuse the audited FOK ask-ladder walk (forward_paper.walk_ask_ladder). Fee follows the
Polymarket documented form (rate * price * (1-price)); when the fee is unresolved the net edge is None and
no side is actionable. No DB / network / orders.
"""
from __future__ import annotations

from . import forward_paper as FP


def _executable_price(book, gross_usdc, min_order_size):
    fill = FP.walk_ask_ladder((book or {}).get("asks"), gross_usdc=gross_usdc, min_order_size=min_order_size)
    return fill["depth_weighted_fill_price"] if fill.get("filled") else None


def _fee_inclusive(price, fee_rate, fee_known):
    if not fee_known or fee_rate is None or price is None:
        return None
    return round(price + float(fee_rate) * price * (1.0 - price), 6)


def _net_edge(q_side, fee_inc_cost, margin):
    if q_side is None or fee_inc_cost is None:
        return None
    return round(q_side - fee_inc_cost - margin, 6)


def _microprice(book):
    bb, ba = (book or {}).get("best_bid"), (book or {}).get("best_ask")
    bids, asks = (book or {}).get("bids") or [], (book or {}).get("asks") or []
    if bb is None or ba is None or not bids or not asks:
        return None
    bsz, asz = float(bids[0][1]), float(asks[0][1])
    tot = bsz + asz
    return round((float(bb) * asz + float(ba) * bsz) / tot, 6) if tot > 0 else None


def _ofi(book):
    bids, asks = (book or {}).get("bids") or [], (book or {}).get("asks") or []
    if not bids or not asks:
        return None
    bsz, asz = float(bids[0][1]), float(asks[0][1])
    tot = bsz + asz
    return round((bsz - asz) / tot, 6) if tot > 0 else None


def build_comparison_matrix(*, q_components, q_ensemble, q_trade, up_book, down_book, fee_rate, fee_known,
                            latent_btc_price, source_prices, gross_usdc=10.0, min_order_size=5,
                            safety_margin=0.0) -> dict:
    q_market = (q_components or {}).get("q_market")
    up_exec = _executable_price(up_book, gross_usdc, min_order_size)
    down_exec = _executable_price(down_book, gross_usdc, min_order_size)
    up_cost = _fee_inclusive(up_exec, fee_rate, fee_known)
    down_cost = _fee_inclusive(down_exec, fee_rate, fee_known)

    q_trade_up = q_trade
    q_trade_down = (1.0 - q_trade) if q_trade is not None else None
    up_net = _net_edge(q_trade_up, up_cost, safety_margin)
    down_net = _net_edge(q_trade_down, down_cost, safety_margin)

    actionable = None
    candidates = [(s, e) for s, e in (("Up", up_net), ("Down", down_net)) if e is not None and e > 0]
    if candidates:
        actionable = max(candidates, key=lambda x: x[1])[0]

    prices = [float(p) for p in (source_prices or {}).values() if p is not None]
    basis = (max(prices) - min(prices)) if len(prices) >= 2 else 0.0

    return {
        # probabilities
        "q_components": dict(q_components or {}),
        "q_market": q_market,
        "q_ensemble": q_ensemble,
        "q_trade": q_trade,
        "market_residual": (round(q_ensemble - q_market, 6) if (q_ensemble is not None and q_market is not None) else None),
        "forecast_edge_vs_midpoint": (round(q_trade - q_market, 6) if (q_trade is not None and q_market is not None) else None),
        # executable token prices
        "up_best_ask": (up_book or {}).get("best_ask"),
        "down_best_ask": (down_book or {}).get("best_ask"),
        "up_executable_price": up_exec,
        "down_executable_price": down_exec,
        "up_fee_inclusive_cost_per_share": up_cost,
        "down_fee_inclusive_cost_per_share": down_cost,
        "up_microprice": _microprice(up_book),
        "down_microprice": _microprice(down_book),
        "up_order_flow_imbalance": _ofi(up_book),
        "down_order_flow_imbalance": _ofi(down_book),
        # net edges + decision
        "up_net_edge_vs_executable": up_net,
        "down_net_edge_vs_executable": down_net,
        "actionable_side": actionable,
        "safety_margin": safety_margin,
        "fee_rate": fee_rate,
        "fee_known": fee_known,
        # sources
        "latent_btc_price": latent_btc_price,
        "source_prices": dict(source_prices or {}),
        "cross_source_basis_abs": round(basis, 6),
    }
