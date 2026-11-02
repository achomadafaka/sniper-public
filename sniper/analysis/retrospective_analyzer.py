"""Retrospective Signal-Outcome Analyzer

This is NOT a backtester.  It retrospectively asks "of the signals we captured,
which gate configurations would have produced the best outcomes?" using today's
data with known caveats about look-ahead bias.

Three data sources, each with different trust levels:

1. **paper_trades** (cleanest) — ``signal_insider_score`` and
   ``signal_speculator_score`` are frozen at trade entry time.  P&L is
   computed by the paper trader itself.  Small dataset (~43 closed trades).

2. **position_signals** (⚠ caveat) — joined with ``market_outcomes`` for
   resolution.  Scores reflect the LATEST analysis run, NOT values at signal
   creation time.  ~152 resolved signals.

3. **fwa** (followed-wallet activity, ⚠ caveat) — immutable trade records
   joined with ``market_outcomes``.  Scores come from the wallet's CURRENT
   ``user_analysis`` row.  Very sparse overlap (~14 resolved trades).

All analysis is read-only and never writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from db import (
    analyzer_fwa_trades,
    analyzer_paper_trades,
    analyzer_signal_outcomes,
    get_connection,
)

# ---------------------------------------------------------------------------
# Configuration & result dataclasses
# ---------------------------------------------------------------------------

VALID_DATA_SOURCES = {"paper_trades", "position_signals", "fwa"}

BIAS_WARNINGS = {
    "paper_trades": None,
    "position_signals": (
        "Scores reflect the wallet's LATEST analysis, not scores at signal "
        "creation time.  Results may overstate or understate the edge that "
        "existed when the signal was originally generated."
    ),
    "fwa": (
        "Scores come from the wallet's current user_analysis row.  Only a "
        "tiny fraction of followed-wallet trades have resolved market "
        "outcomes, so results are based on a very small sample."
    ),
}


@dataclass
class AnalysisConfig:
    """Configurable gate filters for the retrospective analyzer."""

    data_source: str = "paper_trades"
    min_insider_score: float | None = None
    max_speculator_score: float | None = None
    max_entry_price: float | None = None
    min_qualifying_roi: float | None = None
    skip_classifications: set = field(default_factory=lambda: frozenset())
    base_size_usdc: float = 25.0
    start_date: str | None = None
    end_date: str | None = None
    wallet_filter: set | None = None


@dataclass
class AnalysisResult:
    """Aggregate results of a retrospective analysis run."""

    trades: List[Dict[str, Any]]
    total_pnl: float
    win_rate: float | None
    trade_count: int
    avg_roi: float | None
    max_drawdown: float
    equity_curve: List[Dict[str, Any]]
    data_source: str
    bias_warning: str | None
    gate_config: Dict[str, Any]
    pre_filter_count: int


# ---------------------------------------------------------------------------
# Core analysis logic
# ---------------------------------------------------------------------------


def _apply_gates_paper(
    trades: List[Dict[str, Any]],
    config: AnalysisConfig,
) -> List[Dict[str, Any]]:
    """Filter paper trades using configurable gates on entry-time scores."""
    out = []
    for t in trades:
        insider = t.get("signal_insider_score")
        spec = t.get("signal_speculator_score")
        ep = t.get("entry_price") or 0.0

        if config.min_insider_score is not None and (insider is None or insider < config.min_insider_score):
            continue
        if config.max_speculator_score is not None and (spec is not None and spec >= config.max_speculator_score):
            continue
        if config.max_entry_price is not None and ep > config.max_entry_price:
            continue
        if config.wallet_filter and t.get("wallet", "").lower() not in config.wallet_filter:
            continue
        out.append(t)
    return out


def _apply_gates_signals(
    trades: List[Dict[str, Any]],
    config: AnalysisConfig,
) -> List[Dict[str, Any]]:
    """Filter position signals using configurable gates on CURRENT scores."""
    out = []
    for t in trades:
        insider = t.get("position_insider_score")
        spec = t.get("position_speculator_score")
        ep = t.get("entry_price") or 0.0
        classification = t.get("position_classification") or ""

        if config.min_insider_score is not None and (insider is None or insider < config.min_insider_score):
            continue
        if config.max_speculator_score is not None and (spec is not None and spec >= config.max_speculator_score):
            continue
        if config.max_entry_price is not None and ep > config.max_entry_price:
            continue
        if config.skip_classifications and classification in config.skip_classifications:
            continue
        if config.wallet_filter and t.get("wallet", "").lower() not in config.wallet_filter:
            continue
        out.append(t)
    return out


def _apply_gates_fwa(
    trades: List[Dict[str, Any]],
    config: AnalysisConfig,
) -> List[Dict[str, Any]]:
    """Filter FWA trades using configurable gates on CURRENT wallet scores."""
    out = []
    for t in trades:
        insider = t.get("insider_score")
        spec = t.get("speculator_score")
        ep = t.get("entry_price") or 0.0
        classification = t.get("classification") or ""

        if config.min_insider_score is not None and (insider is None or insider < config.min_insider_score):
            continue
        if config.max_speculator_score is not None and (spec is not None and spec >= config.max_speculator_score):
            continue
        if config.max_entry_price is not None and ep > config.max_entry_price:
            continue
        if config.skip_classifications and classification in config.skip_classifications:
            continue
        if config.wallet_filter and t.get("wallet", "").lower() not in config.wallet_filter:
            continue
        out.append(t)
    return out


def _compute_pnl_paper(trade: Dict[str, Any], base_size: float) -> Dict[str, Any]:
    """Compute P&L for a paper trade using its own pnl_usdc/roi_percent."""
    pnl = float(trade.get("pnl_usdc") or 0.0)
    roi = float(trade.get("roi_percent") or 0.0)
    status = trade.get("status") or ""
    won = status == "resolution_win"
    return {
        "wallet": trade.get("wallet", ""),
        "condition_id": trade.get("condition_id", ""),
        "title": trade.get("market_title", ""),
        "entry_price": float(trade.get("entry_price") or 0),
        "exit_price": float(trade.get("exit_price") or 0),
        "size_usdc": float(trade.get("size_usdc") or base_size),
        "pnl": pnl,
        "roi_pct": roi,
        "won": won,
        "status": status,
        "insider_score": trade.get("signal_insider_score"),
        "speculator_score": trade.get("signal_speculator_score"),
        "opened_at": trade.get("opened_at"),
        "closed_at": trade.get("closed_at"),
    }


def _compute_pnl_resolved(
    trade: Dict[str, Any],
    base_size: float,
    insider_key: str = "position_insider_score",
    spec_key: str = "position_speculator_score",
) -> Dict[str, Any]:
    """Compute P&L from entry_price vs resolved_price for signal/FWA trades."""
    entry = float(trade.get("entry_price") or 0)
    resolved = float(trade.get("resolved_price") or 0)
    if entry <= 0:
        roi = 0.0
    else:
        roi = (resolved - entry) / entry * 100.0
    pnl = base_size * roi / 100.0
    won = resolved > entry
    return {
        "wallet": trade.get("wallet", ""),
        "condition_id": trade.get("condition_id", ""),
        "title": trade.get("title", ""),
        "entry_price": entry,
        "exit_price": resolved,
        "size_usdc": base_size,
        "pnl": round(pnl, 2),
        "roi_pct": round(roi, 2),
        "won": won,
        "status": "resolved",
        "insider_score": trade.get(insider_key),
        "speculator_score": trade.get(spec_key),
        "resolved_at": trade.get("resolved_at"),
    }


def _build_equity_curve(evaluated: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build cumulative P&L equity curve from evaluated trades."""
    cumulative = 0.0
    curve = []
    for i, t in enumerate(evaluated):
        cumulative += t["pnl"]
        curve.append({
            "trade_num": i + 1,
            "cumulative_pnl": round(cumulative, 2),
            "trade_pnl": round(t["pnl"], 2),
            "won": t["won"],
        })
    return curve


def _max_drawdown(equity_curve: List[Dict[str, Any]]) -> float:
    """Compute max drawdown from equity curve."""
    if not equity_curve:
        return 0.0
    peak = 0.0
    max_dd = 0.0
    for point in equity_curve:
        val = point["cumulative_pnl"]
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def run_analysis(config: AnalysisConfig | None = None, conn=None) -> AnalysisResult:
    """Run a retrospective analysis with the given configuration.

    Parameters
    ----------
    config : AnalysisConfig, optional
        Gate filters and data source selection.  Defaults to paper_trades
        with no filters.
    conn : sqlite3.Connection, optional
        Existing DB connection.  A new one is opened if not provided.

    Returns
    -------
    AnalysisResult
        Aggregate metrics, per-trade detail, equity curve, and bias warnings.
    """
    if config is None:
        config = AnalysisConfig()
    if config.data_source not in VALID_DATA_SOURCES:
        raise ValueError(f"Invalid data_source: {config.data_source!r}. "
                         f"Valid: {VALID_DATA_SOURCES}")

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        return _run(config, conn)
    finally:
        if close_conn:
            conn.close()


def _run(config: AnalysisConfig, conn) -> AnalysisResult:
    """Internal: fetch data, apply gates, compute metrics."""

    # Fetch raw data
    if config.data_source == "paper_trades":
        raw = analyzer_paper_trades(conn)
        filtered = _apply_gates_paper(raw, config)
        evaluated = [_compute_pnl_paper(t, config.base_size_usdc) for t in filtered]
    elif config.data_source == "position_signals":
        raw = analyzer_signal_outcomes(conn)
        filtered = _apply_gates_signals(raw, config)
        evaluated = [
            _compute_pnl_resolved(t, config.base_size_usdc,
                                  insider_key="position_insider_score",
                                  spec_key="position_speculator_score")
            for t in filtered
        ]
    else:  # fwa
        raw = analyzer_fwa_trades(conn)
        filtered = _apply_gates_fwa(raw, config)
        evaluated = [
            _compute_pnl_resolved(t, config.base_size_usdc,
                                  insider_key="insider_score",
                                  spec_key="speculator_score")
            for t in filtered
        ]

    # Build metrics
    trade_count = len(evaluated)
    total_pnl = round(sum(t["pnl"] for t in evaluated), 2) if evaluated else 0.0
    wins = sum(1 for t in evaluated if t["won"])
    win_rate = round(wins / trade_count * 100, 1) if trade_count > 0 else None
    avg_roi = (
        round(sum(t["roi_pct"] for t in evaluated) / trade_count, 2)
        if trade_count > 0
        else None
    )
    equity_curve = _build_equity_curve(evaluated)
    max_dd = _max_drawdown(equity_curve)

    gate_config = {
        "data_source": config.data_source,
        "min_insider_score": config.min_insider_score,
        "max_speculator_score": config.max_speculator_score,
        "max_entry_price": config.max_entry_price,
        "min_qualifying_roi": config.min_qualifying_roi,
        "base_size_usdc": config.base_size_usdc,
    }

    return AnalysisResult(
        trades=evaluated,
        total_pnl=total_pnl,
        win_rate=win_rate,
        trade_count=trade_count,
        avg_roi=avg_roi,
        max_drawdown=max_dd,
        equity_curve=equity_curve,
        data_source=config.data_source,
        bias_warning=BIAS_WARNINGS.get(config.data_source),
        gate_config=gate_config,
        pre_filter_count=len(raw),
    )


def sweep_insider_threshold(
    data_source: str = "paper_trades",
    thresholds: List[float] | None = None,
    base_config: AnalysisConfig | None = None,
    conn=None,
) -> List[Dict[str, Any]]:
    """Sweep insider_score thresholds and return metrics for each.

    Useful for answering "what insider threshold maximizes risk-adjusted return?"
    """
    if thresholds is None:
        thresholds = [0, 40, 50, 55, 60, 65, 70, 75, 80, 85, 90]
    if base_config is None:
        base_config = AnalysisConfig(data_source=data_source)

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        results = []
        for threshold in thresholds:
            cfg = AnalysisConfig(
                data_source=data_source,
                min_insider_score=threshold,
                max_speculator_score=base_config.max_speculator_score,
                max_entry_price=base_config.max_entry_price,
                min_qualifying_roi=base_config.min_qualifying_roi,
                skip_classifications=base_config.skip_classifications,
                base_size_usdc=base_config.base_size_usdc,
                wallet_filter=base_config.wallet_filter,
            )
            result = _run(cfg, conn)
            results.append({
                "threshold": threshold,
                "trade_count": result.trade_count,
                "win_rate": result.win_rate,
                "total_pnl": result.total_pnl,
                "avg_roi": result.avg_roi,
                "max_drawdown": result.max_drawdown,
            })
        return results
    finally:
        if close_conn:
            conn.close()


def sweep_2d(
    data_source: str = "paper_trades",
    insider_thresholds: List[float] | None = None,
    price_thresholds: List[float] | None = None,
    base_config: AnalysisConfig | None = None,
    conn=None,
) -> Dict[str, Any]:
    """2D parameter sweep: insider_score × max_entry_price.

    Returns a grid of metrics for every (insider, price) combination,
    suitable for rendering as a heatmap.

    Returns
    -------
    dict with keys:
        insider_axis: list of insider thresholds (rows)
        price_axis: list of price thresholds (columns)
        grid: list of dicts, one per cell, with insider, price, trade_count,
              win_rate, total_pnl, avg_roi, max_drawdown
        best_cell: the cell with highest total_pnl (min 3 trades)
        data_source: str
        bias_warning: str | None
    """
    if insider_thresholds is None:
        insider_thresholds = [0, 40, 50, 55, 60, 65, 70, 75, 80, 85, 90]
    if price_thresholds is None:
        price_thresholds = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
    if base_config is None:
        base_config = AnalysisConfig(data_source=data_source)

    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        grid: List[Dict[str, Any]] = []
        best_cell: Dict[str, Any] | None = None

        for insider in insider_thresholds:
            for price in price_thresholds:
                cfg = AnalysisConfig(
                    data_source=data_source,
                    min_insider_score=insider if insider > 0 else None,
                    max_speculator_score=base_config.max_speculator_score,
                    max_entry_price=price if price < 1.0 else None,
                    min_qualifying_roi=base_config.min_qualifying_roi,
                    skip_classifications=base_config.skip_classifications,
                    base_size_usdc=base_config.base_size_usdc,
                    wallet_filter=base_config.wallet_filter,
                )
                result = _run(cfg, conn)
                cell = {
                    "insider": insider,
                    "price": price,
                    "trade_count": result.trade_count,
                    "win_rate": result.win_rate,
                    "total_pnl": result.total_pnl,
                    "avg_roi": result.avg_roi,
                    "max_drawdown": result.max_drawdown,
                }
                grid.append(cell)

                if (
                    result.trade_count >= 3
                    and (best_cell is None or result.total_pnl > best_cell["total_pnl"])
                ):
                    best_cell = cell

        # Detect non-binding price cap on best cell
        price_cap_note = None
        if best_cell is not None:
            lookup = {(c["insider"], c["price"]): c for c in grid}
            best_ins = best_cell["insider"]
            best_pr = best_cell["price"]
            best_count = best_cell["trade_count"]
            higher_prices = [p for p in price_thresholds if p > best_pr]
            if higher_prices and all(
                lookup.get((best_ins, hp), {}).get("trade_count", 0) == best_count
                for hp in higher_prices
            ):
                price_cap_note = (
                    f"Price cap is non-binding above \u2264{best_pr:.2f} \u2014 "
                    f"no qualifying trades entered above {int(best_pr * 100)}\u00a2 "
                    f"at this score level."
                )

        return {
            "insider_axis": insider_thresholds,
            "price_axis": price_thresholds,
            "grid": grid,
            "best_cell": best_cell,
            "price_cap_note": price_cap_note,
            "data_source": data_source,
            "bias_warning": BIAS_WARNINGS.get(data_source),
        }
    finally:
        if close_conn:
            conn.close()
