"""OpenWeatherMap feed adapter.

Polls the OpenWeatherMap forecast API on a 5-minute cadence and stores
normalised snapshots in the ``weather_snapshots`` table.  Intended as
a low-complexity experimental feed for validating the multi-strategy
framework end-to-end.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import FeedAdapter

logger = logging.getLogger(__name__)


class WeatherFeedAdapter(FeedAdapter):
    """OpenWeatherMap-backed feed adapter."""

    feed_id = "weather"
    display_name = "OpenWeatherMap"
    default_poll_seconds = 300  # 5 minutes
    mode = "poll"

    # Default locations to poll (can be extended via config)
    DEFAULT_LOCATIONS = ("london", "new_york", "tokyo")

    def validate_config(self) -> bool:
        """Return True if ``OPENWEATHER_API_KEY`` env var is set."""
        key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
        return bool(key)

    def warmup(self, conn) -> None:
        """Ensure the ``weather_snapshots`` table exists."""
        from db import ensure_weather_snapshots_table
        ensure_weather_snapshots_table(conn)

    def tick(self, conn) -> None:
        """Fetch forecasts for all configured locations and store snapshots."""
        api_key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
        if not api_key:
            logger.warning("weather feed tick skipped: OPENWEATHER_API_KEY not set")
            return

        from db import insert_weather_snapshot

        instance_tag = (
            os.environ.get("INSTANCE_TAG", "").strip()
            or os.environ.get("SNIPER_INSTANCE_TAG", "").strip()
            or "default"
        )
        locations = self._get_locations()

        for location in locations:
            try:
                payload = self._fetch_forecast(api_key, location)
                if payload is None:
                    continue
                forecast_ts = self._extract_forecast_ts(payload)
                insert_weather_snapshot(
                    conn,
                    instance_tag=instance_tag,
                    feed_id=self.feed_id,
                    location_key=location,
                    forecast_ts=forecast_ts,
                    payload_json=json.dumps(payload),
                )
                logger.info("weather snapshot stored: location=%s forecast_ts=%d", location, forecast_ts)
            except Exception:
                logger.exception("weather feed tick failed for location=%s", location)

    # -- internal helpers -------------------------------------------------------

    def _get_locations(self) -> List[str]:
        """Return the list of location keys to poll."""
        env_locations = os.environ.get("WEATHER_LOCATIONS", "").strip()
        if env_locations:
            return [loc.strip().lower() for loc in env_locations.split(",") if loc.strip()]
        return list(self.DEFAULT_LOCATIONS)

    @staticmethod
    def _fetch_forecast(api_key: str, location: str) -> Dict[str, Any] | None:
        """Fetch 5-day/3-hour forecast from OpenWeatherMap.

        Returns the parsed JSON dict or None on failure.
        """
        import urllib.parse
        import urllib.request
        import urllib.error

        # Normalise location: underscores -> spaces, percent-encode for URL safety
        query = urllib.parse.quote(location.replace("_", " "))
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?q={query}&appid={api_key}&units=metric"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sniper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.error("weather API HTTP error for %s: %s", location, exc.code)
            return None
        except Exception:
            logger.exception("weather API request failed for %s", location)
            return None

    @staticmethod
    def _extract_forecast_ts(payload: Dict[str, Any]) -> int:
        """Extract the first forecast timestamp from the API response.

        Falls back to current UTC time if the payload structure is unexpected.
        """
        try:
            first_entry = payload.get("list", [{}])[0]
            return int(first_entry.get("dt", int(datetime.now(timezone.utc).timestamp())))
        except (IndexError, TypeError, ValueError):
            return int(datetime.now(timezone.utc).timestamp())
