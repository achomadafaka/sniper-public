"""The micro-band m5 DOLLAR floor, added 2026-09-18.

The operator's target is a take-profit inside two minutes. Measured on 218 funded copy closes that
carry the gate's own volume reading, organic m5 volume = provider five-minute volume minus the
copied buy:

    micro band ($50k-250k mcap)        n    fast TP (<=120s)    pnl
      organic exactly $0               6           33%       +$102.36
      organic $1 - $5,000             24            4%       -$153.41
      organic $5,000 - $100,000       75           36%       +$424.50
      organic >= $100,000             23           70%       +$457.41

ZERO IS NOT THIN. An organic value of exactly $0 means the only five-minute volume was the buy we
copy: a market that has not started, not one that stopped. Those six closes are the second-best
cell. The tests below pin that edge, because a naive "under $5k" bar refuses them too.

Both halves agree on the refused cell (2026-09-08..12: 6 closes, 17% fast, -$47.69; 09-13..18:
18 closes, 0% fast, -$105.72). The RATIO form of the same rule does not survive that split, so the
leg is expressed in dollars.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sniper.settings.memecoin as cfg
from sniper.services.memecoin_real_runner import volume_alive_decision as V

MCAP = 150_000.0
BAR = 5_000.0


def decide(vol_m5, **kw):
    """The gate with every other leg silent, so only the dollar floor can speak."""
    args = dict(abs_floor=0.0, turnover_floor=0.0, vol_m5=vol_m5, m5_floor=0.0, m5_ceiling=0.0,
                m5_abs_floor_usd=BAR, signal_usd=0.0)
    args.update(kw)
    return V(vol_h1=None, mcap=MCAP, **args)


def test_a_thin_tape_is_refused():
    allow, need, why = decide(1_800.0)
    assert allow is False
    assert why == "m5_volume_under_abs_floor"
    assert need == BAR


def test_an_absent_tape_is_admitted():
    """Organic $0 is the cell with 33% fast take-profits. It must not read as thin."""
    allow, _need, why = decide(0.0)
    assert allow is True
    assert why != "m5_volume_under_abs_floor"


def test_the_copied_buy_alone_is_absent_not_thin():
    """volume_m5 == their own size subtracts to zero: the market has not started."""
    allow, _need, why = decide(900.0, signal_usd=900.0)
    assert allow is True
    assert why != "m5_volume_under_abs_floor"


def test_an_unknown_tape_stays_unknown():
    allow, _need, why = decide(None)
    assert allow is True
    assert why != "m5_volume_under_abs_floor"


def test_volume_at_or_above_the_bar_passes():
    for value in (BAR, BAR + 1.0, 120_000.0):
        allow, _need, why = decide(value)
        assert allow is True, value
        assert why != "m5_volume_under_abs_floor"


def test_the_copied_buy_is_subtracted_before_the_comparison():
    """$6,000 of tape that is $5,500 our own source buy leaves $500 of market: refuse."""
    allow, _need, why = decide(6_000.0, signal_usd=5_500.0)
    assert allow is False
    assert why == "m5_volume_under_abs_floor"


def test_zero_bar_disables_the_leg():
    allow, _need, why = decide(10.0, m5_abs_floor_usd=0.0)
    assert allow is True
    assert why != "m5_volume_under_abs_floor"


def test_a_malformed_bar_does_not_refuse():
    allow, _need, why = decide(10.0, m5_abs_floor_usd="not a number")
    assert allow is True


def test_it_runs_before_the_ceiling():
    """A coin both under the dollar bar and over the ceiling reports the floor, like the ratio legs."""
    allow, _need, why = decide(1_000.0, m5_ceiling=0.001, m5_band_max_mcap=2_000_000.0)
    assert allow is False
    assert why == "m5_volume_under_abs_floor"


def test_the_trade_count_leg_still_decides_first():
    allow, _need, why = decide(1_000.0, m5_txns=1, m5_txns_floor=2)
    assert allow is False
    assert why == "m5_txns_under_floor"


@pytest.mark.parametrize("band,chain,expected", [
    ("micro", "solana", 5_000.0),
    ("micro", "4663", 5_000.0),
    ("micro", "56", 0.0),
    ("micro", "5042", 0.0),
    ("nano", "solana", 0.0),
    ("small", "solana", 0.0),
    (None, "solana", 0.0),
])
def test_the_scope_is_the_micro_band_on_the_two_measured_chains(band, chain, expected):
    env = {"MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD": "5000",
           "MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_CHAINS": "solana,4663"}
    assert cfg.micro_min_volume_m5_usd_for(chain, band, env=env) == expected


def test_an_unset_value_leaves_the_leg_inert(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_MIN_VOLUME_M5_USD", 0.0)
    assert cfg.micro_min_volume_m5_usd_for("solana", "micro", env={}) == 0.0


def test_the_refusal_is_captured_as_its_own_family():
    from sniper.services import memecoin_refusal_capture as capture
    reason = "m5 organic volume $1800 under floor $5000 (m5_volume_under_abs_floor, revival guard)"
    assert capture.classify_gate(reason) == "m5_volume_under_abs_floor"
    assert capture.should_capture("m5_volume_under_abs_floor") is True


def test_the_revival_watch_does_not_re_ask_on_it():
    """It matches the RATIO floor reason exactly, and this is a different cut."""
    from sniper.services import memecoin_revival_watch as rw
    assert rw.should_watch("m5_volume_under_abs_floor", mcap=MCAP, max_mcap=0.0) is False


def test_the_evidence_record_carries_the_number_the_gate_decided_on():
    """The old field subtracts unconditionally; the decision does not. Both are recorded."""
    from sniper.services.memecoin_real_runner import _volume_notification_evidence as E
    # The source buy is 1 s old and the provider lags 90 s, so it cannot be in the number yet:
    # the decision must NOT subtract it, and the legacy field still does.
    rec = E(volume_m5=4_000.0, signal_usd=3_500.0, mcap=MCAP, effective_ceiling=0.0,
            reason="m5_turnover_under_floor", need=1.0, source_age_s=1.0, index_lag_s=90.0)
    assert rec["organic_volume_m5"] == 500.0
    assert rec["decided_organic_volume_m5"] == 4_000.0


def test_the_two_organic_numbers_agree_when_the_buy_is_indexable():
    from sniper.services.memecoin_real_runner import _volume_notification_evidence as E
    rec = E(volume_m5=9_000.0, signal_usd=1_000.0, mcap=MCAP, effective_ceiling=0.0,
            reason="m5_turnover_under_floor", need=1.0, source_age_s=600.0, index_lag_s=90.0)
    assert rec["organic_volume_m5"] == rec["decided_organic_volume_m5"] == 8_000.0
