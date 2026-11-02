from sniper.analysis.winning_wallet_thesis.data.book_price_path import BookPricePathProvider
from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
from sniper.feeds.thesis_feed_types import FillEvent, OrderBookPoint


def test_book_price_path_provider_returns_mid_path_for_condition():
    points = [OrderBookPoint("asset1", 1000, .4, .6, .5, .2, 1, 1, 1, 1, "test")]
    provider = BookPricePathProvider(points, condition_to_asset={"cond1": "asset1"})
    assert provider.path("cond1", start_ts=1, end_ts=2) == [(1, .5)]


def test_book_price_path_provider_uses_outcome_side_mapping():
    points = [
        OrderBookPoint("yes_asset", 1000, .8, .9, .85, .1, 1, 1, 1, 1, "test"),
        OrderBookPoint("no_asset", 1000, .1, .2, .15, .1, 1, 1, 1, 1, "test"),
    ]
    provider = BookPricePathProvider(points, condition_to_asset={
        ("cond1", 0): "yes_asset",
        ("cond1", 1): "no_asset",
    })
    assert provider.path("cond1", outcome_index=1, start_ts=1, end_ts=2) == [(1, .15)]


def test_book_price_path_provider_returns_chronological_path():
    points = [
        OrderBookPoint("asset1", 3000, .6, .8, .7, .2, 1, 1, 1, 1, "test"),
        OrderBookPoint("asset1", 1000, .4, .6, .5, .2, 1, 1, 1, 1, "test"),
    ]
    provider = BookPricePathProvider(points, condition_to_asset={"cond1": "asset1"})
    assert provider.path("cond1", start_ts=1, end_ts=3) == [(1, .5), (3, .7)]


def test_book_price_path_provider_none_for_unknown_condition():
    provider = BookPricePathProvider([], condition_to_asset={})
    assert provider.path("cond1", start_ts=1, end_ts=2) is None


def test_onchain_fill_identity_provider_filters_wallet():
    fill = FillEvent("tx", "log", 10, "0xmaker", "0xtaker", "a", "b", 1, 2)
    provider = OnchainFillIdentityProvider([fill])
    assert provider.available is True
    assert provider.fills_for_wallet("0xmaker", start_ts=0, end_ts=20) == [fill]
    assert provider.fills_for_wallet("0xother", start_ts=0, end_ts=20) == []


def test_feed_providers_satisfy_phase4_contracts():
    # structural: the feed-backed providers are drop-in for the Phase 4 gates.
    from sniper.analysis.winning_wallet_thesis.methods.liquidity import stale_liquidity
    from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
    ep = PositionEpisode("0xw", "cond1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=1, resolved=True, won=1)
    ctx = ClusterContext(EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1,
                                     [(ep.condition_id, 0, 0)]), PositionBook("0xw", [ep]))
    points = [OrderBookPoint("asset1", 2000, .5, .7, .6, .2, 1, 1, 1, 1, "t")]
    provider = BookPricePathProvider(points, condition_to_asset={"cond1": "asset1"})
    finding = stale_liquidity(ctx, price_path=provider)   # no longer blocked — feed present
    assert finding.state in {"supported_proxy", "contradicted"}
