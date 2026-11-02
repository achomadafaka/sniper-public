from __future__ import annotations

from .base import FeedAdapter


class PolymarketFeedAdapter(FeedAdapter):
    """Thin wrapper around the existing Polymarket CLOB ingestion pipeline.

    Data ingestion is handled by ``stream_low_buys.py`` (WebSocket streamer)
    and table creation by ``init_db()`` — this adapter is intentionally a
    no-op so that strategy instances can declare a dependency on the
    ``"polymarket"`` feed without any new moving parts.
    """

    feed_id = "polymarket"
    display_name = "Polymarket CLOB"
    default_poll_seconds = 30
    mode = "stream"

    def validate_config(self) -> bool:
        # Polymarket CLOB is public; no API keys required for read access.
        return True

    def warmup(self, conn) -> None:
        # Existing init_db() handles all table creation.
        pass

    def tick(self, conn) -> None:
        # Existing stream_low_buys.py handles data ingestion.
        pass
