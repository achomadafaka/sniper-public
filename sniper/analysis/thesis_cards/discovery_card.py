"""discovery_card producer — first-attention edge: she enters BEFORE attention arrives,
then independent wallets trade her direction and price drifts her way.

Statistic = fraction of her (non-self-referential) entries that are first-N-trader AND
draw >=3 independent followers within 2h AND drift her way. Null = permute the
first-N labels (does being-early co-occur with followed drift beyond chance?). Pure
function; returns a ThesisCard.

DATA REALITY (honest, not optimistic): the current Becker trades parquet
(build_cse_pit_index.py:10) has NO per-fill timestamp and NO trader identity, so
`independent_followers_2h` and `self_volume_frac` cannot be populated from current
data. When the attention fields are absent the card returns data_tier=BLOCKED +
underpowered — the producer is correct and ready for a future fill feed with wallet
identity, but it will not fabricate a first-attention signal the data can't support.
Spec: rev2 §discovery_card (IMPORTANT-4 + MINOR-1). The literal entry-timestamp-shuffle
null needs per-market trade sequences (also absent), so the first-N-label permutation
is the documented position-level analog; PARTIAL even when fields are supplied.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card

MIN_N = 20
SELF_VOLUME_CUT = 0.20    # her trade >= 20% of +/-1h volume -> self-referential, excluded
MIN_FOLLOWERS = 3         # independent wallets trading her direction within 2h


def _has_attention(p: Mapping[str, Any]) -> bool:
    # self_volume_frac is REQUIRED: without it we can't verify she wasn't the volume,
    # so the self-referential exclusion can't be applied → treat as missing data, not 0.
    return all(p.get(k) is not None
               for k in ("is_first_n", "independent_followers_2h", "drifted_her_way", "self_volume_frac"))


def _qual_fraction(firsts: list[bool], followed: list[bool], drifted: list[bool]) -> float:
    if not firsts:
        return 0.0
    return sum(1 for f, fo, dr in zip(firsts, followed, drifted) if f and fo and dr) / len(firsts)


def discovery_card(positions: Sequence[Mapping[str, Any]], *, n_perm: int = 500, seed: int = 29) -> ThesisCard:
    attn = [p for p in positions if _has_attention(p)]
    if len(attn) < MIN_N:
        # current real feed can't populate attention fields -> blocked, not fabricated
        return producer_card(card_id="discovery_card", lens="first-attention -> followed drift",
                             n=len(attn), score=0.0, effect=None, p_value=None, placebo_passed=None,
                             evidence_state="underpowered",
                             evidence={"data_tier": "BLOCKED",
                                       "reason": "Becker trades parquet has no per-fill wallet identity "
                                                 "or sub-hour timestamp; independent_followers_2h / "
                                                 "self_volume_frac cannot be derived from current data",
                                       "n_with_attention_fields": len(attn)})

    # exclude self-referential markets (she was the volume); self_volume_frac is present
    # for every attn position (guaranteed by _has_attention).
    kept = [p for p in attn if float(p["self_volume_frac"]) < SELF_VOLUME_CUT]
    n = len(kept)
    if n < MIN_N:
        return producer_card(card_id="discovery_card", lens="first-attention -> followed drift",
                             n=n, score=0.0, effect=None, p_value=None, placebo_passed=None,
                             evidence_state="underpowered",
                             evidence={"data_tier": "PARTIAL", "n_after_self_volume_exclusion": n})

    firsts = [bool(p["is_first_n"]) for p in kept]
    followed = [int(p["independent_followers_2h"]) >= MIN_FOLLOWERS for p in kept]
    drifted = [bool(p["drifted_her_way"]) for p in kept]
    obs = _qual_fraction(firsts, followed, drifted)
    base_ev = {"data_tier": "PARTIAL", "n_after_self_volume_exclusion": n,
               "qualifying_fraction": obs}

    if obs <= 0:
        return producer_card(card_id="discovery_card", lens="first-attention -> followed drift",
                             n=n, score=0.0, effect=obs, p_value=None, placebo_passed=None,
                             evidence_state="contradicted", evidence=base_ev)

    # null: permute first-N labels (break the early<->followed-drift association)
    rng = random.Random(seed)
    perm_first = list(firsts)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(perm_first)
        if _qual_fraction(perm_first, followed, drifted) >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    supported = p < 0.10
    return producer_card(
        card_id="discovery_card", lens="first-attention -> followed drift",
        n=n, score=max(0.0, obs), effect=obs, p_value=p, placebo_passed=supported,
        evidence_state="supported" if supported else "exploratory", evidence=base_ev,
    )
