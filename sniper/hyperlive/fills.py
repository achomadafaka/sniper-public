"""Taker-fill realism for the paper copy book — pure functions, no I/O.

The copy sim fills at ``markPx`` (≈mid) for infinite size. Real money crosses the spread and
walks the book. These functions price that gap from an L2 snapshot so a captured trade can be
replayed at any notional, and so the running book can report deployable-at-size PnL instead of
idealized paper. All bps are COSTS (positive = worse than mid). Not orders; measurement only."""
from __future__ import annotations

from typing import Iterable


def _levels(side_levels: Iterable) -> list[tuple[float, float]]:
    """Accept [{'px':..,'sz':..}] or [[px,sz], ...] -> [(px, sz)] floats, dropping junk."""
    out = []
    for lv in (side_levels or []):
        if isinstance(lv, dict):
            px, sz = lv.get("px"), lv.get("sz")
        else:
            try:
                px, sz = lv[0], lv[1]
            except (TypeError, IndexError):
                continue
        try:
            px, sz = float(px), float(sz)
        except (TypeError, ValueError):
            continue
        if px > 0 and sz > 0:
            out.append((px, sz))
    return out


def _mid(book: dict) -> float | None:
    m = book.get("mid")
    if m:
        try:
            m = float(m)
            if m > 0:
                return m
        except (TypeError, ValueError):
            pass
    bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    return None


def taker_slippage_bps(book: dict, notional: float, buy: bool) -> float | None:
    """VWAP slippage in bps to execute ``notional`` USD as a taker.
      buy  -> lift asks (ascending); cost = (vwap-mid)/mid
      sell -> hit bids (descending); cost = (mid-vwap)/mid
    None if the (captured) book is too thin to absorb the notional. 0 notional -> 0."""
    if notional <= 0:
        return 0.0
    mid = _mid(book)
    if not mid:
        return None
    ladder = _levels(book.get("asks") if buy else book.get("bids"))
    if buy:
        ladder = sorted(ladder, key=lambda x: x[0])          # cheapest ask first
    else:
        ladder = sorted(ladder, key=lambda x: -x[0])         # highest bid first
    filled = shares = 0.0
    for px, sz in ladder:
        avail = px * sz
        take = min(avail, notional - filled)
        shares += take / px
        filled += take
        if filled >= notional * (1 - 1e-9):
            break
    if filled < notional * (1 - 1e-6) or shares <= 0:
        return None                                          # book can't absorb it
    vwap = filled / shares
    slip = (vwap - mid) / mid if buy else (mid - vwap) / mid
    return round(slip * 1e4, 3)


def round_trip_slippage_bps(book_entry: dict, notional: float,
                            book_exit: dict | None = None) -> float | None:
    """Both legs of a round trip at ``notional``. A copy trade always buys once and sells once
    (order depends on long/short), so the cost is buy_leg + sell_leg regardless of direction.
    Uses book_exit for the closing leg if given, else re-uses book_entry as a proxy.
    None if either leg can't be filled."""
    be = book_entry
    bx = book_exit if book_exit is not None else book_entry
    buy_leg = taker_slippage_bps(be, notional, buy=True)
    sell_leg = taker_slippage_bps(bx, notional, buy=False)
    if buy_leg is None or sell_leg is None:
        return None
    return round(buy_leg + sell_leg, 3)


def depth_within_bps(book: dict, bps: float, buy: bool) -> float:
    """Notional (USD) available within ``bps`` of mid on the ask (buy) or bid (sell) side."""
    mid = _mid(book)
    if not mid:
        return 0.0
    ladder = _levels(book.get("asks") if buy else book.get("bids"))
    lim = mid * (1 + bps / 1e4) if buy else mid * (1 - bps / 1e4)
    tot = 0.0
    for px, sz in sorted(ladder, key=lambda x: x[0] if buy else -x[0]):
        if (buy and px > lim) or (not buy and px < lim):
            break
        tot += px * sz
    return round(tot, 2)


def spread_bps(book: dict) -> float | None:
    sb = book.get("spread_bps")
    if sb is not None:
        try:
            return round(float(sb), 3)
        except (TypeError, ValueError):
            pass
    mid = _mid(book)
    bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
    if mid and bids and asks:
        return round((asks[0][0] - bids[0][0]) / mid * 1e4, 3)
    return None


def compact_book(book: dict, levels: int | None = None, *, max_levels: int = 100,
                 captured_ts_ms: int | None = None) -> dict:
    """Store every provider-returned level, bounded defensively with explicit provenance.

    ``levels`` remains an explicit compatibility seam for callers/tests that request a smaller
    snapshot. The default no longer imposes the old silent 15-level ceiling.
    """
    returned_bids = _levels(book.get("bids"))
    returned_asks = _levels(book.get("asks"))
    limit = min(int(levels), int(max_levels)) if levels is not None else int(max_levels)
    limit = max(0, limit)
    bids = returned_bids[:limit]
    asks = returned_asks[:limit]
    return {
        "capture_format_version": 2,
        "mid": _mid(book),
        "spread_bps": spread_bps(book),
        "bids": [[px, sz] for px, sz in bids],
        "asks": [[px, sz] for px, sz in asks],
        "returned_bid_levels": len(returned_bids),
        "returned_ask_levels": len(returned_asks),
        "stored_bid_levels": len(bids),
        "stored_ask_levels": len(asks),
        "returned_bid_notional": round(sum(px * sz for px, sz in returned_bids), 2),
        "returned_ask_notional": round(sum(px * sz for px, sz in returned_asks), 2),
        "stored_bid_notional": round(sum(px * sz for px, sz in bids), 2),
        "stored_ask_notional": round(sum(px * sz for px, sz in asks), 2),
        "client_truncated": len(bids) < len(returned_bids) or len(asks) < len(returned_asks),
        "depth_scope": "provider_returned_snapshot",
        "captured_ts_ms": int(captured_ts_ms) if captured_ts_ms is not None else None,
    }


def depth_scope(book: dict) -> str:
    declared = book.get("depth_scope")
    if declared:
        return str(declared)
    bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
    if len(bids) == 15 and len(asks) == 15:
        return "legacy_client_truncated_15"
    return "provider_returned_snapshot_unversioned"


def execution_at_size(book: dict, notional: float, *, max_rt_bps: float) -> dict:
    """Resolve a requested size without treating absent deeper levels as a rejection."""
    if _mid(book) is None or not _levels(book.get("bids")) or not _levels(book.get("asks")):
        return {"status": "unavailable", "feasible": None, "slippage_bps": None,
                "depth_scope": depth_scope(book)}
    slippage = round_trip_slippage_bps(book, notional)
    if slippage is None:
        return {"status": "censored_depth", "feasible": None, "slippage_bps": None,
                "depth_scope": depth_scope(book)}
    return {
        "status": "measured",
        "feasible": slippage <= float(max_rt_bps),
        "slippage_bps": slippage,
        "depth_scope": depth_scope(book),
    }


def max_size_within_slippage(book: dict, max_rt_bps: float, *, hi_usd: float = 5_000_000.0,
                             iters: int = 40) -> float:
    """Largest round-trip notional whose slippage stays <= max_rt_bps (the deployable capacity of
    this book at a given cost tolerance). Binary search; 0 if even a dust trade exceeds budget."""
    def ok(s: float) -> bool:
        rt = round_trip_slippage_bps(book, s)
        return rt is not None and rt <= max_rt_bps
    if not ok(1.0):
        return 0.0
    if ok(hi_usd):
        return hi_usd
    lo, hi = 1.0, hi_usd
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 0)


def capacity_with_provenance(book: dict, max_rt_bps: float, *,
                             hi_usd: float = 5_000_000.0) -> dict:
    """Confirmed capacity plus whether the returned snapshot censored its upper bound."""
    scope = depth_scope(book)
    if _mid(book) is None or not _levels(book.get("bids")) or not _levels(book.get("asks")):
        return {"status": "unavailable", "capacity_lower_bound_usd": 0.0,
                "capacity_upper_bound_usd": None, "depth_scope": scope}
    capacity = max_size_within_slippage(book, max_rt_bps, hi_usd=hi_usd)
    captured = min(
        sum(px * sz for px, sz in _levels(book.get("bids"))),
        sum(px * sz for px, sz in _levels(book.get("asks"))),
    )
    probe = min(captured + 1.0, max(float(capacity) + 1.0, float(capacity) * 1.001))
    probe_slippage = round_trip_slippage_bps(book, probe) if probe > capacity else None
    client_truncated = bool(book.get("client_truncated"))
    exhausted_before_cost_boundary = probe_slippage is None
    status = (
        "lower_bound"
        if scope == "legacy_client_truncated_15" or client_truncated
        or float(capacity) >= float(hi_usd) or exhausted_before_cost_boundary
        else "measured"
    )
    return {
        "status": status,
        "capacity_lower_bound_usd": float(capacity),
        "capacity_upper_bound_usd": float(capacity) if status == "measured" else None,
        "depth_scope": scope,
        "captured_round_trip_notional_usd": round(captured, 2),
    }


def book_summary(book: dict) -> dict:
    """Human/scorecard summary of a book's liquidity at a glance."""
    return {
        "mid": _mid(book),
        "spread_bps": spread_bps(book),
        "ask_depth_5bps": depth_within_bps(book, 5, buy=True),
        "ask_depth_25bps": depth_within_bps(book, 25, buy=True),
        "bid_depth_5bps": depth_within_bps(book, 5, buy=False),
        "bid_depth_25bps": depth_within_bps(book, 25, buy=False),
    }
