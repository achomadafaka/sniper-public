from sniper.hyperlive.calibration import Calibration
from sniper.hyperlive.models import MarketSnapshot
from sniper.hyperlive import strategist as ST
from sniper.hyperlive import setups as S
from sniper.hyperlive.engine import HyperliveEngine, Portfolio


def snap(asset="BTC", spread_bps=4.0, **feat):
    return MarketSnapshot(asset, 1000, 100.0, 100.0 + spread_bps / 100, 100.0, spread_bps, dict(feat), "ok")


def test_swing_setups_fire_on_their_signals_with_long_horizons():
    p = S.propose("basis_reversion", snap(basis=0.015))           # 150bps mark-oracle basis
    assert p and p[0] == "short" and p[3] == 1_800_000           # ~30m
    p = S.propose("funding_reversion", snap(funding=0.0005))      # 5bps funding
    assert p and p[0] == "short" and p[3] == 3_600_000           # ~1h
    p = S.propose("post_cascade", snap(liq_rate=3, ret300_bps=120))
    assert p and p[0] == "short" and p[3] == 900_000             # ~15m


def test_track_filter_scopes_each_engine_to_its_horizon_band():
    s = snap(vol_bps=20.0, liq_rate=5.0, ret300_bps=120.0, event_side="long", event_strength_bps=300.0)
    fast = ST.select([s], Calibration(), S.TRACK_SETUPS["fast"])[0]
    swing = ST.select([s], Calibration(), S.TRACK_SETUPS["swing"])[0]
    assert fast is not None and fast.setup in S.TRACK_SETUPS["fast"]
    assert swing is not None and swing.setup == "post_cascade"   # the swing play on a cascade


def test_portfolio_guard_blocks_same_asset_across_tracks():
    pf = Portfolio()
    pf.register("fast", "BTC", "long")
    assert pf.can_enter("BTC", "short", "swing") is False        # same asset, other track -> blocked
    assert pf.can_enter("ETH", "long", "swing") is True
    assert pf.can_enter("BTC", "long", "fast") is True           # own track can re-enter


def test_engine_aborts_entry_when_portfolio_holds_the_asset():
    pf = Portfolio()
    pf.register("fast", "BTC", "long")                           # fast already long BTC
    e = HyperliveEngine(track="swing", allowed_setups=S.TRACK_SETUPS["swing"], portfolio=pf)
    s = snap(asset="BTC", vol_bps=20.0, liq_rate=5.0, ret300_bps=120.0)
    for _ in range(5):                                           # SCANNING->SELECTING->DESIGNING->ARMED(blocked)
        e.step([s], 1000)
    assert e.pos is None
    assert "portfolio guard" in e.no_trade_reason
