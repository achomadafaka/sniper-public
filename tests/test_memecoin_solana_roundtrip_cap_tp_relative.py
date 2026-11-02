"""The Solana entry round-trip budget is a fraction of the position's own take-profit.

Operator decision, 2026-09-08. The two funded chains capped one concept under two rules:

    solana   MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT = 10          flat, absolute
    EVM      0.4 x entry_tp_pct(chain)                       TP-relative

They agreed only by arithmetic accident — TP is 25.0 on all five bands and 0.4*25.0 == 10.0
exactly — so a TP retune moved the EVM budget and left Solana's stale. Of the 21 live Solana
round-trip refusals, 7 break even between TP 25.1% and 27.8%: a 2-point retune separates the two
rules on a third of them. The five oldest cost 70-95% round trip and stay refused under any
plausible TP, which is what makes widening safe to reason about.

The TP is read from the reservation's FROZEN funded contract, not re-resolved, so the budget the
gate spends is the budget the position is then managed under.

Everything here is a no-op at today's TP. That is the point, and the first test pins it.
"""
import json

import pytest

from sniper.settings.memecoin import entry_roundtrip_cap_pct
from sniper.execution.solana_swap_executor import _contract_tp_pct


def _pending(tp):
    return {"funded_contract_json": json.dumps({"policy": {"tp_pct": tp, "coin_band": "micro"}})}


# --- the no-op guarantee -------------------------------------------------------------------------

def test_at_todays_tp_the_budget_is_exactly_the_flat_cap_it_replaces():
    """THE SAFETY PROPERTY. Not 'about 10' — exactly 10.0, so no live decision moves."""
    assert 0.4 * 25.0 == 10.0
    assert entry_roundtrip_cap_pct(15.0, 25.0) == 10.0


def test_the_three_live_refusals_today_still_refuse():
    """DDR5 10.15%, BOBBLEHEAD 11.11%, KEYCAT 10.16% — all over budget at TP 25."""
    cap = entry_roundtrip_cap_pct(15.0, 25.0)
    for cost in (10.15, 11.11, 10.16):
        assert cost > cap, cost


# --- what changes, and only when TP moves --------------------------------------------------------

def test_a_wider_take_profit_buys_a_wider_budget():
    assert entry_roundtrip_cap_pct(15.0, 35.0) == pytest.approx(14.0)


def test_a_narrower_take_profit_tightens_it():
    """The flat rule could not do this either — it goes stale in BOTH directions."""
    assert entry_roundtrip_cap_pct(15.0, 15.0) == pytest.approx(6.0)


def test_a_two_point_retune_separates_the_rules_on_a_real_refusal():
    """NCAT broke even at 25.98%. Refused at TP 25, admitted at TP 27 — the whole decision."""
    assert 10.392 > entry_roundtrip_cap_pct(15.0, 25.0)      # NCAT cost, refused today
    assert 10.392 <= entry_roundtrip_cap_pct(15.0, 27.0)     # admitted after a 2-point retune


def test_the_broken_routes_stay_refused_at_any_plausible_tp():
    """The five oldest refusals cost 70-95%. Widening must not readmit them."""
    for tp in (25.0, 35.0, 50.0, 100.0):
        assert 70.0 > entry_roundtrip_cap_pct(15.0, tp)


# --- fail-closed, the three ways the EVM rule already is -----------------------------------------

@pytest.mark.parametrize("tp", [None, 0, -5.0, float("nan"), float("inf"), "junk"])
def test_an_unusable_tp_falls_back_to_the_absolute_knob_not_to_passing(tp):
    """A bare TP-relative test would return 0 and admit everything whenever TP is missing."""
    assert entry_roundtrip_cap_pct(15.0, tp) == entry_roundtrip_cap_pct(15.0)


def test_a_caller_that_supplies_no_tp_is_byte_identical():
    """The LI.FI bridge callers price a bridge, not a position. They must not move."""
    for one_way in (5.0, 15.0, None, "nan"):
        assert entry_roundtrip_cap_pct(one_way, None) == entry_roundtrip_cap_pct(one_way)


def test_the_fraction_can_be_switched_off_without_disabling_the_gate(monkeypatch):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC", 0.0)
    assert cfg.entry_roundtrip_cap_pct(15.0, 25.0) == cfg.entry_roundtrip_cap_pct(15.0)
    assert cfg.entry_roundtrip_cap_pct(15.0, 25.0) > 0, "switching the fraction off must not open the gate"


# --- reading the TP off the frozen contract ------------------------------------------------------

def test_the_tp_comes_from_the_reservations_own_contract():
    assert _contract_tp_pct(_pending(25.0)) == 25.0
    assert _contract_tp_pct(_pending(35.0)) == 35.0


@pytest.mark.parametrize("pending", [
    None, {}, {"funded_contract_json": None}, {"funded_contract_json": ""},
    {"funded_contract_json": "{not json"}, {"funded_contract_json": "{}"},
    {"funded_contract_json": json.dumps({"policy": {}})},
    {"funded_contract_json": json.dumps({"policy": {"tp_pct": None}})},
    {"funded_contract_json": json.dumps({"policy": {"tp_pct": "abc"}})},
])
def test_a_missing_or_malformed_contract_yields_none_never_a_number(pending):
    """A fabricated TP here would silently widen or narrow a funded entry gate."""
    assert _contract_tp_pct(pending) is None


def test_the_executor_passes_the_contract_tp_into_the_cap():
    import inspect
    import sniper.execution.solana_swap_executor as X
    src = inspect.getsource(X.execute_entry)
    assert "entry_roundtrip_cap_pct(cap, _contract_tp_pct(pending))" in src
