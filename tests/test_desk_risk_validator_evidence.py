"""The live parser applies evidence predicates after its existing mechanical guards."""

import copy
import json

import pytest

from sniper.services import memecoin_desk_risk_live as L


POLICY = {"stop_pct": 20, "trail_pct": 15, "tp_pct": 60}
BOUNDS = {"sl_pct": (10, 40), "trail_pct": (8, 30), "tp_pct": (15, 150)}


def block(key="pos:1"):
    return {"k": key, "sb": 0, "pnl_pct_exact": None,
            "rails": {"sl_pct": 20, "trail_pct": 15, "tp_pct": 60},
            "ev": {"fresh": True, "n5": 0.02, "entry_price": 1, "last_price": 1.10,
                   "buy_sol_30s": 1, "sell_sol_30s": 2, "distinct_sellers_30s": 3,
                   "buy_sol_prev_30s": 3}}


def proposal(key="pos:1", action="E", code=11, rails=None):
    return {"k": key, "a": action, "r": rails, "c": code}


def validate(items, blocks=None, policies=None, **kwargs):
    blocks = [block()] if blocks is None else blocks
    policies = {b["k"]: dict(POLICY) for b in blocks} if policies is None else policies
    return L.validate(json.dumps(items), blocks=blocks, policies=policies, bounds=BOUNDS, **kwargs)


def assert_unbacked(result, code):
    assert result[:2] == ("HOLD", None)
    assert result[2].startswith("unbacked_reason:%s" % code)
    assert result[3] == code


def test_mixed_batch_gates_each_position_against_its_own_code_built_evidence():
    blocks = [block("pos:%s" % i) for i in range(1, 5)]
    blocks[1]["ev"]["distinct_sellers_30s"] = None
    del blocks[3]["ev"]
    items = [proposal("pos:1"), proposal("pos:2"),
             proposal("pos:3", "T", 6, {"trail_pct": 10}), proposal("pos:4", "T", 6, {"trail_pct": 10})]
    before = copy.deepcopy(blocks)
    out = validate(items, blocks, gate_params={})
    assert out["pos:1"] == ("EXIT", None, "exit", 11)
    assert out["pos:3"] == ("TIGHTEN", {"trail_pct": 10.0}, "tighten", 6)
    assert_unbacked(out["pos:2"], 11)
    assert_unbacked(out["pos:4"], 6)
    assert blocks == before


def test_explicit_threshold_is_per_call_and_does_not_change_later_defaults():
    item = [proposal()]
    assert validate(item, gate_params={})["pos:1"][0] == "EXIT"
    assert_unbacked(validate(item, gate_params={"flow_sell_multiple": 3})["pos:1"], 11)
    assert validate(item, gate_params={})["pos:1"][0] == "EXIT"


@pytest.mark.parametrize("action,code,rails", [
    ("E", 6, None), ("E", 16, None), ("T", 11, {"trail_pct": 10}),
    ("T", 6, {"sl_pct": 12}), ("T", 8, {"tp_pct": 40, "trail_pct": 10}),
])
def test_wrong_reason_action_or_rail_pair_is_unbacked(action, code, rails):
    out = validate([proposal(action=action, code=code, rails=rails)], gate_params={})
    assert_unbacked(out["pos:1"], code)


@pytest.mark.parametrize("fresh", [None, False, 1])
def test_missing_or_unproven_coverage_is_unbacked(fresh):
    b = block()
    b["ev"]["fresh"] = fresh
    assert_unbacked(validate([proposal()], [b], gate_params={})["pos:1"], 11)


@pytest.mark.parametrize("trail", [None, 0])
def test_code9_can_add_a_known_disabled_trail_without_market_or_quote_evidence(trail):
    b = {"k": "pos:1", "rails": {"trail_pct": trail}, "pnl_pct_exact": None}
    out = validate([proposal(action="T", code=9, rails={"trail_pct": 8})], [b],
                   policies={"pos:1": {**POLICY, "trail_pct": trail}}, gate_params={})
    assert out["pos:1"] == ("TIGHTEN", {"trail_pct": 8.0}, "tighten", 9)


def test_code9_cannot_use_an_absent_current_trail_key_as_proof():
    b = {"k": "pos:1", "rails": {}, "pnl_pct_exact": None}
    out = validate([proposal(action="T", code=9, rails={"trail_pct": 8})], [b],
                   policies={"pos:1": {**POLICY, "trail_pct": None}}, gate_params={})
    assert_unbacked(out["pos:1"], 9)


@pytest.mark.parametrize("gate_params", [None, {}])
@pytest.mark.parametrize("sb", [None, 0])
def test_code10_requires_structure_confirmation_even_with_the_gate_disabled(gate_params, sb):
    b = block()
    b["sb"] = sb
    assert validate([proposal(code=10)], [b], gate_params=gate_params)["pos:1"] == (
        "HOLD", None, "unbacked_reason:10", 10)


def test_confirmed_code10_requires_fresh_evidence_only_when_the_gate_is_enabled():
    b = {"k": "pos:1", "sb": 1, "pnl_pct_exact": None}
    assert validate([proposal(code=10)], [b])["pos:1"] == ("EXIT", None, "exit", 10)
    assert_unbacked(validate([proposal(code=10)], [b], gate_params={})["pos:1"], 10)
    b["ev"] = {"fresh": True}
    assert validate([proposal(code=10)], [b], gate_params={})["pos:1"] == ("EXIT", None, "exit", 10)


def test_gate_none_preserves_legacy_exit_and_tighten_callers_without_evidence():
    blocks = [{"k": "pos:1", "pnl_pct_exact": None}, {"k": "pos:2", "pnl_pct_exact": None}]
    items = [proposal("pos:1"), proposal("pos:2", "T", 6, {"trail_pct": 10})]
    legacy = validate(items, blocks)
    assert legacy["pos:1"][:2] == ("EXIT", None)
    assert legacy["pos:2"][:2] == ("TIGHTEN", {"trail_pct": 10.0})
    assert validate(items, blocks, gate_params=None) == legacy


@pytest.mark.parametrize("rails,code,pnl,reason", [
    ({"trail_pct": 20}, 6, None, "tighten_loosens:trail_pct"),
    ({"sl_pct": 12}, 7, -13, "tighten_triggers:sl_pct"),
    ({"tp_pct": 40}, 8, 45, "tighten_triggers:tp_pct"),
])
def test_existing_rail_and_exact_quote_trigger_guards_keep_precedence(rails, code, pnl, reason):
    b = block()
    b["pnl_pct_exact"] = pnl
    out = validate([proposal(action="T", code=code, rails=rails)], [b], gate_params={})
    assert out["pos:1"] == ("HOLD", None, reason, code)


def test_loosen_uses_its_existing_independent_validator_without_evidence(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_LOOSEN", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", 25)
    item = proposal(action="L", code=17, rails={"trail_pct": 22})
    item["f"] = ["trend_up", "higher_lows"]
    b = {"k": "pos:1", "pnl_pct_exact": None}
    out = validate([item], [b], gate_params={})
    assert out["pos:1"] == ("LOOSEN_TRAIL", {"trail_pct": 22.0}, "loosen:higher_lows,trend_up", 17)
