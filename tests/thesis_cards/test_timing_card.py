"""timing_card — dead-zone (stale-book) entry → next-session repricing.

Positions: {entry_ts (unix), entry_price, next_session_price (her-side price ~1d
post-entry; None if unavailable near the data freeze)}. Statistic = mean post-entry
drift on dead-hour entries minus live-hour entries (does trading the stale book
capture more repricing?). Null = permute dead/live labels. data_tier=PARTIAL
(post-entry price truncates near the Becker freeze).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.analysis.thesis_cards.timing_card import timing_card

_DEAD = int(datetime(2026, 2, 3, 4, tzinfo=timezone.utc).timestamp())    # Tue 04:00 UTC -> dead
_LIVE = int(datetime(2026, 2, 3, 15, tzinfo=timezone.utc).timestamp())   # Tue 15:00 UTC -> live
_WKND = int(datetime(2026, 2, 7, 15, tzinfo=timezone.utc).timestamp())   # Sat -> dead


def test_timing_card_detects_dead_zone_repricing():
    # dead-hour entries reprice +0.15 her way; live-hour entries flat.
    pos = []
    for _ in range(20):
        pos.append({"entry_ts": _DEAD, "entry_price": 0.4, "next_session_price": 0.55})
    for _ in range(20):
        pos.append({"entry_ts": _LIVE, "entry_price": 0.4, "next_session_price": 0.4})
    card = timing_card(pos, n_perm=400, seed=1)
    assert card.card_id == "timing_card"
    assert card.effect > 0
    assert card.p_value is not None and card.p_value < 0.05
    assert card.evidence_state == "supported"
    assert card.evidence.get("data_tier") == "PARTIAL"
    assert card.q_value is None and card.rank_key is None  # router-owned


def test_timing_card_weekend_counts_as_dead():
    pos = [{"entry_ts": _WKND, "entry_price": 0.4, "next_session_price": 0.6} for _ in range(20)]
    pos += [{"entry_ts": _LIVE, "entry_price": 0.4, "next_session_price": 0.4} for _ in range(20)]
    card = timing_card(pos, n_perm=300, seed=1)
    assert card.evidence["by_bucket"]["dead"]["n"] == 20


def test_timing_card_no_dead_advantage_is_contradicted_or_exploratory():
    # dead and live both flat -> no edge
    pos = [{"entry_ts": _DEAD, "entry_price": 0.5, "next_session_price": 0.5} for _ in range(20)]
    pos += [{"entry_ts": _LIVE, "entry_price": 0.5, "next_session_price": 0.5} for _ in range(20)]
    card = timing_card(pos, n_perm=300, seed=1)
    assert card.evidence_state in ("contradicted", "exploratory")


def test_timing_card_underpowered_when_one_bucket_empty():
    pos = [{"entry_ts": _LIVE, "entry_price": 0.4, "next_session_price": 0.6} for _ in range(40)]
    card = timing_card(pos, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered"
    assert card.p_value is None


def test_timing_card_drops_positions_without_post_entry_price():
    # most lack next_session_price (near-freeze) -> too few -> underpowered
    pos = [{"entry_ts": _DEAD, "entry_price": 0.4, "next_session_price": None} for _ in range(30)]
    pos += [{"entry_ts": _LIVE, "entry_price": 0.4, "next_session_price": 0.4} for _ in range(3)]
    card = timing_card(pos, n_perm=200, seed=1)
    assert card.evidence_state == "underpowered"


def test_timing_card_is_pure_deterministic():
    pos = [{"entry_ts": _DEAD, "entry_price": 0.4, "next_session_price": 0.55} for _ in range(20)]
    pos += [{"entry_ts": _LIVE, "entry_price": 0.4, "next_session_price": 0.4} for _ in range(20)]
    a = timing_card(pos, n_perm=200, seed=5)
    b = timing_card(pos, n_perm=200, seed=5)
    assert (a.effect, a.p_value, a.evidence_state) == (b.effect, b.p_value, b.evidence_state)
