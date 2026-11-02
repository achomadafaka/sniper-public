from __future__ import annotations

from abc import ABC, abstractmethod


class FeedAdapter(ABC):
    """Abstract base class for data-feed adapters.

    Each feed adapter represents a single data source (e.g. Polymarket CLOB,
    a weather API, etc.).  Strategy instances declare which feed(s) they depend
    on via ``required_feeds``, and the runner initialises only the adapters
    that are actually needed.
    """

    feed_id: str = ""
    display_name: str = ""
    default_poll_seconds: int = 60
    mode: str = "poll"  # "poll" or "stream"

    # -- lifecycle hooks (override as needed) ----------------------------------

    def validate_config(self) -> bool:
        """Return True if this feed has everything it needs to run.

        Override to check for API keys, env vars, network connectivity, etc.
        """
        return True

    def warmup(self, conn) -> None:
        """Called once on startup to ensure tables exist, seed data, etc.

        *conn* is a ``sqlite3.Connection``.  Default is a no-op.
        """

    @abstractmethod
    def tick(self, conn) -> None:
        """Called each poll cycle to fetch and store data.

        *conn* is a ``sqlite3.Connection``.  Must be implemented by every
        concrete adapter (even if the implementation is a no-op for stream-mode
        feeds whose data ingestion is handled elsewhere).
        """
        raise NotImplementedError

    # -- streaming hooks (override for mode="stream" feeds) --------------------

    def start(self) -> None:
        """Start a long-lived streaming connection.  Default is a no-op."""

    def stop(self) -> None:
        """Gracefully stop the streaming connection.  Default is a no-op."""
