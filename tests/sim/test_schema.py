from sniper.sim.schema import RawEvent, EventType


def test_raw_event_required_fields():
    evt = RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"asset_id": "123", "price": 0.65, "size": 100, "side": "buy"},
    )
    assert evt.venue == "polymarket"
    assert evt.event_type == EventType.TRADE
    assert evt.ingest_seq == 1
    assert evt.asset_id is None
    assert evt.outcome_index is None
    assert evt.exchange_ts is None
    assert evt.venue_seq is None


def test_raw_event_optional_fields():
    evt = RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.BOOK_SNAPSHOT,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=2,
        payload={"bids": [], "asks": []},
        asset_id="36272374199536",
        outcome_index=0,
        exchange_ts="2026-04-01T14:23:01.100Z",
        venue_seq=42,
    )
    assert evt.asset_id == "36272374199536"
    assert evt.outcome_index == 0
    assert evt.venue_seq == 42


def test_event_type_enum():
    assert EventType.BOOK_SNAPSHOT.value == "book_snapshot"
    assert EventType.BOOK_DELTA.value == "book_delta"
    assert EventType.TRADE.value == "trade"
    assert EventType.BEST_BBA.value == "best_bba"
    assert EventType.MARKET_METADATA.value == "market_metadata"
    assert EventType.MARKET_RESOLVED.value == "market_resolved"


# --- VenueAdapter protocol tests ---

from sniper.sim.venue import VenueAdapter


def test_venue_adapter_is_runtime_checkable_protocol():
    """VenueAdapter must be a runtime-checkable Protocol."""
    assert isinstance(VenueAdapter, type)
    class NotAnAdapter:
        pass
    assert not isinstance(NotAnAdapter(), VenueAdapter)


class FakeAdapter:
    """Minimal conformance test — implements all required methods."""
    venue_name = "fake"

    def make_market_id(self, venue_native_id: str) -> str:
        return f"fake:{venue_native_id}"

    def classify_market(self, identifier: str) -> str:
        return "other"

    def normalize_price(self, raw_price: float) -> float:
        return raw_price

    def parse_event(self, channel: str, raw_msg: dict) -> RawEvent | list[RawEvent]:
        return RawEvent(
            venue="fake", market_id="fake:test", venue_market_id="test",
            market_type="other", event_type=EventType.TRADE,
            capture_ts="2026-01-01T00:00:00.000Z", ingest_seq=0,
            payload=raw_msg,
        )


def test_fake_adapter_conforms_to_protocol():
    adapter = FakeAdapter()
    assert isinstance(adapter, VenueAdapter)
    assert adapter.venue_name == "fake"
    assert adapter.make_market_id("abc") == "fake:abc"
    assert adapter.classify_market("something") == "other"
    assert adapter.normalize_price(0.65) == 0.65
    result = adapter.parse_event("trade", {"price": 0.5})
    assert isinstance(result, RawEvent)


def test_incomplete_adapter_does_not_conform():
    class MissingMethods:
        venue_name = "broken"
    assert not isinstance(MissingMethods(), VenueAdapter)


# --- Classifier contract tests ---

from sniper.sim.ids import make_market_id
from sniper.sim.market_type import classify, group


def test_classify_polymarket_returns_string():
    assert classify("polymarket", "US strikes Iran by Feb 28?") == "geopolitical"


def test_classify_kalshi_returns_string():
    assert classify("kalshi", "KXNFLGAME-26JAN11BUFJAC-BUF") == "sports_nfl"


def test_classify_venues_agree_on_coarse_group():
    pm = classify("polymarket", "Fed cuts interest rate?")
    kalshi = classify("kalshi", "KXFEDRATE-26MAY07-T425")
    assert group(pm) == group(kalshi) == "economics"


def test_group_maps_all_known_types():
    known_types = [
        "crypto_threshold", "crypto_updown", "crypto_launch", "valuation_target",
        "matchup", "sports_spread_ou", "sports_nfl", "sports_nba",
        "social_media", "geopolitical", "politics_us",
        "economics_fed", "economics_cpi", "weather_temp",
        "deadline_event", "timebound_event", "achievement",
    ]
    for mt in known_types:
        assert group(mt) != "other", f"{mt} should have a defined group"


def test_classify_unknown_returns_other():
    assert classify("polymarket", "Something nobody predicted") == "other"
    assert group("other") == "other"


def test_classifier_output_valid_in_raw_event():
    mt = classify("polymarket", "US strikes Iran by Feb 28?")
    evt = RawEvent(
        venue="polymarket", market_id=make_market_id("polymarket", "0xabc"),
        venue_market_id="0xabc", market_type=mt,
        event_type=EventType.TRADE, capture_ts="2026-01-01T00:00:00.000Z",
        ingest_seq=0, payload={},
    )
    assert evt.market_type == "geopolitical"
    assert group(evt.market_type) == "geopolitical"
