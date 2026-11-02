import math
import pytest
from sniper.algos.polymarket_crypto5m import reachability as RZ


def test_far_otm_near_expiry_rejected():
    # the 0.11 Up near expiry: z≈Φ⁻¹(0.11)≈-1.23, little time. Rejected — and at the REALIZED fee too
    # (it's a reachability/theta failure, not a fee failure).
    r = RZ.entry_ev(z=-1.23, vol_per_s=0.0002, seconds_left=40, conviction=0.6,
                    side_ask=0.11, side_bid=0.10)
    assert r["passed"] is False
    assert r["ev_007"] < 0 and r["ev_realfee"] < 0
    assert r["reject_reason"] == "ev_negative"


def test_atm_strong_read_dual_fee_split():
    # strong ATM read with room: blocked at documented 0.07 but PASSES at realized-0. v1 gates on realized,
    # so the LIVE decision (passed) follows passed_realfee.
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=1.0,
                    side_ask=0.50, side_bid=0.495)
    assert r["passed_007"] is False        # documented 0.07 overlay too strict (real-money-readiness)
    assert r["passed_realfee"] is True     # realized-0 admits it
    assert r["passed"] is True             # v1 gate_on_realized=True -> live decision follows realized
    assert r["ev_realfee"] > r["ev_007"]


def test_gate_selector_documented_vs_realized():
    # the SAME read: gated on realized (default) it passes; gated on documented 0.07 it's blocked. Both fee
    # columns are logged regardless of which one gates.
    kw = dict(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=1.0, side_ask=0.50, side_bid=0.495)
    assert RZ.entry_ev(cfg=RZ.EVConfig(gate_on_realized=True), **kw)["passed"] is True
    r = RZ.entry_ev(cfg=RZ.EVConfig(gate_on_realized=False), **kw)
    assert r["passed"] is False
    assert r["ev_007"] is not None and r["ev_realfee"] is not None


def test_atm_cap_binds_no_divzero():
    # z->0: |z|->0 would divide by zero in the interior form; the capped form is finite.
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=200, conviction=0.8,
                    side_ask=0.50, side_bid=0.49)
    assert r["t_star"] is None             # unbounded optimum -> reported as None
    assert r["t_hold"] == pytest.approx(30.0)   # cap (T_CEILING) binds
    assert r["gain"] is not None and math.isfinite(r["gain"])


def test_conviction_monotonic():
    base = dict(z=-0.3, vol_per_s=0.0002, seconds_left=120, side_ask=0.45, side_bid=0.44,
                cfg=RZ.EVConfig(k=0.4))
    lo = RZ.entry_ev(conviction=0.6, **base)["ev_007"]
    hi = RZ.entry_ev(conviction=0.9, **base)["ev_007"]
    assert hi > lo


def test_fee_monotonic():
    r = RZ.entry_ev(z=-0.2, vol_per_s=0.0002, seconds_left=120, conviction=0.9,
                    side_ask=0.45, side_bid=0.44, cfg=RZ.EVConfig(k=0.4))
    assert r["ev_realfee"] > r["ev_007"]   # lower fee -> higher EV, always


def test_delta_rises_near_expiry_raises_ev_at_fixed_moneyness():
    # At a FIXED moneyness z, the binary's delta = phi(z)/(vol*sqrt(t_left)) is LARGER near expiry, and a
    # capped hold captures a larger fraction sqrt(t_hold/t_left) of the shorter remaining diffusion. So less
    # time left -> higher per-trade EV. (The "last seconds -> no trade" case is handled by no_hold_room.)
    base = dict(z=-0.3, vol_per_s=0.0002, conviction=0.9, side_ask=0.45, side_bid=0.44,
                cfg=RZ.EVConfig(k=0.4))
    much_time = RZ.entry_ev(seconds_left=120, **base)["ev_007"]
    little_time = RZ.entry_ev(seconds_left=40, **base)["ev_007"]
    assert little_time > much_time


def test_degenerate_inputs_fail_closed():
    for bad in (dict(vol_per_s=0.0), dict(vol_per_s=None), dict(seconds_left=0), dict(z=None)):
        kw = dict(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=0.8,
                  side_ask=0.5, side_bid=0.49)
        kw.update(bad)
        r = RZ.entry_ev(**kw)
        assert r["passed"] is False and r["reject_reason"] == "degenerate_inputs"


def test_price_band_guardrail():
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=0.9,
                    side_ask=0.97, side_bid=0.96)
    assert r["passed"] is False and r["reject_reason"] == "price_band"


def test_no_hold_room():
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=22, conviction=0.9,
                    side_ask=0.5, side_bid=0.49)
    assert r["passed"] is False and r["reject_reason"] == "no_hold_room"


def test_probit_roundtrip_and_bounds():
    from math import erf, sqrt
    for z in (-1.5, -0.3, 0.0, 0.7, 1.9):
        p = 0.5 * (1 + erf(z / sqrt(2)))
        assert RZ.probit(p) == pytest.approx(z, abs=1e-3)
    assert RZ.probit(0.0) is None and RZ.probit(1.0) is None and RZ.probit(None) is None


def test_default_config_admits_a_strong_atm_read():
    # GUARD: the SHIPPED default (k=0.10, gate_on_realized=True) MUST still ENTER on a genuine strong ATM
    # read — else a future k tightening could silently FREEZE the live engine with every other test green.
    r = RZ.entry_ev(z=0.0, vol_per_s=0.0002, seconds_left=120, conviction=0.8,
                    side_ask=0.50, side_bid=0.49)   # default EVConfig()
    assert r["passed"] is True and r["passed_realfee"] is True
    assert r["passed_007"] is False                  # the 0.07 overlay correctly does NOT gate (v1)
