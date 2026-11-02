"""The in-band m5 turnover floor, restored 2026-09-08.

`0ef67c91` (2026-08-31) added `_volume_active = mcap > BAND_MAX_MCAP` to the caller. Inside
`volume_alive_decision`, `band_ok` is the OPPOSITE test (`mc <= m5_band_max_mcap`), and
`eff_floor = m5_floor if band_ok else m5_floor_over_band`. So the in-band floor was only ever
supplied when the coin was out of band, and `MEMECOIN_REAL_MIN_TURNOVER_M5` could not reject at any
market cap. Measured over the eight days to 2026-09-08 the whole volume block ran on 80 of 12,682
attempts and was active 7 times, all on coins between $37M and $136M.

The funded book is nano/micro, so the filter was pointed away from everything it trades.

WHAT THE FLOOR IS WORTH, measured on 621 closed funded positions carrying volume_m5 and mcap:

    floor   refuses            net effect
    1.0%    n=47  -$105.86      +$105.86
    1.5%    n=56  -$202.21      +$202.21
    2.0%    n=68  -$262.26      +$262.26   <- chosen
    3.0%    n=81  -$154.48      +$154.48
    5.0%    n=120 -$149.44      +$149.44

2% is the peak and its neighbours hold, so the cut sits on a plateau rather than a spike. The kept
population is n=556 worth +$826.19. Both time halves of the refused group are negative
(-$82.88, -$179.38). Operator chose it globally rather than per-chain: on 4663 the sub-2% band is
-$308 across 30, on Solana it is +$45 across 38, so going global forgoes that $45 and still nets
+$262.

SCOPE, deliberately narrow. Only the in-band m5 FLOOR turns on. The ceiling stays inactive
(operator decision 2026-09-08, and it refuses a profitable >=3x tail). The h1 floors and the m5 txn
floor keep their existing above-band-only scope — they were never measured on nano/micro. And a
missing m5 reading still PASSES: "missing readings alone do not justify rejecting trades".
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.services.memecoin_real_runner import volume_alive_decision as V

BAND = 2_000_000.0
FLOOR = 0.02


_UNSET = object()


def _inband(mcap, turnover, *, vol_m5=_UNSET, floor=FLOOR, txns=50):
    """Drive the callee exactly as the caller does for an IN-BAND coin: every above-band knob is
    zero, so only the in-band floor can speak.

    `vol_m5` uses a sentinel, not None: None is a MEANINGFUL value here (no reading at all) and
    must be passable, while a computed 0.0 is a reading OF zero and must refuse. Collapsing the two
    is the exact defect this file's neighbours were written for.
    """
    return V(50_000.0, mcap,
             abs_floor=0.0, turnover_floor=0.0,
             vol_m5=(mcap * turnover if vol_m5 is _UNSET else vol_m5),
             m5_floor=floor, m5_band_max_mcap=0.0, m5_floor_over_band=0.0,
             m5_ceiling=0.0, m5_required=False,
             m5_txns=txns, m5_txns_floor=0, m5_txns_required=False, signal_usd=0.0)


def test_a_quiet_in_band_coin_is_refused():
    """THE REGRESSION: this returned (True, 'ok') at every market cap for eight days."""
    ok, need, why = _inband(50_000.0, 0.005)
    assert ok is False
    assert why == "m5_turnover_under_floor"
    assert need == pytest.approx(50_000.0 * FLOOR)


@pytest.mark.parametrize("mcap", [15_000.0, 50_000.0, 250_000.0, 1_999_999.0])
def test_it_refuses_across_the_whole_in_band_range(mcap):
    assert _inband(mcap, 0.001)[0] is False


def test_a_coin_at_the_floor_passes():
    """Exactly at the bar is admitted — the comparison is `<`, not `<=`."""
    assert _inband(50_000.0, FLOOR)[0] is True
    assert _inband(50_000.0, FLOOR * 1.5)[0] is True


def test_a_missing_m5_reading_still_passes():
    """Operator, 2026-09-08: missing readings alone do not justify rejecting trades. 37% of the
    funded book carries no m5 reading, so failing closed here would refuse a third of everything."""
    assert _inband(50_000.0, 0.0, vol_m5=None)[0] is True


def test_a_ZERO_m5_reading_is_refused():
    """A reading of zero is evidence of a dead tape; only an ABSENT reading is excused."""
    ok, _n, why = _inband(50_000.0, 0.0, vol_m5=0.0)
    assert ok is False and why == "m5_turnover_under_floor"


def test_an_unknown_mcap_still_passes():
    """`if mc and mc > 0` guards the block; a coin we cannot value is not a coin we refuse."""
    assert V(50_000.0, None, abs_floor=0.0, turnover_floor=0.0, vol_m5=0.0,
             m5_floor=FLOOR, m5_band_max_mcap=0.0, m5_floor_over_band=0.0,
             m5_ceiling=0.0, m5_required=False, signal_usd=0.0)[0] is True


def test_the_ceiling_stays_off_for_in_band_coins():
    """Operator left it inactive, and the measured >=3x tail is profitable (+$72.69 over n=25)."""
    assert _inband(50_000.0, 5.0)[0] is True


def test_the_signal_buy_is_excluded_before_the_floor_compares():
    """A $2.5k copied buy on a $20k coin must not satisfy the floor by itself."""
    mcap = 20_000.0
    ok, _need, why = V(50_000.0, mcap, abs_floor=0.0, turnover_floor=0.0,
                       vol_m5=500.0, m5_floor=FLOOR, m5_band_max_mcap=0.0,
                       m5_floor_over_band=0.0, m5_ceiling=0.0, m5_required=False,
                       signal_usd=500.0)
    assert ok is False and why == "m5_turnover_under_floor"


def test_the_over_band_leg_is_untouched():
    """Above the boundary the caller still supplies the 3% over-band floor and the band bound, and
    that path must behave exactly as before."""
    ok, _need, why = V(50_000.0, 9_000_000.0, abs_floor=0.0, turnover_floor=0.0,
                       vol_m5=9_000_000.0 * 0.001, m5_floor=0.005,
                       m5_band_max_mcap=BAND, m5_floor_over_band=0.03,
                       m5_ceiling=1.5, m5_required=False, signal_usd=0.0)
    assert ok is False and why == "m5_turnover_under_big_floor"


def test_the_caller_supplies_the_in_band_floor():
    """Source guard: the defect was entirely in the CALLER, which only ever passed a non-zero
    m5_floor when the coin was out of band — the one case the callee cannot use it."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sniper/services/memecoin_real_runner.py")).read()
    # MATCH ON THE BRANCH, NOT ON A SPELLING. This originally asserted the literal constant name
    # and went red on 2026-09-08 when the value became per-chain via
    # `cfg.inband_min_turnover_m5_for_chain(...)` — the wiring was correct and the guard was
    # checking how it was spelled. Accept either form; what must hold is that the NOT-active
    # branch resolves an in-band floor rather than passing 0.
    def _is_inband(text):
        low = text.lower()
        return "inband" in low or "in_band" in low
    assert _is_inband(src), "the in-band floor is never supplied anywhere in the runner"
    # 2026-09-12: the arguments moved into `volume_gate_args`, shared with the revival watch.
    seg = src[src.index("def volume_gate_args("):][:4000]
    line = next(l for l in seg.splitlines() if "m5_floor=" in l and "over_band" not in l)
    idx = seg.index(line)
    window = seg[idx:idx + 320]
    assert _is_inband(window), "in-band floor not wired to the else branch: " + line
