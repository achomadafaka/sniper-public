"""N1 residual 1, the direct routed path (Alpha, 2026-10-07): a routed buy's whole average is bounded
by its own venue event's average, with no new read.

A routed buy taken straight from the processed frame prices the hard copy-premium cap from the
owner's USDC fall over the coins it gained. An unrelated USDC outflow in the same slot inflates that
average and loosens MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT. The frame's own pump event prices the same
buy at the snapshot's SOL/USD: a whole average above it by more than
MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO is unknown and refuses while the cap is on. A lower one only
tightens the cap and is kept.
"""
import dataclasses
from types import SimpleNamespace

import pytest

from sniper.settings import memecoin as cfg
from test_slot_lane_price_cap import BLOCKHASH, CAP, _curve_builder, _ps_builder

R = 1.10


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO", R, raising=False)


def _routed(spent_atomic, acquired_atomic, decimals=6):
    return SimpleNamespace(usdc_spent_atomic=int(spent_atomic), token_delta_atomic=int(acquired_atomic),
                           token_decimals=decimals)


def _ps(routed=None):
    b, buy, _event, _facts = _ps_builder(6)
    if routed is not None:
        buy = SimpleNamespace(**vars(buy), routed=routed)
    return b.build(buy, 5.0, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=175.0))


def _curve(routed=None):
    b, buy = _curve_builder()
    if routed is not None:
        buy = SimpleNamespace(**vars(buy), routed=routed)
    return b.build(buy, 175.0, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=121.5))


def _whole(event_average, ratio, decimals, acquired=10 ** 15):
    """A receipt whose whole average is `ratio` x the event's, USD per whole token."""
    return _routed(event_average * ratio * acquired / 10 ** decimals * 1e6, acquired, decimals)


@pytest.mark.parametrize("build, decimals", [(_ps, 6), (_curve, 6)], ids=["pumpswap", "pump_curve"])
def test_a_whole_average_far_above_its_event_is_unknown(build, decimals):
    message, validity = build()
    assert message is not None, validity
    event_avg = validity["pump_price_limit"]["source_average_price_usd"]
    message, why = build(_whole(event_avg, R * 1.02, decimals))
    assert message is None and why.startswith("routed average 1.12")


@pytest.mark.parametrize("build, decimals", [(_ps, 6), (_curve, 6)], ids=["pumpswap", "pump_curve"])
def test_fees_and_the_hop_under_the_bound_sign_at_the_whole_average(build, decimals):
    _m, validity = build()
    event_avg = validity["pump_price_limit"]["source_average_price_usd"]
    message, validity = build(_whole(event_avg, R * 0.98, decimals))
    assert message is not None, validity
    assert validity["pump_price_limit"]["source_average_price_usd"] == pytest.approx(event_avg * R * 0.98,
                                                                                      rel=1e-9)


def test_with_the_hard_cap_off_the_bound_does_not_apply(monkeypatch):
    _m, validity = _ps()
    event_avg = validity["pump_price_limit"]["source_average_price_usd"]
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0)
    message, validity = _ps(_whole(event_avg, R * 3, 6))
    assert message is not None, validity


def test_the_quote_curve_takes_the_same_bound(monkeypatch):
    import test_slot_lane_quote_curve as Q
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_curve"}), raising=False)
    b = Q._builder()
    buy = Q._slot_buy(Q.EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    usd = 100.0 / (118_090 / 1e8)
    message, validity = b.build(buy, 100.0, Q.SNAP, leg=Q._leg(), quote=(8, usd, 0, 0))
    assert message is not None, validity
    event_avg = validity["pump_price_limit"]["source_average_price_usd"]
    over = dataclasses.replace(buy, routed=_whole(event_avg, R * 1.02, 6))
    message, why = b.build(over, 100.0, Q.SNAP, leg=Q._leg(), quote=(8, usd, 0, 0))
    assert message is None and why.startswith("routed average 1.12")


@pytest.mark.parametrize("raw", ["0.99", "nan", "inf"])
def test_the_ratio_setting_must_be_at_least_one(monkeypatch, raw):
    import importlib
    monkeypatch.setenv("MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO", raw)
    with pytest.raises(ValueError, match="MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO"):
        importlib.reload(cfg)
    monkeypatch.delenv("MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO")
    importlib.reload(cfg)


# --- CPMM and LaunchLab: the swap or trade event's own average (Alpha, 2026-10-07) ---------------

def _routed_venue(name):
    import test_slot_lane_source_average_cap as SA
    return {"cpmm": SA._cpmm, "launchlab": SA._launchlab}[name]


@pytest.mark.parametrize("name", ["cpmm", "launchlab"])
def test_a_routed_cpmm_or_launchlab_whole_average_far_above_its_event_is_unknown(name):
    venue, source_in = _routed_venue(name), 40 * 10 ** 6
    (message, why), _avg = venue(source_in, spent=int(source_in * R * 1.02))
    assert message is None and why.startswith("routed average 1.12")


@pytest.mark.parametrize("name", ["cpmm", "launchlab"])
def test_a_routed_cpmm_or_launchlab_whole_average_under_the_bound_signs_at_it(name):
    venue, source_in = _routed_venue(name), 40 * 10 ** 6
    (message, validity), event_avg = venue(source_in, spent=int(source_in * R * 0.98))
    assert message is not None, validity
    assert validity["pump_price_limit"]["source_average_price_usd"] == pytest.approx(
        event_avg * R * 0.98, rel=1e-6)
