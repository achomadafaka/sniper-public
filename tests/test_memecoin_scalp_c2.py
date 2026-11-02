"""scalp_c2 — the corrected conditioned arm. The operator's "big caps need a small quick TP",
tested WITHOUT the two rails that sank the first attempt.

scalp_c (v1) LOST -$921 over 57 closes while `control` made +$1,739 on the same entries. The exit
breakdown showed exactly why, and it was not the conditioning:

    mirror (copy the trader's sell)   16 closes   +$12.48 avg   <- the profit engine
    scalp_take_profit                  5 closes    -$1.61 avg    <- ~neutral, fired rarely
    scalp_stop (tight -20/-30)         8 closes   -$62.50 avg
    scalp_timecap_stale (max-hold)    12 closes   -$52.28 avg    <- the villain

Paired against control on the SAME coins, the timecap force-closed coins that control (no timecap)
rode back to +$0.90 avg. The tight stop landed on genuinely-declining coins where control also lost.

So v1 confounded three changes at once (small TP + tight stop + hard time cap) and the two hard
exits buried the signal. scalp_c2 isolates the ONE change the operator actually hypothesised:
condition the take-profit by market cap, and otherwise use control's structure exactly —

    * TP conditioned by mcap (small for big caps, ride for sub-$1M where the +100% tail lives);
    * WIDE stop, same as control (-60%);
    * NO time cap (max_hold = inf) — the v1 killer, removed;
    * mirror the trader's sell (the profit engine).

If scalp_c2 beats control, conditioning the TP is real edge. If it does not, the fixed rail wins and
we stop chasing per-token take-profits. Either answer advances the engine.
"""
from sniper.services.memecoin_policy import resolve_policy


def _p(mcap):
    return resolve_policy({"entry_mcap": mcap}, arm="scalp_c2")


def test_big_caps_get_a_small_take_profit():
    """A $200M coin reaches +10% only ~19% of the time and NEVER +40/+60 — so bank the pop small."""
    pol, pid = _p(200_000_000)
    assert pol.tp_pct <= 15, "big-cap TP must be a scalp, got %s" % pol.tp_pct
    assert pid.startswith("scalp_c2")


def test_sub_1m_still_rides_for_the_fat_tail():
    """Sub-$1M is where the +100% winners live (22/53 hit +20% vs 0/39 for big caps). Do not cap it."""
    pol, _ = _p(500_000)
    assert pol.tp_pct >= 100, "sub-$1M must ride, got %s" % pol.tp_pct


def test_tp_falls_as_mcap_rises():
    tps = [_p(mc)[0].tp_pct for mc in (500_000, 3_000_000, 15_000_000, 200_000_000)]
    assert tps == sorted(tps, reverse=True), "TP must decrease as mcap rises, got %s" % tps


def test_no_time_cap_the_v1_killer_is_gone():
    """Every band. The timecap was the single biggest loss rail in v1; scalp_c2 must never carry one."""
    for mc in (200_000_000, 10_000_000, 2_000_000, 500_000, None):
        pol, _ = _p(mc)
        assert pol.max_hold_s == float("inf"), "band mcap=%s still has a time cap" % mc


def test_stop_is_wide_like_control_not_the_tight_v1_stop():
    """The tight -20/-30 stop is gone. A wide stop matches the arm that actually wins (control -60)."""
    for mc in (200_000_000, 2_000_000, 500_000):
        pol, _ = _p(mc)
        assert pol.stop_pct <= -50, "stop must be wide (<=-50), got %s at mcap=%s" % (pol.stop_pct, mc)


def test_mirror_is_on_the_profit_engine():
    for mc in (200_000_000, 500_000):
        assert _p(mc)[0].mirror_sell_mode == "on"


def test_unknown_mcap_falls_back_to_the_fixed_rail_never_refuses():
    """Unknown mcap must NOT be treated as a big cap (that dropped sub-$1M winners on feed gaps)."""
    pol, pid = _p(None)
    assert pol.tp_pct > 0 and pol.stop_pct < 0 and pol.max_hold_s == float("inf")
    assert "fallback" in pid
