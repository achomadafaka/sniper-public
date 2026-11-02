import json

from sniper.feeds.polymarket_trade_tap import TradeTick
from scripts.stink_bid_shadow_daemon import (
    ShadowCandidate,
    ShadowDaemon,
    discover_shadow_candidates,
    parse_args,
    parse_market_tokens,
)


class FakeClient:
    def __init__(self, books):
        self.books = books

    def get_orderbook(self, token_id):
        return self.books[token_id]


def _book(token_id, *, market="cid", bid=0.65, ask=0.67, last=0.66):
    return {
        "market": market,
        "asset_id": token_id,
        "timestamp": "1710000000",
        "hash": f"hash-{token_id}",
        "bids": [{"price": str(bid), "size": "10"}],
        "asks": [{"price": str(ask), "size": "20"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "last_trade_price": str(last),
    }


def test_parse_market_tokens_accepts_json_encoded_gamma_fields():
    market = {
        "conditionId": "cid",
        "question": "Alice vs Bob",
        "outcomes": '["Alice", "Bob"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }

    assert parse_market_tokens(market) == [("Alice", "tok_yes"), ("Bob", "tok_no")]


def test_discover_shadow_candidates_selects_live_favorite_from_books():
    pairs = [
        {
            "market": {
                "conditionId": "cid",
                "question": "Alice vs Bob",
                "outcomes": json.dumps(["Alice", "Bob"]),
                "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
            }
        }
    ]
    client = FakeClient(
        {
            "tok_yes": _book("tok_yes", bid=0.69, ask=0.71, last=0.70),
            "tok_no": _book("tok_no", bid=0.29, ask=0.31, last=0.30),
        }
    )

    candidates = discover_shadow_candidates(
        pairs,
        client=client,
        dip_pct=0.15,
        min_favorite=0.65,
        max_favorite=0.90,
        min_bid=0.05,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )

    assert candidates == [
        ShadowCandidate(
            condition_id="cid",
            title="Alice vs Bob",
            outcome="Alice",
            token_id="tok_yes",
            favorite_price=0.70,
            bid_price=0.59,
        )
    ]


def test_discover_shadow_candidates_rejects_near_resolved_favorite():
    pairs = [
        {
            "market": {
                "conditionId": "cid",
                "question": "Alice vs Bob",
                "outcomes": json.dumps(["Alice", "Bob"]),
                "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
            }
        }
    ]
    client = FakeClient(
        {
            "tok_yes": _book("tok_yes", bid=0.998, ask=1.0, last=0.999),
            "tok_no": _book("tok_no", bid=0.001, ask=0.002, last=0.001),
        }
    )

    candidates = discover_shadow_candidates(
        pairs,
        client=client,
        dip_pct=0.15,
        min_favorite=0.65,
        max_favorite=0.90,
        min_bid=0.05,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )

    assert candidates == []


def test_shadow_daemon_routes_trade_ticks_to_candidate_monitor(tmp_path):
    candidate = ShadowCandidate(
        condition_id="cid",
        title="Alice vs Bob",
        outcome="Alice",
        token_id="tok_yes",
        favorite_price=0.70,
        bid_price=0.59,
    )
    client = FakeClient({"tok_yes": _book("tok_yes", bid=0.59, ask=0.61, last=0.60)})
    daemon = ShadowDaemon(
        client=client,
        output_path=tmp_path / "shadow.jsonl",
        dip_pct=0.15,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )

    daemon.set_candidates([candidate])
    evaluation = daemon.on_tick(
        TradeTick(
            token_id="tok_yes",
            condition_id="cid",
            price=0.58,
            size=60.0,
            side="SELL",
            timestamp_ms=1710000000000,
        )
    )

    assert evaluation is not None
    assert evaluation.queue_fillable is True
    row = json.loads((tmp_path / "shadow.jsonl").read_text().strip().splitlines()[-1])
    assert row["condition_id"] == "cid"
    assert row["queue_fillable"] is True


def test_parse_args_supports_once_mode():
    args = parse_args(["--once"])

    assert args.once is True
    parse_args,
