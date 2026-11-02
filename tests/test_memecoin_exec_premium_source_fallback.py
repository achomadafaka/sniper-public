"""A missing chart price is judged against the trader's price, not waved through (2026-09-06).

The execution-premium gate returned `no_display_price` and FAILED OPEN whenever DexScreener carried
no price. Measured over 30 days that is 289 of 861 funded closes -- one in THREE, not the one in six
we had assumed -- for a net -$293.88, every one admitted unjudged. Of the 155 with a usable trader
price, that reference orders their returns cleanly: +7.52% below 1.15x (n=73), then -3.59%,
-22.56%, -5.90%. At the same cap it refuses 43 of them worth -$348.79.

No network.
"""
import pytest

from sniper.services import memecoin_market_state as MS

CAP = 1.5


@pytest.fixture(autouse=True)
def _cap_and_fallback(monkeypatch):
    monkeypatch.setattr(MS.cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", CAP, raising=False)
    monkeypatch.setattr(MS.cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK", True, raising=False)


def _classification(display=None):
    return {"facts": ({"display_price": display} if display is not None else {})}


# (a) chart missing, trader price present, over the cap -> refused, named against the source
def test_no_chart_price_over_the_cap_is_refused_against_the_trader_price():
    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=1.6, source_price=1.0)

    assert out.allow is False
    assert out.reference_basis == "source"
    assert out.reference_price == 1.0
    assert round(out.ratio, 3) == 1.6
    assert out.reason == "execution_premium_over_cap"


# (b) chart missing, under the cap -> admitted, and the basis says which price decided
def test_no_chart_price_under_the_cap_is_admitted_on_the_source_basis():
    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=1.2, source_price=1.0)

    assert out.allow is True
    assert out.reference_basis == "source"
    assert round(out.ratio, 3) == 1.2


# (c) chart present -> unchanged: chart is the reference, trader price is only a floor
def test_a_present_chart_price_still_decides_exactly_as_before():
    """The docstring's objection to a source reference holds where it was made; this changes only
    the case where there is no chart price at all."""
    out = MS.execution_premium_decision(
        _classification(display=1.0), exact_quote_price=1.2, source_price=0.5)

    assert out.allow is True                       # 1.2 / 1.0 = 1.2x, under the cap
    assert out.reference_basis == "chart"
    assert out.reference_price == 1.0

    # and the trader price is still the FLOOR when it is higher, exactly as today
    floored = MS.execution_premium_decision(
        _classification(display=0.5), exact_quote_price=1.2, source_price=1.0)
    assert floored.reference_basis == "source" and floored.reference_price == 1.0


def test_a_chart_price_that_would_refuse_is_not_rescued_by_the_fallback():
    out = MS.execution_premium_decision(
        _classification(display=1.0), exact_quote_price=1.6, source_price=1.0)

    assert out.allow is False and out.reference_basis == "chart"


# (d) neither price -> today's verdict, untouched
def test_neither_price_keeps_todays_fail_open_verdict():
    """Not widened and not narrowed: with no reference at all the chains keep what they do now."""
    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=1.6, source_price=None)

    assert out.allow is True
    assert out.reason == "no_display_price"
    assert out.reference_basis is None


def test_no_quote_price_is_still_unpriceable_not_a_source_judgement():
    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=None, source_price=1.0)

    assert out.allow is True and out.reason == "no_display_price"


# (e) the switch restores the old verdict exactly
def test_the_switch_off_restores_the_previous_fail_open(monkeypatch):
    monkeypatch.setattr(MS.cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOURCE_FALLBACK", False,
                        raising=False)

    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=1.6, source_price=1.0)

    assert out.allow is True
    assert out.reason == "no_display_price"
    assert out.reference_basis is None


def test_the_unit_fault_bound_still_refuses_a_broken_quote_on_the_source_basis():
    """A 2,487x 'premium' is a WETH/USD unit fault, not a route cost, and must stay separable."""
    out = MS.execution_premium_decision(
        _classification(), exact_quote_price=2487.26, source_price=0.0065)

    assert out.allow is False and out.reason == "quote_unit_fault"


# (f) the EVM leg consumes the verdict; Solana's copy was retired
def test_the_evm_leg_consumes_the_premium_verdict():
    """A fallback the EVM leg does not see is no gate at all.

    NARROWED 2026-09-22: this asserted BOTH legs. The Solana chart-mid gate is retired — it
    compared the route to a mid that lags 25-55 s, so it measured the coin's move and called it an
    execution premium (27 refusals since 09-15, two of them CHEAPER than the trader's own fill).
    On Solana, execution cost is MEMECOIN_ENTRY_IMPACT_CAP_PCT and the chase check owns the
    source-referenced question. EVM keeps this gate and this fallback.
    """
    import inspect
    from sniper.execution import evm_swap_executor, solana_swap_executor

    src = inspect.getsource(evm_swap_executor._execution_premium)
    assert "execution_premium_decision" in src
    assert "source_price" in src
    assert "reference_basis" in src                        # the trace can split by basis
    assert "reference_price" in src

    assert not hasattr(solana_swap_executor, "_execution_premium"), (
        "the retired Solana premium gate came back; execution cost there is the impact cap")
