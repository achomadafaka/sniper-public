"""Exit engine V5 quant policy — the frozen quant tree (2026-07-24 prereg) with V5's registered
partial-size and per-band horizon. V2/V3 are retired; V5 is the only installed engine.

Thesis under test: leave fast, never cap the tail. The property test is the important one — every
V5 action must reduce or hold risk, because the whole engine is only safe if that is structural.
"""
import random

import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, RIDE, TAKE_PROFIT, exit_engine, propose_v5,
)

_TRUSTED = {f: Provenance("pool_ticks", quality="trusted") for f in ("price", "flow", "vol", "depth")}
_REQ = ("price", "flow", "vol", "depth")


def _state(**kw):
    base = dict(chain_id="solana", mint="C", pool="p", position_id=1, engine_version="v",
                feature_version="v2", state=StateLabel.TRENDING,
                required_families=_REQ, provenance=_TRUSTED)
    base.update(kw)
    return MarketState(**base)


class _Cfg:
    MEMECOIN_REAL_UNSELLABLE_SWEEPS = 4


def test_lp_pull_cuts_immediately_and_outranks_everything():
    """An LP pull means the market the position depends on is gone. It must beat even a strong
    uptrend — the tail cannot survive a pool with no liquidity."""
    s = _state(depth_pull_flag=True, higher_low_state=True, net_flow_quote="9999",
               arrival_accel=5.0, realized_vol=0.9)
    d = propose_v5({"trail_pct": 30.0}, s, _Cfg())
    assert d.action == CUT and "lp_pull" in d.reason_codes


def test_unsellable_streak_cuts():
    s = _state(higher_low_state=True, net_flow_quote="10")
    d = propose_v5({"trail_pct": 30.0, "exact_quote_misses": 4}, s, _Cfg())
    assert d.action == CUT and "unsellable" in d.reason_codes
    # below the streak it must NOT cut
    d2 = propose_v5({"trail_pct": 30.0, "exact_quote_misses": 3}, s, _Cfg())
    assert d2.action != CUT


def test_break_with_negative_flow_cuts_but_positive_flow_rejects():
    broke = _state(nominal_price=1.0, prior_low=1.5, net_flow_quote="-5")
    assert propose_v5({"trail_pct": 30.0}, broke, _Cfg()).action == CUT
    held = _state(nominal_price=1.0, prior_low=1.5, net_flow_quote="5")
    d = propose_v5({"trail_pct": 30.0}, held, _Cfg())
    assert d.action == DEFER and "flow_not_confirming" in d.reason_codes


def test_take_profit_only_on_a_climax_never_at_a_fixed_gain():
    # climax: arrival accelerating + vol at/above the trail distance + higher-lows intact
    climax = _state(higher_low_state=True, arrival_accel=2.0, realized_vol=0.31,
                    net_flow_quote="10")
    d = propose_v5({"trail_pct": 30.0}, climax, _Cfg())
    assert d.action == TAKE_PROFIT and 0.0 < d.proposed_fraction <= 1.0
    # same strength WITHOUT the blowoff must ride, not bank — capping is the measured leak
    calm = _state(higher_low_state=True, arrival_accel=2.0, realized_vol=0.01, net_flow_quote="10")
    assert propose_v5({"trail_pct": 30.0}, calm, _Cfg()).action == RIDE


def test_derisk_on_lower_high_with_vol_expansion():
    s = _state(lower_high_state=True, realized_vol=0.35, net_flow_quote="1")
    d = propose_v5({"trail_pct": 30.0}, s, _Cfg())
    assert d.action == DERISK and 0.0 < d.proposed_fraction <= 1.0 and "lower_high" in d.reason_codes


def test_derisk_at_the_registered_band_edge_duration():
    """Per-band edge-duration: a nano coin banks half after its measured 1524-second median, not the
    old uniform 3h prior. Band comes off the frozen contract's coin_band."""
    s = _state(net_flow_quote="1")
    c = {"trail_pct": 30.0, "coin_band": "nano<1M"}
    young = propose_v5({**c, "held_s": 1523}, s, _Cfg())      # below the band median
    old = propose_v5({**c, "held_s": 1525, "executable_no_progress": True}, s, _Cfg())
    assert young.action != DERISK
    assert old.action == DERISK and "edge_duration_elapsed" in old.reason_codes


def test_ride_is_distinct_from_defer():
    """RIDE asserts the tail is alive; DEFER says we have nothing to add. Pooling them would make
    every read uninterpretable."""
    riding = _state(higher_low_state=True, net_flow_quote="0")
    assert propose_v5({"trail_pct": 30.0}, riding, _Cfg()).action == RIDE
    quiet = _state(higher_low_state=None, net_flow_quote=None)
    assert propose_v5({"trail_pct": 30.0}, quiet, _Cfg()).action == DEFER


def test_non_capable_state_always_defers():
    s = _state(state=StateLabel.DATA_STALE, required_families=(), provenance={})
    assert propose_v5({"trail_pct": 30.0}, s, _Cfg()).action == DEFER


def test_monotonic_risk_reduction_property():
    """THE load-bearing test. Over randomized states V5 must never emit an action outside the
    risk-reducing vocabulary, never propose a fraction >1 or <=0, and always defer when the state
    is not candidate-capable."""
    rnd = random.Random(20260724)
    for _ in range(600):
        s = _state(
            state=rnd.choice(list(StateLabel)),
            required_families=rnd.choice([_REQ, ()]),
            provenance=rnd.choice([_TRUSTED, {}]),
            nominal_price=rnd.choice([None, 0.5, 1.0, 2.0]),
            prior_low=rnd.choice([None, 1.0, 1.5]),
            higher_low_state=rnd.choice([None, True, False]),
            lower_high_state=rnd.choice([None, True, False]),
            depth_pull_flag=rnd.choice([None, True, False]),
            realized_vol=rnd.choice([None, 0.0, 0.01, 0.35, 5.0]),
            arrival_accel=rnd.choice([None, -1.0, 0.0, 3.0]),
            net_flow_quote=rnd.choice([None, "-1", "0", "1", "junk"]),
        )
        c = {"trail_pct": rnd.choice([10.0, 30.0, 50.0]),
             "coin_band": rnd.choice(["nano<1M", "mid5-30M", None]),
             "held_s": rnd.choice([None, 60, 10_801]),
             "exact_quote_misses": rnd.choice([0, 1, 9])}
        d = propose_v5(c, s, _Cfg())
        assert d.action in (RIDE, DERISK, TAKE_PROFIT, CUT, DEFER)
        if d.proposed_fraction is not None:
            assert 0.0 < d.proposed_fraction <= 1.0
        # WO-1: a non-actionable lifecycle label blocks every rule (candidate_capable(()) is the
        # label-only gate). Per-rule family capability is decoupled from the profile-wide gate, so
        # `candidate_capable()` (profile-level) no longer implies the whole state defers.
        if not s.candidate_capable(()):
            assert d.action == DEFER


# --- P1 regressions, Codex review 2026-07-24 -------------------------------------------------
def test_derisk_fires_at_most_once_per_position():
    """prereg §2.3 'fires at most once per position'. Without the guard the engine re-proposed
    DERISK on every 30 s pass and would have exited the position in halves."""
    s = _state(lower_high_state=True, realized_vol=0.30)
    assert propose_v5({"trail_pct": 20.0}, s).action == DERISK
    assert propose_v5({"trail_pct": 20.0, "derisked_at": "2026-07-24T00:00:00Z"}, s).action != DERISK


def test_edge_duration_derisk_also_respects_the_once_guard():
    s = _state()
    held = {"trail_pct": 20.0, "held_s": 99_999, "executable_no_progress": True}
    assert propose_v5(held, s).action == DERISK
    assert propose_v5({**held, "derisked_at": "2026-07-24T00:00:00Z"}, s).action != DERISK


def test_v5_uses_registered_band_duration_and_preserves_one_shot_guard():
    """The per-band profile is real: a nano coin banks half at its 1524-second median, and the
    once-guard still holds once derisked_at is set."""
    s = _state()
    contract = {"trail_pct": 30.0, "coin_band": "nano<1M", "held_s": 1524,
                "executable_no_progress": True}
    d = propose_v5(contract, s)
    assert d.action == DERISK and "edge_duration_elapsed" in d.reason_codes
    assert propose_v5({**contract, "held_s": 1523}, s).action != DERISK
    assert propose_v5({**contract, "derisked_at": "2026-07-25T00:00:00Z"}, s).action != DERISK


def test_v5_scales_both_partial_actions_inside_registered_bounds():
    """Both partial paths scale above the blowoff boundary instead of pinning at 0.75."""
    at_boundary = _state(higher_low_state=True, arrival_accel=2.0, realized_vol=0.30,
                         net_flow_quote="10")
    stronger = _state(lower_high_state=True, realized_vol=0.60, net_flow_quote="-1")
    contract = {"trail_pct": 30.0, "coin_band": "nano<1M", "held_s": 1}
    boundary_decision = propose_v5(contract, at_boundary)
    stronger_decision = propose_v5(contract, stronger)
    assert boundary_decision.action == TAKE_PROFIT
    assert boundary_decision.proposed_fraction == 0.5
    assert stronger_decision.action == DERISK
    assert 0.5 < stronger_decision.proposed_fraction < 0.75


def test_v5_no_signal_evidence_names_v5():
    quiet = _state(higher_low_state=None, net_flow_quote=None, realized_vol=0.01)
    decision = propose_v5(
        {"trail_pct": 30.0, "coin_band": "mid5-30M", "held_s": 10}, quiet, _Cfg())
    assert decision.no_action_reason == "no v5 trigger met"


def test_engine_selector_is_v5_only_and_fails_closed():
    version, proposer = exit_engine("v5")
    assert version == "exit-brain-v5" and proposer is propose_v5
    for retired in ("v3", "v2", "V6", "", None):
        with pytest.raises(ValueError):
            exit_engine(retired)
