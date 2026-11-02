"""Thesis-profile assembler: one wallet -> all cards -> ranked profile + admission
annotations + one honest headline.

Contract:
- runs every producer over the wallet's positions (cards degrade honestly on missing fields);
- applies the router admission rule (q/eligible/rank annotations);
- shows ALL cards — eligible ones ranked first, dead-ends (descriptive_only / BLOCKED /
  underpowered / contradicted) shown, never hidden;
- headline names the forward-testable edge, or honestly says there is none.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.analysis.thesis_cards.contract import producer_card
from sniper.analysis.thesis_cards.profile import assemble_profile, ThesisProfile

_DEAD = int(datetime(2026, 2, 3, 4, tzinfo=timezone.utc).timestamp())
_LIVE = int(datetime(2026, 2, 3, 15, tzinfo=timezone.utc).timestamp())

PRODUCER_IDS = {"exit_card", "sizing_card", "timing_card", "discovery_card"}


def _conviction_positions():
    # positive base edge (60% @ 0.45) + big sizing on winners -> sizing_card supported/eligible
    return [{"entry_price": 0.45, "size": 1000 if i < 24 else 50, "won": 1 if i < 24 else 0}
            for i in range(40)]


def test_profile_runs_all_producers_and_hides_nothing():
    prof = assemble_profile("0xabc", _conviction_positions())
    assert isinstance(prof, ThesisProfile)
    assert prof.wallet == "0xabc"
    ids = {c.card_id for c in prof.cards}
    assert PRODUCER_IDS <= ids                    # all four present, none hidden
    assert prof.n_positions == 40


def test_profile_promotes_real_sizing_edge_and_shows_dead_ends():
    prof = assemble_profile("0xabc", _conviction_positions())
    elig = [c for c in prof.cards if c.eligible_for_paper]
    assert [c.card_id for c in elig] == ["sizing_card"]
    assert prof.cards[0].card_id == "sizing_card"  # eligible ranked first
    assert prof.cards[0].q_value is not None and prof.cards[0].rank_key is not None
    # dead-ends present and honestly stated
    by_id = {c.card_id: c for c in prof.cards}
    assert by_id["discovery_card"].evidence.get("data_tier") == "BLOCKED"
    assert by_id["exit_card"].evidence_state == "underpowered"     # no is_early_exit rows
    assert by_id["sizing_card"].card_id in prof.headline or "sizing" in prof.headline.lower()


def test_profile_no_edge_is_honest():
    # 50% @ 0.50 -> zero base edge, no sizing skill, no other fields -> nothing eligible
    pos = [{"entry_price": 0.5, "size": 100, "won": i % 2} for i in range(40)]
    prof = assemble_profile("0xdead", pos)
    assert not any(c.eligible_for_paper for c in prof.cards)
    assert "no forward-testable edge" in prof.headline.lower()
    # still shows every card's state
    assert {c.card_id for c in prof.cards} >= PRODUCER_IDS


_FTD = {"metric": "calibration_edge", "horizon": "to_resolution", "success_threshold": 0.0,
        "kill_threshold": -0.05, "min_forward_n": 30, "stake_convention": "uniform_paper_unit"}


def test_profile_accepts_extra_cards_and_ranks_by_score():
    # a pre-built selection_card (e.g. CSE adapter) with higher score outranks sizing.
    # It MUST carry a forward_test_design to be enrollable (enrollment contract).
    extra = producer_card(card_id="selection_card", lens="picks beat price paid", n=40,
                          score=9.0, effect=0.09, p_value=0.001, placebo_passed=True,
                          evidence_state="supported", forward_test_design=_FTD)
    prof = assemble_profile("0xabc", _conviction_positions(), extra_cards=[extra])
    elig = [c.card_id for c in prof.cards if c.eligible_for_paper]
    assert "selection_card" in elig and "sizing_card" in elig
    assert prof.cards[0].card_id == "selection_card"   # higher score ranks first


def test_profile_extra_card_without_forward_design_is_not_enrolled():
    # statistically eligible but no forward_test_design -> rejected missing_forward_test_design
    extra = producer_card(card_id="selection_card", lens="picks beat price paid", n=40,
                          score=9.0, effect=0.09, p_value=0.001, placebo_passed=True,
                          evidence_state="supported")  # no forward_test_design
    prof = assemble_profile("0xabc", _conviction_positions(), extra_cards=[extra])
    assert not any(c.card_id == "selection_card" and c.eligible_for_paper for c in prof.cards)
    dec = {d.card.card_id: d for d in prof.decisions}
    assert dec["selection_card"].reason == "missing_forward_test_design"


def test_profile_run_id_is_deterministic():
    pos = _conviction_positions()
    a = assemble_profile("0xabc", pos)
    b = assemble_profile("0xabc", pos)
    assert a.profile_run_id == b.profile_run_id and len(a.profile_run_id) == 16


def test_profile_run_id_changes_with_content():
    a = assemble_profile("0xabc", _conviction_positions())
    other = [{"entry_price": 0.5, "size": 100, "won": i % 2} for i in range(40)]
    b = assemble_profile("0xabc", other)
    c = assemble_profile("0xDIFFERENT", _conviction_positions())   # wallet differs too
    assert a.profile_run_id != b.profile_run_id
    assert a.profile_run_id != c.profile_run_id


def test_profile_decisions_account_for_every_card():
    prof = assemble_profile("0xabc", _conviction_positions())
    assert {d.card.card_id for d in prof.decisions} >= PRODUCER_IDS
    # discovery is BLOCKED -> its decision reason is the data-tier one, not ineligible_q
    dec = {d.card.card_id: d for d in prof.decisions}
    assert dec["discovery_card"].reason == "ineligible_data_tier"
    assert dec["exit_card"].reason == "ineligible_data_tier"        # PROXY tier


def test_profile_is_pure_deterministic():
    pos = _conviction_positions()
    a = assemble_profile("0xabc", pos)
    b = assemble_profile("0xabc", pos)
    assert [(c.card_id, c.evidence_state, c.eligible_for_paper) for c in a.cards] == \
           [(c.card_id, c.evidence_state, c.eligible_for_paper) for c in b.cards]
    assert a.headline == b.headline
