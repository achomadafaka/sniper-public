"""timing_card producer — does she profit from trading STALE books (the dead zone)?

Thesis: entries placed when liquidity is thin (overnight UTC / weekend) get repriced
favorably in the next session. Statistic = mean post-entry her-side drift on dead-hour
entries minus live-hour entries. Null = permute the dead/live labels (the repricing
must concentrate on her dead-hour entries beyond chance). Pure function; returns a
ThesisCard (a lead, not a claim).

DATA TIER = PARTIAL: `next_session_price` (the her-side price ~1 day after entry) is
sampled from the market price series, which truncates near the Becker freeze — those
positions arrive with next_session_price=None and are dropped here. Spec:
2026-05-30-thesis-extractor-multi-lens rev2, §timing_card (MINOR — data tier).
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card

MIN_N = 20            # total positions with post-entry price
MIN_PER_BUCKET = 8    # need both dead and live represented to contrast
_DEAD_UTC_HOURS = range(2, 10)   # ~21:00–05:00 US Eastern: thin overnight books

# How this card is forward-tested if it enrolls (enrollment contract §5). Real test
# but data_tier=PARTIAL near the freeze — PARTIAL may enroll WITH this design.
_FORWARD = {
    "metric": "dead_zone_drift_gap",       # forward dead-hour minus live-hour drift
    "horizon": "next_session",
    "success_threshold": 0.0,
    "kill_threshold": -0.02,
    "min_forward_n": MIN_N,
    "stake_convention": "uniform_paper_unit",
}


def _is_dead(ts: int) -> bool:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt.weekday() >= 5:          # Sat/Sun
        return True
    return dt.hour in _DEAD_UTC_HOURS


def _gap(drifts: list[float], dead_mask: list[bool]) -> float:
    d = [x for x, m in zip(drifts, dead_mask) if m]
    l = [x for x, m in zip(drifts, dead_mask) if not m]
    if not d or not l:
        return 0.0
    return sum(d) / len(d) - sum(l) / len(l)


def timing_card(positions: Sequence[Mapping[str, Any]], *, n_perm: int = 500, seed: int = 23) -> ThesisCard:
    drifts: list[float] = []
    dead_mask: list[bool] = []
    for p in positions:
        nsp = p.get("next_session_price")
        try:
            ts = int(p["entry_ts"]); entry = float(p["entry_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if nsp is None or not (0.0 < entry < 1.0):
            continue
        try:
            nsp = float(nsp)
        except (TypeError, ValueError):
            continue
        drifts.append(nsp - entry)          # her-side post-entry repricing (ADR-003)
        dead_mask.append(_is_dead(ts))

    n = len(drifts)
    n_dead = sum(dead_mask)
    n_live = n - n_dead
    by_bucket = {
        "dead": {"n": n_dead, "mean_drift": (sum(x for x, m in zip(drifts, dead_mask) if m) / n_dead) if n_dead else 0.0},
        "live": {"n": n_live, "mean_drift": (sum(x for x, m in zip(drifts, dead_mask) if not m) / n_live) if n_live else 0.0},
    }
    base_ev = {"by_bucket": by_bucket, "data_tier": "PARTIAL", "n_with_post_entry_price": n}

    if n < MIN_N or n_dead < MIN_PER_BUCKET or n_live < MIN_PER_BUCKET:
        return producer_card(card_id="timing_card", lens="dead-zone entry -> next-session repricing",
                             n=n, score=0.0, effect=None, p_value=None, placebo_passed=None,
                             evidence_state="underpowered", evidence=base_ev,
                             forward_test_design=_FORWARD)

    obs = _gap(drifts, dead_mask)
    if obs <= 0:
        return producer_card(card_id="timing_card", lens="dead-zone entry -> next-session repricing",
                             n=n, score=0.0, effect=obs, p_value=None, placebo_passed=None,
                             evidence_state="contradicted", evidence=base_ev,
                             forward_test_design=_FORWARD)

    rng = random.Random(seed)
    mask = list(dead_mask)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(mask)
        if _gap(drifts, mask) >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    supported = p < 0.05
    return producer_card(
        card_id="timing_card", lens="dead-zone entry -> next-session repricing",
        n=n, score=max(0.0, obs), effect=obs, p_value=p, placebo_passed=supported,
        evidence_state="supported" if supported else "exploratory", evidence=base_ev,
        forward_test_design=_FORWARD,
    )
