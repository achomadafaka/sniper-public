"""Crowd Fade v1 strategy plugin: exploit the longshot bias in binary prediction markets.

The edge: binary outcomes priced below 10 cents are SYSTEMATICALLY overpriced
(Kalshi study, 300k+ real transactions, 2025). The crowd consistently overpays
for unlikely outcomes. This strategy buys the near-certainty side (>= 0.90),
fading the crowd's overvalued longshots.

Signal type: structural (no wallet tracking required).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from sniper.settings.paper import (
    PAPER_MAX_CONCURRENT,
    PAPER_MAX_EXPOSURE_PER_MARKET,
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


WALLET = "structural_bias_longshot"


class CrowdFadeV1Strategy(StrategyPlugin):
    plugin_id = "crowd_fade_v1"
    display_name = "Crowd Fade v1"
    description = (
        "Exploits the longshot bias: buys the near-certainty side (>= 0.90) "
        "in binary markets where the crowd systematically overprices unlikely outcomes."
    )

    strategy_family = "structural"
    supported_execution_modes = ("paper",)
    required_feeds = ()  # reads poly_price_snapshots directly — no live feed adapter needed
    required_tables = ("poly_price_snapshots",)
    default_loop_seconds = 300

    def discover(self, conn: sqlite3.Connection, strategy: Dict[str, Any]) -> List[StrategySignal]:
        rows = conn.execute(
            """
            SELECT DISTINCT ps.condition_id, ps.title, ps.outcome, ps.price,
                   ps.captured_at, ps.market_type, ps.commence_time
            FROM poly_price_snapshots ps
            WHERE ps.captured_at >= datetime('now', '-4 hours')
              AND ps.outcome IN ('yes', 'no')
              AND ps.price >= 0.90
              AND ps.price <= 0.97
              AND (ps.market_type IS NULL OR ps.market_type NOT IN ('h2h', 'spreads', 'totals'))
            ORDER BY ps.price DESC, ps.captured_at DESC
            """
        ).fetchall()

        signals: List[StrategySignal] = []
        seen: set = set()

        for row in rows:
            condition_id = str(row["condition_id"] or "")
            outcome = str(row["outcome"] or "").lower()
            key = (condition_id, outcome)
            if key in seen:
                continue

            near_certainty_price = _to_float(row["price"])
            longshot_outcome = "no" if outcome == "yes" else "yes"

            # Verify the other side (longshot) exists and is <= 0.10
            longshot_row = conn.execute(
                """
                SELECT price FROM poly_price_snapshots
                WHERE condition_id = ?
                  AND LOWER(outcome) = ?
                  AND captured_at >= datetime('now', '-4 hours')
                ORDER BY captured_at DESC LIMIT 1
                """,
                (condition_id, longshot_outcome),
            ).fetchone()

            if longshot_row is None:
                continue
            longshot_price = _to_float(longshot_row["price"])
            if longshot_price > 0.10:
                continue

            # Guard against wide-spread or negativeRisk markets
            price_sum = near_certainty_price + longshot_price
            if not (0.90 <= price_sum <= 1.10):
                continue

            seen.add(key)
            signals.append(
                StrategySignal(
                    wallet=WALLET,
                    condition_id=condition_id,
                    outcome=outcome,
                    title=str(row["title"] or ""),
                    current_price=near_certainty_price,
                    position_value=0.0,
                    analyzed_ts=_now_ts(),
                    metadata={
                        "longshot_price": longshot_price,
                        "market_type": str(row["market_type"] or "") or None,
                        "signal_type": "longshot_bias",
                        "commence_time": str(row["commence_time"] or "") or None,
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

        min_price = _to_float(params.get("min_price", 0.90))
        max_price = _to_float(params.get("max_price", 0.97))
        max_concurrent = int(params.get("max_concurrent_positions") or params.get("max_concurrent") or PAPER_MAX_CONCURRENT)
        bet_size_usdc = _to_float(params.get("bet_size_usdc"), PAPER_POSITION_SIZE)
        max_days_to_resolution = int(params.get("max_days_to_resolution") or 45)
        min_longshot_price = _to_float(params.get("min_longshot_price"), 0.03)

        price = signal.current_price
        longshot_price = _to_float(signal.metadata.get("longshot_price", 0.0))

        # 1. Duplicate check
        if any(
            str(p.get("condition_id") or "").lower() == signal.condition_id.lower()
            and str(p.get("outcome") or "").lower() == signal.outcome.lower()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this market/outcome")

        # 2. Price range check
        if price < min_price or price > max_price:
            return StrategyEntryDecision(
                False,
                f"near-certainty price {price:.3f} outside [{min_price:.2f}, {max_price:.2f}]",
            )

        # 3. Longshot price gate — skip near-resolved markets
        if longshot_price < min_longshot_price:
            return StrategyEntryDecision(
                False,
                f"longshot price {longshot_price:.3f} < {min_longshot_price:.3f} (near-resolved)",
            )

        # 4. Max concurrent check — use DB count as ground truth (in-memory list may be stale)
        try:
            db_open_count = conn.execute(
                "SELECT COUNT(*) FROM strategy_positions WHERE strategy_id=? AND state='open'",
                (str(strategy.get("id", "")),)
            ).fetchone()[0]
        except Exception:
            db_open_count = len(open_positions)
        if db_open_count >= max_concurrent or len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                False,
                f"max concurrent positions reached (db={db_open_count}, mem={len(open_positions)}, max={max_concurrent})",
            )

        # 5. Days to resolution check
        commence_time = str(signal.metadata.get("commence_time") or "")
        if commence_time:
            try:
                end_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                if days_left > max_days_to_resolution:
                    return StrategyEntryDecision(
                        False,
                        f"market resolves in {days_left}d > {max_days_to_resolution}d",
                    )
            except Exception:
                pass

        # 6. Per-market exposure cap
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

        # Clamp to remaining market headroom
        size_usdc = bet_size_usdc
        market_headroom = max_exposure - current_market_exposure
        if size_usdc > market_headroom:
            size_usdc = market_headroom

        return StrategyEntryDecision(
            True,
            f"longshot_bias: near-certainty at {price:.3f}",
            size_usdc=size_usdc,
            entry_price=price,
            execution_mode=str(params.get("execution_mode") or "paper").strip().lower(),
            metadata={
                "longshot_price": longshot_price,
                "signal_type": "longshot_bias",
            },
        )

    def manage(self, conn, strategy: Dict[str, Any], position: Dict[str, Any]) -> StrategyManageState:
        condition_id = str(position.get("condition_id") or "")
        outcome = str(position.get("outcome") or "")

        row = conn.execute(
            """
            SELECT price FROM poly_price_snapshots
            WHERE condition_id = ? AND LOWER(outcome) = LOWER(?)
            ORDER BY captured_at DESC LIMIT 1
            """,
            (condition_id, outcome),
        ).fetchone()

        if row is not None:
            return StrategyManageState(
                current_price=_to_float(row["price"]),
                signal_open=True,
            )

        # No snapshot found — use entry price as fallback but keep signal_open=True.
        # A missing snapshot is a feed gap, not a signal to exit. The stale_days
        # gate in exit() handles genuinely old positions. Without this, any 30-min
        # gap in the poly_binary_snapshot cron would exit ALL open positions.
        return StrategyManageState(
            current_price=_to_float(position.get("avg_entry_price")),
            signal_open=True,
            metadata={"snapshot_missing": True},
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        win_threshold = _to_float(params.get("resolution_win_threshold"), RESOLUTION_WIN_THRESHOLD)
        loss_threshold = _to_float(params.get("resolution_loss_threshold"), RESOLUTION_LOSS_THRESHOLD)
        stale_days = int(params.get("stale_days") or STALE_DAYS)

        current_price = _to_float(state.current_price, _to_float(position.get("avg_entry_price")))
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts > 0 else 0

        # 1. Resolution win
        if current_price >= win_threshold:
            return StrategyExitDecision(True, "resolution_win", exit_price=current_price, status="closed")

        # 2. Resolution loss — market turned against us
        if current_price <= loss_threshold:
            return StrategyExitDecision(True, "resolution_loss", exit_price=current_price, status="closed")

        # 3. Age-based stale (feed gaps are handled gracefully in manage())
        if age_days >= stale_days:
            return StrategyExitDecision(True, f"stale_{age_days}d", exit_price=current_price, status="closed")

        return StrategyExitDecision(False, "hold", exit_price=current_price, status="open")

    def validate_parameters(self, parameters: dict) -> List[str]:
        errors: List[str] = []
        min_price = parameters.get("min_price")
        max_price = parameters.get("max_price")
        bet_size_usdc = parameters.get("bet_size_usdc")

        if min_price is not None:
            try:
                v = float(min_price)
                if not (0.5 <= v <= 0.99):
                    errors.append(f"min_price {v} must be in [0.5, 0.99]")
            except (TypeError, ValueError):
                errors.append(f"min_price must be a number, got {min_price!r}")

        if max_price is not None and min_price is not None:
            try:
                vmax = float(max_price)
                vmin = float(min_price)
                if vmax < vmin:
                    errors.append(f"max_price {vmax} must be >= min_price {vmin}")
            except (TypeError, ValueError):
                errors.append(f"max_price must be a number, got {max_price!r}")

        if bet_size_usdc is not None:
            try:
                v = float(bet_size_usdc)
                if v <= 0:
                    errors.append(f"bet_size_usdc {v} must be > 0")
            except (TypeError, ValueError):
                errors.append(f"bet_size_usdc must be a number, got {bet_size_usdc!r}")

        return errors

    def report(self, conn, strategy: Dict[str, Any], positions: List[Dict[str, Any]]) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})
