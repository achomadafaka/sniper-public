"""The in-band m5 turnover ceiling, made reachable 2026-09-08.

`MEMECOIN_REAL_MAX_TURNOVER_M5` (1.5) cannot fire at any market cap. The caller sets
`_volume_active = mcap > MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD` and only then passes a non-zero
`m5_ceiling`; inside `volume_alive_decision`, `ceil_ok = band_ok` and `band_ok` is
`mc <= m5_band_max_mcap` — the opposite test. Above the band the ceiling is skipped because
band_ok is False; below it, because the caller passed 0.

THE EVIDENCE FOR AN UPPER BOUND. Codex's win/loss review found a conjunction that I replicated on an
independent 370-close cut:

    turnover 10-50% + source $1k-10k   n=44   +21.72%   win 75.0%
    turnover 10-50%, other source      n=89    +5.54%
    other turnover, source $1k-10k     n=97    -1.60%
    neither                           n=140    -1.80%

The band has an UPPER edge, and my own cut puts >=0.8 turnover at -7.88% over n=73. The floor alone
cannot express that.

THIS IS NOT THE 1.5 CEILING. That one I priced separately and it was one day
(2026-08-27 = -$264.82, every other day +$169.48) — the operator's decision to leave
MEMECOIN_REAL_MAX_TURNOVER_M5 inactive stands, and this ships as a SEPARATE in-band setting
defaulting to 0 = off so nothing changes until a value is set.

RAW, NOT ORGANIC. The floor subtracts the source buy because it is asking "is there a market beyond
the one trade we are reacting to". The ceiling asks "is the float churning", and churn is churn
whoever traded it — subtracting from a safety bound would loosen it. It is also what the evidence
measured: the conjunction's turnover numerator includes the source buy.
"""
import pytest

from sniper.services.memecoin_real_runner import volume_alive_decision as V

CEIL = 0.50


def _inband(mcap, turnover, *, ceiling=CEIL, signal_usd=0.0, vol_m5=None):
    """Drive the callee exactly as the caller does for an IN-BAND coin."""
    return V(50_000.0, mcap,
             abs_floor=0.0, turnover_floor=0.0,
             vol_m5=(mcap * turnover if vol_m5 is None else vol_m5),
             m5_floor=0.0, m5_band_max_mcap=0.0, m5_floor_over_band=0.0,
             m5_ceiling=ceiling, m5_required=False,
             m5_txns=50, m5_txns_floor=0, m5_txns_required=False, signal_usd=signal_usd)


def test_a_churning_in_band_coin_is_refused():
    """THE REGRESSION: this returned (True, 'ok') at every market cap."""
    ok, need, why = _inband(50_000.0, 0.9)
    assert ok is False
    assert why == "m5_turnover_over_ceiling"
    assert need == pytest.approx(50_000.0 * CEIL)


@pytest.mark.parametrize("mcap", [15_000.0, 120_000.0, 900_000.0, 1_999_999.0])
def test_it_refuses_across_the_whole_in_band_range(mcap):
    assert _inband(mcap, 3.0)[0] is False


def test_a_coin_inside_the_band_passes():
    """The band is INCLUSIVE at both edges, and that is deliberate symmetry with the floor: the
    floor refuses on `organic < mcap * floor` and the ceiling on `raw > mcap * ceiling`, so a coin
    sitting exactly on either edge is admitted. My first draft asserted the opposite here."""
    assert _inband(50_000.0, 0.30)[0] is True
    assert _inband(50_000.0, CEIL)[0] is True         # exactly at the cap is NOT over
    assert _inband(50_000.0, CEIL * 1.01)[0] is False


def test_zero_is_off():
    assert _inband(50_000.0, 9.0, ceiling=0.0)[0] is True


def test_a_missing_m5_reading_passes():
    assert _inband(50_000.0, 0.0, vol_m5=None)[0] is True


def test_the_ceiling_reads_RAW_volume_not_organic():
    """The floor subtracts the source buy; the ceiling must not. Churn is churn whoever traded it,
    and subtracting from a safety bound would loosen it."""
    mcap = 20_000.0
    raw = mcap * 0.9                      # 0.9x turnover, over a 0.50 cap
    ok, _n, why = _inband(mcap, 0.0, vol_m5=raw, signal_usd=raw * 0.8)
    assert ok is False and why == "m5_turnover_over_ceiling", (
        "the signal buy was subtracted from a ceiling, which loosens a safety bound")


def test_the_over_band_path_is_untouched():
    """Above the boundary the caller still supplies the band bound, and that path must not move."""
    ok, _n, why = V(50_000.0, 9_000_000.0, abs_floor=0.0, turnover_floor=0.0,
                    vol_m5=9_000_000.0 * 0.001, m5_floor=0.005,
                    m5_band_max_mcap=2_000_000.0, m5_floor_over_band=0.03,
                    m5_ceiling=1.5, m5_required=False, signal_usd=0.0)
    assert ok is False and why == "m5_turnover_under_big_floor"


def test_the_caller_supplies_the_in_band_ceiling():
    """Source guard: the defect lived entirely in the caller."""
    from _source_guard import code_of
    from sniper.services.memecoin_real_runner import real_entry_hook, volume_gate_args

    # 2026-09-12: the arguments moved into `volume_gate_args`, shared with the revival watch.
    assert "coin_band=getattr(classification, 'coin_band', None)" in code_of(real_entry_hook)
    src = code_of(volume_gate_args)
    assert "else cfg.inband_max_turnover_m5_for(network_id, coin_band)" in src
    assert "m5_ceiling=ceiling" in src
    assert "ceiling = getattr(cfg, 'MEMECOIN_REAL_MAX_TURNOVER_M5', 0.0) if active else" in src
