# tests/sim/test_compact_integration.py
import gzip
import json
import os
import tempfile

import pyarrow.parquet as pq

from sniper.sim.compact.run import compact_directory
from sniper.sim.schema import RawEvent, EventType
from sniper.sim.serialize import event_to_jsonl


def _write_fixture(raw_dir: str) -> str:
    """Write a synthetic .jsonl.gz file with known events."""
    date_dir = os.path.join(raw_dir, "polymarket", "2026-04-03")
    os.makedirs(date_dir)
    gz_path = os.path.join(date_dir, "12.jsonl.gz")

    events = [
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.TRADE,
                 capture_ts="2026-04-03T12:00:00.000Z", ingest_seq=1,
                 payload={"price": "0.65", "size": "100", "side": "BUY"},
                 asset_id="asset_yes", exchange_ts="2026-04-03T12:00:00.000Z"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.BOOK_DELTA,
                 capture_ts="2026-04-03T12:00:01.000Z", ingest_seq=2,
                 payload={"price": "0.62", "size": "500", "side": "BUY",
                          "best_bid": "0.61", "best_ask": "0.64"},
                 asset_id="asset_yes"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.BOOK_SNAPSHOT,
                 capture_ts="2026-04-03T12:00:02.000Z", ingest_seq=3,
                 payload={"bids": [{"price": "0.60", "size": "1000"}],
                          "asks": [{"price": "0.65", "size": "800"}]},
                 asset_id="asset_yes"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.MARKET_METADATA,
                 capture_ts="2026-04-03T12:00:03.000Z", ingest_seq=4,
                 payload={
                     "question": "Test?", "outcomes": ["Yes", "No"],
                     "tokens": [
                         {"token_id": "tok_yes_123", "outcome": "Yes", "winner": False},
                         {"token_id": "tok_no_456", "outcome": "No", "winner": False},
                     ],
                     "fees_enabled": True, "enable_order_book": True, "fee": "0.02",
                 }),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.MARKET_RESOLVED,
                 capture_ts="2026-04-03T18:00:00.000Z", ingest_seq=5,
                 payload={
                     "outcome_prices": ["0.01", "0.99"],
                     "resolved_ts": "2026-04-03T18:00:00.000Z",
                 }),
    ]

    with gzip.open(gz_path, "wt") as f:
        for evt in events:
            f.write(event_to_jsonl(evt) + "\n")

    return gz_path


def test_compact_produces_all_tables():
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = os.path.join(tmpdir, "raw")
        out_dir = os.path.join(tmpdir, "normalized")
        manifest_path = os.path.join(tmpdir, "manifest.json")

        _write_fixture(raw_dir)
        report = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")

        assert report["files_processed"] == 1
        assert report["records_read"] == 5
        assert report["records_rejected"] == 0
        assert "trades" in report["tables"]
        assert "quotes_top" in report["tables"]
        assert "book_levels" in report["tables"]
        assert "book_deltas" in report["tables"]
        assert "market_lifecycle" in report["tables"]

        # Verify trades
        trades = pq.read_table(os.path.join(out_dir, "trades", "2026-04-03.parquet"))
        assert len(trades) == 1
        assert trades.column("price").to_pylist() == [0.65]

        # Verify quotes_top has entries from delta + snapshot
        quotes = pq.read_table(os.path.join(out_dir, "quotes_top", "2026-04-03.parquet"))
        assert len(quotes) >= 2  # at least delta + snapshot

        # Verify book_levels has all levels from snapshot
        levels = pq.read_table(os.path.join(out_dir, "book_levels", "2026-04-03.parquet"))
        assert len(levels) == 2  # 1 bid + 1 ask

        # Verify market_lifecycle preserves 2B-critical fields through JSON round-trip
        lifecycle = pq.read_table(os.path.join(out_dir, "market_lifecycle", "2026-04-03.parquet"))
        assert len(lifecycle) == 2  # metadata + resolved
        event_types = lifecycle.column("event_type").to_pylist()
        assert "market_metadata" in event_types
        assert "market_resolved" in event_types

        # Find the metadata row and verify 2B-critical fields survive
        payloads = lifecycle.column("payload").to_pylist()
        meta_payload = json.loads(payloads[event_types.index("market_metadata")])
        assert meta_payload["tokens"][0]["token_id"] == "tok_yes_123"
        assert meta_payload["tokens"][1]["token_id"] == "tok_no_456"
        assert meta_payload["fees_enabled"] is True
        assert meta_payload["enable_order_book"] is True
        assert meta_payload["fee"] == "0.02"

        # Verify resolution payload
        resolved_payload = json.loads(payloads[event_types.index("market_resolved")])
        assert resolved_payload["outcome_prices"] == ["0.01", "0.99"]
        assert resolved_payload["resolved_ts"] == "2026-04-03T18:00:00.000Z"


def test_compact_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = os.path.join(tmpdir, "raw")
        out_dir = os.path.join(tmpdir, "normalized")
        manifest_path = os.path.join(tmpdir, "manifest.json")

        _write_fixture(raw_dir)

        report1 = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")
        assert report1["files_processed"] == 1

        report2 = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")
        assert report2["files_processed"] == 0  # already in manifest

        # Data should be the same (not doubled)
        trades = pq.read_table(os.path.join(out_dir, "trades", "2026-04-03.parquet"))
        assert len(trades) == 1
