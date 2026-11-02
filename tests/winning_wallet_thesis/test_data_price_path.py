from sniper.analysis.winning_wallet_thesis.data.price_path import InMemoryPricePathProvider


def test_price_path_returns_none_when_condition_absent():
    provider = InMemoryPricePathProvider({})
    assert provider.path("c1", start_ts=100, end_ts=200) is None


def test_price_path_filters_window():
    provider = InMemoryPricePathProvider({"c1": [(90, .4), (110, .5), (210, .6)]})
    assert provider.path("c1", start_ts=100, end_ts=200) == [(110, .5)]
