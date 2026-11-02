"""Measurement Audit v1 primitives — microprice, CKS order-flow imbalance, and EXECUTABLE
marking. Cost-clearing claims are marked buy-at-ask / sell-at-bid + fee, never at mid or
microprice (microprice is a transform of the predictor; marking PnL on it fabricates edge).
"""
from __future__ import annotations


def microprice(best_bid: float, best_ask: float, bid_sz: float, ask_sz: float) -> float | None:
    """Size-weighted fair value (Stoikov): leans toward the ask when bid size dominates."""
    b, a, bs, as_ = _f(best_bid), _f(best_ask), _f(bid_sz), _f(ask_sz)
    if None in (b, a, bs, as_) or (bs + as_) <= 0:
        return None
    return (a * bs + b * as_) / (bs + as_)


def ofi_step(prev: tuple, cur: tuple) -> float | None:
    """Cont-Kukanov-Stoikov best-level order-flow imbalance between two book snapshots.

    Each snapshot is (bid_px, bid_sz, ask_px, ask_sz). Positive OFI = net buy pressure.
    """
    if prev is None or cur is None:
        return None
    pbp, pbs, pap, pas = (_f(x) for x in prev)
    cbp, cbs, cap, cas = (_f(x) for x in cur)
    if None in (pbp, pbs, pap, pas, cbp, cbs, cap, cas):
        return None
    if cbp > pbp:
        bid = cbs
    elif cbp == pbp:
        bid = cbs - pbs
    else:
        bid = -pbs
    if cap < pap:
        ask = -cas
    elif cap == pap:
        ask = -(cas - pas)
    else:
        ask = pas
    return bid + ask


def executable_return(side: str, entry_bid: float, entry_ask: float,
                      exit_bid: float, exit_ask: float, fee_rate: float = 0.00045) -> float | None:
    """Net taker round-trip return marked at executable prices (+ both-leg fee).

    long  = buy entry_ask, sell exit_bid;  short = sell entry_bid, buy exit_ask.
    """
    eb, ea, xb, xa, fee = (_f(entry_bid), _f(entry_ask), _f(exit_bid), _f(exit_ask), _f(fee_rate) or 0.0)
    if side == "long":
        if ea is None or xb is None or ea <= 0:
            return None
        return (xb - ea) / ea - 2.0 * fee
    if side == "short":
        if eb is None or xa is None or eb <= 0:
            return None
        return (eb - xa) / eb - 2.0 * fee
    return None


def signed_microprice_return(entry_micro: float, future_micro: float, side: str) -> float | None:
    """Lead-structure label only — forward microprice move, side-aware. NEVER a PnL mark."""
    e, f = _f(entry_micro), _f(future_micro)
    if e is None or f is None or e <= 0:
        return None
    raw = (f - e) / e
    return raw if side == "long" else (-raw if side == "short" else None)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
