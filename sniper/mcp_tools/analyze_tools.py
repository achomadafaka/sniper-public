"""Analyze MCP tools for Sniper — CPU-intensive, no mutations."""
from __future__ import annotations

from db import get_readonly_connection
from sniper.analysis.retrospective_analyzer import AnalysisConfig, run_analysis, sweep_2d
from sniper.mcp_tools.schemas import make_envelope


def _paper_trades_freshness(conn=None) -> float | None:
    """Get most recent paper_trades closed_at as epoch timestamp."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection()
        close_conn = True
    try:
        row = conn.execute(
            "SELECT MAX(closed_at) as mca FROM paper_trades WHERE status != 'open'"
        ).fetchone()
        if row and row["mca"]:
            from datetime import datetime, timezone
            try:
                dt_obj = datetime.strptime(row["mca"], "%Y-%m-%d %H:%M:%S")
                return dt_obj.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
    except Exception:
        pass
    finally:
        if close_conn:
            conn.close()
    return None


def _bias_warning(data_source: str) -> str | None:
    if data_source == "position_signals":
        return "position_signals source uses current prices (look-ahead bias)"
    if data_source == "fwa":
        return "followed_wallet_activity has sparse coverage and outcome join caveats"
    return None


def historical_outcomes(
    *,
    data_source: str = "paper_trades",
    min_insider_score: float | None = None,
    max_entry_price: float | None = None,
    min_sample: int = 10,
    conn=None,
) -> dict:
    """Internal implementation for analyze_historical_outcomes tool."""
    config = AnalysisConfig(
        data_source=data_source,
        min_insider_score=min_insider_score,
        max_entry_price=max_entry_price,
    )
    result = run_analysis(config, conn=conn)

    data = {
        "trade_count": result.trade_count,
        "win_rate": result.win_rate,
        "avg_roi": result.avg_roi,
        "total_pnl": result.total_pnl,
        "max_drawdown": result.max_drawdown,
        "equity_curve_points": len(result.equity_curve),
        "data_source": result.data_source,
        "gate_config": result.gate_config,
        "pre_filter_count": result.pre_filter_count,
    }

    if result.trade_count < min_sample:
        data["sample_warning"] = (
            f"Only {result.trade_count} trades (min_sample={min_sample}). "
            "Results may not be statistically meaningful."
        )

    fresh_ts = _paper_trades_freshness(conn) if data_source == "paper_trades" else None
    return make_envelope(
        data,
        data_source=data_source,
        bias_warning=_bias_warning(data_source),
        last_write_ts=fresh_ts,
    )


def sweep_filters(
    *,
    data_source: str = "paper_trades",
    insider_range: list | None = None,
    price_range: list | None = None,
    conn=None,
) -> dict:
    """Internal implementation for sweep_signal_filters tool."""
    insider_thresholds = None
    if insider_range and len(insider_range) == 3:
        start, end, step = insider_range
        insider_thresholds = []
        val = start
        while val <= end:
            insider_thresholds.append(val)
            val += step

    price_thresholds = None
    if price_range and len(price_range) == 3:
        start, end, step = price_range
        price_thresholds = []
        val = start
        while val <= end + 0.001:
            price_thresholds.append(round(val, 2))
            val += step

    result = sweep_2d(
        data_source=data_source,
        insider_thresholds=insider_thresholds,
        price_thresholds=price_thresholds,
        conn=conn,
    )

    fresh_ts = _paper_trades_freshness(conn) if data_source == "paper_trades" else None
    return make_envelope(
        result,
        data_source=data_source,
        bias_warning=_bias_warning(data_source),
        last_write_ts=fresh_ts,
    )


def register_analyze_tools(mcp_server):
    """Register analyze tools with the MCP server."""

    @mcp_server.tool()
    def analyze_historical_outcomes(
        data_source: str = "paper_trades",
        min_insider_score: float = 55,
        max_entry_price: float = 0.70,
        min_sample: int = 10,
    ) -> dict:
        """Analyze historical trade outcomes. NOT a true backtest — uses retrospective data with known caveats. Sources: 'paper_trades' (cleanest), 'position_signals' (look-ahead bias), 'fwa' (sparse)."""
        return historical_outcomes(
            data_source=data_source,
            min_insider_score=min_insider_score,
            max_entry_price=max_entry_price,
            min_sample=min_sample,
        )

    @mcp_server.tool()
    def sweep_signal_filters(
        data_source: str = "paper_trades",
        insider_range: list = [0, 90, 10],
        price_range: list = [0.30, 1.0, 0.10],
    ) -> dict:
        """2D sweep of insider_score x max_entry_price thresholds. Returns grid of trade counts, win rates, and PnL for each combination."""
        return sweep_filters(
            data_source=data_source,
            insider_range=insider_range,
            price_range=price_range,
        )
