"""The absolute source-buy band, in addition to the market-cap ratio.

`MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO` is a FRACTION of market cap and runs backwards across
coin sizes: measured on armed-wallet buys since 2026-09-01 the configured 0.008 passes 97.6% at
nano (<$50k mcap, median buy $500) and refuses 84% at mid ($1-5M, median buy $4,996). It refuses
least where the coins are smallest. There was no absolute floor and no upper bound at all.

Both knobs default to 0 = OFF so the code lands inert.

WHAT THE CEILING IS: half of a replicated conjunction — turnover 10-50% with a $1k-$10k source buy,
n=44, +21.72%, 75% win, surviving both chains, both time halves, the removal of the best two days,
and the removal of Degen. It is NOT evidence that a large buy is independently bad: buys >= $2,500
lose -$453 over n=89, but that is n=10 +$1.45 under $250k mcap against n=79 -$454.64 at or above it.
Source size proxies for coin size.
"""
import pathlib

import pytest

from sniper.settings import memecoin as cfg


class _Ev:
    signal_source = "follow_activity"
    their_market_cap = None

    def __init__(self, size):
        self.their_size_usd = size
        self.mint = "0x" + "ab" * 20


def _band(their_usd, *, abs_min=0.0, abs_max=0.0):
    """THE GATE'S OWN FUNCTION, not a restatement of it.

    The first version of this file reimplemented the comparison here and passed, which proved only
    that the copy agreed with itself. Same trap as `singular == batch` after a shared refactor.
    """
    from sniper.services.memecoin_real_runner import source_buy_band_reason
    return source_buy_band_reason(their_usd, abs_min, abs_max)


# --- the band ---------------------------------------------------------------------------------

def test_off_by_default_refuses_nothing():
    for size in (1.0, 500.0, 50_000.0):
        assert _band(size) is None


def test_a_buy_under_the_floor_is_refused():
    assert "under absolute floor" in _band(500.0, abs_min=1000.0)


def test_a_buy_over_the_ceiling_is_refused():
    assert "over absolute ceiling" in _band(50_000.0, abs_max=10_000.0)


def test_the_conjunction_band_passes():
    for size in (1000.0, 4_000.0, 10_000.0):
        assert _band(size, abs_min=1000.0, abs_max=10_000.0) is None, size


def test_the_bounds_are_inclusive_at_the_edges():
    assert _band(1000.0, abs_min=1000.0) is None, "a buy exactly at the floor passes"
    assert _band(10_000.0, abs_max=10_000.0) is None, "a buy exactly at the ceiling passes"


def test_unknown_size_PASSES(caplog):
    """A field the source did not report says nothing about the trade."""
    assert _band(None, abs_min=1000.0, abs_max=10_000.0) is None


def test_only_one_bound_may_be_set():
    assert _band(50_000.0, abs_min=1000.0) is None, "no ceiling configured"
    assert _band(10.0, abs_max=10_000.0) is None, "no floor configured"


# --- the wiring -------------------------------------------------------------------------------

def test_both_settings_exist_and_default_to_off():
    for name in ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", "MEMECOIN_REAL_MAX_SOURCE_BUY_USD"):
        assert hasattr(cfg, name), name
    # the release default must be inert; a checkout that loaded .env may legitimately differ
    import os
    if not os.getenv("MEMECOIN_REAL_MIN_SOURCE_BUY_USD"):
        assert cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_USD == 0.0
    if not os.getenv("MEMECOIN_REAL_MAX_SOURCE_BUY_USD"):
        assert cfg.MEMECOIN_REAL_MAX_SOURCE_BUY_USD == 0.0


def test_the_gate_reads_the_source_reported_field_not_a_computed_one():
    """The conjunction was measured on `their_size_usd`. A gate reading anything else stops
    measuring what the evidence measured."""
    import inspect
    from sniper.services import memecoin_real_runner as R
    src = inspect.getsource(R.real_entry_hook)
    assert 'their_usd = getattr(ev, "their_size_usd", None)' in src


def test_the_band_runs_even_when_the_ratio_is_off():
    """The ratio guard used to be the only path that parsed the size at all."""
    import inspect
    from sniper.services import memecoin_real_runner as R
    src = inspect.getsource(R.real_entry_hook)
    hoisted = src.index('their_usd = getattr(ev, "their_size_usd", None)')
    guarded = src.index('ratio > 0 or unknown_mcap_floor > 0 or band_ratios')
    assert hoisted < guarded, "the size parse must be hoisted above the ratio guard"


def test_the_band_does_not_replace_the_ratio():
    """The ratio carries its own signal and stays wired; the operator sets the combination.

    The deferral used to be asserted by finding its text inside `real_entry_hook`. The family is
    now one pure function shared with the promotion shadow (2026-09-23 review, R4), so the text
    moved -- and a source scan that follows the text to its new home would still only prove where
    the code lives. This asserts the BEHAVIOUR instead: a size that breaks both the mcap ratio and
    the absolute band must report the ratio, because one entry yields one reason.
    """
    import inspect
    from sniper.services import memecoin_real_runner as R

    assert "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO" in inspect.getsource(R.real_entry_hook)
    reason, _info = R.source_buy_family_reason(
        their_usd=100.0, source_mcap=200_000.0, prior_net_usd=None, ratio=0.008,
        unknown_mcap_floor=0.0, abs_min=200.0, abs_max=0.0, family_active=True)
    assert reason is not None and "under mcap floor" in reason, reason
    assert "absolute floor" not in reason, "the band must defer to a ratio refusal"


def test_the_ceiling_note_does_not_claim_large_buys_are_bad():
    """The confound has to be stated where the next reader will see it."""
    import inspect
    from sniper.services import memecoin_real_runner as R
    src = inspect.getsource(R.real_entry_hook)
    assert "proxying for COIN size" in src or "proxies for coin size" in src


def test_both_are_operator_adjustable_and_bounded():
    from sniper.services import memecoin_entry_knobs as K
    for name in ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", "MEMECOIN_REAL_MAX_SOURCE_BUY_USD"):
        assert name in K.ADJUSTABLE
        knob = K.KNOBS[name]
        assert knob.gate_family == "source_buy_floor"
        assert knob.off_value == 0 and knob.minimum == 0
        with pytest.raises(ValueError):
            knob.coerce(-1)


# --- boundary tolerance on the mcap-ratio floor -------------------------------------------------
# 2026-09-24, operator. 0xdedrater bought $1,486.29 of 和平熊猫 at 0.797% against a 0.800% bar of
# $1,493 and was REFUSED BY $7. A hard floor with no tolerance at the boundary measures rounding,
# not conviction. The level itself is unchanged and stays unchanged: below 0.8% is the worst cell
# on almost every chain (4663 micro n=35 -$216 against 0.8-2% n=78 +$139; all chains 106 closes
# -$302), so this widens the boundary, never the rule.
DEDRATER_USD = 1486.2879420000002        # his real buy
DEDRATER_MCAP = 1493.0 / 0.008           # the mcap that puts the bar at his $1,493
SUNNYDOG_USD = 994.5717030000001         # the same coin, 78 seconds earlier
SUNNYDOG_MCAP = 169576.71427195147       # bar $1,356.61 -- 26.6% below, not a boundary case


def _family(their_usd, mcap, tolerance):
    from sniper.services.memecoin_real_runner import source_buy_family_reason
    return source_buy_family_reason(
        their_usd=their_usd, source_mcap=mcap, prior_net_usd=None, ratio=0.008,
        unknown_mcap_floor=0.0, abs_min=0.0, abs_max=0.0, family_active=True,
        floor_tolerance_pct=tolerance)


def test_the_seven_dollar_refusal_stands_at_zero_tolerance():
    """Zero must reproduce the old rule exactly, or the rollback is not a rollback."""
    reason, info = _family(DEDRATER_USD, DEDRATER_MCAP, 0.0)
    assert reason is not None and "under mcap floor" in reason
    assert info["floor_usd"] == pytest.approx(1493.0)
    assert info["effective_floor_usd"] == pytest.approx(1493.0), "0% moves nothing"


def test_two_percent_admits_the_seven_dollar_refusal():
    reason, info = _family(DEDRATER_USD, DEDRATER_MCAP, 2.0)
    assert reason is None, reason
    # The NOMINAL floor keeps being reported beside the effective one: an operator reading the
    # trace must still see the configured rule, not only the bar it was judged against.
    assert info["floor_usd"] == pytest.approx(1493.0)
    assert info["effective_floor_usd"] == pytest.approx(1493.0 * 0.98)
    assert info["floor_tolerance_pct"] == 2.0


def test_two_percent_does_NOT_admit_a_buy_that_misses_by_a_quarter():
    """The value is what makes this reachable: $994.57 against $1,356.61 is 26.6% short.

    Without this the suite would prove only that a tolerance admits things, never that it stays a
    BOUNDARY tolerance -- a bug that read the knob as percentage points of the ratio, or applied it
    to the ratio instead of the floor, would pass every test above and fail this one.
    """
    reason, info = _family(SUNNYDOG_USD, SUNNYDOG_MCAP, 2.0)
    assert reason is not None, "a quarter below the bar is not a rounding miss"
    assert "$995" in reason and "0.587%" in reason
    assert info["effective_floor_usd"] == pytest.approx(169576.71427195147 * 0.008 * 0.98)


def test_a_negative_tolerance_cannot_tighten_a_money_gate():
    """The one direction this knob must never move. A negative would refuse what 0 admits."""
    assert _family(DEDRATER_USD * 10, DEDRATER_MCAP, -5.0)[0] is None
    assert _family(DEDRATER_USD, DEDRATER_MCAP, -5.0)[0] == _family(
        DEDRATER_USD, DEDRATER_MCAP, 0.0)[0]
    assert _family(DEDRATER_USD, DEDRATER_MCAP, -5.0)[1]["floor_tolerance_pct"] == 0.0


def test_the_setting_clamps_both_ends_and_defaults_to_off(caplog):
    """A fat-fingered 200 would take 100% off the floor and switch the rule off entirely."""
    assert cfg.MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT == 0.0, "lands inert"
    assert cfg._source_buy_floor_tolerance_pct(-5) == 0.0
    assert cfg._source_buy_floor_tolerance_pct(2) == 2.0
    assert cfg._source_buy_floor_tolerance_pct("x") == 0.0
    assert cfg._source_buy_floor_tolerance_pct(float("nan")) == 0.0
    with caplog.at_level("WARNING", logger="memecoin"):
        assert cfg._source_buy_floor_tolerance_pct(200) == 50.0
    assert "ceiling" in caplog.text, "a clamped knob says so; a silent clamp is a lie"


def test_an_unknown_size_still_fails_the_ratio_floor_at_any_tolerance():
    """The asymmetry the docstring names, unchanged: unknown PASSES the band, FAILS the ratio."""
    for tol in (0.0, 2.0, 50.0):
        assert _family(None, DEDRATER_MCAP, tol)[0] is not None


def test_the_trace_carries_both_floors_so_an_admit_is_attributable():
    """Alpha's post-deploy proof reads `effective_floor_usd` off the trace, and the first version
    of this change did not put it there -- `mark_substage` forwards named kwargs only, so the two
    new `info` keys existed and never reached a row.

    A buy admitted BY THE TOLERANCE is otherwise indistinguishable from one that cleared the floor
    outright, which is exactly the question the deploy has to answer.
    """
    import sniper.services.memecoin_real_runner as rr

    seen = {}

    class _Trace:
        def mark_substage(self, name, **fields):
            seen[name] = fields

    _, info = _family(DEDRATER_USD, DEDRATER_MCAP, 2.0)
    assert info["effective_floor_usd"] == pytest.approx(1493.0 * 0.98)
    src = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
    call = src[src.index('trace.mark_substage(\n                    "source_buy_floor"'):]
    call = call[:call.index("rejected=bool(source_reason)")]
    assert "effective_floor_usd=" in call, "the effective floor never reaches the trace"
    assert "floor_tolerance_pct=" in call, "the tolerance never reaches the trace"
