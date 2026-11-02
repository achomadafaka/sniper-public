"""discovery_card — first-attention edge: she enters before attention arrives, then
independent wallets trade her direction and price drifts her way.

Positions need attention fields: {entry_ts, is_first_n (bool), independent_followers_2h
(int), self_volume_frac (her trade as frac of +/-1h volume), drifted_her_way (bool)}.
Self-referential markets (self_volume_frac >= 0.20) are excluded. Null = permute the
first-N labels (position-level analog of the spec's entry-timestamp shuffle, which
needs per-market trade sequences absent from the current Becker feed).

DATA REALITY: the Becker trades parquet (build_cse_pit_index.py:10) carries no per-fill
timestamp or trader identity, so independent_followers_2h / self_volume_frac cannot be
populated from current data. When those fields are absent the card self-reports
data_tier=BLOCKED + underpowered rather than fabricating a signal.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.discovery_card import discovery_card


def _attn(first, followers, drift, self_frac=0.02):
    return {"entry_ts": 1770000000, "is_first_n": first, "independent_followers_2h": followers,
            "self_volume_frac": self_frac, "drifted_her_way": drift}


def test_discovery_card_blocked_when_attention_fields_absent():
    # current real feed: only entry_ts + price -> no attention fields -> BLOCKED, not fabricated
    pos = [{"entry_ts": 1770000000, "entry_price": 0.4} for _ in range(40)]
    card = discovery_card(pos, n_perm=300, seed=1)
    assert card.card_id == "discovery_card"
    assert card.evidence_state == "underpowered"
    assert card.evidence.get("data_tier") == "BLOCKED"
    assert card.p_value is None


def test_discovery_card_blocked_when_self_volume_missing():
    # attention fields present but self_volume_frac absent → can't verify she wasn't the
    # volume → must NOT treat missing as 0.0 and run; return BLOCKED.
    pos = [{"entry_ts": 1770000000, "is_first_n": True, "independent_followers_2h": 5,
            "drifted_her_way": True} for _ in range(40)]
    card = discovery_card(pos, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered"
    assert card.evidence.get("data_tier") == "BLOCKED"


def test_discovery_card_detects_first_attention_edge():
    # she is first-N, gets >=3 independent followers, and price drifts her way
    pos = [_attn(True, 5, True) for _ in range(30)]
    pos += [_attn(False, 0, False) for _ in range(10)]   # late, unfollowed, no drift
    card = discovery_card(pos, n_perm=400, seed=1)
    assert card.effect > 0
    assert card.p_value is not None and card.p_value < 0.10
    assert card.evidence_state == "supported"
    assert card.evidence.get("data_tier") == "PARTIAL"
    assert card.q_value is None  # router-owned


def test_discovery_card_excludes_self_referential_markets():
    # huge self-volume share (she WAS the volume) excluded -> too few left -> underpowered
    pos = [_attn(True, 5, True, self_frac=0.80) for _ in range(40)]
    card = discovery_card(pos, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered"
    assert card.evidence.get("n_after_self_volume_exclusion") == 0


def test_discovery_card_no_followers_is_contradicted_or_exploratory():
    # first-N but nobody follows and no drift -> qualifying fraction ~0
    pos = [_attn(True, 0, False) for _ in range(40)]
    card = discovery_card(pos, n_perm=300, seed=1)
    assert card.evidence_state in ("contradicted", "exploratory")


def test_discovery_card_is_pure_deterministic():
    pos = [_attn(True, 5, True) for _ in range(25)] + [_attn(False, 0, False) for _ in range(15)]
    a = discovery_card(pos, n_perm=200, seed=4)
    b = discovery_card(pos, n_perm=200, seed=4)
    assert (a.effect, a.p_value, a.evidence_state) == (b.effect, b.p_value, b.evidence_state)
