"""TDD for the drift-corrected first-passage reachability module (brain v2 taker redesign, SHADOW).

The whole point of this module is to escape the phi(-z)=phi(z) symmetry that made the driftless ev-reach
gate EV-neutral and unpredictive (finding/feature-study/crypto5m-reachability-viability-2026-06-26). A
MEASURED drift mu is what breaks the symmetry — test_touch_prob_symmetry_BREAK is the load-bearing test.
"""
import math

import pytest

from sniper.algos.polymarket_crypto5m import drift_reachability as DR


def _Phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --- drift_estimate: the measured directional drift (replaces conviction-as-fake-magnitude) ------

def test_drift_estimate_no_signals_is_zero():
    # fail-closed: with no flow signal the model collapses to the driftless baseline (never worse)
    assert DR.drift_estimate() == 0.0
    assert DR.drift_estimate(tape_ofi=None, ema_slope=None, volume_dir=None) == 0.0


def test_drift_estimate_sign_follows_flow_and_is_bounded():
    up = DR.drift_estimate(tape_ofi=1.0, ema_slope=1.0, volume_dir=0.5)
    dn = DR.drift_estimate(tape_ofi=-1.0, ema_slope=-1.0, volume_dir=-0.5)
    assert up > 0 and dn < 0
    assert up == pytest.approx(-dn)
    assert up == pytest.approx(DR.K_DRIFT * DR.SHRINK)                  # full-positive saturates to the cap
    assert abs(DR.drift_estimate(tape_ofi=9.0, ema_slope=9.0, volume_dir=9.0)) <= DR.K_DRIFT * DR.SHRINK + 1e-18


def test_drift_estimate_partial_signals_dont_crash():
    assert DR.drift_estimate(tape_ofi=1.0) == pytest.approx(DR.K_DRIFT * DR.SHRINK)
    assert DR.drift_estimate(volume_dir=-0.5) == pytest.approx(-DR.K_DRIFT * DR.SHRINK)


# --- touch_prob: drift-corrected first-passage (reflection + Girsanov) ----------------------------

def test_touch_prob_driftless_equals_reflection_principle():
    b, sigma, tau = 6e-5, 5e-5, 100.0
    expect = 2.0 * _Phi(-b / (sigma * math.sqrt(tau)))                  # 2*Phi(-b/(sigma*sqrt(tau)))
    assert DR.touch_prob(b, sigma, tau, 0.0) == pytest.approx(expect, rel=1e-9)


def test_touch_prob_increasing_in_drift_and_bounded():
    b, sigma, tau = 6e-5, 5e-5, 100.0
    lo = DR.touch_prob(b, sigma, tau, -1e-4)
    mid = DR.touch_prob(b, sigma, tau, 0.0)
    hi = DR.touch_prob(b, sigma, tau, +1e-4)
    assert lo < mid < hi
    assert all(0.0 <= p <= 1.0 for p in (lo, mid, hi))


def test_touch_prob_degenerate_and_already_touched():
    assert DR.touch_prob(6e-5, 0.0, 100.0, 0.0) is None                # sigma<=0
    assert DR.touch_prob(6e-5, 5e-5, 0.0, 0.0) is None                 # tau<=0
    assert DR.touch_prob(None, 5e-5, 100.0, 0.0) is None
    assert DR.touch_prob(-1e-5, 5e-5, 100.0, 0.0) == 1.0               # b<=0 -> already favorable


def test_touch_prob_symmetry_BREAK():
    # THE load-bearing test: drift breaks the Up/Down symmetry that killed the prior design.
    b, sigma, tau, mu = 6e-5, 5e-5, 100.0, 1e-4
    favorable = DR.touch_prob(b, sigma, tau, +mu)                       # drift toward the barrier
    adverse = DR.touch_prob(b, sigma, tau, -mu)                        # drift away
    assert favorable > adverse                                         # NOT equal -> escaped phi(-z)=phi(z)


# --- expected_fav_move: the validated quantity (vs realized_mid_move) ------------------------------

def test_expected_fav_move_zero_drift_is_zero():
    assert DR.expected_fav_move(0.2, 5e-5, 100.0, 0.0, 10.0) == 0.0     # driftless -> no directional move


def test_expected_fav_move_sign_and_scale():
    base = DR.expected_fav_move(0.2, 5e-5, 100.0, 1e-4, 10.0)
    assert base > 0
    assert DR.expected_fav_move(0.2, 5e-5, 100.0, -1e-4, 10.0) == pytest.approx(-base)
    assert DR.expected_fav_move(0.2, 5e-5, 100.0, 1e-4, 20.0) == pytest.approx(2 * base)   # scales with t_hold


def test_expected_fav_move_degenerate():
    assert DR.expected_fav_move(0.2, 0.0, 100.0, 1e-4, 10.0) is None
    assert DR.expected_fav_move(0.2, 5e-5, 100.0, 1e-4, 0.0) is None


# --- max_hold_s: gamma-theta breakeven hold (4*tau/z^2; sigma cancels) ----------------------------

def test_max_hold_s_shrinks_with_moneyness():
    tau = 100.0
    near = DR.max_hold_s(2.5, tau)                                      # 4*100/6.25 = 64
    far = DR.max_hold_s(5.0, tau)                                       # 4*100/25  = 16
    assert near > far
    assert 0 < far <= tau and 0 < near <= tau


def test_max_hold_s_caps_at_tau_and_handles_atm():
    tau = 100.0
    assert DR.max_hold_s(0.0, tau) == tau                              # z~0 -> theta ~0 -> cap to tau
    assert DR.max_hold_s(1e-9, tau) == tau
    assert DR.max_hold_s(0.5, tau) == tau                              # 4*100/0.25=1600 -> capped
    assert DR.max_hold_s(0.2, 0.0) is None                            # tau<=0 degenerate


# --- side_prediction: compose the primitives for a CONFIRMED side (the logged prediction) ---------

def _common():
    return dict(z=0.2, vol_per_s=5e-5, seconds_left=200.0, mu_hat=5e-5, t_hold=10.0)


def test_side_prediction_up_vs_down_sign():
    up = DR.side_prediction(side="Up", **_common())
    dn = DR.side_prediction(side="Down", **_common())
    # mu_hat>0 (BTC drifting up) FAVORS Up, is ADVERSE to Down — the signed prediction must reflect that
    assert up["exp_fav_move"] > 0 and dn["exp_fav_move"] < 0
    assert up["touch_prob"] > dn["touch_prob"]
    assert up["mu_signed"] == pytest.approx(5e-5) and dn["mu_signed"] == pytest.approx(-5e-5)
    # the driftless baseline is 0 for both -> every row is a paired drift-vs-0 comparison
    assert up["exp_fav_move_driftless"] == 0.0 and dn["exp_fav_move_driftless"] == 0.0


def test_side_prediction_bankable_barrier_and_fields():
    p = DR.side_prediction(side="Up", **_common())
    assert p["b_barrier"] > 0                                          # positive favorable distance for the margin
    assert 0.0 <= p["touch_prob"] <= 1.0
    assert p["max_hold_s"] > 0


def test_side_prediction_degenerate_is_none():
    assert DR.side_prediction(side="Up", z=0.2, vol_per_s=0.0, seconds_left=200.0, mu_hat=5e-5, t_hold=10.0) is None
    assert DR.side_prediction(side="Up", z=None, vol_per_s=5e-5, seconds_left=200.0, mu_hat=5e-5, t_hold=10.0) is None
