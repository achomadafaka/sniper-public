"""WO-3 — LP-removal severity.

`depth_pull` becomes a 3-way: minor (no action, refresh route), material (DERISK), catastrophic
(CUT). DEGENERATE default: with no registered boundaries EVERY burn is catastrophic -> CUT (today's
behavior) — so the classifier ships inert until WO-7 registers per-chain boundaries from the decoded
burn history (>= 10 LP-removal events). Fail-safe: an unmeasurable removal, a gone/blown route, or a
fraction over the catastrophic bound is catastrophic — never a missed rug.
"""
import pytest

from sniper.analysis.exit_features import liquidity_removed_fraction, largest_lp_removal_fraction
from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, LP_CATASTROPHIC, LP_MATERIAL, LP_MINOR, classify_lp_removal, propose_v5)

_ALL = ("price", "flow", "vol", "depth")


def _prov(**q):
    return {f: Provenance("t", quality=q.get(f, "trusted")) for f in _ALL}


def _state(**kw):
    return MarketState(
        chain_id="solana", mint="M", pool="p", position_id=1, engine_version="v",
        feature_version="v2", state=StateLabel.EXIT_PROPOSED, required_families=_ALL,
        provenance=_prov(), depth_pull_flag=True, **kw)


class _Cfg:                              # degenerate — no registered severity bounds
    MEMECOIN_REAL_UNSELLABLE_SWEEPS = 4


class _CfgReg(_Cfg):                     # WO-7 later registers these per chain
    MEMECOIN_LP_SEVERITY_BOUNDS = {"solana": {"minor": 0.05, "catastrophic": 0.30}}


# --- feature ------------------------------------------------------------------------------
def test_liquidity_removed_fraction():
    assert liquidity_removed_fraction(300, 700) == pytest.approx(0.3)      # 300/(300+700)
    assert liquidity_removed_fraction("50", "950") == pytest.approx(0.05)   # string amounts
    assert liquidity_removed_fraction(None, 700) is None
    assert liquidity_removed_fraction(0, 0) is None                         # empty pool


def test_largest_fraction_pairs_each_burn_with_its_same_transaction_sync():
    rows = [
        {"kind": "sol_burn", "block_no": 10, "tx_sig": "a", "amount1": "-300"},
        {"kind": "sol_sync", "block_no": 10, "tx_sig": "a", "amount1": "700"},
        {"kind": "sol_burn", "block_no": 11, "tx_sig": "b", "amount1": "-50"},
        {"kind": "sol_sync", "block_no": 11, "tx_sig": "b", "amount1": "950"},
        # Unrelated later reserve must not become either burn's denominator.
        {"kind": "sol_sync", "block_no": 12, "tx_sig": "c", "amount1": "10"},
    ]
    assert largest_lp_removal_fraction(rows, quote_side=1) == pytest.approx(0.30)


def test_unpaired_or_provisional_burn_fraction_is_unmeasurable():
    assert largest_lp_removal_fraction([
        {"kind": "sol_burn", "block_no": 10, "tx_sig": "a", "amount1": "-300"},
    ], quote_side=1) is None
    assert largest_lp_removal_fraction([
        {"kind": "sol_burn", "block_no": 10, "tx_sig": None, "amount1": "-300"},
        {"kind": "sol_sync", "block_no": 10, "tx_sig": None, "amount1": "700"},
    ], quote_side=1) is None


# --- classifier ---------------------------------------------------------------------------
def test_classifier_degenerate_without_registered_bounds():
    assert classify_lp_removal(0.01, minor_frac=None, catastrophic_frac=None) == LP_CATASTROPHIC
    assert classify_lp_removal(0.5, minor_frac=0.05, catastrophic_frac=None) == LP_CATASTROPHIC


def test_classifier_three_way_when_registered():
    assert classify_lp_removal(0.02, minor_frac=0.05, catastrophic_frac=0.30) == LP_MINOR
    assert classify_lp_removal(0.10, minor_frac=0.05, catastrophic_frac=0.30) == LP_MATERIAL
    assert classify_lp_removal(0.40, minor_frac=0.05, catastrophic_frac=0.30) == LP_CATASTROPHIC


def test_classifier_fail_safe_to_catastrophic():
    assert classify_lp_removal(None, minor_frac=0.05, catastrophic_frac=0.30) == LP_CATASTROPHIC
    assert classify_lp_removal(0.01, minor_frac=0.05, catastrophic_frac=0.30,
                               route_gone=True) == LP_CATASTROPHIC
    assert classify_lp_removal(0.01, minor_frac=0.05, catastrophic_frac=0.30,
                               route_degraded=True) == LP_MINOR


def test_invalid_or_wrong_chain_bounds_stay_degenerate():
    class WrongChain(_Cfg):
        MEMECOIN_LP_SEVERITY_BOUNDS = {"4663": {"minor": 0.05, "catastrophic": 0.30}}
    class Inverted(_Cfg):
        MEMECOIN_LP_SEVERITY_BOUNDS = {"solana": {"minor": 0.40, "catastrophic": 0.30}}
    assert propose_v5({"trail_pct": 30.0}, _state(liquidity_removed_fraction=0.01), WrongChain()).action == CUT
    assert propose_v5({"trail_pct": 30.0}, _state(liquidity_removed_fraction=0.01), Inverted()).action == CUT


# --- exit_brain integration ---------------------------------------------------------------
def test_depth_pull_degenerate_cuts_unchanged():
    """Ships inert: no registered bounds -> every burn CUTs, exactly like pre-WO-3."""
    d = propose_v5({"trail_pct": 30.0}, _state(), _Cfg())
    assert d.action == CUT and "lp_pull" in d.reason_codes


def test_depth_pull_minor_falls_through_no_cut():
    d = propose_v5({"trail_pct": 30.0, "lp_route_health_complete": True},
                   _state(liquidity_removed_fraction=0.02), _CfgReg())
    assert d.action != CUT and d.action != DERISK        # minor -> the LP rule does not fire


def test_depth_pull_material_derisks():
    d = propose_v5({"trail_pct": 30.0, "lp_route_health_complete": True},
                   _state(liquidity_removed_fraction=0.10), _CfgReg())
    assert d.action == DERISK and "lp_removal" in d.reason_codes
    assert d.proposed_fraction == 0.5


def test_material_lp_after_partial_is_policy_selected_not_implicitly_retried():
    state = _state(liquidity_removed_fraction=0.10)
    base = {"trail_pct": 30.0, "lp_route_health_complete": True, "derisked_at": "done"}
    suppressed = propose_v5(base, state, _CfgReg())
    escalated = propose_v5(
        {**base, "partial_lp_escalation": "escalate_to_cut"}, state, _CfgReg())

    assert suppressed.action not in (CUT, DERISK)
    assert escalated.action == CUT
    assert "material_after_partial" in escalated.reason_codes


def test_lp_material_size_does_not_reuse_volatility_conviction():
    d = propose_v5(
        {"trail_pct": 30.0, "lp_route_health_complete": True},
        _state(liquidity_removed_fraction=0.10, realized_vol=30.0), _CfgReg())
    assert d.action == DERISK and d.proposed_fraction == 0.5


def test_depth_pull_catastrophic_cuts():
    d = propose_v5({"trail_pct": 30.0}, _state(liquidity_removed_fraction=0.40), _CfgReg())
    assert d.action == CUT and "lp_pull" in d.reason_codes


def test_registered_fraction_bounds_stay_degenerate_until_route_health_is_complete():
    d = propose_v5({"trail_pct": 30.0},
                   _state(liquidity_removed_fraction=0.02), _CfgReg())
    assert d.action == CUT and "lp_severity:catastrophic" in d.reason_codes


def test_depth_pull_unmeasurable_fraction_cuts_failsafe():
    d = propose_v5({"trail_pct": 30.0}, _state(liquidity_removed_fraction=None), _CfgReg())
    assert d.action == CUT                               # can't size -> treat as rug


def test_depth_pull_route_gone_cuts_even_on_small_fraction():
    d = propose_v5({"trail_pct": 30.0, "route_gone": True},
                   _state(liquidity_removed_fraction=0.01), _CfgReg())
    assert d.action == CUT


def test_depth_pull_small_fraction_with_degraded_route_does_not_become_catastrophic():
    d = propose_v5({"trail_pct": 30.0, "route_degraded": True,
                    "lp_route_health_complete": True},
                   _state(liquidity_removed_fraction=0.01), _CfgReg())
    assert d.action != CUT
