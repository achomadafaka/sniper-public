"""Book integrity reconciliation: compare the locally reconstructed book against a fresh REST full
book. The market channel has no documented monotonic sequence number, so a consumer cannot prove
completeness from message ordering alone — hence periodic REST reconciliation. Any unexplained
divergence marks the interval unusable for (future) simulated execution.
"""
from __future__ import annotations

from .book_state import BookState

_SIZE_EPS = 1e-9


def _levels_diff(a: dict, b: dict) -> int:
    """Count price levels that differ between two price->size maps (present-in-one or size mismatch)."""
    diverged = 0
    for price in set(a) | set(b):
        sa, sb = a.get(price), b.get(price)
        if sa is None or sb is None or abs(float(sa) - float(sb)) > _SIZE_EPS:
            diverged += 1
    return diverged


def compare_books(reconstructed: BookState, rest_full: dict) -> dict:
    """Compare a reconstructed ``BookState`` to a fresh REST ``{"bids":[...], "asks":[...]}`` book."""
    fresh = BookState(token_id=reconstructed.token_id, condition_id=reconstructed.condition_id,
                      side=reconstructed.side)
    fresh.apply_book(bids=rest_full.get("bids"), asks=rest_full.get("asks"))

    bid_div = _levels_diff(reconstructed.bids, fresh.bids)
    ask_div = _levels_diff(reconstructed.asks, fresh.asks)
    divergence_levels = bid_div + ask_div
    # both empty = no evidence, not agreement: report indeterminate (match=None), don't claim a match
    both_empty = not (reconstructed.bids or reconstructed.asks or fresh.bids or fresh.asks)
    return {
        "match": None if both_empty else divergence_levels == 0,
        "indeterminate": both_empty,
        "divergence_levels": divergence_levels,
        "bid_divergence_levels": bid_div,
        "ask_divergence_levels": ask_div,
        "best_bid_match": reconstructed.best_bid() == fresh.best_bid(),
        "best_ask_match": reconstructed.best_ask() == fresh.best_ask(),
        "rest_best_bid": fresh.best_bid(),
        "rest_best_ask": fresh.best_ask(),
    }


def should_quarantine(cmp: dict) -> bool:
    """Quarantine a reconciliation interval ONLY on a top-of-book (decision-relevant) reconstruction
    failure — a best-bid or best-ask mismatch. Interior-level divergence alone is benign book MOVEMENT
    between the WS state and a later REST refetch (different instants), not a reconstruction error, so it
    must NOT quarantine (the old ``divergence_levels > 0`` criterion fired on ~every interval and zeroed
    model-eligibility). ``indeterminate`` (no evidence) never quarantines."""
    if cmp.get("indeterminate"):
        return False
    return not cmp["best_bid_match"] or not cmp["best_ask_match"]
