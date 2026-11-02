"""Pure assembly of the copy-fidelity capture fields the runner persists — no I/O.

The runner does the fetching (L2 book, funding history); these functions turn that raw data plus
the target's clearinghouse position into the exact columns hl_copy_book stores. Kept pure so the
fidelity math is unit-tested without hitting Hyperliquid."""
from __future__ import annotations

import json

from .fills import compact_book, spread_bps


def lag_bps(side: str, our_entry_mark: float | None, target_entry_px: float | None) -> float | None:
    """Detection-lag tax: how much worse OUR fill (at the mark when we detected the entry) is than
    the TARGET's own fill. Positive = we entered worse (the cost of polling lag). Signed by side."""
    if not target_entry_px or target_entry_px <= 0 or our_entry_mark is None:
        return None
    sign = 1.0 if side == "long" else -1.0
    return round(sign * (our_entry_mark - target_entry_px) / target_entry_px * 1e4, 3)


def entry_capture(side: str, our_entry_mark: float, target_pos: dict | None,
                  book: dict | None, *, captured_ts_ms: int | None = None) -> dict:
    """Capture dict for open_copy(**...). Any missing input degrades to None for that field only."""
    tp = target_pos or {}
    tpx = tp.get("entry_px") or None
    szi = tp.get("szi")
    notional = abs(szi) * tpx if (szi and tpx) else None
    return {
        "target_entry_px": tpx,
        "target_szi": szi,
        "target_notional": round(notional, 2) if notional else None,
        "entry_spread_bps": spread_bps(book) if book else None,
        "entry_depth_json": json.dumps(compact_book(
            book, captured_ts_ms=captured_ts_ms
        )) if book else None,
        "lag_bps": lag_bps(side, our_entry_mark, tpx),
    }


def exit_capture(book: dict | None, *, captured_ts_ms: int | None = None) -> dict:
    return {
        "exit_spread_bps": spread_bps(book) if book else None,
        "exit_depth_json": json.dumps(compact_book(
            book, captured_ts_ms=captured_ts_ms
        )) if book else None,
    }


def funding_cost_bps(funding_rows: list[dict] | None, side: str,
                     start_ms: int, end_ms: int) -> float | None:
    """Funding paid over the hold, in bps, as a COST to us. Longs pay positive funding, shorts
    receive it. funding_rows: [{time, rate}] hourly. None if no rows in the window."""
    rows = [r for r in (funding_rows or []) if start_ms < r.get("time", -1) <= end_ms]
    if not rows:
        return None
    s = sum(r.get("rate", 0.0) for r in rows)
    cost = s if side == "long" else -s
    return round(cost * 1e4, 3)


def is_holdout(detect_ts_ms: int, freeze_ts_ms: int | None) -> int:
    """1 if this open happened at/after the forward-holdout freeze (out-of-sample)."""
    return int(freeze_ts_ms is not None and detect_ts_ms >= freeze_ts_ms)
