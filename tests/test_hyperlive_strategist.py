from sniper.hyperlive.calibration import Calibration
from sniper.hyperlive.models import MarketSnapshot
from sniper.hyperlive import strategist as ST
from sniper.hyperlive.regime import classify


def snap(asset="BTC", spread_bps=4.0, **feat):
    return MarketSnapshot(asset, 1000, 100.0, 100.0 + spread_bps / 100, 100.0, spread_bps, dict(feat), "ok")


def test_calm_regime_produces_no_trade():
    s = snap(vol_bps=3.0, ret60_bps=2.0, ret300_bps=1.0)
    assert classify(s) == "calm"
    idea, ranked, reason = ST.select([s], Calibration())
    assert idea is None
    assert "EV" in reason


def test_low_vol_continuation_does_not_clear_the_fee():
    # trending but quiet: continuation target is fee-floored ~22.5, prior p_win 0.5 -> EV<0
    s = snap(vol_bps=5.0, ret60_bps=12.0, ret300_bps=15.0)
    assert classify(s) == "trending"
    idea, ranked, reason = ST.select([s], Calibration())
    assert idea is None                       # fee discipline: no junk trade
    assert any(c.setup == "continuation" and c.ev_bps < 0 for c in ranked)


def test_cascade_event_reaction_clears_ev_and_is_selected():
    s = snap(asset="HYPE", spread_bps=4.0, vol_bps=20.0, liq_rate=5.0,
             event_side="long", event_strength_bps=100.0)
    assert classify(s) == "cascade"
    idea, ranked, reason = ST.select([s], Calibration())
    assert idea is not None
    assert idea.setup == "event_reaction" and idea.side == "long"
    assert idea.ev_bps > 0 and idea.asset == "HYPE"


def test_selects_highest_ranked_across_assets():
    weak = snap(asset="BTC", vol_bps=20.0, liq_rate=5.0, event_side="long", event_strength_bps=40.0)
    strong = snap(asset="SOL", vol_bps=20.0, liq_rate=5.0, event_side="short", event_strength_bps=300.0)
    idea, ranked, reason = ST.select([weak, strong], Calibration())
    assert idea is not None and idea.asset == "SOL"


def test_calibration_shifts_decision():
    # a setup the calibration says loses (p_win low) should be rejected even with a big target
    c = Calibration()
    for _ in range(40):
        c.update("event_reaction", "cascade", won=False, realized_move_bps=0.0)
    s = snap(vol_bps=20.0, liq_rate=5.0, event_side="long", event_strength_bps=100.0)
    idea, ranked, reason = ST.select([s], c)
    assert idea is None   # learned win-rate ~0 -> negative EV -> NO-TRADE


def test_continuation_skipped_when_oi_diverges_from_move():
    from sniper.hyperlive import setups as S
    s_fueled = snap(vol_bps=5.0, ret60_bps=12.0, ret300_bps=15.0, oi_confirm=1)
    s_covering = snap(vol_bps=5.0, ret60_bps=12.0, ret300_bps=15.0, oi_confirm=-1)
    assert S.propose("continuation", s_fueled) is not None        # OI confirms -> proposes
    assert S.propose("continuation", s_covering) is None          # OI diverges -> skipped


def test_reversion_fires_sooner_near_funding_settlement():
    from sniper.hyperlive import setups as S
    far = snap(vol_bps=10.0, ret60_bps=33.0, mins_to_funding=30.0)   # 33 < 4*10=40 -> no fade
    near = snap(vol_bps=10.0, ret60_bps=33.0, mins_to_funding=2.0)   # 33 >= 3*10=30 -> fade
    assert S.propose("reversion", far) is None
    assert S.propose("reversion", near) is not None
