"""Replay MCP tools for Sniper — deterministic forward simulation."""
from __future__ import annotations

from db import get_readonly_connection
from sniper.analysis.replay_engine import ReplayConfig, run_replay
from sniper.mcp_tools.schemas import make_envelope


def replay_impl(
    *,
    strategy_type: str = "insider_copy_v1",
    initial_bankroll: float = 1000.0,
    base_size_usdc: float = 25.0,
    min_insider_score: float = 55.0,
    max_speculator_score: float = 70.0,
    max_entry_price: float = 0.70,
    max_concurrent_positions: int = 20,
    max_exposure_per_market_usdc: float = 200.0,
    max_total_exposure_usdc: float = 1000.0,
    circuit_breaker_max_consecutive_losses: int | None = None,
    circuit_breaker_max_drawdown_pct: float | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    conn=None,
) -> dict:
    """Internal implementation for run_replay MCP tool."""
    config = ReplayConfig(
        strategy_type=strategy_type,
        initial_bankroll=initial_bankroll,
        base_size_usdc=base_size_usdc,
        min_insider_score=min_insider_score,
        max_speculator_score=max_speculator_score,
        max_entry_price=max_entry_price,
        max_concurrent_positions=max_concurrent_positions,
        max_exposure_per_market_usdc=max_exposure_per_market_usdc,
        max_total_exposure_usdc=max_total_exposure_usdc,
        circuit_breaker_max_consecutive_losses=circuit_breaker_max_consecutive_losses,
        circuit_breaker_max_drawdown_pct=circuit_breaker_max_drawdown_pct,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    close_conn = False
    if conn is None:
        conn = get_readonly_connection()
        close_conn = True

    try:
        result = run_replay(conn, config)
    except ValueError as exc:
        return make_envelope(
            {"error": str(exc)},
            data_source="signal_history_curated",
            bias_warning="Replay uses entry-time scores from signal_history first snapshots. "
                         "Resolution prices are retrospective — this is NOT a live forward test.",
        )
    except Exception:
        return make_envelope(
            {"error": "internal error"},
            data_source="signal_history_curated",
            bias_warning="Replay uses entry-time scores from signal_history first snapshots. "
                         "Resolution prices are retrospective — this is NOT a live forward test.",
        )
    finally:
        if close_conn:
            conn.close()

    data = {
        "summary": result.summary,
        "config": result.config.to_dict(),
        "trade_count": result.summary.get("total_trades", 0),
        "equity_curve_points": len(result.equity_curve),
        "skip_reason_counts": _count_skip_reasons(result.skipped),
    }

    return make_envelope(
        data,
        data_source="signal_history_curated",
        bias_warning="Replay uses entry-time scores from signal_history first snapshots. "
                     "Resolution prices are retrospective — this is NOT a live forward test.",
    )


def _count_skip_reasons(skipped: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in skipped:
        reason = s.get("skip_reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def register_replay_tools(mcp_server):
    """Register replay tools with the MCP server."""

    @mcp_server.tool()
    def run_replay_simulation(
        strategy_type: str = "insider_copy_v1",
        initial_bankroll: float = 1000.0,
        base_size_usdc: float = 25.0,
        min_insider_score: float = 55.0,
        max_speculator_score: float = 70.0,
        max_entry_price: float = 0.70,
        max_concurrent_positions: int = 20,
        max_exposure_per_market_usdc: float = 200.0,
        max_total_exposure_usdc: float = 1000.0,
        circuit_breaker_max_consecutive_losses: int = -1,
        circuit_breaker_max_drawdown_pct: float = -1.0,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> dict:
        """Run a deterministic forward replay of the insider-copy strategy against
        curated historical signal data.  Simulates bankroll, sizing, exposure caps,
        and circuit breakers.  Returns trade count, win rate, P&L, max drawdown,
        and skip reason breakdown.  NOT a live forward test — uses retrospective
        resolution prices."""
        return replay_impl(
            strategy_type=strategy_type,
            initial_bankroll=initial_bankroll,
            base_size_usdc=base_size_usdc,
            min_insider_score=min_insider_score,
            max_speculator_score=max_speculator_score,
            max_entry_price=max_entry_price,
            max_concurrent_positions=max_concurrent_positions,
            max_exposure_per_market_usdc=max_exposure_per_market_usdc,
            max_total_exposure_usdc=max_total_exposure_usdc,
            circuit_breaker_max_consecutive_losses=(
                circuit_breaker_max_consecutive_losses
                if circuit_breaker_max_consecutive_losses >= 0 else None
            ),
            circuit_breaker_max_drawdown_pct=(
                circuit_breaker_max_drawdown_pct
                if circuit_breaker_max_drawdown_pct >= 0 else None
            ),
            start_ts=start_ts if start_ts > 0 else None,
            end_ts=end_ts if end_ts > 0 else None,
        )
