"""WO-5B — action-specific sizing, route-degradation feed, one-partial escalation semantics.

The shared `strength-None -> 0.5 midpoint` fallback is GONE: each action sizes from its own evidence
and a missing required input yields no proposal, never a fabricated fraction. The route-degradation
feed derives route_degraded/route_gone from the WO-5A controller columns. Sizing stays conservative
(today's registered fractions) until WO-7 replay freezes the values — reachable runtime is unchanged
except the deliberate one-partial-per-position escalation rule (TP no longer banks a second partial
after a DERISK; a full CUT is always still available).
"""
from sniper.analysis import exit_flex_v5 as v5
from sniper.analysis.exit_controller import ControllerState
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, RIDE, TAKE_PROFIT, propose_v5)
from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel

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
    MEMECOIN_ROUTE_GONE_STREAK = None
    MEMECOIN_NO_PROGRESS_WINDOW_S = None


# --- no shared midpoint fallback ---------------------------------------------------------------

def test_conviction_scaled_fraction_no_longer_falls_back_to_midpoint():
    assert v5.conviction_scaled_fraction(None) is None        # was 0.5 — the fallback is dead
    assert v5.conviction_scaled_fraction(0.0) == 0.25
    assert v5.conviction_scaled_fraction(1.0) == 0.75


# --- four action-specific sizers ---------------------------------------------------------------

def test_size_take_profit_requires_strength_never_midpoint():
    assert v5.size_take_profit(strength_ratio=None) is None    # no evidence -> no proposal
    assert v5.size_take_profit(strength_ratio=1.0) == 0.75
    assert v5.size_take_profit(strength_ratio=0.0) == 0.25


def test_size_vol_derisk_requires_strength():
    assert v5.size_vol_derisk(strength_ratio=None) is None
    assert 0.25 <= v5.size_vol_derisk(strength_ratio=0.5) <= 0.75


def test_size_time_and_lp_derisk_use_own_registered_fraction():
    assert v5.size_time_derisk() == v5.TIME_DERISK_FRACTION
    assert v5.size_time_derisk(registered_fraction=0.3) == 0.3
    assert v5.size_lp_derisk() == v5.LP_MATERIAL_DERISK_FRACTION
    assert v5.size_lp_derisk(registered_fraction=0.4) == 0.4


# --- final-fraction clamp ----------------------------------------------------------------------

def test_clamp_partial_fraction_is_min_of_present_bounds():
    assert v5.clamp_partial_fraction(0.75, slippage_cap_max=0.4, remaining_budget=0.9) == 0.4
    assert v5.clamp_partial_fraction(0.75, slippage_cap_max=None, remaining_budget=0.5) == 0.5
    assert v5.clamp_partial_fraction(0.6, slippage_cap_max=None, remaining_budget=None) == 0.6
    # a spent partial budget produces no action, never an unexecutable zero-size action
    assert v5.clamp_partial_fraction(0.75, slippage_cap_max=0.4, remaining_budget=0.0) is None
    assert v5.clamp_partial_fraction(None, slippage_cap_max=0.4, remaining_budget=0.9) is None


def test_partial_fraction_invalid_or_outside_money_safe_bounds_fails_closed():
    assert v5.clamp_partial_fraction(float("nan"), remaining_budget=1.0) is None
    assert v5.clamp_partial_fraction(float("inf"), remaining_budget=1.0) is None
    assert v5.clamp_partial_fraction(-0.1, remaining_budget=1.0) is None
    assert v5.clamp_partial_fraction(1.1, remaining_budget=1.0) is None


# --- route-degradation feed --------------------------------------------------------------------

def test_route_health_conservative_by_default():
    # empty controller state -> not degraded, not gone (today's behavior)
    assert v5.route_health(ControllerState()) == (False, False)


def test_route_health_degraded_from_onset_gone_from_streak():
    degraded = ControllerState(route_degradation_since="2026-07-25T00:00:00Z")
    assert v5.route_health(degraded) == (True, False)
    # route_gone needs a REGISTERED streak; without it the feed can never declare gone
    streaked = ControllerState(route_failure_streak=5)
    assert v5.route_health(streaked, gone_streak=None) == (False, False)
    assert v5.route_health(streaked, gone_streak=3) == (True, True)   # degraded + gone


# --- one-partial-per-position escalation -------------------------------------------------------

def test_take_profit_suppressed_after_a_prior_partial():
    # a climax TP fires when no partial has been banked...
    climax = _state(higher_low_state=True, arrival_accel=2.0, realized_vol=0.31, net_flow_quote="10")
    assert propose_v5({"trail_pct": 30.0}, climax, _Cfg()).action == TAKE_PROFIT
    # ...but NOT a second partial once derisked_at marks a banked partial (one partial per position)
    d = propose_v5({"trail_pct": 30.0, "derisked_at": "2026-07-25T00:00:00Z"}, climax, _Cfg())
    assert d.action != TAKE_PROFIT


def test_full_cut_still_fires_after_a_prior_partial():
    # escalation to a FULL exit is always available — one-partial only bounds PARTIAL actions.
    broke = _state(nominal_price=1.0, prior_low=1.5, net_flow_quote="-5")
    d = propose_v5({"trail_pct": 30.0, "derisked_at": "2026-07-25T00:00:00Z"}, broke, _Cfg())
    assert d.action == CUT


# --- executable_no_progress wired from controller state ----------------------------------------

def test_time_derisk_reads_controller_no_progress_when_registered():
    """The time-DERISK trigger is band age AND no-new-executable-high for the registered window —
    fed from the WO-5A controller peak, never age alone."""
    s = _state(net_flow_quote="1")
    ctrl = ControllerState(executable_peak_price=5.0, executable_peak_at="2026-07-25T00:00:00Z")
    base = {"trail_pct": 30.0, "coin_band": "nano<1M", "held_s": 2000,
            "controller_state": ctrl, "quote_fresh": True, "now_iso": "2026-07-25T00:10:00Z"}

    class Reg(_Cfg):
        MEMECOIN_NO_PROGRESS_WINDOW_S = 300          # registered -> 600 s elapsed clears it

    class NoReg(_Cfg):
        MEMECOIN_NO_PROGRESS_WINDOW_S = None         # unregistered -> age alone never DERISKs

    assert propose_v5(base, s, Reg()).action == DERISK
    assert propose_v5(base, s, NoReg()).action != DERISK


def test_structure_cut_requires_two_distinct_events_when_controller_is_present():
    broke = _state(nominal_price=1.0, prior_low=1.5, net_flow_quote="-5")
    one = dict(trail_pct=30.0, now_iso="2026-07-25T00:00:01Z",
               controller_state=ControllerState(
                   consecutive_break_events=1, structure_break_since="2026-07-25T00:00:00Z"))
    two = dict(one, controller_state=ControllerState(
        consecutive_break_events=2, structure_break_since="2026-07-25T00:00:00Z"))

    assert propose_v5(one, broke, _Cfg()).action == DEFER
    assert propose_v5(two, broke, _Cfg()).action == CUT
