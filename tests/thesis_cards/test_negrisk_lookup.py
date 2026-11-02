"""negRisk lookup -> set of negativeRisk condition_ids, fed into the provider.

Tested against a fake markets fetcher (no network). Confirms: only confirmed-negRisk
wanted condition_ids are returned; batching; empty input; and that the resulting set,
fed via negrisk_conditions=, makes the provider exclude those markets.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.forward_window_provider import WalletForwardWindowProvider
from sniper.analysis.thesis_cards.negrisk_lookup import load_negrisk_conditions


def _market(cid, neg):
    return {"conditionId": cid, "negRisk": neg}


def test_returns_only_confirmed_negrisk():
    def fetch(batch):
        return [_market("0xA", True), _market("0xB", False), _market("0xC", True)]
    out = load_negrisk_conditions(["0xA", "0xB", "0xC"], fetch_markets=fetch)
    assert out == {"0xA", "0xC"}


def test_empty_input_returns_empty_set_without_fetch():
    calls = []
    load_negrisk_conditions([], fetch_markets=lambda b: calls.append(b) or [])
    assert calls == []


def test_ignores_markets_not_requested():
    def fetch(batch):
        return [_market("0xUNWANTED", True), _market("0xA", True)]
    assert load_negrisk_conditions(["0xA"], fetch_markets=fetch) == {"0xA"}


def test_batches_requests():
    seen = []
    def fetch(batch):
        seen.append(list(batch))
        return [_market(c, True) for c in batch]
    ids = [f"0x{i:02d}" for i in range(25)]
    out = load_negrisk_conditions(ids, fetch_markets=fetch, batch_size=10)
    assert len(seen) == 3 and len(out) == 25            # 10 + 10 + 5


def test_missing_market_treated_as_not_negrisk():
    # fetcher returns nothing for the batch -> not excluded
    assert load_negrisk_conditions(["0xA"], fetch_markets=lambda b: []) == set()


def test_set_fed_into_provider_excludes_those_markets():
    raw = lambda w: [
        {"avgPrice": 0.45, "curPrice": 1.0, "timestamp": 2000, "realizedPnl": 55.0, "conditionId": "0xNEG"},
        {"avgPrice": 0.45, "curPrice": 1.0, "timestamp": 2000, "realizedPnl": 55.0, "conditionId": "0xOK"},
    ]
    negset = load_negrisk_conditions(["0xNEG", "0xOK"],
                                     fetch_markets=lambda b: [_market("0xNEG", True), _market("0xOK", False)])
    prov = WalletForwardWindowProvider(raw, negrisk_conditions=negset)
    win = prov("0xw", 1000, "calibration_edge")
    assert len(win) == 1                                # 0xNEG excluded, 0xOK kept


def test_negrisk_cache_dedups_and_persists(tmp_path):
    from sniper.analysis.thesis_cards.negrisk_lookup import NegriskCache
    calls = []
    def fetch(batch):
        calls.append(list(batch))
        return [_market(c, c == "0xA") for c in batch]      # 0xA is negRisk
    path = str(tmp_path / "negrisk.json")
    cache = NegriskCache(path, fetch_markets=fetch, batch_size=50)
    assert cache(["0xA", "0xB"]) == {"0xA"}                  # first call -> fetch
    assert cache(["0xA", "0xB"]) == {"0xA"}                  # cached -> no new fetch
    assert cache(["0xB", "0xC"]) == set()                    # only 0xC is new
    fetched = {c for b in calls for c in b}
    assert fetched == {"0xA", "0xB", "0xC"}                  # each unique id fetched exactly once
    # durable: a fresh cache from the same path needs no fetch for known ids
    calls.clear()
    cache2 = NegriskCache(path, fetch_markets=fetch, batch_size=50)
    assert cache2(["0xA", "0xB", "0xC"]) == {"0xA"} and calls == []
