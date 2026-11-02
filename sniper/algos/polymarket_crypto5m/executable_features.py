"""Executable-price features (family C/exec) — what a $5/$10/$20 FOK taker buy would actually cost on the
CURRENT book, for Up and Down INDEPENDENTLY. These are OBSERVATIONS exposed from the taker engine, not
trade decisions.

The ask walk (executable price, slippage, levels, fillability) is fee-exclusive and is computed even when
the fee is unresolved (walk at fee_rate=0). The fee-INCLUSIVE cost is null unless the fee is known — we
never substitute a guessed fee.
"""
from __future__ import annotations

from . import taker_sim as TS

STAKES_USDC = (5, 10, 20)


def _side_features(side: str, book, *, walk_fee: float, fee_known: bool) -> dict:
    asks = (book or {}).get("asks") or []
    integrity = (book or {}).get("book_integrity_status", "usable")
    out = {}
    for stake in STAKES_USDC:
        r = TS.estimate_taker_buy(asks=asks, fee_rate=walk_fee, cash_budget=float(stake),
                                  fee_known=True, book_integrity_status=integrity)
        filled = bool(r.get("filled"))
        out[f"{side}_executable_price_{stake}_usdc"] = r.get("depth_weighted_fill_price")
        out[f"{side}_fee_inclusive_cost_{stake}_usdc"] = (
            r.get("fee_inclusive_average_cost") if (fee_known and filled) else None)
        out[f"{side}_slippage_vs_best_ask_{stake}_usdc"] = r.get("slippage_vs_best_ask")
        out[f"{side}_fillability_{stake}_usdc"] = 1.0 if filled else 0.0
        out[f"{side}_levels_consumed_{stake}_usdc"] = r.get("levels_consumed")
    return out


def build_executable_features(up_book, down_book, *, fee_rate, fee_known) -> dict:
    """Per-side, per-stake executable metrics. ``fee_rate``/``fee_known`` come from the reconciled fee
    (fee_status=='known'). Fee unknown -> fee-inclusive fields null; the fee-exclusive ask walk still runs."""
    eff_known = bool(fee_known) and fee_rate is not None
    walk_fee = float(fee_rate) if eff_known else 0.0
    feats = {}
    feats.update(_side_features("up", up_book, walk_fee=walk_fee, fee_known=eff_known))
    feats.update(_side_features("down", down_book, walk_fee=walk_fee, fee_known=eff_known))
    return feats
