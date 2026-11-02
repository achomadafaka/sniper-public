from sniper.hyperlive.engine import HyperliveEngine
from sniper.hyperlive.models import MarketSnapshot


def snap(bid, ask, ts=1000, asset="HYPE", **feat):
    return MarketSnapshot(asset, ts, bid, ask, (bid + ask) / 2,
                          (ask - bid) / ((ask + bid) / 2) * 1e4, dict(feat), "ok")


CASCADE = dict(vol_bps=20.0, liq_rate=5.0, event_side="long", event_strength_bps=300.0)


def _drive(engine, snaps_seq):
    states = []
    for snaps, ts in snaps_seq:
        st = engine.step([s for s in snaps], ts)
        states.append(st["state"])
    return states


def test_full_cycle_enter_take_profit_evaluate_and_loop():
    e = HyperliveEngine()
    base = snap(100.0, 100.04, **CASCADE)
    tp = snap(103.0, 103.04, ts=50_000, **CASCADE)   # price jumped up -> long TP
    seq = [([base], 1000)] * 4 + [([tp], 50_000)] * 3
    states = _drive(e, seq)

    assert "MANAGING" in states and "EVALUATING" in states
    assert e.state == "SCANNING"               # looped back
    assert len(e.history) == 1
    assert e.history[0]["reason"] == "take_profit" and e.history[0]["won"] is True
    assert e.history[0]["setup"] == "event_reaction"
    assert e.calib.count("event_reaction", "cascade") == 1   # learned from the outcome


def test_calm_market_loops_no_trade_without_position():
    e = HyperliveEngine()
    calm = snap(100.0, 100.04, vol_bps=3.0, ret60_bps=2.0, ret300_bps=1.0)
    _drive(e, [([calm], 1000)] * 4)
    assert e.pos is None
    assert e.cycle_id >= 1                       # advanced cycles via NO-TRADE
    assert "no candidate" in e.no_trade_reason


def test_stop_loss_is_recorded_as_loss():
    e = HyperliveEngine()
    base = snap(100.0, 100.04, **CASCADE)
    down = snap(90.0, 90.04, ts=20_000, **CASCADE)  # crashed against the long -> SL
    _drive(e, [([base], 1000)] * 4 + [([down], 20_000)] * 3)
    assert len(e.history) == 1
    assert e.history[0]["reason"] == "stop_loss" and e.history[0]["won"] is False
