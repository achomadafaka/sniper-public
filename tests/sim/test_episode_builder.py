# tests/sim/test_episode_builder.py
import json
import os
import tempfile

import pyarrow.parquet as pq

from sniper.sim.compact.writer import ParquetTableWriter
from sniper.sim.episodes.builder import build_episode, EpisodeSummary


def _build_normalized_fixture(base_dir: str) -> None:
    """Build a complete set of normalized Parquet tables using ParquetTableWriter.

    Uses asset_id == token_id (as in production) to test the 2A→2B contract.
    """
    writer = ParquetTableWriter(base_dir=base_dir)
    date = "2026-04-03"

    # --- Trades: 2 trades for market pm:0x1 ---
    writer.append("trades", date, [
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.65, "size": 100.0, "side": "BUY",
            "fee_rate_bps": 200, "transaction_hash": "0xabc",
            "market_type": "geopolitical", "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "asset_id": "tok_no",
            "capture_ts": "2026-04-03T14:00:00.000Z",
            "exchange_ts": "2026-04-03T14:00:00.000Z",
            "ingest_seq": 10, "price": 0.35, "size": 50.0, "side": "SELL",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geopolitical", "venue": "polymarket",
        },
    ])
    # A trade for a DIFFERENT market (should be excluded)
    writer.append("trades", date, [
        {
            "market_id": "pm:0xother", "asset_id": "tok_other",
            "capture_ts": "2026-04-03T13:00:00.000Z",
            "exchange_ts": "2026-04-03T13:00:00.000Z",
            "ingest_seq": 5, "price": 0.50, "size": 200.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geopolitical", "venue": "polymarket",
        },
    ])

    # --- Quotes: 3 quotes for market pm:0x1 ---
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
            "exchange_ts": None,
            "ingest_seq": 3, "best_bid": 0.62, "best_ask": 0.66,
            "bid_size": None, "ask_size": None,
            "source_event": "book_delta", "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "asset_id": "tok_no",
            "capture_ts": "2026-04-03T13:00:00.000Z",
            "exchange_ts": "2026-04-03T13:00:00.000Z",
            "ingest_seq": 6, "best_bid": 0.34, "best_ask": 0.38,
            "bid_size": 300.0, "ask_size": 400.0,
            "source_event": "best_bba", "venue": "polymarket",
        },
    ])

    # --- Book levels (optional depth) ---
    writer.append("book_levels", date, [
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "side": "bid", "price": 0.60,
            "size": 500.0, "level_index": 0, "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "side": "ask", "price": 0.65,
            "size": 800.0, "level_index": 0, "venue": "polymarket",
        },
    ])

    # --- Market lifecycle ---
    meta_payload = json.dumps({
        "question": "Will it happen?",
        "outcomes": ["Yes", "No"],
        "tokens": [
            {"token_id": "tok_yes", "label": "Yes", "index": 0},
            {"token_id": "tok_no", "label": "No", "index": 1},
        ],
        "fees_enabled": True,
        "enable_order_book": True,
        "fee": "0.02",
    })
    resolved_payload = json.dumps({
        "outcome_prices": ["0.01", "0.99"],
        "resolved_ts": "2026-04-03T18:00:00.000Z",
    })
    writer.append("market_lifecycle", date, [
        {
            "market_id": "pm:0x1", "capture_ts": "2026-04-03T12:00:03.000Z",
            "ingest_seq": 4, "event_type": "market_metadata",
            "market_type": "geopolitical",
            "payload": meta_payload, "venue": "polymarket",
        },
        {
            "market_id": "pm:0x1", "capture_ts": "2026-04-03T18:00:00.000Z",
            "ingest_seq": 5, "event_type": "market_resolved",
            "market_type": "geopolitical",
            "payload": resolved_payload, "venue": "polymarket",
        },
    ])

    writer.flush()


def _build_minimal_fixture(base_dir: str, market_id: str = "pm:0x2",
                           fees_enabled: bool = False,
                           include_trades: bool = True,
                           include_quotes: bool = True) -> None:
    """Build a minimal normalized fixture for validation tests."""
    writer = ParquetTableWriter(base_dir=base_dir)
    date = "2026-04-03"

    if include_trades:
        writer.append("trades", date, [{
            "market_id": market_id, "asset_id": "tok_a",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.50, "size": 100.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geopolitical", "venue": "polymarket",
        }])

    if include_quotes:
        writer.append("quotes_top", date, [{
            "market_id": market_id, "asset_id": "tok_a",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": 0.48, "best_ask": 0.52,
            "bid_size": 100.0, "ask_size": 100.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        }])

    writer.append("market_lifecycle", date, [
        {
            "market_id": market_id, "capture_ts": "2026-04-03T10:00:00.000Z",
            "ingest_seq": 0, "event_type": "market_metadata",
            "market_type": "geopolitical",
            "payload": json.dumps({
                "question": "Test?", "outcomes": ["Yes", "No"],
                "tokens": [
                    {"token_id": "tok_a", "label": "Yes", "index": 0},
                    {"token_id": "tok_b", "label": "No", "index": 1},
                ],
                "fees_enabled": fees_enabled, "enable_order_book": True,
                "fee": "0.02" if fees_enabled else None,
            }),
            "venue": "polymarket",
        },
        {
            "market_id": market_id, "capture_ts": "2026-04-03T18:00:00.000Z",
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


# --- Happy path ---

def test_build_episode_creates_directory():
    """build_episode produces the expected episode directory structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        assert isinstance(summary, EpisodeSummary)
        assert summary.market_id == "pm:0x1"
        assert summary.trade_count == 2
        assert summary.quote_count == 3
        assert summary.book_depth_count == 2
        assert os.path.isdir(summary.episode_dir)


def test_episode_metadata_json():
    """metadata.json contains market info, tokens, fee metadata, token fee rates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        meta_path = os.path.join(summary.episode_dir, "metadata.json")
        assert os.path.exists(meta_path)
        with open(meta_path) as f:
            meta = json.load(f)

        assert meta["market_id"] == "pm:0x1"
        assert meta["question"] == "Will it happen?"
        assert meta["outcomes"] == ["Yes", "No"]
        assert len(meta["tokens"]) == 2
        assert meta["fees_enabled"] is True
        assert meta["fee"] == "0.02"
        assert meta["enable_order_book"] is True
        assert meta["token_fee_rates"] == {"tok_yes": 0.02, "tok_no": 0.02}


def test_episode_settlement_json():
    """settlement.json contains outcome, outcome_prices, resolved_ts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        settlement_path = os.path.join(summary.episode_dir, "settlement.json")
        assert os.path.exists(settlement_path)
        with open(settlement_path) as f:
            settlement = json.load(f)

        assert settlement["market_id"] == "pm:0x1"
        assert settlement["outcome"] == "No"
        assert settlement["outcome_prices"] == ["0.01", "0.99"]
        assert settlement["resolved_ts"] == "2026-04-03T18:00:00.000Z"


def test_episode_trades_enriched():
    """Episode trades.parquet has outcome_index and ticker columns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        trades_path = os.path.join(summary.episode_dir, "trades.parquet")
        trades = pq.read_table(trades_path)

        assert len(trades) == 2
        assert "outcome_index" in trades.column_names
        assert "ticker" in trades.column_names
        assert all(mid == "pm:0x1" for mid in trades.column("market_id").to_pylist())
        oi = trades.column("outcome_index").to_pylist()
        tickers = trades.column("ticker").to_pylist()
        asset_ids = trades.column("asset_id").to_pylist()
        for aid, idx, ticker in zip(asset_ids, oi, tickers):
            if aid == "tok_yes":
                assert idx == 0
                assert ticker == "Yes"
            elif aid == "tok_no":
                assert idx == 1
                assert ticker == "No"


def test_episode_trades_sorted():
    """Episode trades are sorted by exchange_ts, capture_ts, ingest_seq."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        trades = pq.read_table(os.path.join(summary.episode_dir, "trades.parquet"))
        seqs = trades.column("ingest_seq").to_pylist()
        assert seqs == sorted(seqs)


def test_episode_quotes_enriched():
    """Episode quotes.parquet has outcome_index and ticker columns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        quotes = pq.read_table(os.path.join(summary.episode_dir, "quotes.parquet"))
        assert len(quotes) == 3
        assert "outcome_index" in quotes.column_names
        assert "ticker" in quotes.column_names


def test_episode_quotes_sorted():
    """Episode quotes sorted by time (exchange_ts preferred, capture_ts fallback)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        quotes = pq.read_table(os.path.join(summary.episode_dir, "quotes.parquet"))
        capture_ts = quotes.column("capture_ts").to_pylist()
        assert capture_ts == sorted(capture_ts)


def test_episode_book_depth():
    """book_depth.parquet written when book_levels data exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        summary = build_episode(
            market_id="pm:0x1",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
            fee_rates={"tok_yes": 0.02, "tok_no": 0.02},
        )

        depth_path = os.path.join(summary.episode_dir, "book_depth.parquet")
        assert os.path.exists(depth_path)
        depth = pq.read_table(depth_path)
        assert len(depth) == 2
        assert "outcome_index" in depth.column_names
        assert "ticker" in depth.column_names


def test_episode_no_book_depth():
    """book_depth.parquet omitted when no book_levels data for market."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_minimal_fixture(normalized_dir, market_id="pm:0x2")

        summary = build_episode(
            market_id="pm:0x2",
            normalized_dir=normalized_dir,
            episodes_dir=episodes_dir,
        )

        depth_path = os.path.join(summary.episode_dir, "book_depth.parquet")
        assert not os.path.exists(depth_path)
        assert summary.book_depth_count == 0


# --- Validation tests ---

def test_build_episode_rejects_no_trades():
    """Episode with 0 trades for the market is rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_minimal_fixture(normalized_dir, include_trades=False)

        try:
            build_episode("pm:0x2", normalized_dir, episodes_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "trade" in str(e).lower()


def test_build_episode_rejects_no_quotes():
    """Episode with 0 quotes for the market is rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_minimal_fixture(normalized_dir, include_quotes=False)

        try:
            build_episode("pm:0x2", normalized_dir, episodes_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "quote" in str(e).lower()


def test_build_episode_rejects_unresolved_asset():
    """Trade with asset_id not in token lookup → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        writer = ParquetTableWriter(base_dir=normalized_dir)
        date = "2026-04-03"

        writer.append("trades", date, [{
            "market_id": "pm:0x1", "asset_id": "unknown_tok",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.65, "size": 100.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geopolitical", "venue": "polymarket",
        }])
        writer.append("quotes_top", date, [{
            "market_id": "pm:0x1", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": 0.60, "best_ask": 0.65,
            "bid_size": 100.0, "ask_size": 100.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        }])
        writer.append("market_lifecycle", date, [
            {
                "market_id": "pm:0x1", "capture_ts": "2026-04-03T10:00:00.000Z",
                "ingest_seq": 0, "event_type": "market_metadata",
                "market_type": "geopolitical",
                "payload": json.dumps({
                    "question": "Test?", "outcomes": ["Yes", "No"],
                    "tokens": [
                        {"token_id": "tok_yes", "label": "Yes", "index": 0},
                        {"token_id": "tok_no", "label": "No", "index": 1},
                    ],
                    "fees_enabled": False, "enable_order_book": True, "fee": None,
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

        try:
            build_episode("pm:0x1", normalized_dir, episodes_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "outcome_index" in str(e).lower() or "asset" in str(e).lower()


def test_build_episode_rejects_missing_fee_rates():
    """Fee-enabled market without fee_rates → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        try:
            build_episode("pm:0x1", normalized_dir, episodes_dir, fee_rates=None)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "fee" in str(e).lower()


def test_build_episode_rejects_incomplete_fee_rates():
    """Fee-enabled market with fee_rates missing an asset → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_normalized_fixture(normalized_dir)

        try:
            build_episode("pm:0x1", normalized_dir, episodes_dir,
                          fee_rates={"tok_yes": 0.02})
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "fee" in str(e).lower() or "tok_no" in str(e).lower()


def test_build_episode_fees_disabled_no_fee_rates_needed():
    """Fee-disabled market does not require fee_rates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        _build_minimal_fixture(normalized_dir, market_id="pm:0x3")

        summary = build_episode("pm:0x3", normalized_dir, episodes_dir)
        assert summary.trade_count == 1
