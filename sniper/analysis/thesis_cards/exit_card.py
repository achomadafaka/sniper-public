"""exit_card producer — is the wallet's alpha in SELLING, not buying?

Statistic: actual realized PnL − hold-to-resolution PnL, on EARLY-EXIT positions
only (realized != hold), **stratified by entry-price band** so entry quality (the
confounder — good entries make any exit look good) is absorbed. Null: within-stratum
sign-flip permutation of the per-position exit gains (does her exit timing add value
beyond chance?). Pure function; returns a ThesisCard (a lead, not a claim).

Spec: docs/superpowers/specs/2026-05-30-thesis-extractor-multi-lens.md rev2,
§Pre-registered card protocols / exit_card (IMPORTANT-3).
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card

MIN_PER_STRATUM = 15
_BANDS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))


def _band(price: float) -> str:
    for lo, hi in _BANDS:
        if lo <= price < hi:
            return f"[{lo},{hi})"
    return "[0.8,1.0)"


def _gain(p: Mapping[str, Any]) -> tuple[str, float] | None:
    """(entry_band, exit_gain) for an explicitly-ATTRIBUTED early-exit position.

    Early exits come ONLY from the `is_early_exit` flag (set upstream by exit
    attribution from real SELL evidence) — never inferred from realized != hold, since
    a fee/rounding/settlement mismatch is not an exit decision. None if not an
    attributed early exit, or malformed."""
    if not p.get("is_early_exit"):
        return None
    try:
        entry = float(p["entry_price"]); size = float(p["size"])
        won = int(p["won"]); realized = float(p["realized_pnl"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= entry <= 1.0) or size <= 0:
        return None
    hold = size * (won - entry)
    return _band(entry), realized - hold


def _weighted_mean(strata: dict[str, list[float]]) -> float:
    num = den = 0.0
    for gains in strata.values():
        num += sum(gains)          # stratum total
        den += len(gains)          # stratum n
    return num / den if den else 0.0


def exit_card(positions: Sequence[Mapping[str, Any]], *, n_perm: int = 500, seed: int = 17) -> ThesisCard:
    strata: dict[str, list[float]] = {}
    for p in positions:
        g = _gain(p)
        if g is not None:
            strata.setdefault(g[0], []).append(g[1])
    # only strata with enough early-exits are powered
    powered = {b: gs for b, gs in strata.items() if len(gs) >= MIN_PER_STRATUM}
    n = sum(len(gs) for gs in powered.values())
    by_band = {b: {"n": len(gs), "mean_gain": (sum(gs) / len(gs)) if gs else 0.0}
               for b, gs in strata.items()}

    if not powered:
        return producer_card(card_id="exit_card", lens="exit timing vs hold-to-resolution",
                             n=n, score=0.0, effect=None, p_value=None, placebo_passed=None,
                             evidence_state="underpowered",
                             evidence={"by_band": by_band, "data_tier": "PROXY"})

    obs = _weighted_mean(powered)
    if obs <= 0:
        return producer_card(card_id="exit_card", lens="exit timing vs hold-to-resolution",
                             n=n, score=0.0, effect=obs, p_value=None, placebo_passed=None,
                             evidence_state="contradicted",
                             evidence={"by_band": by_band, "data_tier": "PROXY"})

    # PROXY null only. The pre-registered null is a within-band exit-timestamp shuffle
    # (compare her exit timing to alternate exits in the same band), which needs
    # per-position exit-time price paths we do NOT have. The sign-flip below tests only
    # whether the gain is systematically positive vs symmetric noise — NOT timing skill —
    # so it is reported as proxy_signflip_p, the card stays DESCRIPTIVE_ONLY, and
    # placebo_passed is left None so admission will not promote it.
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        perm = {b: [g if rng.random() < 0.5 else -g for g in gs] for b, gs in powered.items()}
        if _weighted_mean(perm) >= obs:
            ge += 1
    proxy_p = (ge + 1) / (n_perm + 1)
    return producer_card(
        card_id="exit_card", lens="exit timing vs hold-to-resolution",
        n=n, score=max(0.0, obs), effect=obs, p_value=None, placebo_passed=None,
        evidence_state="descriptive_only",
        evidence={"by_band": by_band, "n_powered_strata": len(powered),
                  "data_tier": "PROXY", "proxy_signflip_p": proxy_p},
    )
