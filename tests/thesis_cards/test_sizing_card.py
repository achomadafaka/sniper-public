"""sizing_card — dollar-weighted minus equal-weighted calibration, with a within-
wallet size permutation null. Positions: {entry_price, size, won (0/1)}.
Near-certainty entries (>=0.80) are excluded so "big on favorites" isn't scored.

Spec (rev2 §sizing_card) requires ALL of: (1) dollar-wtd > equal-wtd; (2) the
underlying calibration edge passes its OWN test (she must actually have a positive
base edge — sizing skill on a losing book is not an edge); (3) within-wallet size
permutation p < 0.10. Missing (2) or (3) → exploratory at most.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.sizing_card import sizing_card


def test_sizing_card_detects_conviction():
    # positive BASE edge (60% win @ 0.45) AND sizes big on the winners → supported
    pos = []
    for i in range(40):
        won = 1 if i < 24 else 0          # 24/40 = 60% at entry 0.45 → base edge +0.15
        size = 1000 if won else 50        # big on winners
        pos.append({"entry_price": 0.45, "size": size, "won": won})
    card = sizing_card(pos, n_perm=400, seed=1)
    assert card.card_id == "sizing_card"
    assert card.effect > 0                     # dollar-wtd beats equal-wtd
    assert card.p_value is not None and card.p_value < 0.10
    assert card.evidence_state == "supported"
    assert card.q_value is None                # router-owned


def test_sizing_card_negative_base_edge_not_supported():
    # she LOSES on average (40% win @ 0.50 = -0.10 base edge) but sizes on the winners.
    # sizing skill on a losing book must NOT certify → exploratory, never supported.
    pos = []
    for i in range(20):
        won = 1 if i < 8 else 0           # 8/20 = 40% at 0.50 → NEGATIVE base edge
        size = 1000 if won else 50
        pos.append({"entry_price": 0.5, "size": size, "won": won})
    card = sizing_card(pos, n_perm=600, seed=1)
    assert card.effect > 0                     # sizing edge alone is positive...
    assert card.evidence_state == "exploratory"  # ...but base edge fails its own test
    assert card.evidence.get("base_calibration", 0.0) <= 0


def test_sizing_card_no_skill_is_exploratory_or_contradicted():
    # size uncorrelated with outcome → sizing edge ~0, permutation not significant
    pos = []
    for i in range(40):
        pos.append({"entry_price": 0.5, "size": 100 + (i % 5) * 10, "won": i % 2})
    card = sizing_card(pos, n_perm=400, seed=1)
    assert card.evidence_state in ("exploratory", "contradicted")


def test_sizing_card_excludes_near_certainty():
    # huge sizes on 0.95 near-certainties must NOT create a sizing edge (excluded)
    pos = [{"entry_price": 0.95, "size": 100000, "won": 1} for _ in range(30)]
    pos += [{"entry_price": 0.5, "size": 100, "won": i % 2} for i in range(30)]
    card = sizing_card(pos, n_perm=300, seed=1)
    # only the <0.80 entries count → n == 30
    assert card.n == 30


def test_sizing_card_underpowered():
    pos = [{"entry_price": 0.5, "size": 100, "won": i % 2} for i in range(5)]
    card = sizing_card(pos, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered" and card.p_value is None


def test_sizing_card_is_pure_deterministic():
    pos = [{"entry_price": 0.45, "size": 1000 if i < 24 else 50, "won": 1 if i < 24 else 0}
           for i in range(40)]
    a = sizing_card(pos, n_perm=200, seed=3)
    b = sizing_card(pos, n_perm=200, seed=3)
    assert (a.effect, a.p_value, a.evidence_state) == (b.effect, b.p_value, b.evidence_state)
