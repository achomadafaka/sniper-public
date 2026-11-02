# tests/sim/test_replay_engine.py
"""Engine tests — full replay lifecycle with deterministic outcomes."""
import json
import os
import tempfile

from sniper.sim.compact.writer import ParquetTableWriter
from sniper.sim.episodes.builder import build_episode
from sniper.sim.replay.adapters import PolymarketAdapter
from sniper.sim.replay.engine import ReplayEngine, EpisodeResult
from sniper.sim.replay.context import ReplayContext


def _build_episode_fixture(tmpdir: str, market_id: str = "pm:0x1",
                           yes_price: float = 0.65,
                           winner: str = "No") -> str:
    """Build a fixture episode. Winner determines outcome_prices."""
    normalized_dir = os.path.join(tmpdir, "normalized")
    episodes_dir = os.path.join(tmpdir, "episodes")
    writer = ParquetTableWriter(base_dir=normalized_dir)
    date = "2026-04-03"

    if winner == "Yes":
        outcome_prices = ["0.99", "0.01"]
    else:
        outcome_prices = ["0.01", "0.99"]

    # Quote before trade (so strategy has a quote to act on)
    writer.append("quotes_top", date, [
        {
            "market_id": market_id, "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": yes_price - 0.05, "best_ask": yes_price,
            "bid_size": 500.0, "ask_size": 800.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        },
    ])

    # Trade event (observed, not acted on by strategy)
    writer.append("trades", date, [
        {
            "market_id": market_id, "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": yes_price, "size": 100.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geopolitical", "venue": "polymarket",
        },
    ])

    writer.append("market_lifecycle", date, [
        {
            "market_id": market_id, "capture_ts": "2026-04-03T10:00:00.000Z",
            "ingest_seq": 0, "event_type": "market_metadata",
            "market_type": "geopolitical",
            "payload": json.dumps({
                "question": "Will it happen?", "outcomes": ["Yes", "No"],
                "tokens": [
                    {"token_id": "tok_yes", "label": "Yes", "index": 0},
                    {"token_id": "tok_no", "label": "No", "index": 1},
                ],
                "fees_enabled": False, "enable_order_book": True, "fee": None,
            }),
            "venue": "polymarket",
        },
        {
            "market_id": market_id, "capture_ts": "2026-04-03T18:00:00.000Z",
            "ingest_seq": 99, "event_type": "market_resolved",
            "market_type": "geopolitical",
            "payload": json.dumps({
                "outcome_prices": outcome_prices,
                "resolved_ts": "2026-04-03T18:00:00.000Z",
            }),
            "venue": "polymarket",
        },
    ])
    writer.flush()

    summary = build_episode(
        market_id=market_id,
        normalized_dir=normalized_dir,
        episodes_dir=episodes_dir,
    )
    return summary.episode_dir


# --- Strategies for testing ---

class BuyAndHoldYes:
    """Buy YES on first quote, hold to settlement."""
    def __init__(self, size: int = 100):
        self._size = size
        self._bought = False

    def on_episode_start(self, metadata: dict) -> None:
        self._bought = False

    def act(self, ctx: ReplayContext) -> None:
        if not self._bought and "Yes" in ctx.get_markets():
            result = ctx.place_order(ticker="Yes", side="BUY", count=self._size)
            if result.filled > 0:
                self._bought = True

    def on_episode_end(self, result: dict) -> None:
        pass


class DoNothing:
    """Strategy that never trades."""
    def on_episode_start(self, metadata: dict) -> None: pass
    def act(self, ctx: ReplayContext) -> None: pass
    def on_episode_end(self, result: dict) -> None: pass


# --- Tests ---

def test_engine_buy_and_hold_yes_wins():
    """Buy YES → YES wins → positive PnL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir, winner="Yes")
        adapter = PolymarketAdapter()
        episode = adapter.load(episode_dir)

        engine = ReplayEngine(starting_cash=1000.0)
        result = engine.run_episode(BuyAndHoldYes(100), episode)

        assert isinstance(result, EpisodeResult)
        assert result.market_id == "pm:0x1"
        assert len(result.fills) == 1
        assert result.fills[0].ticker == "Yes"
        assert result.fills[0].side == "BUY"
        assert result.fills[0].count == 100
        # Bought 100 @ 0.65 = $65 cost, settled at $0.99 = $99 payout
        # PnL = 99 - 65 = $34 (no fees, outcome_price is 0.99 not 1.00)
        assert abs(result.pnl - 34.0) < 0.01
        assert result.outcome == "Yes"


def test_engine_buy_and_hold_yes_loses():
    """Buy YES → NO wins → negative PnL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir, winner="No")
        adapter = PolymarketAdapter()
        episode = adapter.load(episode_dir)

        engine = ReplayEngine(starting_cash=1000.0)
        result = engine.run_episode(BuyAndHoldYes(100), episode)

        assert len(result.fills) == 1
        # Bought 100 @ 0.65 = $65, YES settles at $0.01 = $1 payout
        # PnL = 1 - 65 = -$64
        assert abs(result.pnl - (-64.0)) < 0.01


def test_engine_do_nothing():
    """Strategy that never trades → zero PnL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir)
        adapter = PolymarketAdapter()
        episode = adapter.load(episode_dir)

        engine = ReplayEngine(starting_cash=1000.0)
        result = engine.run_episode(DoNothing(), episode)

        assert result.pnl == 0.0
        assert len(result.fills) == 0
        assert result.ending_cash == 1000.0


def test_engine_deterministic():
    """Same episode + same strategy = same result, always."""
    with tempfile.TemporaryDirectory() as tmpdir:
        episode_dir = _build_episode_fixture(tmpdir, winner="Yes")
        adapter = PolymarketAdapter()
        episode = adapter.load(episode_dir)

        engine = ReplayEngine(starting_cash=1000.0)
        r1 = engine.run_episode(BuyAndHoldYes(100), episode)
        r2 = engine.run_episode(BuyAndHoldYes(100), episode)

        assert r1.pnl == r2.pnl
        assert r1.ending_cash == r2.ending_cash
        assert len(r1.fills) == len(r2.fills)


def test_engine_run_multiple_episodes():
    """run() processes multiple episodes and returns per-episode results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ep1_dir = _build_episode_fixture(tmpdir, market_id="pm:0x1", winner="Yes")

    with tempfile.TemporaryDirectory() as tmpdir2:
        ep2_dir = _build_episode_fixture(tmpdir2, market_id="pm:0x2", winner="No")

        engine = ReplayEngine(starting_cash=1000.0)
        # Need to load from existing dirs — but tmpdir1 is gone.
        # Use tmpdir2 for both by building two episodes in one dir.

    # Simpler: build both in one tmpdir
    with tempfile.TemporaryDirectory() as tmpdir:
        ep1 = _build_episode_fixture(tmpdir, market_id="pm:0x1", winner="Yes")
        # Build second in separate normalized dir
        normalized2 = os.path.join(tmpdir, "norm2")
        episodes2 = os.path.join(tmpdir, "eps2")
        writer = ParquetTableWriter(base_dir=normalized2)
        writer.append("trades", "2026-04-03", [{
            "market_id": "pm:0x2", "asset_id": "tok_a",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.50, "size": 100.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geo", "venue": "polymarket",
        }])
        writer.append("quotes_top", "2026-04-03", [{
            "market_id": "pm:0x2", "asset_id": "tok_a",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": 0.45, "best_ask": 0.50,
            "bid_size": 500.0, "ask_size": 500.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        }])
        writer.append("market_lifecycle", "2026-04-03", [
            {"market_id": "pm:0x2", "capture_ts": "2026-04-03T10:00:00.000Z",
             "ingest_seq": 0, "event_type": "market_metadata", "market_type": "geo",
             "payload": json.dumps({
                 "question": "Test 2?", "outcomes": ["Yes", "No"],
                 "tokens": [{"token_id": "tok_a", "label": "Yes", "index": 0},
                            {"token_id": "tok_b", "label": "No", "index": 1}],
                 "fees_enabled": False, "enable_order_book": True, "fee": None,
             }), "venue": "polymarket"},
            {"market_id": "pm:0x2", "capture_ts": "2026-04-03T18:00:00.000Z",
             "ingest_seq": 99, "event_type": "market_resolved", "market_type": "geo",
             "payload": json.dumps({"outcome_prices": ["0.01", "0.99"],
                                    "resolved_ts": "2026-04-03T18:00:00.000Z"}),
             "venue": "polymarket"},
        ])
        writer.flush()
        summary2 = build_episode("pm:0x2", normalized2, episodes2)
        ep2 = summary2.episode_dir

        engine = ReplayEngine(starting_cash=1000.0)
        results = engine.run([BuyAndHoldYes(50), BuyAndHoldYes(50)][0],
                             [ep1, ep2])

        assert len(results) == 2
        assert results[0].market_id == "pm:0x1"
        assert results[1].market_id == "pm:0x2"
        # ep1 YES wins → positive, ep2 NO wins → negative
        assert results[0].pnl > 0
        assert results[1].pnl < 0


def test_engine_per_token_fee_rates():
    """Per-token fee rates from episode metadata are applied, not a flat default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        normalized_dir = os.path.join(tmpdir, "normalized")
        episodes_dir = os.path.join(tmpdir, "episodes")
        writer = ParquetTableWriter(base_dir=normalized_dir)
        date = "2026-04-03"

        writer.append("quotes_top", date, [{
            "market_id": "pm:0xfee", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T11:59:00.000Z",
            "exchange_ts": "2026-04-03T11:59:00.000Z",
            "ingest_seq": 0, "best_bid": 0.45, "best_ask": 0.50,
            "bid_size": 500.0, "ask_size": 500.0,
            "source_event": "book_snapshot", "venue": "polymarket",
        }])
        writer.append("trades", date, [{
            "market_id": "pm:0xfee", "asset_id": "tok_yes",
            "capture_ts": "2026-04-03T12:00:00.000Z",
            "exchange_ts": "2026-04-03T12:00:00.000Z",
            "ingest_seq": 1, "price": 0.50, "size": 100.0, "side": "BUY",
            "fee_rate_bps": None, "transaction_hash": None,
            "market_type": "geo", "venue": "polymarket",
        }])
        writer.append("market_lifecycle", date, [
            {"market_id": "pm:0xfee", "capture_ts": "2026-04-03T10:00:00.000Z",
             "ingest_seq": 0, "event_type": "market_metadata", "market_type": "geo",
             "payload": json.dumps({
                 "question": "Fee test?", "outcomes": ["Yes", "No"],
                 "tokens": [{"token_id": "tok_yes", "label": "Yes", "index": 0},
                            {"token_id": "tok_no", "label": "No", "index": 1}],
                 "fees_enabled": True, "enable_order_book": True, "fee": "0.01",
             }), "venue": "polymarket"},
            {"market_id": "pm:0xfee", "capture_ts": "2026-04-03T18:00:00.000Z",
             "ingest_seq": 99, "event_type": "market_resolved", "market_type": "geo",
             "payload": json.dumps({"outcome_prices": ["0.99", "0.01"],
                                    "resolved_ts": "2026-04-03T18:00:00.000Z"}),
             "venue": "polymarket"},
        ])
        writer.flush()

        # Build with YES token fee rate of 0.05 (5%), NO at 0.01 (1%)
        summary = build_episode("pm:0xfee", normalized_dir, episodes_dir,
                                fee_rates={"tok_yes": 0.05, "tok_no": 0.01})
        episode = PolymarketAdapter().load(summary.episode_dir)

        engine = ReplayEngine(starting_cash=1000.0)
        result = engine.run_episode(BuyAndHoldYes(100), episode)

        # Fee should use the YES token rate (0.05), not the default (0.01)
        assert len(result.fills) == 1
        fill = result.fills[0]
        # Expected fee: 0.05 × 0.50 × 0.50 × 100 = 1.25
        expected_fee = 0.05 * 0.50 * 0.50 * 100
        assert abs(fill.fee - expected_fee) < 0.001, f"Fee {fill.fee} != expected {expected_fee}"

        # If default rate (0.01) were used instead: 0.01 × 0.50 × 0.50 × 100 = 0.25
        wrong_fee = 0.01 * 0.50 * 0.50 * 100
        assert abs(fill.fee - wrong_fee) > 0.5, "Fee should NOT match the default rate"
