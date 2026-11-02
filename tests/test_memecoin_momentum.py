from sniper.services.memecoin_momentum import momentum_decision

DEF = dict(rollover_sweeps=2, trail_pct=15.0, stop_pct=30.0, max_hold_s=900)


def _step(entry, peak, nnh, price, held_s=10):
    return momentum_decision(entry_price=entry, peak_price=peak, no_new_high_sweeps=nnh,
                             price=price, held_s=held_s, **DEF)


def test_new_high_resets_counter_no_exit():
    r = _step(1.0, 1.0, 1, 1.2)
    assert r["peak_price"] == 1.2 and r["no_new_high_sweeps"] == 0 and r["exit_reason"] is None


def test_rollover_after_n_stalled_sweeps():
    r1 = _step(1.0, 1.2, 0, 1.15)
    assert r1["no_new_high_sweeps"] == 1 and r1["exit_reason"] is None
    r2 = _step(1.0, 1.2, 1, 1.15)
    assert r2["exit_reason"] == "a_rollover"


def test_trailing_before_rollover_on_sharp_reversal():
    r = _step(1.0, 1.2, 0, 1.2 * 0.80)
    assert r["exit_reason"] == "a_trailing"


def test_hard_stop_has_priority():
    r = _step(1.0, 1.2, 0, 0.65)
    assert r["exit_reason"] == "a_stop"


def test_time_cap_when_price_flat():
    r = _step(1.0, 1.0, 0, 1.0, held_s=1000)
    assert r["exit_reason"] == "a_timecap"


def test_no_exit_while_pumping_under_cap():
    r = _step(1.0, 1.5, 0, 1.6, held_s=100)
    assert r["exit_reason"] is None
