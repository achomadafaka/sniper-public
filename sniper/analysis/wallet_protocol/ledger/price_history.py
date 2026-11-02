"""Slice 5 — ledger price-history closures.

Two surfaces (spec §14, plan v4 §D3a):

1. ``price_history_fn(token_id: str, ts: int) -> float | None`` — exact
   ``PriceHistoryFn`` signature consumed by H6 / FH6. Mirrors
   ``sniper.analysis.wallet_protocol.price_history_adapter.build_price_history_fn``:
   query ``market_price_points`` from ``target_ts = ts`` through
   ``target_ts + max(fidelity_minutes * 60, 600)``, choose the first
   bar with ``bar_ts >= target_ts``, fall back to the last bar in that
   tight forward window only if none reach ``target_ts``, return
   ``None`` when no usable bar exists. Predecessor / nearest lookup is
   forbidden.
2. ``get_price_bars(token_id: str) -> PriceBarsResult`` — diagnostics.
   Returns the bar list inside ``window`` plus a ``coverage_status``
   enum (``covered`` / ``gap`` / ``not_captured``) read from
   ``market_price_coverage_ranges``.

Both surfaces share the same fidelity assumption and load price points
once at factory-construction time so callers don't need to keep the
DuckDB connection alive across analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import duckdb

from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow


PriceHistoryFn = Callable[[str, int], float | None]
CoverageStatus = Literal["covered", "gap", "not_captured"]


@dataclass(frozen=True)
class PriceBar:
    """One captured price bar.

    Attributes:
        ts: unix-epoch seconds at the bar's left edge.
        price: the captured mid price (probability space, [0, 1]).
        source_version: the price source's version string at capture time.
    """

    ts: int
    price: float
    source_version: str


@dataclass(frozen=True)
class PriceBarsResult:
    """Diagnostics-only return type for ``get_price_bars``.

    Not consumed by ``analyze_wallet``. Useful for materializer tests
    and manual probes that need to inspect the underlying market data.
    """

    token_id: str
    bars: list[PriceBar]
    coverage_status: CoverageStatus


def _forward_window_seconds(fidelity_minutes: int) -> int:
    """Return ``max(fidelity_minutes * 60, 600)`` — the live adapter rule.

    A 60-second-fidelity feed gives the floor of 600 s; coarser feeds
    use the natural step. Never less than the floor.
    """
    return max(int(fidelity_minutes) * 60, 600)


def _load_price_bars(
    conn: duckdb.DuckDBPyConnection,
    *,
    window: AnalysisWindow,
    fidelity_minutes: int,
    source_version: str | None,
) -> dict[str, list[PriceBar]]:
    """Load every captured bar inside the window plus the forward-window pad.

    The pad lets the spot-lookup closure resolve a target ts inside the
    window even when its forward bar is just outside ``window.end_ts``.
    """
    pad = _forward_window_seconds(fidelity_minutes)
    if source_version is None:
        rows = conn.execute(
            """
            SELECT token_id, ts, price, source_version
            FROM market_price_points
            WHERE point_status = 'captured'
              AND fidelity_minutes = ?
              AND ts BETWEEN ? AND ?
            ORDER BY token_id, ts
            """,
            [fidelity_minutes, window.start_ts, window.end_ts + pad],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT token_id, ts, price, source_version
            FROM market_price_points
            WHERE point_status = 'captured'
              AND fidelity_minutes = ?
              AND source_version = ?
              AND ts BETWEEN ? AND ?
            ORDER BY token_id, ts
            """,
            [
                fidelity_minutes,
                source_version,
                window.start_ts,
                window.end_ts + pad,
            ],
        ).fetchall()

    by_token: dict[str, list[PriceBar]] = {}
    for token_id, ts, price, src_version in rows:
        if token_id is None or ts is None or price is None:
            continue
        by_token.setdefault(str(token_id), []).append(
            PriceBar(
                ts=int(ts),
                price=float(price),
                source_version=str(src_version),
            )
        )
    return by_token


def _load_coverage_statuses(
    conn: duckdb.DuckDBPyConnection,
    *,
    window: AnalysisWindow,
    fidelity_minutes: int,
    source_version: str | None,
) -> dict[str, CoverageStatus]:
    """Eagerly snapshot coverage status per token overlapping ``window``.

    Loaded once at factory time so ``get_price_bars`` does not need the
    DuckDB connection to stay open. Tokens not present in this dict are
    treated as ``not_captured`` by callers.

    Mapping rules:

    * ``covered``: at least one ``fully_captured`` range overlaps the window.
    * ``gap``: ranges exist that overlap, but none are ``fully_captured``.
    * ``not_captured``: no overlapping range row exists (key absent).
    """
    if source_version is None:
        rows = conn.execute(
            """
            SELECT token_id, coverage_status
            FROM market_price_coverage_ranges
            WHERE fidelity_minutes = ?
              AND range_start_ts <= ?
              AND range_end_ts >= ?
            """,
            [fidelity_minutes, window.end_ts, window.start_ts],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT token_id, coverage_status
            FROM market_price_coverage_ranges
            WHERE fidelity_minutes = ?
              AND source_version = ?
              AND range_start_ts <= ?
              AND range_end_ts >= ?
            """,
            [
                fidelity_minutes,
                source_version,
                window.end_ts,
                window.start_ts,
            ],
        ).fetchall()
    by_token: dict[str, set[str]] = {}
    for token_id, status in rows:
        if token_id is None:
            continue
        by_token.setdefault(str(token_id), set()).add(str(status))
    out: dict[str, CoverageStatus] = {}
    for token_id, statuses in by_token.items():
        if "fully_captured" in statuses:
            out[token_id] = "covered"
        else:
            out[token_id] = "gap"
    return out


def make_ledger_price_history_fn(
    conn: duckdb.DuckDBPyConnection,
    *,
    window: AnalysisWindow,
    fidelity_minutes: int,
    source_version: str | None = None,
) -> tuple[PriceHistoryFn, Callable[[str], PriceBarsResult]]:
    """Build the two price surfaces for a materialized wallet view.

    Args:
        conn: open DuckDB connection on ``market_data.duckdb``.
        window: the analysis window. Bars outside ``[start_ts, end_ts]``
            are not surfaced through ``get_price_bars`` (the spot
            closure may still touch the forward-window pad past
            ``end_ts``).
        fidelity_minutes: the bar fidelity to honor.
        source_version: optional pin. When provided, only bars with the
            matching ``source_version`` are loaded; coverage rows are
            filtered the same way. Pass ``None`` to allow any version
            captured for the token.

    Returns:
        ``(price_history_fn, get_price_bars)`` where the closures share
        the same in-memory bar dictionary built from a single SQL pass.
    """
    by_token = _load_price_bars(
        conn,
        window=window,
        fidelity_minutes=fidelity_minutes,
        source_version=source_version,
    )
    coverage_by_token = _load_coverage_statuses(
        conn,
        window=window,
        fidelity_minutes=fidelity_minutes,
        source_version=source_version,
    )
    forward_window = _forward_window_seconds(fidelity_minutes)

    def _price_history_fn(token_id: str, ts: int) -> float | None:
        if not token_id or ts is None:
            return None
        bars = by_token.get(str(token_id))
        if not bars:
            return None
        target = int(ts)
        upper = target + forward_window
        in_window = [bar for bar in bars if target <= bar.ts <= upper]
        if not in_window:
            return None
        # First bar with t >= target.
        for bar in in_window:
            if bar.ts >= target:
                return bar.price
        # Defensive: every bar in `in_window` already satisfies
        # bar.ts >= target by construction. Last-bar fallback retained
        # for parity with the live adapter contract.
        return in_window[-1].price

    def _get_price_bars(token_id: str) -> PriceBarsResult:
        bars = by_token.get(str(token_id), [])
        in_window_bars = [
            bar for bar in bars if window.start_ts <= bar.ts <= window.end_ts
        ]
        coverage: CoverageStatus = coverage_by_token.get(
            str(token_id), "not_captured"
        )
        return PriceBarsResult(
            token_id=token_id,
            bars=in_window_bars,
            coverage_status=coverage,
        )

    return _price_history_fn, _get_price_bars


__all__ = [
    "CoverageStatus",
    "PriceBar",
    "PriceBarsResult",
    "PriceHistoryFn",
    "make_ledger_price_history_fn",
]
