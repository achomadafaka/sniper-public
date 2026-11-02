"""End-to-end CSE integration on a controlled synthetic universe.

Validates the whole chain (F1→F2→F3/F4→detection→learner→verdict) recovers a
planted selection rule, and does NOT fabricate one from random selection.
"""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext
from sniper.analysis.counterfactual_selection import runner as runner_mod
from sniper.analysis.counterfactual_selection.runner import run_counterfactual_selection


def _dense_prices(price):
    return [(1000 + 100 * k, price) for k in range(60)]  # dense → active fidelity


def _market(cid, price, resolved):
    return {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
            "market_type": "binary", "outcome_role": "underdog", "favorite_rank": 2,
            "resolved_to_side": resolved}


def _ladder(cid, i, price):
    ts = 2000 + i * 120
    return [{"type": "TRADE", "side": "BUY", "conditionId": cid, "asset": f"a{i}",
             "price": price, "size": 10, "timestamp": ts + k * 5, "outcome": "YES",
             "market_category": "sports"} for k in range(4)]


def test_recovers_planted_cheap_underdog_rule():
    meta, pidx = {}, {}
    for i in range(60):
        cid = f"m{i}"
        cheap = i < 30
        # cheap ~0.20-0.34 (varying), expensive ~0.45-0.59 (varying) → real variance
        price = (0.20 + 0.005 * (i % 15)) if cheap else (0.45 + 0.005 * ((i - 30) % 15))
        resolved = (1 if i < 21 else 0) if cheap else (1 if i < 41 else 0)  # cheap 70% win, exp ~37%
        meta[cid] = _market(cid, price, resolved)
        pidx[cid] = _dense_prices(price)
    ctx = MarketContext(market_meta=meta, price_index=pidx)
    activity = [f for i in range(30) for f in _ladder(f"m{i}", i, 0.25)]  # picks the 30 cheap

    out = run_counterfactual_selection(activity, ctx, n_perm=200)

    # discriminator: price separates, picks lower, significant after FDR
    price_disc = [d for d in out["discriminators"] if d["feature"] == "price"]
    assert any(d["direction"] == "picked_lower" and d["significant"] for d in price_disc)
    # edge: her cheap picks out-resolve the expensive unpicked
    assert out["selection_edge"]["supported"] is True
    assert out["selection_edge"]["edge"] > 0.2
    # rule recovered + verdict routes to rebuild
    assert out["rule"]["rule_found"]
    assert out["rule"]["best"]["feature"] == "price" and out["rule"]["best"]["op"] == "<="
    assert out["verdict"]["classification"] == "algorithmic_self_contained"
    assert out["verdict"]["route"] == "rebuild_rule"


def test_random_selection_does_not_fabricate_a_rule():
    meta, pidx = {}, {}
    for i in range(60):
        cid = f"m{i}"
        meta[cid] = _market(cid, 0.40, resolved=i % 2)   # all same price, 50% resolve
        pidx[cid] = _dense_prices(0.40)
    ctx = MarketContext(market_meta=meta, price_index=pidx)
    activity = [f for i in range(30) for f in _ladder(f"m{i}", i, 0.40)]  # picks m0..m29 (no signal)

    out = run_counterfactual_selection(activity, ctx, n_perm=200)
    assert out["selection_edge"]["supported"] is not True   # no edge
    assert out["verdict"]["route"] != "rebuild_rule"        # never fabricates a rule
    assert out["verdict"]["classification"] != "algorithmic_self_contained"


def test_runner_fdr_corrects_selection_edges(monkeypatch):
    calls = {"edge": 0}

    def fake_discriminator(rows, feature, *, n_perm=500):
        return [{"group_id": "g", "feature": feature, "p_value": 0.04}]

    def fake_edge(rows, *, n_perm=500):
        calls["edge"] += 1
        return {
            "picked_win_rate": 0.8,
            "unpicked_win_rate": 0.4,
            "edge": 0.4,
            "p_value": 0.04,
            "n_picked": 10,
            "n_unpicked": 10,
            "supported": True,
        }

    monkeypatch.setattr(
        "sniper.analysis.counterfactual_selection.runner.within_group_discriminator",
        fake_discriminator,
    )
    monkeypatch.setattr(
        "sniper.analysis.counterfactual_selection.runner.selection_edge",
        fake_edge,
    )

    ctx = MarketContext(
        market_meta={
            "m": {"created_at": 0, "end_date": 1000, "category": "sports",
                  "market_type": "binary", "resolved_to_side": 1},
            "c": {"created_at": 0, "end_date": 1000, "category": "sports",
                  "market_type": "binary", "resolved_to_side": 0},
        },
        price_index={
            "m": [(10, 0.2), (11, 0.2), (12, 0.2), (13, 0.2), (14, 0.2)],
            "c": [(10, 0.4), (11, 0.4), (12, 0.4), (13, 0.4), (14, 0.4)],
        },
    )
    rows = [{"type": "TRADE", "side": "BUY", "conditionId": "m", "asset": "a",
             "price": 0.2, "size": 1, "timestamp": 20, "outcome": "YES"}]

    out = run_counterfactual_selection(rows, ctx)

    assert calls["edge"] == 2
    assert out["selection_edge_raw"]["q_value"] == 0.04
    assert out["selection_edge_matched"]["q_value"] == 0.04


def test_runner_flags_raw_edge_when_matching_cannot_confirm_it(monkeypatch):
    rows = [
        # picks win ~20% at entry 0.2 → calibration-neutral, so this isolates the
        # CONFOUND path (a real calibration edge would correctly rescue to mixed).
        {"group_id": "g", "picked": 1, "resolved": (1 if i < 6 else 0), "ts": i,
         "features": {"price": 0.2, "entry_price": 0.2, "market_age": 100 + i,
                      "time_to_resolution": 1000}}
        for i in range(30)
    ] + [
        {"group_id": "g", "picked": 0, "resolved": 0, "ts": i,
         "features": {"price": 0.6, "market_age": 200 + i, "time_to_resolution": 1000}}
        for i in range(30)
    ]
    edge_calls = iter([
        {"supported": True, "edge": 0.51, "p_value": 0.001, "n_picked": 30, "n_unpicked": 30},
        {"supported": False, "edge": 0.032, "p_value": 0.33, "n_picked": 30, "n_unpicked": 30},
    ])

    monkeypatch.setattr(runner_mod, "build_engagements", lambda activity: [])
    monkeypatch.setattr(runner_mod, "build_decision_matrix", lambda *a, **kw: rows)
    monkeypatch.setattr(runner_mod, "within_group_discriminator", lambda *a, **kw: [])
    monkeypatch.setattr(runner_mod, "selection_edge", lambda *a, **kw: next(edge_calls))
    monkeypatch.setattr(runner_mod, "nearest_neighbor_match", lambda rows: rows)
    monkeypatch.setattr(
        runner_mod,
        "_balance",
        lambda matched: {"worst_abs_smd": 0.43, "scope_valid": False},
    )
    monkeypatch.setattr(
        runner_mod,
        "learn_selection_rules",
        lambda rows, features: {"best": None, "rule_found": False},
    )

    out = runner_mod.run_counterfactual_selection([], MarketContext())

    assert out["confound_flag"] is True
    assert out["verdict"]["classification"] == "undetermined"
    assert out["verdict"]["route"] == "collect_more"
