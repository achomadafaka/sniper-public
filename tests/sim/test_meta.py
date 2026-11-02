# tests/sim/test_meta.py
import json
from sniper.sim.collector.meta import (
    parse_gamma_market,
    filter_and_classify,
    write_market_snapshot,
    build_metadata_events,
    detect_resolutions,
    _has_clear_winner,
)


def test_parse_gamma_market():
    raw = {
        "conditionId": "0xabc123",
        "question": "US strikes Iran by March 31?",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.25", "0.75"]),
        "clobTokenIds": json.dumps(["token_yes_123", "token_no_456"]),
        "volume": "150000",
        "volume24hr": "5000",
        "active": True,
        "closed": False,
        "endDate": "2026-06-01T00:00:00Z",
        "feesEnabled": True,
        "enableOrderBook": True,
        "fee": "0.02",
    }
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["condition_id"] == "0xabc123"
    assert result["question"] == "US strikes Iran by March 31?"
    assert result["outcomes"] == ["Yes", "No"]
    assert result["tokens"] == [
        {"token_id": "token_yes_123", "label": "Yes", "index": 0},
        {"token_id": "token_no_456", "label": "No", "index": 1},
    ]
    assert result["volume"] == 150000.0
    assert result["market_type"] == "geopolitical"
    assert result["market_id"] == "pm:0xabc123"
    assert result["outcome_prices"] == ["0.25", "0.75"]
    assert result["fees_enabled"] is True
    assert result["enable_order_book"] is True
    assert result["fee"] == "0.02"


def test_parse_gamma_market_keeps_fee_flag_distinct_from_order_book_flag():
    raw = {
        "conditionId": "0xfee123",
        "question": "BTC above 100K?",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.25", "0.75"]),
        "clobTokenIds": json.dumps(["token_yes_123", "token_no_456"]),
        "volume": "150000",
        "active": True,
        "closed": False,
        "endDate": "2026-06-01T00:00:00Z",
        "feesEnabled": False,
        "enableOrderBook": True,
    }
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["fees_enabled"] is False
    assert result["enable_order_book"] is True


def test_parse_gamma_market_returns_none_for_missing_condition():
    assert parse_gamma_market({}) is None


def test_filter_and_classify():
    # Use a relative future date so the test isn't clock-dependent.
    # The original hardcoded "2026-05-01T00:00:00Z" silently expired post-midnight
    # UTC and made UniverseFilter reject every candidate (delta < 0).
    from datetime import datetime, timedelta, timezone
    end_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = [
        {
            "conditionId": "0x1",
            "question": "BTC above 100K?",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps(["t1", "t2"]),
            "volume": "50000",
            "active": True,
            "closed": False,
            "endDate": end_date,
        },
        {
            "conditionId": "0x2",
            "question": "Low volume market",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps(["t3", "t4"]),
            "volume": "500",  # below threshold
            "active": True,
            "closed": False,
            "endDate": end_date,
        },
    ]
    result = filter_and_classify(markets, max_markets=10)
    assert len(result) == 1
    assert result[0]["condition_id"] == "0x1"


def test_write_market_snapshot(tmp_path):
    markets = [{"condition_id": "0x1", "tokens": [], "market_type": "crypto_threshold"}]
    path = tmp_path / "markets_snapshot.json"
    write_market_snapshot(str(path), markets)
    assert path.exists()
    assert json.loads(path.read_text()) == markets


def test_build_market_lifecycle_events():
    markets = [
        {
            "market_id": "pm:0x1",
            "condition_id": "0x1",
            "question": "BTC above 100K?",
            "outcomes": ["Yes", "No"],
            "outcome_prices": ["1", "0"],
            "tokens": [{"token_id": "t1", "label": "Yes", "index": 0}],
            "volume": 50000.0,
            "active": True,
            "closed": True,
            "end_date": "2026-05-01T00:00:00Z",
            "market_type": "crypto_threshold",
        }
    ]
    events = build_metadata_events(markets, ingest_seq_start=10, capture_ts="2026-04-01T00:00:00.000Z")
    assert len(events) == 2
    assert events[0].event_type.value == "market_metadata"
    assert events[1].event_type.value == "market_resolved"
    assert events[0].market_id == "pm:0x1"


def test_detect_resolutions_finds_newly_closed():
    previously_known = {
        "0x1": {"condition_id": "0x1", "closed": False, "outcome_prices": []},
        "0x2": {"condition_id": "0x2", "closed": False, "outcome_prices": []},
    }
    current = [
        {"condition_id": "0x1", "closed": True, "outcome_prices": ["1", "0"]},  # newly resolved
        {"condition_id": "0x2", "closed": False, "outcome_prices": []},  # still active
        {"condition_id": "0x3", "closed": True, "outcome_prices": ["0", "1"]},  # new and closed, not previously known
    ]
    resolved = detect_resolutions(current, previously_known)
    assert len(resolved) == 1
    assert resolved[0]["condition_id"] == "0x1"


def test_detect_resolutions_ignores_already_closed():
    previously_known = {
        "0x1": {"condition_id": "0x1", "closed": True, "outcome_prices": ["1", "0"]},
    }
    current = [
        {"condition_id": "0x1", "closed": True, "outcome_prices": ["1", "0"]},
    ]
    resolved = detect_resolutions(current, previously_known)
    assert len(resolved) == 0


def test_detect_resolutions_rejects_junk_prices():
    """Markets with outcome_prices=["0","0"] are not valid resolutions."""
    previously_known = {
        "0x1": {"condition_id": "0x1", "closed": False, "outcome_prices": []},
    }
    current = [
        {"condition_id": "0x1", "closed": True, "outcome_prices": ["0", "0"]},
    ]
    resolved = detect_resolutions(current, previously_known)
    assert len(resolved) == 0


def test_has_clear_winner():
    assert _has_clear_winner(["1", "0"]) is True
    assert _has_clear_winner(["0", "1"]) is True
    assert _has_clear_winner(["0.99", "0.01"]) is True
    assert _has_clear_winner(["0.995", "0.005"]) is True
    assert _has_clear_winner(["0", "0"]) is False
    assert _has_clear_winner(["0.5", "0.5"]) is False
    assert _has_clear_winner([]) is False
    assert _has_clear_winner(None) is False
