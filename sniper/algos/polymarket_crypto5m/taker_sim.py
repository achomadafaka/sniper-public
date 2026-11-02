"""Deterministic FOK taker-fill estimator for ONE token's ask ladder.

Cash-budget FOK: deploy the full ``cash_budget`` (inclusive of taker fee) within visible ask depth or
skip with ``insufficient_depth``. Per share: ``cost_per_share = price + fee_rate·price·(1-price)``.
Walks asks ascending (result is independent of input order). One token only — the opposite token's
book is never used (no complement arithmetic). No maker fills.
"""
from __future__ import annotations

from .fee_model import cost_per_share

_EPS = 1e-9


def _levels(asks):
    out = []
    for a in asks or []:
        p, s = (float(a["price"]), float(a["size"])) if isinstance(a, dict) else (float(a[0]), float(a[1]))
        if s > 0:
            out.append((p, s))
    out.sort(key=lambda x: x[0])   # ascending price — never trust input order
    return out


def _skip(reason):
    return {"filled": False, "reason": reason}


def estimate_taker_buy(*, asks, fee_rate, cash_budget=10.0, min_order_size=5.0, book_ts=None,
                       book_age_ms=None, modeled_latency_ms=0, book_integrity_status="usable",
                       market_closed=False, fee_known=True, max_book_age_ms=None) -> dict:
    """Estimate a $``cash_budget`` FOK taker buy. Returns ``{filled: bool, reason, ...metrics}``."""
    if market_closed:
        return _skip("market_closed")
    if book_integrity_status == "quarantined":
        return _skip("book_quarantined")
    if book_integrity_status != "usable":
        return _skip("book_stale")
    if max_book_age_ms is not None and book_age_ms is not None and book_age_ms > max_book_age_ms:
        return _skip("book_stale")
    if not fee_known or fee_rate is None:
        return _skip("fee_unknown")

    levels = _levels(asks)
    if not levels:
        return _skip("insufficient_depth")

    best_ask = levels[0][0]
    remaining = float(cash_budget)
    shares = gross = fee = 0.0
    worst = best_ask
    consumed = 0
    for price, size in levels:
        if remaining <= _EPS:
            break
        cps = cost_per_share(price, fee_rate)
        affordable = remaining / cps
        take = min(size, affordable)
        if take <= 0:
            continue
        shares += take
        gross += take * price
        fee += take * (cps - price)
        remaining -= take * cps
        worst = price
        consumed += 1

    if remaining > _EPS:                       # full budget not deployable within visible depth
        return _skip("insufficient_depth")
    if shares < float(min_order_size):
        return _skip("below_min_order")

    total = gross + fee
    return {
        "filled": True, "reason": None,
        "shares": shares, "gross_trade_cost": gross, "taker_fee": fee, "total_cash_cost": total,
        "depth_weighted_fill_price": gross / shares,
        "fee_inclusive_average_cost": total / shares,
        "best_ask": best_ask, "worst_ask_level": worst,
        "slippage_vs_best_ask": gross / shares - best_ask,
        "levels_consumed": consumed, "book_ts": book_ts, "book_age_ms": book_age_ms,
        "modeled_latency_ms": modeled_latency_ms,
    }
