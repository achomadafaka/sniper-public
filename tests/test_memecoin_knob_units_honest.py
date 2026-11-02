"""A knob's unit must describe the number the gate actually reads.

Controls review, 2026-09-08. Two defects, both on funded selection gates:

1. RAW RATIOS LABELLED AS PERCENTAGES. Six knobs are stored and compared as ratios of market cap
   (`vol_m5 >= mcap * value`) while declaring `unit="% of mcap"`. The live values read 100x wrong:

       MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5   0.02   shown "0.02 %"   means 2%
       MEMECOIN_REAL_MAX_TURNOVER_M5          1.5    shown "1.5 %"    means 150%
       MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO 0.008 shown "0.008 %"  means 0.8%

   An operator who reads "2 % of mcap" and types 2 sets a 200% floor and refuses everything.

2. A RATIO LABELLED AS A DOLLAR BOUNDARY. `MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND` holds 0.03 — the
   turnover floor applied ABOVE the band — but was labelled "Big-mcap boundary", `unit="USD"`,
   `maximum=1e10`. It is not the boundary; the boundary is
   MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD ($2,000,000), which is not exposed as a knob at all.
   Typing the intended 2000000 into that box would set a 2,000,000x turnover floor and refuse every
   above-band entry, live, with no other signal.

The generic guard below is the one that would have caught defect 2 without anyone reviewing labels:
a dollar amount is never 0.03.
"""
import re

from sniper.services import memecoin_entry_knobs as K

# NO `.env` LOADING, AND THE FIRST DRAFT OF THIS FILE HAD IT. Reading `.env` at import made every
# test here an ERROR in a release checkout, which has no `.env` — the deploy gate refused
# a8f87722 with 8 of them and was right to. Every assertion below reads the knob DECLARATION, which
# is identical in a dev tree, a worktree and a release, so no live value is needed. That is also
# what makes the guards able to fail: a value-based version passed against the live defect because
# the setting fell back to its default.


def _knobs():
    return {k.name: k for k in K._KNOBS}


# THE GUARD IS STRUCTURAL, NOT VALUE-BASED, AND THE FIRST DRAFT WAS NOT.
# I wrote it as "no USD knob holds a value below $1" and it PASSED against the live defect,
# because pytest does not load `.env`: the setting fell back to its 0.0 default and `0 < 0.0` is
# false. A guard that cannot fail is the thing it was written to catch. It now reads the knob
# DECLARATION, which is identical in a test and in production.
def _is_ratio_knob(name):
    """A knob compared as `mcap * value`. Names ending _USD are genuinely dollar-denominated
    (the unknown-mcap source-buy floor is a real dollar amount) and are excluded by name, not by
    gate family — `source_buy_floor` covers both a ratio knob and a dollar one."""
    if name.endswith("_USD"):
        return False
    return "TURNOVER" in name or name.endswith("_MCAP_RATIO")


def test_no_ratio_knob_claims_dollars_or_percent():
    """THE GENERIC GUARD. These gates all compare `mcap * value`, so the value is a ratio.

    Declaring USD invites a market cap into a multiplier — which is exactly how
    MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND came to be labelled "Big-mcap boundary" with a 1e10
    maximum. Declaring % is a 100x misread of every live value.
    """
    offenders = []
    for name, knob in _knobs().items():
        if not _is_ratio_knob(name):
            continue
        unit = str(getattr(knob, "unit", "") or "")
        if "USD" in unit or "%" in unit:
            offenders.append((name, knob.label, unit))
    assert not offenders, "ratio knobs with a non-ratio unit: %s" % offenders


def test_ratio_knobs_do_not_claim_percent():
    """A value compared as `mcap * value` is a RATIO. Calling it a percent is a 100x lie."""
    ratio_knobs = [
        "MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5",
        "MEMECOIN_REAL_MIN_TURNOVER_M5",
        "MEMECOIN_REAL_MAX_TURNOVER_M5",
        "MEMECOIN_REAL_MIN_TURNOVER_H1",
        "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND",
        "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO",
    ]
    knobs = _knobs()
    bad = []
    for name in ratio_knobs:
        knob = knobs.get(name)
        if knob is None:
            continue
        if "%" in str(getattr(knob, "unit", "")):
            bad.append((name, knob.unit))
    assert not bad, "ratio knobs declaring a percent unit: %s" % bad


def test_the_over_band_floor_is_not_called_a_boundary():
    """It is the floor APPLIED above the band, not the band's edge."""
    knob = _knobs().get("MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND")
    assert knob is not None
    label = str(knob.label).lower()
    assert "boundary" not in label, knob.label
    assert "floor" in label, knob.label
    assert "USD" not in str(knob.unit), knob.unit


def test_a_ratio_knobs_maximum_is_not_a_dollar_range():
    """maximum=1e10 on a ratio invites a dollar value into a multiplier."""
    knob = _knobs().get("MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND")
    assert float(knob.maximum) <= 100, knob.maximum


def test_every_ratio_knob_note_shows_a_worked_example():
    """The unit alone is not enough — the note must make 0.02 unambiguous."""
    knobs = _knobs()
    for name in ("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5",
                 "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND"):
        note = str(getattr(knobs[name], "note", "") or "")
        assert re.search(r"0\.\d+\s*=\s*\d", note), (name, note)
