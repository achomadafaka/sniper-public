"""Commodity price feed adapter.

Polls commodity prices (oil, gold, gas) on a 30-minute cadence and stores
snapshots in the ``commodity_snapshots`` table.  Primary provider is yfinance
(no API key needed).  Per-symbol failure is non-fatal.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from sniper.settings.web import INSTANCE_TAG

from .base import FeedAdapter

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("BZ=F", "CL=F", "GC=F", "NG=F")


class CommoditiesFeedAdapter(FeedAdapter):
    """yfinance-backed commodity price feed."""

    feed_id = "commodities"
    display_name = "Commodity Prices"
    default_poll_seconds = 1800  # 30 minutes
    mode = "poll"

    def validate_config(self) -> bool:
        """Always True — yfinance needs no API key."""
        return True

    def warmup(self, conn) -> None:
        from db import ensure_commodity_snapshots_table
        ensure_commodity_snapshots_table(conn)

    def tick(self, conn) -> None:
        from db import insert_commodity_snapshot

        symbols = self._get_symbols()
        results = self._fetch_prices(symbols)

        stored = 0
        for item in results:
            try:
                row_id = insert_commodity_snapshot(
                    conn,
                    instance_tag=INSTANCE_TAG,
                    symbol=item["symbol"],
                    price_usd=item["price_usd"],
                    market_ts=item["market_ts"],
                    provider="yfinance",
                    volume=item.get("volume"),
                    payload_json=json.dumps(item.get("payload", {})),
                )
                if row_id is not None:
                    stored += 1
            except Exception:
                logger.exception("commodity tick: failed to store %s", item.get("symbol"))

        if stored:
            logger.info("commodity snapshots stored: %d/%d symbols", stored, len(symbols))
        elif results:
            logger.debug("commodity tick: all snapshots were duplicates")
        else:
            logger.warning("commodity tick: no prices fetched for %d symbols", len(symbols))

    def _get_symbols(self) -> List[str]:
        env_symbols = os.environ.get("COMMODITY_SYMBOLS", "").strip()
        if env_symbols:
            return [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
        return list(DEFAULT_SYMBOLS)

    @staticmethod
    def _fetch_prices(symbols: List[str]) -> List[Dict[str, Any]]:
        """Fetch current prices via yfinance.  Returns list of dicts.

        Per-symbol failure is non-fatal: log and skip.
        """
        results: List[Dict[str, Any]] = []
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance not installed — cannot fetch commodity prices")
            return results

        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                info = ticker.fast_info
                price = float(getattr(info, "last_price", 0) or 0)
                if price <= 0:
                    logger.warning("commodity: no price for %s", symbol)
                    continue
                raw_ts = getattr(info, "regularMarketTime", None)
                if raw_ts:
                    market_ts = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat()
                else:
                    market_ts = datetime.now(timezone.utc).isoformat()
                results.append({
                    "symbol": symbol,
                    "price_usd": price,
                    "market_ts": market_ts,
                    "volume": float(getattr(info, "last_volume", 0) or 0) or None,
                    "payload": {
                        "last_price": price,
                        "currency": getattr(info, "currency", "USD"),
                    },
                })
            except Exception:
                logger.exception("commodity: failed to fetch %s", symbol)

        return results
