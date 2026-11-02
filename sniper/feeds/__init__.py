"""Feed adapter package."""

from .base import FeedAdapter
from .registry import get_feed_adapter, list_feed_adapters

__all__ = ["FeedAdapter", "get_feed_adapter", "list_feed_adapters"]
