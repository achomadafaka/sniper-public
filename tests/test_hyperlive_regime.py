from sniper.hyperlive.models import MarketSnapshot
from sniper.hyperlive.regime import classify


def snap(**f):
    return MarketSnapshot("BTC", 1000, 100.0, 100.04, 100.02, 4.0, dict(f), "ok")


def test_thin_book_triggers_dislocation_on_a_smaller_move():
    # vol 10, move 30bps: 30 < 4*10=40 normally, but >= 2.5*10=25 when the book is thin.
    assert classify(snap(vol_bps=10, ret60_bps=30, depth_ratio=0.5)) == "dislocation"
    assert classify(snap(vol_bps=10, ret60_bps=30, depth_ratio=1.0)) != "dislocation"


def test_cascade_and_calm_unaffected_by_depth():
    assert classify(snap(vol_bps=20, liq_rate=5, depth_ratio=0.4)) == "cascade"
    assert classify(snap(vol_bps=3, ret60_bps=2, ret300_bps=1, depth_ratio=0.4)) == "calm"
