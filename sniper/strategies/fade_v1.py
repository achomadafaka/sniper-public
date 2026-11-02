"""Fade v1 strategy plugin: bet OPPOSITE to a tracked wallet.

Targets wallets whose positions are reliably wrong — e.g. arb_fund wallets
that consistently lose, or any wallet with historically poor signal quality.
When the source wallet buys YES, this strategy buys NO, and vice versa.

Key differences from insider_copy_v1:
- Outcome is INVERTED at entry (YES → NO, NO → YES)
- Entry price is the OPPOSITE token price (1 - source_price)
- No insider score gate (we want bad wallets, not good ones)
- Win/loss thresholds are evaluated on the FADED outcome
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from sniper.settings.paper import (
    PAPER_MAX_CONCURRENT,
    PAPER_MAX_DAYS_TO_RESOLUTION,
    PAPER_MAX_EXPOSURE_PER_MARKET,
    PAPER_MAX_POSITION_SIZE,
    PAPER_MIN_POSITION_SIZE,
    PAPER_POSITION_SIZE,
    RESOLUTION_LOSS_THRESHOLD,
    RESOLUTION_WIN_THRESHOLD,
    STALE_DAYS,
)

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    import json
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(params, dict):
        return {}
    return params


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


DEFAULTS: Dict[str, Any] = {
    # If fade cascade fires, the bad wallet might be getting lucky — follow normal exit
    "cascade_exit_mode": "follow",
}


def _flip_outcome(outcome: str) -> str:
    """Invert YES ↔ NO."""
    return "NO" if outcome.upper() == "YES" else "YES"


class FadeV1Strategy(StrategyPlugin):
    plugin_id = "fade_v1"
    display_name = "Fade v1"
    description = (
        "Contrarian strategy: bets opposite to a tracked wallet. "
        "Targets wallets whose positions are reliably wrong."
    )

    strategy_family = "fade"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket",)
    default_loop_seconds = 30

    def discover(self, conn: sqlite3.Connection, strategy: Dict[str, Any]) -> List[StrategySignal]:
        wallet = str(strategy.get("wallet") or "").strip().lower()
        if not wallet:
            return []

        params = _parse_params(strategy)
        min_position_value = _to_float(params.get("min_position_value_usdc"), 500.0)

        rows = conn.execute(
            """
            SELECT
                ps.wallet,
                ps.condition_id,
                ps.outcome,
                ps.title,
                ps.current_price,
                ps.entry_price,
                ps.position_value,
                ps.analyzed_ts,
                ps.position_insider_score,
                ps.position_speculator_score,
                ps.position_classification,
                ps.end_date_iso,
                ps.entry_notional,
                ua.win_rate,
                ua.qualifying_roi,
                ua.classification AS wallet_classification
            FROM position_signals ps
            LEFT JOIN user_analysis ua ON ua.wallet = ps.wallet
            WHERE ps.wallet = ?
              AND ps.is_open = 1
              AND ps.position_value >= ?
              AND ps.current_price > 0
            ORDER BY ps.position_value DESC, ps.analyzed_ts DESC
            """,
            (wallet, min_position_value),
        ).fetchall()

        signals: List[StrategySignal] = []
        for row in rows:
            source_outcome = str(row["outcome"] or "").upper()
            faded_outcome = _flip_outcome(source_outcome)
            source_price = _to_float(row["current_price"])
            # Opposite token price in a binary market
            faded_price = max(0.0, min(1.0, 1.0 - source_price))

            signals.append(
                StrategySignal(
                    wallet=str(row["wallet"] or ""),
                    condition_id=str(row["condition_id"] or ""),
                    outcome=faded_outcome,
                    title=str(row["title"] or ""),
                    current_price=faded_price,
                    position_value=_to_float(row["position_value"]),
                    analyzed_ts=int(row["analyzed_ts"] or 0),
                    metadata={
                        "source_outcome": source_outcome,
                        "source_price": source_price,
                        "source_entry_price": _to_float(row["entry_price"]),
                        "source_notional": _to_float(row["entry_notional"]),
                        "position_insider_score": _to_float(row["position_insider_score"]),
                        "position_speculator_score": _to_float(row["position_speculator_score"]),
                        "position_classification": str(row["position_classification"] or ""),
                        "end_date_iso": str(row["end_date_iso"] or ""),
                        "win_rate": _to_float(row["win_rate"]),
                        "qualifying_roi": row["qualifying_roi"],
                        "wallet_classification": str(row["wallet_classification"] or ""),
                    },
                )
            )
        return signals

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        max_concurrent = int(params.get("max_concurrent_positions") or PAPER_MAX_CONCURRENT)
        max_entry_price = _to_float(params.get("max_entry_price"), 0.70)
        max_days_to_resolution = int(params.get("max_days_to_resolution") or PAPER_MAX_DAYS_TO_RESOLUTION)
        min_source_notional = _to_float(params.get("min_source_notional_usdc"), 100.0)

        # Duplicate check on the FADED outcome
        if any(
            p.get("wallet") == signal.wallet
            and p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this wallet/market/outcome")

        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(False, f"max concurrent positions reached ({max_concurrent})")

        if signal.current_price <= 0:
            return StrategyEntryDecision(False, "invalid faded price")

        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(False, f"faded price {signal.current_price:.3f} > {max_entry_price:.2f}")

        # Source notional gate — only fade significant positions
        source_notional = _to_float(signal.metadata.get("source_notional"))
        if source_notional < min_source_notional:
            return StrategyEntryDecision(
                False,
                f"source notional ${source_notional:.0f} < ${min_source_notional:.0f} minimum",
            )

        # Resolution date filter
        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                if days_left > max_days_to_resolution:
                    return StrategyEntryDecision(False, f"market resolves in {days_left}d > {max_days_to_resolution}d")
            except Exception:
                pass

        # Per-market exposure cap
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc"), PAPER_MAX_EXPOSURE_PER_MARKET)
        current_market_exposure = sum(
            _to_float(p.get("entry_shares"), 0) * _to_float(p.get("avg_entry_price"), 0)
            for p in open_positions
            if str(p.get("condition_id") or "").lower() == signal.condition_id.lower()
        )
        if current_market_exposure >= max_exposure:
            return StrategyEntryDecision(
                False,
                f"market exposure ${current_market_exposure:.0f} >= ${max_exposure:.0f} cap",
            )

        # Simple fixed-size for fade (no multiplicative sizing — contrarian doesn't
        # have the same signal quality indicators as insider copy).
        base_size = _to_float(params.get("bet_size_usdc"), PAPER_POSITION_SIZE)
        size_usdc = max(PAPER_MIN_POSITION_SIZE, min(PAPER_MAX_POSITION_SIZE, base_size))

        # Clamp to remaining market headroom
        market_headroom = max_exposure - current_market_exposure
        if size_usdc > market_headroom:
            size_usdc = market_headroom

        execution_mode = str(params.get("execution_mode") or "paper").strip().lower()
        return StrategyEntryDecision(
            True,
            f"fade: source {signal.metadata.get('source_outcome')} → faded {signal.outcome}",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode=execution_mode,
            metadata={
                "source_outcome": signal.metadata.get("source_outcome"),
                "source_price": signal.metadata.get("source_price"),
                "source_notional": source_notional,
                "source_signal_ts": signal.analyzed_ts,
            },
        )

    def manage(self, conn, strategy: Dict[str, Any], position: Dict[str, Any]) -> StrategyManageState:
        """Track the SOURCE wallet's position to know if they're still in.

        We check position_signals for the source wallet + condition_id.
        The source outcome is the OPPOSITE of our position's outcome.
        """
        pos_outcome = str(position.get("outcome") or "").upper()
        source_outcome = _flip_outcome(pos_outcome)

        row = conn.execute(
            """
            SELECT
                ps.is_open,
                ps.current_price,
                ps.position_value,
                ps.position_insider_score,
                ps.position_classification
            FROM position_signals ps
            WHERE ps.wallet = ?
              AND ps.condition_id = ?
              AND UPPER(ps.outcome) = ?
            ORDER BY ps.analyzed_ts DESC
            LIMIT 1
            """,
            (
                str(position.get("wallet") or "").lower(),
                str(position.get("condition_id") or "").lower(),
                source_outcome,
            ),
        ).fetchone()

        if row is not None:
            source_price = _to_float(row["current_price"])
            faded_price = max(0.0, min(1.0, 1.0 - source_price))
            return StrategyManageState(
                current_price=faded_price,
                signal_open=bool(int(row["is_open"] or 0)),
                metadata={
                    "source_price": source_price,
                    "position_value": _to_float(row["position_value"]),
                    "position_insider_score": _to_float(row["position_insider_score"]),
                },
            )

        return StrategyManageState(
            current_price=_to_float(position.get("avg_entry_price")),
            signal_open=False,
            metadata={},
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days") or STALE_DAYS)
        win_threshold = _to_float(params.get("resolution_win_threshold"), RESOLUTION_WIN_THRESHOLD)
        loss_threshold = _to_float(params.get("resolution_loss_threshold"), RESOLUTION_LOSS_THRESHOLD)
        current_price = _to_float(state.current_price, _to_float(position.get("avg_entry_price")))
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts else 0

        # Resolution thresholds apply to our FADED position's price
        if current_price >= win_threshold:
            return StrategyExitDecision(True, "resolution_win", exit_price=current_price, status="closed")
        if current_price <= loss_threshold:
            return StrategyExitDecision(True, "resolution_loss", exit_price=current_price, status="closed")
        if not state.signal_open:
            return StrategyExitDecision(True, "source_exited", exit_price=current_price, status="closed")
        # Cascade — fade_v1 default is "follow" (proceed with normal exit logic)
        cascade_active = bool(position.get("cascade_active", False))
        cascade_mode = params.get("cascade_exit_mode", DEFAULTS.get("cascade_exit_mode", "follow"))
        if cascade_active and cascade_mode == "hold_through":
            return StrategyExitDecision(False, "hold_cascade", exit_price=current_price, status="open")
        if age_days >= stale_days:
            return StrategyExitDecision(True, f"stale_{age_days}d", exit_price=current_price, status="closed")
        return StrategyExitDecision(False, "hold", exit_price=current_price, status="open")

    def report(self, conn, strategy: Dict[str, Any], positions: List[Dict[str, Any]]) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})
