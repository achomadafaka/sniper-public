from sniper.feeds.goldsky_fills import fetch_wallet_fills


def _row(i, maker="0xm", asset="5"):
    return {"id": f"l{i}", "timestamp": str(100 + i), "maker": maker, "taker": "0xt",
            "makerAssetId": asset, "takerAssetId": "0", "makerAmountFilled": "1000000",
            "takerAmountFilled": "500000", "fee": "0", "transactionHash": f"0xtx{i}"}


def test_fetch_normalizes_and_caches(tmp_path):
    rows = [_row(1)]
    fills = fetch_wallet_fills("0xW", cache_dir=str(tmp_path), fetch_fn=lambda w: rows)
    assert len(fills) == 1
    assert fills[0].maker == "0xm" and fills[0].tx_hash == "0xtx1"


def test_fetch_uses_cache_on_second_call(tmp_path):
    def boom(_w):
        raise AssertionError("should not refetch — must serve from cache")
    fetch_wallet_fills("0xW", cache_dir=str(tmp_path), fetch_fn=lambda w: [_row(1)])
    fills = fetch_wallet_fills("0xW", cache_dir=str(tmp_path), fetch_fn=boom)
    assert len(fills) == 1 and fills[0].tx_hash == "0xtx1"   # served from cache


def test_transient_failure_is_not_cached(tmp_path):
    def fail(_w):
        raise RuntimeError("subgraph 200-with-errors / timeout")
    try:
        fetch_wallet_fills("0xW", cache_dir=str(tmp_path), fetch_fn=fail)
        assert False, "expected the fetch error to propagate"
    except RuntimeError:
        pass
    import os
    assert not os.path.exists(os.path.join(str(tmp_path), "0xw_fills.json"))   # nothing cached
    # a later success caches normally
    fills = fetch_wallet_fills("0xW", cache_dir=str(tmp_path), fetch_fn=lambda w: [_row(2)])
    assert len(fills) == 1 and fills[0].tx_hash == "0xtx2"


def test_attach_condition_ids_maps_outcome_token():
    from sniper.feeds.goldsky_fills import attach_condition_ids
    from sniper.feeds.thesis_feed_types import FillEvent
    fills = [FillEvent("tx", "l", 1, "0xm", "0xt", "A", "0", 1, 1),       # outcome token "A"
             FillEvent("tx", "l2", 2, "0xm", "0xt", "0", "B", 1, 1)]      # token on the taker side
    out = attach_condition_ids(fills, {"A": "0xcondA", "B": "0xcondB"})
    assert out[0].condition_id == "0xcondA"
    assert out[1].condition_id == "0xcondB"


def test_attach_condition_ids_leaves_unmapped_token_none():
    from sniper.feeds.goldsky_fills import attach_condition_ids
    from sniper.feeds.thesis_feed_types import FillEvent
    out = attach_condition_ids([FillEvent("tx", "l", 1, "0xm", "0xt", "Z", "0", 1, 1)], {})
    assert out[0].condition_id is None
