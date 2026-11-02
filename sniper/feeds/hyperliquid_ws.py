"""Hyperliquid WebSocket feed adapter (stream mode).

Wraps ``stream_hyperliquid.HyperliquidStreamer`` as a ``FeedAdapter`` so
strategy instances can declare ``required_feeds = ("hyperliquid_ws",)``.

Coexists with the poll-mode ``hyperliquid`` adapter — strategies can
depend on either or both. The WS adapter provides real-time trade data
while the poll adapter provides 5-minute market snapshots.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .base import FeedAdapter

logger = logging.getLogger(__name__)


class HyperliquidWsFeedAdapter(FeedAdapter):
    """Stream-mode adapter that ingests Hyperliquid trades via WebSocket."""

    feed_id = "hyperliquid_ws"
    display_name = "Hyperliquid Trades (WebSocket)"
    default_poll_seconds = 60
    mode = "stream"

    def __init__(self):
        super().__init__()
        self._streamer = None
        self._thread: Optional[threading.Thread] = None

    def validate_config(self) -> bool:
        # Hyperliquid WS is public — no keys needed.
        return True

    def warmup(self, conn) -> None:
        from sniper.persistence.db_hyperliquid import ensure_hl_trades_table

        ensure_hl_trades_table(conn)

    def start(self) -> None:
        """Spawn daemon thread running the async streamer."""
        import asyncio
        from archive.legacy.hl_stream.stream_hyperliquid import HyperliquidStreamer

        self._streamer = HyperliquidStreamer()

        def _run():
            try:
                asyncio.run(self._streamer.run())
            except Exception:
                logger.exception("HyperliquidStreamer thread died")

        self._thread = threading.Thread(
            target=_run,
            name="hl-ws-streamer",
            daemon=True,
        )
        self._thread.start()
        logger.info("HyperliquidWsFeedAdapter: streamer thread started")

    def stop(self) -> None:
        """Signal graceful shutdown of the streamer."""
        if self._streamer:
            self._streamer.request_stop()
            logger.info("HyperliquidWsFeedAdapter: stop signal sent")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def tick(self, conn) -> None:
        # Stream-mode: data ingestion handled by the WS listener thread.
        pass
