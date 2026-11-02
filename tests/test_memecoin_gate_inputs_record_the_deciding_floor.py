"""`gate_inputs_json.ratio_floor` must be the floor that DECIDED, not a module constant.

Found by Gamma on 2026-09-09 while pre-registering the band study. `_volume_gate_inputs` recorded
`MEMECOIN_REAL_MIN_TURNOVER_M5` unconditionally — that is the OVER-BAND leg, 0.005 — while
`_volume_active` is true only above `MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD` (500,000). Every
nano, micro and small coin therefore takes the IN-BAND leg, which is a different number entirely.

Two live refusals minutes before the fix:

    589      m5 organic volume $6743 under floor $13008   mcap 130,100 -> decided at 0.1000
    ZOGCASH  m5 organic volume $1999 under floor $6959    mcap  69,600 -> decided at 0.1000
    both recorded  ratio_floor: 0.005

A 20x discrepancy on the one field a study of this gate would read, and it would have silently
corrupted the band study that was being pre-registered around it. `ratio_floor_over_band` already
carries the other leg, so recording the deciding one loses nothing.
"""
import json

import pytest

import sniper.services.memecoin_real_runner as RR


class _Ev:
    their_size_usd = 100.0


ST = {"market_cap": 130_100.0, "volume_m5": 6743.0, "volume_h1": 40_000.0,
      "liquidity": 22_000.0, "txns_m5_buys": 4, "txns_m5_sells": 2}


def _inputs(**kw):
    return json.loads(RR._volume_gate_inputs(ST, _Ev(), "m5_turnover_under_floor", 13008.0, **kw))


def test_the_in_band_leg_is_recorded_when_it_decided():
    """THE REGRESSION: 589 and ZOGCASH were judged at 0.10 and recorded 0.005."""
    assert _inputs(effective_ratio_floor=0.10)["ratio_floor"] == pytest.approx(0.10)


def test_the_over_band_leg_is_recorded_when_it_decided():
    assert _inputs(effective_ratio_floor=0.005)["ratio_floor"] == pytest.approx(0.005)


def test_the_other_leg_is_still_carried_separately():
    """Nothing is lost: `ratio_floor_over_band` keeps the over-band value regardless."""
    got = _inputs(effective_ratio_floor=0.10)
    assert "ratio_floor_over_band" in got and "band_max_mcap" in got


def test_omitting_the_argument_keeps_the_old_constant():
    """Back-compatible for any caller that does not know its leg — the value is then the module
    constant, exactly as before, rather than a fabricated zero."""
    from sniper.settings import memecoin as cfg
    assert _inputs()["ratio_floor"] == pytest.approx(
        float(getattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0) or 0))


def test_a_zero_floor_is_recorded_as_zero_not_dropped():
    """0 means the leg was OFF, which is a real state and must be distinguishable from absent."""
    assert _inputs(effective_ratio_floor=0.0)["ratio_floor"] == 0.0


def test_the_hook_passes_the_leg_that_ran():
    import inspect
    src = " ".join(inspect.getsource(RR.real_entry_hook).split())
    assert "effective_ratio_floor=(" in src
    assert "inband_min_turnover_m5_for_chain(identity.network_id)" in src
    assert "if _volume_active" in src, "the two legs must be selected by the same test the gate uses"


def test_the_recorded_floor_reproduces_the_refusal_arithmetic():
    """The whole point: a reader must be able to recompute the refusal from the record.
    589: $6,743 organic against a $13,008 bar on a $130,100 mcap."""
    got = _inputs(effective_ratio_floor=0.10)
    implied_bar = got["ratio_floor"] * got["mcap"]
    assert implied_bar == pytest.approx(13010.0, abs=5.0)
    assert got["volume_m5"] < implied_bar
