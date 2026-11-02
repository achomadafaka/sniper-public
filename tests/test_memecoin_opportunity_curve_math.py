from sniper.research.memecoin_opportunity.curve_math import (
    ClipQuote,
    constant_product_clip_quote,
    constant_product_exit_quote,
    quote_atomic_for_usd,
)


def test_usd_clip_conversion_uses_integer_atomic_math():
    # $25 at $200 per quote token with 9 decimals = 0.125 quote tokens.
    assert quote_atomic_for_usd(25_000_000, 200_000_000, 9) == 125_000_000


def test_constant_product_quote_models_own_entry_then_immediate_exit():
    quote = constant_product_clip_quote(
        virtual_token_reserves=1_000_000_000,
        virtual_quote_reserves=1_000_000_000,
        quote_in_atomic=25_000_000,
        fee_bps=200,
        network_cost_quote_atomic=100_000,
    )
    assert isinstance(quote, ClipQuote)
    assert quote.tokens_out_atomic > 0
    assert quote.entry_virtual_quote_reserves == 1_024_500_000
    assert quote.entry_virtual_token_reserves < 1_000_000_000
    assert quote.immediate_exit_quote_atomic < 25_000_000
    assert quote.round_trip_return_bps < 0
    assert quote.executable is True


def test_invalid_or_exhausted_reserves_fail_closed():
    quote = constant_product_clip_quote(
        virtual_token_reserves=0,
        virtual_quote_reserves=1,
        quote_in_atomic=1,
        fee_bps=0,
    )
    assert quote.executable is False
    assert quote.blocker == "UNSUPPORTED_CURVE_MATH"


def test_large_clip_reports_capacity_failure_not_fake_fill():
    quote = constant_product_clip_quote(
        virtual_token_reserves=100,
        virtual_quote_reserves=100,
        quote_in_atomic=10_000_000,
        fee_bps=200,
        max_impact_bps=5_000,
    )
    assert quote.executable is False
    assert quote.blocker == "INSUFFICIENT_EXECUTABLE_CAPACITY"


def test_future_exit_carries_hypothetical_entry_impact_forward():
    entry = constant_product_clip_quote(
        virtual_token_reserves=1_000_000,
        virtual_quote_reserves=500_000,
        quote_in_atomic=25_000,
        fee_bps=200,
        network_cost_quote_atomic=100,
    )

    exit_quote = constant_product_exit_quote(
        current_virtual_token_reserves=1_000_000,
        current_virtual_quote_reserves=500_000,
        acquired_token_atomic=entry.tokens_out_atomic,
        hypothetical_quote_in_atomic=entry.quote_in_atomic,
        fee_bps=200,
        network_cost_quote_atomic=100,
    )

    assert exit_quote.executable is True
    assert abs(exit_quote.net_quote_out_atomic - entry.immediate_exit_quote_atomic) <= 1


def test_future_exit_changes_with_observed_pool_price_movement():
    entry = constant_product_clip_quote(
        virtual_token_reserves=1_000_000,
        virtual_quote_reserves=500_000,
        quote_in_atomic=25_000,
        fee_bps=200,
    )

    appreciated = constant_product_exit_quote(
        current_virtual_token_reserves=900_000,
        current_virtual_quote_reserves=560_000,
        acquired_token_atomic=entry.tokens_out_atomic,
        hypothetical_quote_in_atomic=entry.quote_in_atomic,
        fee_bps=200,
    )
    depreciated = constant_product_exit_quote(
        current_virtual_token_reserves=1_100_000,
        current_virtual_quote_reserves=460_000,
        acquired_token_atomic=entry.tokens_out_atomic,
        hypothetical_quote_in_atomic=entry.quote_in_atomic,
        fee_bps=200,
    )

    assert appreciated.executable is True
    assert depreciated.executable is True
    assert appreciated.net_quote_out_atomic > entry.immediate_exit_quote_atomic
    assert depreciated.net_quote_out_atomic < entry.immediate_exit_quote_atomic
