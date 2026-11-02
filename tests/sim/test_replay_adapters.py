# tests/sim/test_replay_adapters.py
import json
import os
import tempfile

from sniper.sim.compact.writer import ParquetTableWriter
from sniper.sim.episodes.builder import build_episode
from sniper.sim.replay.adapters import PolymarketAdapter, EpisodeData, TimelineEvent


def _build_episode_fixture(tmpdir: str) -> str:
    """Build a complete episode via the 2A→2B pipeline, return episode_dir."""
    normalized_dir = os.path.join(tmpdir, "normalized")
    episodes_dir = os.path.join(tmpdir, "episodes")
    writer = ParquetTableWriter(base_dir=normalized_dir)
    date = "2026-04-03"

    writer.append("trades", date, [
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.65, "size": 100.0, "side": "BUY",
            "fee_rate_bps": 200, "transaction_hash": "0xabc",
            "market_type": "geopolitical", "venue": "polymarket",
        },
    ])
    writer.append("quotes_top", date, [
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": 0.60, "best_ask": 0.65,
            "bid_size": 500.0, "ask_size": 800.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T12:30:00.000Z",
            "exchange_ts": "2026-04-03T12:30:00.000Z",
            "ingest_seq": 3, "best_bid": 0.70, "best_ask": 0.75,
            "bid_size": 400.0, "ask_size": 600.0,
            "source_event": "book_delta", "venue": "polymarket",
        },
    ])
    writer.append("market_lifecycle", date, [
        {
            "market_id": "pm:0x1", "capture_ts": "2026-04-03T10:00:00.000Z",
            "ingest_seq": 0, "event_type": "market_metadata",
            "market_type": "geopolitical",
            "payload": json.dumps({
                "question": "Will it happen?", "outcomes": ["Yes", "No"],
                "tokens": [
                    {"token_id": "tok_yes", "label": "Yes", "index": 0},
                    {"token_id": "tok_no", "label": "No", "index": 1},
                ],
                "fees_enabled": True, "enable_order_book": True, "fee": "0.02",
            }),
            "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "capture_ts": "2026-04-03T18:00:00.000Z",
            "ingest_seq": 99, "event_type": "market_resolved",
            "market_type": "geopolitical",
            "payload": json.dumps({
                "outcome_prices": ["0.01", "0.99"],
                "resolved_ts": "2026-04-03T18:00:00.000Z",
            }),
            "venue": "polymarket",
        },
    ])
    writer.flush()

    summary = build_episode(
        market_id="pm:0x1",
        normalized_dir=normalized_dir,
        episodes_dir=episodes_dir,
        fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
    )
    return summary.episode_dir


def test_adapter_loads_episode():
    """PolymarketAdapter loads an episode into EpisodeData."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        adapter = PolymarketAdapter()
        data = adapter.load(episode_dir)

        assert isinstance(data, EpisodeData)
        assert data.market_id == "pm:0x1"
        assert data.metadata["question"] == "Will it happen?"
        assert data.settlement["outcome"] == "No"


def test_adapter_timeline_sorted():
    """Timeline events are in chronological order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        data = PolymarketAdapter().load(episode_dir)

        timestamps = [(e.ts, e.ingest_seq) for e in data.timeline]
        assert timestamps == sorted(timestamps)


def test_adapter_timeline_has_quotes_and_trades():
    """Timeline contains both quote and trade events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        data = PolymarketAdapter().load(episode_dir)

        kinds = {e.kind for e in data.timeline}
        assert "quote" in kinds
        assert "trade" in kinds
        assert len(data.timeline) == 3  # 2 quotes + 1 trade


def test_adapter_timeline_events_have_ticker():
    """Every timeline event has a ticker from outcome resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        data = PolymarketAdapter().load(episode_dir)

        for evt in data.timeline:
            assert evt.ticker == "Yes"  # all events are for tok_yes


def test_adapter_fee_config():
    """Fee config extracted from metadata."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        data = PolymarketAdapter().load(episode_dir)

        assert data.fee_fn_args["fees_enabled"] is True
        assert data.fee_fn_args["token_fee_rates"]["tok_yes"] == 0.02
