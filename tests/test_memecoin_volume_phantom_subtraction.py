"""Never subtract the source buy from a provider number that cannot yet contain it.

PERSON (4663, 2026-09-16 11:57:28.521Z) is the case: DexScreener had indexed 1 swap of the 7 in
that 5-minute window, the trader's own buy landed 0.5 s before our decision, and we subtracted
$1,988 from $2,394.60 to get "$407 organic". The subtraction did not cause that refusal — the
floor was $2,677 and the raw number was already under it — but it is arithmetic on a quantity that
provably was not there, and on 8 of 278 4663 refusals it IS what refused them. Three of the three
gradeable ones ran to +25% or better.
"""
import pytest

import sniper.services.memecoin_real_runner as R


def test_a_buy_younger_than_the_index_lag_is_never_subtracted():
    """PERSON-shaped: the provider cannot contain a buy that landed 0.5 s ago."""
    assert R.organic_volume(2394.60, 1988.0, source_age_s=0.5, index_lag_s=25.0) == 2394.60


def test_a_buy_older_than_the_lag_is_still_subtracted():
    assert R.organic_volume(2394.60, 1988.0, source_age_s=40.0, index_lag_s=25.0) == pytest.approx(406.60)


def test_a_source_timestamp_after_our_decision_is_unindexable():
    """A third of 4663 rows carry a NEGATIVE age: the feed clock says the buy happened after we
    decided. That cannot be in the provider's window either."""
    assert R.organic_volume(2394.60, 1988.0, source_age_s=-12.0, index_lag_s=25.0) == 2394.60


def test_an_unknown_age_keeps_todays_behaviour():
    """The lag rule is an exception and needs a measured age to fire; without one, nothing changes."""
    assert R.organic_volume(2394.60, 1988.0, source_age_s=None, index_lag_s=25.0) == pytest.approx(406.60)


def test_the_pure_function_still_has_to_be_told_a_lag():
    """The evaluator itself defaults to no lag; the CALLER supplies the measured one.

    Until 2026-09-23 the caller supplied 0 on every chain, which is what made the guard dead. That
    default now lives in `cfg.provider_index_lag_s`, not here, so these two lines keep asserting the
    pure arithmetic: told a lag of 0, subtract; told nothing, subtract.
    """
    assert R.organic_volume(2394.60, 1988.0, source_age_s=0.5, index_lag_s=0.0) == pytest.approx(406.60)
    assert R.organic_volume(2394.60, 1988.0) == pytest.approx(406.60)


def test_the_older_guard_still_holds_below_the_buy():
    """A volume below the buy provably excludes it (2026-09-12), whatever the age says."""
    assert R.organic_volume(64.25, 994.44, source_age_s=900.0, index_lag_s=25.0) == 64.25


def test_none_volume_stays_none():
    assert R.organic_volume(None, 994.0, source_age_s=0.1, index_lag_s=25.0) is None


def test_the_decision_reports_whether_it_subtracted_and_why():
    """Provenance: 278 refusals carried no record of this, which is why it was invisible."""
    kept = R.subtraction_provenance(2394.60, 1988.0, source_age_s=0.5, index_lag_s=25.0)
    assert kept["subtraction_applied"] is False
    assert kept["reason"] == "source_buy_younger_than_provider_index_lag"
    assert kept["source_age_s"] == 0.5 and kept["index_lag_s"] == 25.0
    applied = R.subtraction_provenance(2394.60, 1988.0, source_age_s=40.0, index_lag_s=25.0)
    assert applied["subtraction_applied"] is True and applied["reason"] == "applied"
    below = R.subtraction_provenance(64.25, 994.44, source_age_s=900.0, index_lag_s=25.0)
    assert below["subtraction_applied"] is False
    assert below["reason"] == "volume_below_source_buy"


def test_the_gate_decision_carries_the_age_into_both_legs():
    """m5 and h1 both judge organic volume, so both must honour the lag."""
    allow, _need, why = R.volume_alive_decision(
        5000.0, 100000.0, abs_floor=3000.0, turnover_floor=0.01, vol_m5=2394.60,
        m5_floor=0.02, signal_usd=1988.0, source_age_s=0.5, index_lag_s=25.0)
    assert why != "m5_turnover_under_floor" or allow


def test_a_source_older_than_the_lag_in_the_negative_direction_is_still_unindexable():
    """A buy stamped 40 s AFTER our decision is not 40 s old — it is unindexable. Taking the
    absolute value would subtract exactly where the provider cannot have the buy."""
    assert R.organic_volume(2394.60, 1988.0, source_age_s=-40.0, index_lag_s=25.0) == 2394.60


def test_the_flat_hourly_floor_honours_the_lag_too():
    """The abs_floor leg subtracted the source buy itself, so fixing only the ratio legs would
    have left it crediting the provider with a buy it had not indexed."""
    young = R.volume_alive_decision(3200.0, None, abs_floor=3000.0, turnover_floor=0.0,
                                    signal_usd=1988.0, source_age_s=0.5, index_lag_s=25.0)
    assert young[0] is True, "3200 of unindexed-adjusted volume clears a 3000 floor"
    old = R.volume_alive_decision(3200.0, None, abs_floor=3000.0, turnover_floor=0.0,
                                  signal_usd=1988.0, source_age_s=600.0, index_lag_s=25.0)
    assert old[0] is False and old[2] == "under_abs_floor"


def test_the_lag_knob_defaults_to_the_measured_value_and_reads_per_chain(monkeypatch):
    """Unset means the measured default. Explicit 0 means the operator turned the guard off.

    These four assertions used to state the opposite, and stating it is what kept the defect alive
    for a week: the guard shipped on 2026-09-16 and never fired, because it needs a positive lag and
    `MEMECOIN_PROVIDER_INDEX_LAG_S_<chain>` is set for no chain in .env.
    """
    from sniper.settings import memecoin as cfg
    for chain in ("4663", "solana", "8453", "56", "5042"):
        monkeypatch.delenv("MEMECOIN_PROVIDER_INDEX_LAG_S_%s" % chain, raising=False)
        assert cfg.provider_index_lag_s(chain) == 15.0, "no chain may fall back to an inert guard"
    monkeypatch.setenv("MEMECOIN_PROVIDER_INDEX_LAG_S_4663", "25")
    assert cfg.provider_index_lag_s("4663") == 25.0
    assert cfg.provider_index_lag_s("solana") == 15.0, "the override is per chain, not global"
    monkeypatch.setenv("MEMECOIN_PROVIDER_INDEX_LAG_S_4663", "0")
    assert cfg.provider_index_lag_s("4663") == 0.0, "an explicit 0 is the operator's off switch"
    monkeypatch.setenv("MEMECOIN_PROVIDER_INDEX_LAG_S_4663", "not-a-number")
    assert cfg.provider_index_lag_s("4663") == 15.0, "unreadable is unknown, and unknown is not unguarded"


# THE THREE REFUSALS THE DEFAULT ALONE DECIDED, replayed from their own recorded gate inputs.
#
# Each raw provider volume ALREADY CLEARS its floor. What refused them was subtracting the copied
# trader's own buy out of a DexScreener window read a fraction of a second after that buy — a window
# that measurably cannot hold it (index lag p50 25 s on 4663, ~15 s on the fastest Solana venue,
# plus 0-30 s of CDN cache on top).
#
# ⚠️ THE FIXTURE VALUE THAT MAKES THE DEFECT REACHABLE IS THE AGE, and it is load-bearing twice
# over. The age must sit BELOW the default (all three are sub-second against 15 s) AND the raw
# volume must sit ABOVE the floor. Move the raw volume under the floor and the case is refused
# either way, so the test passes against the broken code and proves nothing — which is exactly how
# the block-hash regression test passed against its own defect on 2026-09-23.
_SUBTRACTION_DECIDED = [
    # chain,     ticker,     raw volume, the buy,     floor,   age at the gate
    ("4663",     "AERO",      3450.14,    894.264992, 3000.0,  0.15),
    ("solana",   "MERT",      4273.04,   1294.7305,   3000.0,  0.23),
    ("solana",   "BREADCAT",  3028.99,    808.546454, 3000.0,  0.26),
]


@pytest.mark.parametrize("chain,ticker,raw,buy,floor,age", _SUBTRACTION_DECIDED)
def test_the_configured_default_alone_admits_a_coin_over_its_own_floor(
        monkeypatch, chain, ticker, raw, buy, floor, age):
    """End to end from the config default into the decision, with no env set.

    This is the wiring the guard was missing: `volume_alive_decision` honours a lag it is GIVEN,
    and every caller was giving it 0. The test therefore reads the lag the way the runner does
    rather than passing a literal.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_PROVIDER_INDEX_LAG_S_%s" % chain, raising=False)
    lag = cfg.provider_index_lag_s(chain)
    assert raw >= floor, "the fixture only reaches the defect while the RAW volume clears the floor"
    assert age < lag, "and while the buy is younger than the lag it is subtracted from"

    allow, need, why = R.volume_alive_decision(
        raw, None, abs_floor=floor, turnover_floor=0.0, signal_usd=buy,
        source_age_s=age, index_lag_s=lag)
    assert allow is True, "%s: $%.2f of provider volume clears a $%.0f floor" % (ticker, raw, floor)
    assert why != "under_abs_floor"

    # ... and the same coin read LATE, once the provider has had time to index the buy, still pays
    # the subtraction. The guard is an exception for unindexable reads, not a repeal.
    late_allow, _need, late_why = R.volume_alive_decision(
        raw, None, abs_floor=floor, turnover_floor=0.0, signal_usd=buy,
        source_age_s=600.0, index_lag_s=lag)
    assert late_allow is False and late_why == "under_abs_floor"


def test_a_read_older_than_the_default_still_subtracts_on_every_chain(monkeypatch):
    """10 of the 40 subtraction-decided refusals had an age above 15 s and stay refused.

    Named so nobody reads the default as "the subtraction is gone".
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_PROVIDER_INDEX_LAG_S_4663", raising=False)
    lag = cfg.provider_index_lag_s("4663")
    assert R.organic_volume(3450.14, 894.264992, source_age_s=35.38, index_lag_s=lag) \
        == pytest.approx(2555.875008)
