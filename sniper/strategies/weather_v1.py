"""Weather Bot V1 strategy plugin.

A simple proof-of-concept strategy that:
1. Discovers Polymarket markets with weather-related titles
2. Matches them against OpenWeatherMap forecast snapshots
3. Makes deterministic entry decisions based on forecast vs market threshold

This is intentionally simple -- no ML, no fancy models.  The purpose is to
validate the multi-strategy framework with a second, independent feed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

logger = logging.getLogger(__name__)
_END_DATE_GRACE_DAYS = 7

# Keywords to identify weather-related Polymarket markets
WEATHER_KEYWORDS = (
    "temperature",
    "weather",
    "rain",
    "snow",
    "celsius",
    "fahrenheit",
    "hurricane",
    "tornado",
    "flood",
    "heat wave",
    "heatwave",
    "cold snap",
)


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


def _title_matches_weather(title: str) -> bool:
    """Return True if the market title contains any weather keyword."""
    lower = title.lower()
    return any(kw in lower for kw in WEATHER_KEYWORDS)


def _extract_temperature(payload: Dict[str, Any]) -> float | None:
    """Extract the temperature (Celsius) from the first forecast entry.

    Returns None if the payload structure is unexpected.
    """
    try:
        first = payload.get("list", [{}])[0]
        main = first.get("main", {})
        return float(main.get("temp"))
    except (IndexError, TypeError, ValueError, KeyError):
        return None


class WeatherV1Strategy(StrategyPlugin):
    """Proof-of-concept weather strategy for framework validation."""

    plugin_id = "weather_v1"
    display_name = "Weather Bot V1"
    description = (
        "Proof-of-concept weather strategy. Matches Polymarket weather markets "
        "against OpenWeatherMap forecasts for deterministic entry decisions."
    )

    strategy_family = "weather"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket", "weather")
    default_loop_seconds = 60

    def discover(
        self, conn: sqlite3.Connection, strategy: Dict[str, Any]
    ) -> List[StrategySignal]:
        """Find weather-related Polymarket markets with available forecast data.

        1. Query market_metadata for titles matching weather keywords
        2. Query weather_snapshots for recent forecast data
        3. Return a StrategySignal for each match
        """
        params = _parse_params(strategy)
        max_markets = int(params.get("max_discover_markets", 20))

        # Step 1: Find weather-related markets
        try:
            rows = conn.execute(
                """
                SELECT condition_id, title, end_date_iso, category
                FROM market_metadata
                WHERE is_open = 1
                ORDER BY volume_24h DESC
                LIMIT 500
                """,
            ).fetchall()
        except Exception:
            logger.exception("weather_v1 discover: failed to query market_metadata")
            return []

        weather_markets = []
        for row in rows:
            title = str(row["title"] or "")
            if _title_matches_weather(title):
                weather_markets.append(dict(row))
        weather_markets = weather_markets[:max_markets]

        if not weather_markets:
            return []

        # Step 2: Get recent weather snapshots
        try:
            from db import recent_weather_snapshots
            snapshots = recent_weather_snapshots(conn, hours=24)
        except Exception:
            logger.exception("weather_v1 discover: failed to query weather_snapshots")
            return []

        if not snapshots:
            return []

        # Build a location -> latest snapshot mapping
        snapshot_by_location: Dict[str, Dict[str, Any]] = {}
        for snap in snapshots:
            loc = str(snap.get("location_key") or "")
            if loc and loc not in snapshot_by_location:
                snapshot_by_location[loc] = snap

        # Step 3: Generate signals for matched markets
        signals: List[StrategySignal] = []
        for market in weather_markets:
            condition_id = str(market.get("condition_id") or "")
            title = str(market.get("title") or "")

            # Match market title to a location snapshot (skip if no match)
            best_snapshot = None
            best_location = None
            for loc, snap in snapshot_by_location.items():
                loc_readable = loc.replace("_", " ").lower()
                if loc_readable in title.lower():
                    best_snapshot = snap
                    best_location = loc
                    break

            if best_snapshot is None:
                continue

            # Parse the forecast payload
            try:
                payload = json.loads(str(best_snapshot.get("payload_json") or "{}"))
            except (json.JSONDecodeError, TypeError):
                payload = {}

            temperature = _extract_temperature(payload)

            signals.append(
                StrategySignal(
                    wallet="weather_bot",  # Synthetic wallet for weather strategy
                    condition_id=condition_id,
                    outcome="YES",  # Default; enter() will decide direction
                    title=title,
                    current_price=0.0,  # Will be enriched by runner
                    position_value=0.0,
                    analyzed_ts=_now_ts(),
                    metadata={
                        "location_key": best_location,
                        "forecast_temperature_c": temperature,
                        "forecast_ts": best_snapshot.get("forecast_ts", 0),
                        "end_date_iso": str(market.get("end_date_iso") or ""),
                        "category": str(market.get("category") or ""),
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
        """Decide whether to enter a weather market position.

        Simple deterministic thesis: if we have a forecast, bet on it.
        Gates: max_entry_price, market exposure, resolution date, no duplicates.
        """
        params = _parse_params(strategy)
        max_entry_price = _to_float(params.get("max_entry_price"), 0.70)
        max_concurrent = int(params.get("max_concurrent_positions", 10))
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc"), 200.0)
        bet_size = _to_float(params.get("bet_size_usdc"), 10.0)
        max_days_to_resolution = int(params.get("max_days_to_resolution", 30))

        # Gate: duplicate check
        if any(
            p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this market/outcome")

        # Gate: max concurrent
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                False, f"max concurrent positions reached ({max_concurrent})"
            )

        # Gate: valid price
        if signal.current_price <= 0:
            return StrategyEntryDecision(False, "invalid current price")

        # Gate: max entry price
        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(
                False,
                f"entry price {signal.current_price:.3f} > {max_entry_price:.2f}",
            )

        # Gate: market exposure
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

        # Gate: resolution date
        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                # Weather market end dates can be slightly stale versus the
                # actual market close, so tolerate a short past-date window.
                if days_left < -_END_DATE_GRACE_DAYS:
                    return StrategyEntryDecision(False, "market already past end date")
                if days_left > max_days_to_resolution:
                    return StrategyEntryDecision(
                        False,
                        f"market resolves in {days_left}d > {max_days_to_resolution}d",
                    )
            except Exception:
                pass

        # Gate: must have forecast data
        temperature = signal.metadata.get("forecast_temperature_c")
        if temperature is None:
            return StrategyEntryDecision(False, "no forecast temperature available")

        # Clamp bet size to market headroom
        market_headroom = max_exposure - current_market_exposure
        size_usdc = min(bet_size, market_headroom)
        size_usdc = max(1.0, size_usdc)  # Floor at $1

        return StrategyEntryDecision(
            True,
            f"weather signal: forecast temp {temperature:.1f}C",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata={
                "forecast_temperature_c": temperature,
                "location_key": signal.metadata.get("location_key"),
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        """Track position -- weather positions are always considered 'signal open'
        while the market is active (we don't track a source wallet).
        """
        condition_id = str(position.get("condition_id") or "")

        # Try to get a current price from market_metadata
        row = conn.execute(
            """SELECT is_open FROM market_metadata
               WHERE condition_id = ? LIMIT 1""",
            (condition_id,),
        ).fetchone()

        market_open = True
        if row is not None:
            market_open = bool(int(row["is_open"] or 0))

        entry_price = _to_float(position.get("avg_entry_price"), 0.5)

        return StrategyManageState(
            current_price=entry_price,  # No live price feed for weather yet
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
        """Exit logic for weather positions.

        - Resolution: price hits win/loss threshold
        - Stale: position open > 7 days
        - Market closed: market_metadata.is_open = 0
        """
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days", 7))
        win_threshold = _to_float(params.get("resolution_win_threshold"), 0.97)
        loss_threshold = _to_float(params.get("resolution_loss_threshold"), 0.03)

        current_price = _to_float(
            state.current_price, _to_float(position.get("avg_entry_price"))
        )
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts else 0

        if current_price >= win_threshold:
            return StrategyExitDecision(
                True, "resolution_win", exit_price=current_price, status="closed"
            )
        if current_price <= loss_threshold:
            return StrategyExitDecision(
                True, "resolution_loss", exit_price=current_price, status="closed"
            )
        if not state.signal_open:
            return StrategyExitDecision(
                True, "market_closed", exit_price=current_price, status="closed"
            )
        if age_days >= stale_days:
            return StrategyExitDecision(
                True, f"stale_{age_days}d", exit_price=current_price, status="closed"
            )

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
        """Weather replay deferred -- return None."""
        return None
