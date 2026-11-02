from sniper.algos.hl_microstructure.radar import rank_markets


def test_rank_markets_rejects_dead_or_unpriced_assets():
    rows = [
        {"name": "BTC", "isDelisted": False, "ctx": {"midPx": "100", "dayNtlVlm": "1000000", "openInterest": "100", "funding": "0.0", "premium": "0.0"}},
        {"name": "DEAD", "isDelisted": True, "ctx": {"midPx": "1", "dayNtlVlm": "999999999", "openInterest": "1"}},
        {"name": "ZERO", "ctx": {"midPx": "2", "dayNtlVlm": "0", "openInterest": "1"}},
        {"name": "NOMID", "ctx": {"midPx": None, "dayNtlVlm": "1000000", "openInterest": "1"}},
    ]

    ranked = rank_markets(rows, max_assets=5)

    assert [r.asset for r in ranked] == ["BTC"]
    assert ranked[0].selected is True
    assert "volume" in ranked[0].reasons


def test_rank_markets_promotes_activity_and_stress_over_static_majors():
    rows = [
        {"name": "BTC", "ctx": {"midPx": "64000", "dayNtlVlm": "1000000000", "openInterest": "30000", "funding": "0.000001", "premium": "0.0001"}},
        {"name": "HOT", "ctx": {"midPx": "2", "dayNtlVlm": "6000000", "openInterest": "90000000", "funding": "-0.00020", "premium": "-0.004"}},
        {"name": "MID", "ctx": {"midPx": "3", "dayNtlVlm": "50000000", "openInterest": "5000000", "funding": "0.00001", "premium": "0.0002"}},
    ]

    ranked = rank_markets(rows, max_assets=2)

    assert [r.asset for r in ranked] == ["HOT", "BTC"]
    assert ranked[0].score > ranked[1].score
    assert "funding_extreme" in ranked[0].reasons
    assert "premium_dislocation" in ranked[0].reasons
    assert all(r.selected for r in ranked)


def test_rank_markets_rejects_tiny_volume_stress_traps():
    rows = [
        {"name": "TINY", "ctx": {"midPx": "1", "dayNtlVlm": "250000", "openInterest": "999999999", "funding": "-0.00020", "premium": "-0.004"}},
        {"name": "LIQUID", "ctx": {"midPx": "10", "dayNtlVlm": "2000000", "openInterest": "1000000", "funding": "0.0", "premium": "0.0"}},
    ]

    ranked = rank_markets(rows, max_assets=5)

    assert [r.asset for r in ranked] == ["LIQUID"]
