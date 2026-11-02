"""Tests for CSE-F2 point-in-time market context."""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext


def _ctx():
    meta = {
        "a": {"created_at": 100, "end_date": 1000, "category": "sports", "market_type": "binary", "outcome_role": "underdog"},
        "b": {"created_at": 100, "end_date": 1000, "category": "politics", "market_type": "binary", "outcome_role": "favorite"},
        "c": {"created_at": 800, "end_date": 1000, "category": "sports", "market_type": "binary", "outcome_role": "underdog"},  # not yet created at T=500
        "d": {"created_at": 100, "end_date": 400, "category": "sports", "market_type": "binary", "outcome_role": "underdog"},   # already ended at T=500
    }
    price = {
        "a": [(200, 0.3), (480, 0.32), (490, 0.31), (495, 0.33), (498, 0.34), (505, 0.35)],
        "b": [(200, 0.7)],
    }
    return MarketContext(market_meta=meta, price_index=price)


def test_universe_membership_by_time():
    u = _ctx().universe_at(500)
    assert set(u) == {"a", "b"}     # c not created yet, d already ended


def test_universe_filters_scope():
    u = _ctx().universe_at(500, filters={"category": "sports", "outcome_role": "underdog"})
    assert u == ["a"]               # b is politics/favorite


def test_price_at_nearest_and_fidelity():
    price, staleness, fidelity = _ctx().price_at("a", 500)
    assert price == 0.34 and staleness == 2     # nearest trade at 498
    assert fidelity == "active"                  # >=5 trades in ±1h
    p2, s2, f2 = _ctx().price_at("b", 500)
    assert p2 == 0.7 and f2 == "sparse"          # single trade
    assert _ctx().price_at("z", 500) == (None, None, "pre_trade")


def test_market_state_age_and_ttr():
    st = _ctx().market_state_at("a", 500)
    assert st["market_age_s"] == 400 and st["time_to_resolution_s"] == 500
    assert st["category"] == "sports"


def test_price_at_before_first_trade_does_not_look_ahead():
    ctx = MarketContext(
        market_meta={"m": {"created_at": 100, "end_date": 1000}},
        price_index={"m": [(800, 0.8)]},
    )

    price, staleness, fidelity = ctx.price_at("m", 500)
    state = ctx.market_state_at("m", 500)

    assert price is None
    assert staleness is None
    assert fidelity == "pre_trade"
    assert state["outcome_role"] is None
    assert state["favorite_rank"] is None


def test_unselected_excludes_picked():
    cands = _ctx().unselected_candidates(picked_cid="a", T=500,
                                         filters={"category": "sports", "outcome_role": "underdog"})
    assert "a" not in cands and cands == []   # only a matches that scope at T=500, excluded
