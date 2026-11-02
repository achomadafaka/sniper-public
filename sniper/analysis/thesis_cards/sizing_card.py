"""sizing_card producer — did she size the RIGHT trades (not just win more often)?

Statistic: dollar-weighted calibration − equal-weighted calibration (does she put
more capital where her edge is real?). Near-certainty entries (>=0.80) are EXCLUDED
so "big bets on favorites" aren't miscounted as conviction. Null: within-wallet size
permutation — shuffle her actual sizes across her actual resolved outcomes (entry
prices fixed); without it the comparison is descriptive, not skill. Pure function;
returns a ThesisCard.

Spec: rev2 §Pre-registered card protocols / sizing_card (IMPORTANT-2).
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card

MIN_N = 20
CERTAINTY_CUT = 0.80

# How this card is forward-tested if it enrolls (enrollment contract §5).
_FORWARD = {
    "metric": "calibration_edge",          # forward dollar-wtd − equal-wtd calibration
    "horizon": "to_resolution",
    "success_threshold": 0.0,              # edge must persist > 0 forward
    "kill_threshold": -0.05,
    "min_forward_n": MIN_N,
    "stake_convention": "uniform_paper_unit",
}


def _cal_edge(wons, entries, sizes) -> float:
    """dollar-weighted minus equal-weighted calibration."""
    tot = sum(sizes)
    if tot <= 0 or not wons:
        return 0.0
    dw = (sum(s * w for s, w in zip(sizes, wons)) / tot) - (sum(s * e for s, e in zip(sizes, entries)) / tot)
    ew = (sum(wons) / len(wons)) - (sum(entries) / len(entries))
    return dw - ew


def sizing_card(positions: Sequence[Mapping[str, Any]], *, n_perm: int = 500, seed: int = 19) -> ThesisCard:
    wons, entries, sizes = [], [], []
    for p in positions:
        try:
            e = float(p["entry_price"]); s = float(p["size"]); w = int(p["won"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 < e < CERTAINTY_CUT) or s <= 0:   # exclude near-certainties (>=0.80) + invalid
            continue
        entries.append(e); sizes.append(s); wons.append(w)
    n = len(wons)
    if n < MIN_N:
        return producer_card(card_id="sizing_card", lens="dollar-weighted vs equal-weighted calibration",
                             n=n, score=0.0, effect=None, p_value=None, placebo_passed=None,
                             evidence_state="underpowered", evidence={"n_after_certainty_filter": n},
                             forward_test_design=_FORWARD)

    obs = _cal_edge(wons, entries, sizes)              # sizing edge = dollar-wtd − equal-wtd
    base_cal = sum(wons) / n - sum(entries) / n        # underlying (equal-wtd) calibration edge

    # spec req (2): the underlying calibration edge must pass its OWN test — she must
    # actually have a positive base edge. Poisson-binomial null: each pick wins with the
    # market's implied prob (entry); is her win rate above that beyond chance?
    rng_b = random.Random(seed + 1)
    geb = 0
    for _ in range(n_perm):
        sim_wins = sum(1 for e in entries if rng_b.random() < e)
        if (sim_wins / n - sum(entries) / n) >= base_cal:
            geb += 1
    p_base = (geb + 1) / (n_perm + 1)
    base_ok = base_cal > 0 and p_base < 0.10

    evidence = {"n_after_certainty_filter": n, "dollar_weighted_minus_equal_weighted": obs,
                "base_calibration": base_cal, "p_base": p_base}

    if obs <= 0:
        return producer_card(card_id="sizing_card", lens="dollar-weighted vs equal-weighted calibration",
                             n=n, score=0.0, effect=obs, p_value=None, placebo_passed=None,
                             evidence_state="contradicted", evidence=evidence,
                             forward_test_design=_FORWARD)

    # spec req (1)+(3): within-wallet size permutation — shuffle sizes across (won, entry) pairs.
    rng = random.Random(seed)
    perm_sizes = list(sizes)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(perm_sizes)
        if _cal_edge(wons, entries, perm_sizes) >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    # supported requires ALL THREE: positive sizing edge, base edge passes, sizing perm significant
    supported = base_ok and p < 0.10
    return producer_card(
        card_id="sizing_card", lens="dollar-weighted vs equal-weighted calibration",
        n=n, score=max(0.0, obs), effect=obs, p_value=p, placebo_passed=supported,
        evidence_state="supported" if supported else "exploratory", evidence=evidence,
        forward_test_design=_FORWARD,
    )
