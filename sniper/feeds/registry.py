from __future__ import annotations

from typing import Dict, List, Optional, Type

from .base import FeedAdapter
from .commodities import CommoditiesFeedAdapter
from .hl_whale import HlWhaleFeedAdapter
from .hyperliquid import HyperliquidFeedAdapter
from .hyperliquid_ws import HyperliquidWsFeedAdapter
from .news import NewsFeedAdapter
from .odds import OddsFeedAdapter
from .polymarket import PolymarketFeedAdapter
from .weather import WeatherFeedAdapter


_ADAPTER_CLASSES: Dict[str, Type[FeedAdapter]] = {
    PolymarketFeedAdapter.feed_id: PolymarketFeedAdapter,
    WeatherFeedAdapter.feed_id: WeatherFeedAdapter,
    CommoditiesFeedAdapter.feed_id: CommoditiesFeedAdapter,
    NewsFeedAdapter.feed_id: NewsFeedAdapter,
    OddsFeedAdapter.feed_id: OddsFeedAdapter,
    HyperliquidFeedAdapter.feed_id: HyperliquidFeedAdapter,
    HyperliquidWsFeedAdapter.feed_id: HyperliquidWsFeedAdapter,
    HlWhaleFeedAdapter.feed_id: HlWhaleFeedAdapter,
}


def get_feed_adapter(feed_id: str) -> Optional[FeedAdapter]:
    """Return a **fresh** adapter instance for *feed_id*, or ``None`` if unknown."""
    cls = _ADAPTER_CLASSES.get(str(feed_id or "").strip().lower())
    if cls is None:
        return None
    return cls()


def list_feed_adapters() -> List[FeedAdapter]:
    """Return fresh instances of all registered feed adapters."""
    return [cls() for cls in _ADAPTER_CLASSES.values()]
