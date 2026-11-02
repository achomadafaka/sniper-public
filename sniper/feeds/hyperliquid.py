"""Hyperliquid perpetual futures feed adapter (REST polling).

Wraps the existing ``feeds/hyperliquid_feed.py`` module as a proper
``FeedAdapter`` so strategy instances can declare ``required_feeds = ("hyperliquid",)``.

Data is fetched via REST every 5 minutes and stored in ``hyperliquid_snapshots``.
"""

from __future__ import annotations

import logging

from .base import FeedAdapter

logger = logging.getLogger(__name__)


class HyperliquidFeedAdapter(FeedAdapter):
    """Poll-mode adapter that fetches Hyperliquid mark prices, funding, OI."""

    feed_id = "hyperliquid"
    display_name = "Hyperliquid Perps"
    default_poll_seconds = 300
    mode = "poll"

    def validate_config(self) -> bool:
        # Hyperliquid info API is public — no keys needed.
        return True

    def warmup(self, conn) -> None:
        from sniper.persistence.db_snapshots import ensure_hyperliquid_snapshots_table

        ensure_hyperliquid_snapshots_table(conn)

    def tick(self, conn) -> None:
        try:
            from sniper.feeds.hyperliquid_feed import (
                fetch_meta_and_asset_ctxs,
                parse_snapshots,
                store_snapshots,
                write_brain_summary,
                TRACKED_ASSETS,
            )

            universe, asset_ctxs = fetch_meta_and_asset_ctxs()
            snapshots = parse_snapshots(universe, asset_ctxs, tracked=TRACKED_ASSETS)
            if not snapshots:
                logger.debug("Hyperliquid feed: no snapshots parsed")
                return

            stored = store_snapshots(conn, snapshots)
            write_brain_summary(snapshots)
            logger.info("Hyperliquid feed: %d assets, %d stored", len(snapshots), stored)
        except Exception:
            logger.exception("Hyperliquid feed tick failed")
