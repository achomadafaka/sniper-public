"""Tests for CSE-F11 selection-rule learner."""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.rule_learner import (
    learn_threshold_rule, learn_selection_rules,
)


def _row(picked, price, ts=None):
    return {"picked": picked, "features": {"price": price}, "ts": ts}


def test_recovers_deterministic_threshold_rule():
    # picks iff price < 0.35 — a clean algorithmic selection rule
    picks = [_row(1, 0.10 + 0.015 * i, ts=i) for i in range(16)]        # 0.10..0.325
    skips = [_row(0, 0.36 + 0.015 * i, ts=100 + i) for i in range(16)]  # 0.36..0.585
    out = learn_selection_rules(picks + skips, ["price"])
    assert out["rule_found"]
    best = out["best"]
    assert best["feature"] == "price" and best["op"] == "<="
    assert best["precision"] >= 0.9 and best["coverage"] >= 0.9
    assert 0.32 <= best["threshold"] <= 0.36
    assert best["determinism"] >= 0.9


def test_no_rule_in_noise():
    import random
    rng = random.Random(0)
    rows = [_row(1, rng.uniform(0.2, 0.6)) for _ in range(30)] + \
           [_row(0, rng.uniform(0.2, 0.6)) for _ in range(30)]
    out = learn_selection_rules(rows, ["price"])
    # no threshold cleanly separates → either no qualifying rule, or low determinism
    assert (not out["rule_found"]) or out["best"]["determinism"] < 0.6


def test_leakage_features_excluded():
    rows = [_row(1, 0.2) for _ in range(10)] + [_row(0, 0.5) for _ in range(10)]
    out = learn_selection_rules(rows, ["price", "roi", "future_volume"])
    assert "roi" in out["leakage_excluded"] and "future_volume" in out["leakage_excluded"]
    # only the clean feature could become a rule
    assert out["best"] is None or out["best"]["feature"] == "price"


def test_oos_confirmation_flag():
    # interleave picks/skips in time so both halves of the OOS split have both
    picks = [_row(1, 0.2, ts=2 * i) for i in range(15)]
    skips = [_row(0, 0.5, ts=2 * i + 1) for i in range(15)]
    out = learn_selection_rules(picks + skips, ["price"])
    assert out["best"]["oos_confirmed"] is True


def test_oos_rule_is_fit_on_earlier_split_only():
    rows = []
    # Earlier split says picks are high-price.
    for i in range(10):
        rows.append(_row(1, 0.8, ts=i * 2))
        rows.append(_row(0, 0.2, ts=i * 2 + 1))
    # Later split reverses the relationship. A leaky full-sample fit would pick
    # the later low-price rule and then "confirm" it on the same later rows.
    for i in range(20):
        rows.append(_row(1, 0.2, ts=100 + i * 2))
        rows.append(_row(0, 0.8, ts=100 + i * 2 + 1))

    out = learn_selection_rules(rows, ["price"])

    assert out["best"]["op"] == ">="
    assert out["best"]["oos_confirmed"] is False


def test_single_threshold_helper():
    rows = [_row(1, 0.2) for _ in range(5)] + [_row(0, 0.8) for _ in range(5)]
    r = learn_threshold_rule(rows, "price")
    assert r["op"] == "<=" and r["precision"] == 1.0 and r["coverage"] == 1.0
