"""Tests for the feed adapter base class and registry."""

import sqlite3

import pytest

from sniper.feeds.base import FeedAdapter
from sniper.feeds.polymarket import PolymarketFeedAdapter
from sniper.feeds.registry import get_feed_adapter, list_feed_adapters


# ---------------------------------------------------------------------------
# FeedAdapter ABC
# ---------------------------------------------------------------------------

class TestFeedAdapterABC:
    def test_cannot_instantiate_directly(self):
        """FeedAdapter is abstract — direct instantiation must raise."""
        with pytest.raises(TypeError):
            FeedAdapter()

    def test_concrete_subclass_must_implement_tick(self):
        """A subclass that doesn't implement tick() cannot be instantiated."""

        class IncompleteFeed(FeedAdapter):
            feed_id = "incomplete"

        with pytest.raises(TypeError):
            IncompleteFeed()

    def test_concrete_subclass_with_tick_is_ok(self):
        """A subclass that implements tick() can be instantiated."""

        class CompleteFeed(FeedAdapter):
            feed_id = "complete"

            def tick(self, conn) -> None:
                pass

        adapter = CompleteFeed()
        assert adapter.feed_id == "complete"

    def test_defaults(self):
        """Default values are sensible."""

        class MinimalFeed(FeedAdapter):
            feed_id = "minimal"

            def tick(self, conn) -> None:
                pass

        adapter = MinimalFeed()
        assert adapter.default_poll_seconds == 60
        assert adapter.mode == "poll"
        assert adapter.display_name == ""
        assert adapter.validate_config() is True

    def test_warmup_is_noop(self):
        """Default warmup() does nothing and doesn't raise."""

        class MinimalFeed(FeedAdapter):
            feed_id = "minimal"

            def tick(self, conn) -> None:
                pass

        adapter = MinimalFeed()
        conn = sqlite3.connect(":memory:")
        adapter.warmup(conn)  # should not raise
        conn.close()

    def test_start_stop_are_noops(self):
        """Default start()/stop() do nothing and don't raise."""

        class MinimalFeed(FeedAdapter):
            feed_id = "minimal"

            def tick(self, conn) -> None:
                pass

        adapter = MinimalFeed()
        adapter.start()
        adapter.stop()


# ---------------------------------------------------------------------------
# PolymarketFeedAdapter
# ---------------------------------------------------------------------------

class TestPolymarketFeedAdapter:
    def test_feed_id(self):
        adapter = PolymarketFeedAdapter()
        assert adapter.feed_id == "polymarket"

    def test_display_name(self):
        adapter = PolymarketFeedAdapter()
        assert adapter.display_name == "Polymarket CLOB"

    def test_mode(self):
        adapter = PolymarketFeedAdapter()
        assert adapter.mode == "stream"

    def test_default_poll_seconds(self):
        adapter = PolymarketFeedAdapter()
        assert adapter.default_poll_seconds == 30

    def test_validate_config(self):
        adapter = PolymarketFeedAdapter()
        assert adapter.validate_config() is True

    def test_warmup_callable(self):
        adapter = PolymarketFeedAdapter()
        conn = sqlite3.connect(":memory:")
        adapter.warmup(conn)  # should not raise
        conn.close()

    def test_tick_callable(self):
        adapter = PolymarketFeedAdapter()
        conn = sqlite3.connect(":memory:")
        adapter.tick(conn)  # should not raise
        conn.close()

    def test_is_feed_adapter(self):
        adapter = PolymarketFeedAdapter()
        assert isinstance(adapter, FeedAdapter)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestFeedRegistry:
    def test_get_polymarket(self):
        adapter = get_feed_adapter("polymarket")
        assert adapter is not None
        assert adapter.feed_id == "polymarket"

    def test_get_unknown_returns_none(self):
        assert get_feed_adapter("unknown") is None

    def test_get_none_returns_none(self):
        assert get_feed_adapter(None) is None

    def test_get_empty_returns_none(self):
        assert get_feed_adapter("") is None

    def test_get_is_case_insensitive(self):
        adapter = get_feed_adapter("POLYMARKET")
        assert adapter is not None
        assert adapter.feed_id == "polymarket"

    def test_get_strips_whitespace(self):
        adapter = get_feed_adapter("  polymarket  ")
        assert adapter is not None
        assert adapter.feed_id == "polymarket"

    def test_list_includes_polymarket(self):
        adapters = list_feed_adapters()
        ids = [a.feed_id for a in adapters]
        assert "polymarket" in ids

    def test_list_returns_feed_adapter_instances(self):
        adapters = list_feed_adapters()
        assert len(adapters) >= 1
        for a in adapters:
            assert isinstance(a, FeedAdapter)
