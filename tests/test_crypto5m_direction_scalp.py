"""Directional thesis fusion + the scalper's decisions (pure compute)."""
from sniper.algos.polymarket_crypto5m import direction as D
from sniper.algos.polymarket_crypto5m import scalp_trader as S
from sniper.algos.polymarket_crypto5m.scalp_trader import Position


# ---- thesis fusion ----
def test_agreeing_signals_give_high_conviction_up():
    t = D.directional_thesis(ret_5s=0.0005, ret_15s=0.0004, ret_30s=0.0003, ema_slope=0.0001,
                             microprice_gap=0.02, imbalance=0.4, volume_dir=0.2)
    assert t["side"] == "Up" and t["score"] > 0 and t["agreement"] == 1.0 and t["conviction"] > 0.4


def test_conflicting_signals_kill_conviction():
    t = D.directional_thesis(ret_5s=0.0005, ret_15s=-0.0005, microprice_gap=0.02, imbalance=-0.4)
    assert t["agreement"] < 1.0 and t["conviction"] < 0.4


def test_no_active_signals_is_neutral():
    t = D.directional_thesis(ret_5s=0.0, microprice_gap=0.0, imbalance=0.0)
    assert t["side"] is None and t["conviction"] == 0.0


def test_down_read():
    t = D.directional_thesis(ret_15s=-0.0005, ret_30s=-0.0004, microprice_gap=-0.02, imbalance=-0.4)
    assert t["side"] == "Down" and t["score"] < 0


def test_direction_is_volatility_normalized():
    # the SAME 5bps move: in LOW vol it's a real move (votes Up); in HIGH vol it's noise (no vote)
    lowvol = D.directional_thesis(ret_15s=0.0005, vol_per_s=0.00005)   # z≈2.6
    hivol = D.directional_thesis(ret_15s=0.0005, vol_per_s=0.0005)     # z≈0.26
    assert lowvol["votes"]["mom_15s"] == 1
    assert hivol["votes"]["mom_15s"] == 0


def test_microprice_is_spread_normalized():
    tight = D.directional_thesis(microprice_gap=0.01, spread=0.01)     # gap = 1.0 spread -> votes
    wide = D.directional_thesis(microprice_gap=0.01, spread=0.05)      # gap = 0.2 spread -> noise
    assert tight["votes"]["microprice"] == 1
    assert wide["votes"]["microprice"] == 0


# ---- scalper ----
def _book(up_ask=0.50, up_bid=0.49, down_ask=0.51, down_bid=0.50):
    return {"up_ask": up_ask, "up_bid": up_bid, "down_ask": down_ask, "down_bid": down_bid}

STRONG_UP = {"side": "Up", "conviction": 0.6, "score": 0.6, "n_agree": 3, "votes": {"mom_15s": 1, "microprice": 1, "imbalance": 1}}
FLIP_DOWN = {"side": "Down", "conviction": 0.6, "score": -0.6, "n_agree": 3, "votes": {}}
CONFIRMED_UP = {"confirmed": True, "confirmed_side": "Up", "persistence_ticks": 3}
NOT_CONFIRMED = {"confirmed": False, "confirmed_side": None, "persistence_ticks": 1}


def test_enters_only_on_confirmed_direction():
    a = S.decide(thesis=STRONG_UP, book=_book(up_ask=0.50), position=None, now_ts=100,
                 seconds_left=200, confirm=CONFIRMED_UP)
    assert a["action"] == "enter" and a["side"] == "Up" and a["price"] == 0.50


def test_waits_when_not_confirmed():
    a = S.decide(thesis=STRONG_UP, book=_book(), position=None, now_ts=100,
                 seconds_left=200, confirm=NOT_CONFIRMED)
    assert a["action"] == "wait" and "confirmed" in a["reason"]


def test_rides_while_direction_holds_no_fixed_cap():
    # up +2c, direction still Up, no giveback from peak -> HOLD (ride past any fixed target)
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.52)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.52), position=pos, now_ts=110, seconds_left=180)
    assert a["action"] == "hold"


def test_persistent_flip_exits_but_a_single_tick_flip_does_not():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, flip_since=110)
    hold = S.decide(thesis=FLIP_DOWN, book=_book(up_bid=0.49), position=pos, now_ts=112, seconds_left=150)
    assert hold["action"] == "hold"
    pos2 = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, flip_since=100)
    out = S.decide(thesis=FLIP_DOWN, book=_book(up_bid=0.49), position=pos2, now_ts=100 + S.FLIP_PERSIST_S,
                   seconds_left=150)
    assert out["action"] == "exit" and out["kind"] == "reversal"


def test_faded_flip_does_not_exit():
    # the read flipped and persisted, but conviction faded below the fire bar -> HOLD (not worth banking)
    faded = {"side": "Down", "conviction": 0.20, "score": -0.20, "votes": {}}
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, flip_since=100)
    a = S.decide(thesis=faded, book=_book(up_bid=0.49), position=pos, now_ts=100 + S.FLIP_PERSIST_S,
                 seconds_left=150)
    assert a["action"] == "hold"


def test_patience_stop_ignores_a_gap_but_cuts_a_persistent_adverse_move():
    # adverse move present but only just started -> HOLD (gap-immune)
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=119)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.38), position=pos, now_ts=120, seconds_left=150)
    assert a["action"] == "hold"
    # same adverse move, now persisted >= PATIENCE_S -> CUT
    pos2 = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=100)
    a2 = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.38), position=pos2, now_ts=100 + S.PATIENCE_S,
                  seconds_left=150)
    assert a2["action"] == "exit" and a2["kind"] == "stop"


def test_catastrophe_cap_cuts_a_collapse():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=None)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.24), position=pos, now_ts=104, seconds_left=150)
    assert a["action"] == "exit" and a["kind"] == "stop"


def test_update_exit_state_sets_and_clears_timers():
    pos = Position("Up", 0.50, 100, 20.0)
    S.update_exit_state(pos, _book(up_bid=0.38), STRONG_UP, 130)   # 12c adverse -> timer starts
    assert pos.adverse_since == 130
    S.update_exit_state(pos, _book(up_bid=0.49), STRONG_UP, 132)   # recovered -> timer clears
    assert pos.adverse_since is None
    S.update_exit_state(pos, _book(up_bid=0.49), FLIP_DOWN, 134)   # opposite read -> flip timer starts
    assert pos.flip_since == 134


def test_trailing_locks_after_giveback_from_peak():
    # rode to a +6c peak, gave back to +4c (>1c giveback) -> lock the win
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.56)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.54), position=pos, now_ts=120, seconds_left=170)
    assert a["action"] == "exit" and a["kind"] == "take_profit" and "gave back" in a["reason"]


def test_near_certain_locks():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.90)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.95), position=pos, now_ts=110, seconds_left=120)
    assert a["action"] == "exit" and a["kind"] == "take_profit" and "near-certain" in a["reason"]


def test_timeout_closes_a_stalled_scalp():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.50), position=pos, now_ts=100 + S.HOLD_SECONDS,
                 seconds_left=120)
    assert a["action"] == "exit" and a["kind"] == "timeout"


def test_order_flow_votes_and_is_weighted():
    # tape order flow is a signal in the fusion: strong buy flow on Up votes Up
    t = D.directional_thesis(ret_15s=0.0004, order_flow=0.6, vol_per_s=0.00005)
    assert t["votes"]["order_flow"] == 1 and t["side"] == "Up"
    # contra: momentum up but heavy SELL flow splits agreement
    t2 = D.directional_thesis(ret_15s=0.0004, order_flow=-0.6, vol_per_s=0.00005)
    assert t2["votes"]["order_flow"] == -1 and t2["agreement"] < 1.0
    # it carries the highest weight (the walk-forward's strongest predictor)
    assert D.WEIGHTS["order_flow"] >= max(v for k, v in D.WEIGHTS.items() if k != "order_flow")


def test_tape_order_flow_degrades_gracefully():
    from sniper.algos.polymarket_crypto5m.live_desk import tape_order_flow
    # no condition / missing db -> all-None, never raises
    out = tape_order_flow(None, 1000)
    assert out["ofi_10s"] is None and out["ofi_30s"] is None
    out2 = tape_order_flow("0xdeadbeef", 1000, capture_db="/nonexistent/path.sqlite")
    assert out2["ofi_10s"] is None


def test_holt_trend_velocity():
    from sniper.algos.polymarket_crypto5m.trend_filter import holt_trend, holt_forecast
    up = [100.0 + 0.1 * i for i in range(30)]      # steady uptrend
    level, vel = holt_trend(up)
    assert vel is not None and vel > 0             # positive velocity on an uptrend
    dn = [100.0 - 0.1 * i for i in range(30)]
    assert holt_trend(dn)[1] < 0                   # negative on a downtrend
    assert holt_forecast(up, 5) > up[-1]           # forecast extends the trend
    assert holt_trend([100.0])[1] is None          # too few points -> None


# ---- reachability EV gate (Task 3) ----
def test_ev_none_enters_unchanged():
    # ev=None -> the gate is OFF; ds-patient behavior is untouched
    a = S.decide(thesis=STRONG_UP, book=_book(up_ask=0.50), position=None, now_ts=100,
                 seconds_left=200, confirm=CONFIRMED_UP, ev=None)
    assert a["action"] == "enter" and a["side"] == "Up"


def test_ev_pass_enters_and_carries_ev():
    ev = {"passed": True, "ev_007": 0.01, "t_hold": 12.0, "reject_reason": None}
    a = S.decide(thesis=STRONG_UP, book=_book(up_ask=0.50), position=None, now_ts=100,
                 seconds_left=200, confirm=CONFIRMED_UP, ev=ev)
    assert a["action"] == "enter" and a["ev"] is ev


def test_ev_fail_blocks_entry():
    ev = {"passed": False, "ev_007": -0.02, "t_hold": 5.0, "reject_reason": "ev_negative"}
    a = S.decide(thesis=STRONG_UP, book=_book(up_ask=0.50), position=None, now_ts=100,
                 seconds_left=200, confirm=CONFIRMED_UP, ev=ev)
    assert a["action"] == "wait" and "EV gate" in a["reason"]
