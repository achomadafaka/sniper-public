"""Cold-path maker regime evaluation helpers.

Pure compute only: no DB, no network, no runner side effects.
"""
from __future__ import annotations

from typing import Any, Iterable


def fill_cash_delta(fill: Any) -> float:
    price = _float(fill, "price")
    size = _float(fill, "size")
    return round((-price * size) if _side(fill) == "bid" else (price * size), 8)


def fill_edge_value(fill: Any) -> float | None:
    fair = _maybe_float(fill, "fair")
    if fair is None:
        return None
    price = _float(fill, "price")
    size = _float(fill, "size")
    edge = (fair - price) if _side(fill) == "bid" else (price - fair)
    return round(edge * size, 8)


def future_unwind_value(fill: Any, future_book: Any) -> float | None:
    price = _float(fill, "price")
    size = _float(fill, "size")
    side = _side(fill)
    if side == "bid":
        unwind = _maybe_float(future_book, "bid")
        return None if unwind is None else round((unwind - price) * size, 8)
    unwind = _maybe_float(future_book, "ask")
    return None if unwind is None else round((price - unwind) * size, 8)


def adverse_selection_bps(fill: Any, future_book: Any) -> float | None:
    price = _float(fill, "price")
    side = _side(fill)
    if side == "bid":
        unwind = _maybe_float(future_book, "bid")
        return None if unwind is None else round((unwind - price) * 10_000, 8)
    unwind = _maybe_float(future_book, "ask")
    return None if unwind is None else round((price - unwind) * 10_000, 8)


def mfe_mae_bps(fill: Any, future_books: Iterable[Any]) -> tuple[float | None, float | None]:
    values = [
        v for book in future_books
        if (v := adverse_selection_bps(fill, book)) is not None
    ]
    if not values:
        return None, None
    return max(values), min(values)


def _side(row: Any) -> str:
    return str(_get(row, "side") or "").lower()


def _float(row: Any, key: str) -> float:
    value = _maybe_float(row, key)
    if value is None:
        raise ValueError(f"missing numeric {key}")
    return value


def _maybe_float(row: Any, key: str) -> float | None:
    value = _get(row, key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)
