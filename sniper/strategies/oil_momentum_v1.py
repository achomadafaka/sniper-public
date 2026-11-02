"""Oil Momentum V1 strategy plugin.

Detects directional momentum in oil prices (Brent/WTI) and enters Polymarket
positions on markets whose direction aligns with the momentum signal.

Signal logic:
- Poll commodity_price_history for Brent (BZ=F) or WTI (CL=F)
- Compute % change over window_hours (default 168h / 7 days)
- Find open Polymarket oil markets via find_affected_markets
- Match market direction (oil_up / oil_down) against momentum sign
- Generate StrategySignal for aligned markets

Entry gates:
1. No duplicate position for same condition_id + outcome
2. Max concurrent positions (default 5)
3. Max entry price (default 0.70)
4. Market exposure cap (default $200)
5. Momentum signal freshness (default max 24h old)
6. Resolution date (market must not be past end_date)

Exit logic (checked in order):
1. market_closed  – market_metadata.is_open = 0
2. early_close_win  – price >= 0.97
3. early_close_loss – price <= 0.03
4. take_profit  – ROI >= 30%
5. stale_Xd  – position open >= 14 days
6. hold
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from sniper.markets.polymarket_api import fetch_live_clob_price

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Keywords that indicate an oil_up market (YES side wins if oil rises)
_UP_KEYWORDS = ("exceed", "rise", "higher", "over", "above", "break", "surpass")

# Keywords that indicate an oil_down market (YES side wins if oil falls)
_DOWN_KEYWORDS = ("fall", "below", "drop", "decline", "under", "crash", "slide", "tumble")

# Commodity symbols to check for oil momentum
_OIL_SYMBOLS = ("BZ=F", "CL=F")

# Keywords used to find oil markets on Polymarket
_OIL_KEYWORDS = ["oil", "crude", "brent", "wti", "barrel", "petroleum"]

# Default configuration values
_DEFAULT_WINDOW_HOURS = 168          # 7 days of price history
_DEFAULT_MIN_MOMENTUM_PCT = 1.0      # At least 1% move to signal
_DEFAULT_BET_SIZE = 10.0
_DEFAULT_MAX_ENTRY_PRICE = 0.70
_DEFAULT_MAX_CONCURRENT = 5
_DEFAULT_MAX_EXPOSURE = 200.0
_DEFAULT_STALE_DAYS = 14
_DEFAULT_WIN_THRESHOLD = 0.97
_DEFAULT_LOSS_THRESHOLD = 0.03
_DEFAULT_TAKE_PROFIT_ROI = 0.30      # 30%
_END_DATE_GRACE_DAYS = 7


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
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


def _parse_direction(title: str) -> Optional[str]:
    """Classify a market title as 'oil_up', 'oil_down', or None (ambiguous).

    Returns 'oil_up' if only up-keywords are found, 'oil_down' if only
    down-keywords are found, None if neither or both sets match.
    """
    lower = title.lower()
    has_up = any(kw in lower for kw in _UP_KEYWORDS)
    has_down = any(kw in lower for kw in _DOWN_KEYWORDS)

    if has_up and not has_down:
        return "oil_up"
    if has_down and not has_up:
        return "oil_down"
    return None


def _compute_momentum(series: List[Dict[str, Any]], window_hours: int = 168) -> Optional[float]:
    """Compute % price change over the series.

    Returns None if:
    - series has fewer than 2 points
    - the time span of the series covers < 50% of window_hours
    - oldest price is 0 (division guard)
    """
    if len(series) < 2:
        return None

    # Parse timestamps to check time coverage
    try:
        def _parse_ts(row: Dict[str, Any]) -> datetime:
            ts_str = str(row.get("market_ts") or "")
            # Handle Z suffix
            ts_str = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        timestamps = [_parse_ts(r) for r in series]
        span_hours = (max(timestamps) - min(timestamps)).total_seconds() / 3600.0

        # Must cover >= 50% of the requested window
        if span_hours < window_hours * 0.5:
            return None

    except Exception:
        logger.exception("_compute_momentum: failed to parse timestamps")
        return None

    oldest_price = _to_float(series[0].get("price_usd"))
    newest_price = _to_float(series[-1].get("price_usd"))

    if oldest_price == 0:
        return None

    pct_change = (newest_price - oldest_price) / oldest_price * 100.0
    return pct_change


def _resolve_momentum_signal(
    conn: sqlite3.Connection,
    *,
    symbols: List[str],
    window_hours: int,
    min_momentum_pct: float,
) -> Optional[Dict[str, Any]]:
    """Return a consensus momentum signal across configured oil symbols.

    The strategy defaults to both Brent and WTI. If both produce strong but
    conflicting momentum directions, skip the signal entirely instead of
    letting the first provider win.
    """
    from sniper.context import commodity_price_history

    computed_ts = _now_ts()
    samples: List[Dict[str, Any]] = []

    for symbol in symbols:
        try:
            series = commodity_price_history(conn, symbol, hours=window_hours)
        except Exception:
            logger.exception("oil_momentum_v1: commodity_price_history failed for %s", symbol)
            continue
        pct = _compute_momentum(series, window_hours=window_hours)
        if pct is None:
            continue
        samples.append({"symbol": symbol, "momentum_pct": pct})

    if not samples:
        return None

    strong = [s for s in samples if abs(float(s["momentum_pct"])) >= min_momentum_pct]
    if not strong:
        return None

    directions = {"oil_up" if float(s["momentum_pct"]) > 0 else "oil_down" for s in strong}
    if len(directions) != 1:
        logger.info("oil_momentum_v1: conflicting momentum across symbols: %s", strong)
        return None

    direction = next(iter(directions))
    avg_momentum_pct = sum(float(s["momentum_pct"]) for s in strong) / len(strong)
    primary = max(strong, key=lambda s: abs(float(s["momentum_pct"])))
    return {
        "direction": direction,
        "momentum_pct": avg_momentum_pct,
        "symbol": primary["symbol"],
        "symbols_considered": [str(s["symbol"]) for s in samples],
        "symbols_confirmed": [str(s["symbol"]) for s in strong],
        "momentum_ts": computed_ts,
    }


# ---------------------------------------------------------------------------
# Strategy plugin
# ---------------------------------------------------------------------------

class OilMomentumV1Strategy(StrategyPlugin):
    """Commodity momentum strategy for oil markets on Polymarket."""

    plugin_id = "oil_momentum_v1"
    display_name = "Oil Momentum V1"
    description = (
        "Detects directional momentum in oil prices (Brent/WTI) and enters "
        "Polymarket positions on markets that align with the momentum signal."
    )

    strategy_family = "commodity_momentum"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket", "commodities")
    default_loop_seconds = 300

    def discover(
        self, conn: sqlite3.Connection, strategy: Dict[str, Any]
    ) -> List[StrategySignal]:
        """Find oil markets aligned with commodity price momentum."""
        params = _parse_params(strategy)
        window_hours = int(params.get("window_hours", _DEFAULT_WINDOW_HOURS))
        min_momentum_pct = _to_float(params.get("min_momentum_pct", _DEFAULT_MIN_MOMENTUM_PCT))
        symbols = list(params.get("symbols", _OIL_SYMBOLS))
        oil_keywords = list(params.get("oil_keywords", _OIL_KEYWORDS))

        # Lazy import — all SQL stays in context layer / db.py
        try:
            from sniper.context import find_affected_markets
        except ImportError:
            logger.exception("oil_momentum_v1 discover: context import failed")
            return []

        momentum = _resolve_momentum_signal(
            conn,
            symbols=symbols,
            window_hours=window_hours,
            min_momentum_pct=min_momentum_pct,
        )
        if momentum is None:
            return []

        # Find open oil markets
        try:
            markets = find_affected_markets(conn, oil_keywords)
        except Exception:
            logger.exception("oil_momentum_v1 discover: find_affected_markets failed")
            return []

        signals: List[StrategySignal] = []
        for market in markets:
            condition_id = str(market.get("condition_id") or "")
            title = str(market.get("title") or "")
            end_date_iso = str(market.get("end_date_iso") or "")

            direction = _parse_direction(title)
            if direction is None:
                continue  # Ambiguous title — skip

            # Only enter if market direction aligns with momentum direction
            if direction != momentum["direction"]:
                continue

            # Outcome is always YES (we bet the direction-aligned outcome resolves YES)
            signals.append(
                StrategySignal(
                    wallet="oil_momentum_bot",
                    condition_id=condition_id,
                    outcome="YES",
                    title=title,
                    current_price=0.0,  # Runner enriches via live price
                    position_value=0.0,
                    analyzed_ts=momentum["momentum_ts"],
                    metadata={
                        "direction": direction,
                        "momentum_pct": momentum["momentum_pct"],
                        "momentum_ts": momentum["momentum_ts"],
                        "end_date_iso": end_date_iso,
                        "symbol": momentum["symbol"],
                        "symbols_considered": momentum["symbols_considered"],
                        "symbols_confirmed": momentum["symbols_confirmed"],
                    },
                )
            )

        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        """Gate checks and entry decision."""
        params = _parse_params(strategy)
        bet_size = _to_float(params.get("bet_size_usdc", _DEFAULT_BET_SIZE))
        max_entry_price = _to_float(params.get("max_entry_price", _DEFAULT_MAX_ENTRY_PRICE))
        max_concurrent = int(params.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc", _DEFAULT_MAX_EXPOSURE))

        # Gate 1: Duplicate check
        if any(
            p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this market/outcome")

        # Gate 2: Max concurrent
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                False, f"max concurrent positions reached ({max_concurrent})"
            )

        # Gate 3: Max entry price
        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(
                False,
                f"entry price {signal.current_price:.3f} > {max_entry_price:.2f}",
            )

        # Gate 4: Market exposure cap
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

        # Gate 5: Re-verify momentum is still present at entry time
        window_hours = int(params.get("window_hours", _DEFAULT_WINDOW_HOURS))
        min_momentum_pct = _to_float(params.get("min_momentum_pct", _DEFAULT_MIN_MOMENTUM_PCT))
        symbols = list(signal.metadata.get("symbols_considered") or params.get("symbols", _OIL_SYMBOLS))
        current_momentum = _resolve_momentum_signal(
            conn,
            symbols=symbols,
            window_hours=window_hours,
            min_momentum_pct=min_momentum_pct,
        )
        if current_momentum is None:
            return StrategyEntryDecision(
                False,
                "momentum no longer valid at entry time (no confirmed signal)",
            )
        if str(current_momentum["direction"]) != str(signal.metadata.get("direction") or ""):
            return StrategyEntryDecision(
                False,
                f"momentum direction changed from {signal.metadata.get('direction')} "
                f"to {current_momentum['direction']}",
            )

        # Gate 6: Resolution date (past end date or too far out)
        max_days = int(params.get("max_days_to_resolution", 60))
        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                # Allow a short grace window because market metadata end dates
                # can lag actual closure/resolution by several days.
                if days_left < -_END_DATE_GRACE_DAYS:
                    return StrategyEntryDecision(False, "market already past end date")
                if days_left > max_days:
                    return StrategyEntryDecision(
                        False, f"market resolves in {days_left}d > {max_days}d max"
                    )
            except Exception:
                logger.warning(
                    "oil_momentum_v1 enter: could not parse end_date_iso %r for %s",
                    end_date_iso, signal.condition_id,
                )

        # Insider convergence annotation (not a gate)
        try:
            from sniper.context import insider_convergence_check
            convergence = insider_convergence_check(conn, signal.condition_id)
        except Exception:
            logger.exception("oil_momentum_v1 enter: insider_convergence_check failed")
            convergence = {"insider_count": 0, "total_insider_notional": 0.0, "avg_insider_score": 0.0}

        # Clamp to market headroom
        market_headroom = max_exposure - current_market_exposure
        size_usdc = min(bet_size, market_headroom)
        size_usdc = max(1.0, size_usdc)

        return StrategyEntryDecision(
            True,
            f"oil_momentum: {signal.metadata.get('direction')} "
            f"momentum {signal.metadata.get('momentum_pct', 0):.1f}%",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata={
                "direction": signal.metadata.get("direction"),
                "momentum_pct": current_momentum["momentum_pct"],
                "symbol": current_momentum["symbol"],
                "symbols_confirmed": current_momentum["symbols_confirmed"],
                "insider_convergence": convergence,
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        """Check market status and fetch live price."""
        condition_id = str(position.get("condition_id") or "")
        outcome = str(position.get("outcome") or "YES")
        entry_price = _to_float(position.get("avg_entry_price"), 0.5)

        # ONE allowed direct SQL query: market_metadata is_open check
        row = conn.execute(
            "SELECT is_open FROM market_metadata WHERE condition_id = ? LIMIT 1",
            (condition_id,),
        ).fetchone()

        market_open = True
        if row is not None:
            market_open = bool(int(row["is_open"] or 0))

        # Fetch live price (shared helper from polymarket_api — module-level import)
        live_price = fetch_live_clob_price(condition_id, outcome)
        current_price = live_price if live_price is not None else entry_price

        return StrategyManageState(
            current_price=current_price,
            signal_open=market_open,
            metadata={},
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        """Exit rules for oil momentum positions."""
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days", _DEFAULT_STALE_DAYS))
        win_threshold = _to_float(params.get("win_threshold", _DEFAULT_WIN_THRESHOLD))
        loss_threshold = _to_float(params.get("loss_threshold", _DEFAULT_LOSS_THRESHOLD))
        take_profit_roi = _to_float(params.get("take_profit_roi", _DEFAULT_TAKE_PROFIT_ROI))

        current_price = _to_float(
            state.current_price, _to_float(position.get("avg_entry_price"))
        )
        entry_price = _to_float(position.get("avg_entry_price"), 0.0)
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts else 0

        # Rule 1: Market closed
        if not state.signal_open:
            return StrategyExitDecision(
                True, "market_closed", exit_price=current_price, status="closed"
            )

        # Rule 2: Early close win (near-resolution)
        if current_price >= win_threshold:
            return StrategyExitDecision(
                True, "early_close_win", exit_price=current_price, status="closed"
            )

        # Rule 3: Early close loss (near-resolution)
        if current_price <= loss_threshold:
            return StrategyExitDecision(
                True, "early_close_loss", exit_price=current_price, status="closed"
            )

        # Rule 4: Take profit
        if entry_price > 0:
            roi = (current_price - entry_price) / entry_price
            if roi >= take_profit_roi:
                return StrategyExitDecision(
                    True, "take_profit", exit_price=current_price, status="closed"
                )

        # Rule 5: Stale position
        if age_days >= stale_days:
            return StrategyExitDecision(
                True, f"stale_{age_days}d", exit_price=current_price, status="closed"
            )

        # Rule 6: Hold
        return StrategyExitDecision(
            False, "hold", exit_price=current_price, status="open"
        )

    def report(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        positions: List[Dict[str, Any]],
    ) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})

    def replay_adapter(self):
        """Oil momentum replay not yet implemented."""
        return None
