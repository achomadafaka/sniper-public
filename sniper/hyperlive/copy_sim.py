"""Full-book MIRROR copy logic — 'do exactly what the target wallet does', on paper.

Distinct from follow_sim (which measures the lagged entry counterfactual): this maintains a paper
book that tracks a chosen trader's CURRENT positions in real time — open a mirror when they hold
something we don't, close it when they exit, hold when matched. Enters at the mark when WE detect
(lagged), so the mirror PnL is realistic-follower PnL, not the target's own. Pure functions; the
runner persists. Paper only; not financial advice, no real orders."""
from __future__ import annotations

from .follow_sim import follow_net_bps, _f

FEE_BPS = 9.0


def _coin_key(coin: object) -> str:
    return str(coin or "").casefold()


def _coin_index(mapping: dict) -> tuple[dict[str, tuple[str, object]], set[str]]:
    index: dict[str, tuple[str, object]] = {}
    ambiguous: set[str] = set()
    for original, value in mapping.items():
        key = _coin_key(original)
        if not key:
            continue
        if key in index:
            index.pop(key, None)
            ambiguous.add(key)
        elif key not in ambiguous:
            index[key] = (str(original), value)
    return index, ambiguous


def _gross_bps(side: str, entry: float, mark: float) -> float:
    g = (mark - entry) / entry * 1e4
    return round(-g if side == "short" else g, 2)


def mirror_actions(target_positions: dict, our_book: dict, marks: dict, fee_bps: float = FEE_BPS) -> dict:
    """Reconcile our paper book to the target's current positions.
      open  — target holds a coin/side we don't (skipped if no mark available)
      close — we hold a coin/side the target no longer holds (net-of-fee pnl_bps)
      hold  — matched coin+side
    A side flip = close the old side + open the new."""
    target_index, target_ambiguous = _coin_index(target_positions)
    book_index, book_ambiguous = _coin_index(our_book)
    mark_index, mark_ambiguous = _coin_index(marks)
    unsafe = target_ambiguous | book_ambiguous | mark_ambiguous
    opens, closes, holds = [], [], []
    for coin, t in target_positions.items():
        key = _coin_key(coin)
        if key in unsafe:
            continue
        book_pair = book_index.get(key)
        b = book_pair[1] if book_pair is not None else None
        if b is not None and b["side"] == t["side"]:
            holds.append({"coin": coin, "side": t["side"]})
            continue
        mark_pair = mark_index.get(key)
        mark = _f(mark_pair[1] if mark_pair is not None else None)
        if mark <= 0:
            continue                                   # can't open without a price
        opens.append({"coin": coin, "side": t["side"], "entry_mark": mark})
    for coin, b in our_book.items():
        key = _coin_key(coin)
        if key in unsafe:
            continue
        target_pair = target_index.get(key)
        t = target_pair[1] if target_pair is not None else None
        if t is not None and t["side"] == b["side"]:
            continue                                   # still held, same side -> hold (above)
        mark_pair = mark_index.get(key)
        mark = _f(mark_pair[1] if mark_pair is not None else None)
        net = follow_net_bps(b["side"], b.get("entry_mark"), mark, fee_bps=fee_bps)
        if net is None:
            continue
        closes.append({"coin": coin, "side": b["side"], "entry_mark": b.get("entry_mark"),
                       "exit_mark": mark, "pnl_bps": net})
    return {"open": opens, "close": closes, "hold": holds}


def book_unrealized_bps(our_book: dict, marks: dict) -> dict:
    """Mark-to-market of the open mirror book (gross bps, no fee until close)."""
    per_coin, total = {}, 0.0
    mark_index, ambiguous = _coin_index(marks)
    for coin, b in our_book.items():
        key = _coin_key(coin)
        mark_pair = None if key in ambiguous else mark_index.get(key)
        mark = _f(mark_pair[1] if mark_pair is not None else None)
        if mark <= 0 or _f(b.get("entry_mark")) <= 0:
            continue
        g = _gross_bps(b["side"], _f(b["entry_mark"]), mark)
        per_coin[coin] = g
        total += g
    return {"per_coin": per_coin, "total_bps": round(total, 2), "n": len(per_coin)}
