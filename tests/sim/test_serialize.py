import json
from sniper.sim.schema import RawEvent, EventType
from sniper.sim.serialize import event_to_jsonl, jsonl_to_event


def _sample_event() -> RawEvent:
    return RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=42,
        payload={"asset_id": "123", "price": 0.65, "size": 100.5, "side": "buy"},
        asset_id="123",
        outcome_index=0,
        exchange_ts="2026-04-01T14:23:01.100Z",
    )


def test_round_trip():
    original = _sample_event()
    line = event_to_jsonl(original)
    restored = jsonl_to_event(line)
    assert restored.venue == original.venue
    assert restored.market_id == original.market_id
    assert restored.event_type == original.event_type
    assert restored.capture_ts == original.capture_ts
    assert restored.ingest_seq == original.ingest_seq
    assert restored.asset_id == original.asset_id
    assert restored.outcome_index == original.outcome_index
    assert restored.exchange_ts == original.exchange_ts
    assert restored.venue_seq == original.venue_seq  # None
    assert restored.payload == original.payload


def test_jsonl_is_single_line():
    line = event_to_jsonl(_sample_event())
    assert "\n" not in line


def test_jsonl_is_valid_json():
    line = event_to_jsonl(_sample_event())
    parsed = json.loads(line)
    assert parsed["venue"] == "polymarket"
    assert parsed["event_type"] == "trade"
    assert parsed["ingest_seq"] == 42


def test_nullable_fields_omitted_when_none():
    evt = RawEvent(
        venue="kalshi",
        market_id="kalshi:KXBTC-26MAR31-B85000",
        venue_market_id="KXBTC-26MAR31-B85000",
        market_type="crypto_threshold",
        event_type=EventType.MARKET_METADATA,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"title": "BTC above 85K"},
    )
    line = event_to_jsonl(evt)
    parsed = json.loads(line)
    assert "asset_id" not in parsed
    assert "exchange_ts" not in parsed
    assert "venue_seq" not in parsed
