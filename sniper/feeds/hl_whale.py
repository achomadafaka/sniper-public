"""Hyperliquid whale position polling feed adapter."""
from __future__ import annotations
import logging
from .base import FeedAdapter

logger = logging.getLogger(__name__)


class HlWhaleFeedAdapter(FeedAdapter):
    """Poll-mode adapter that fetches HL whale positions and liquidation proximity."""

    feed_id = "hl_whale"
    display_name = "HL Whale Positions"
    default_poll_seconds = 60
    mode = "poll"

    def validate_config(self) -> bool:
        return True

    def warmup(self, conn) -> None:
        from sniper.persistence.db_hyperliquid import ensure_hl_tracked_wallets_table, ensure_hl_whale_positions_table
        ensure_hl_tracked_wallets_table(conn)
        ensure_hl_whale_positions_table(conn)

    def tick(self, conn) -> None:
        try:
            from archive.legacy.hl_stream.hl_whale_poller import poll_once
            poll_once(conn)
        except Exception:
            logger.exception("HL whale poller tick failed")
