"""Integer-only self-impact and immediate-exit quote math."""

from __future__ import annotations

from dataclasses import dataclass


CONSTANT_PRODUCT_QUOTE_VERSION = "constant-product-v2"


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def quote_atomic_for_usd(
    clip_usd_micros: int,
    quote_usd_price_micros: int,
    quote_decimals: int,
) -> int:
    if clip_usd_micros <= 0 or quote_usd_price_micros <= 0:
        raise ValueError("clip and quote price must be positive")
    if not 0 <= quote_decimals <= 30:
        raise ValueError("quote_decimals must be between 0 and 30")
    return clip_usd_micros * (10**quote_decimals) // quote_usd_price_micros


@dataclass(frozen=True)
class ClipQuote:
    quote_in_atomic: int
    tokens_out_atomic: int
    immediate_exit_quote_atomic: int
    entry_fee_atomic: int
    exit_fee_atomic: int
    network_cost_quote_atomic: int
    entry_price_impact_bps: int
    round_trip_return_bps: int | None
    entry_virtual_token_reserves: int
    entry_virtual_quote_reserves: int
    executable: bool
    blocker: str | None = None


@dataclass(frozen=True)
class ExitQuote:
    acquired_token_atomic: int
    gross_quote_out_atomic: int
    exit_fee_atomic: int
    network_cost_quote_atomic: int
    net_quote_out_atomic: int
    executable: bool
    blocker: str | None = None


def constant_product_exit_quote(
    *,
    current_virtual_token_reserves: int,
    current_virtual_quote_reserves: int,
    acquired_token_atomic: int,
    hypothetical_quote_in_atomic: int,
    fee_bps: int,
    network_cost_quote_atomic: int = 0,
) -> ExitQuote:
    """Sell the modeled entry quantity against current reserves plus our entry impact.

    Later observed trades are held fixed. The hypothetical entry therefore stays as an
    additive reserve overlay: acquired tokens remain removed and net entry quote remains
    on the curve. This is the v0 non-interference replay assumption.
    """
    values = (
        current_virtual_token_reserves,
        current_virtual_quote_reserves,
        acquired_token_atomic,
        hypothetical_quote_in_atomic,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("reserves, quantity, and quote input must be integers")
    cost = max(0, int(network_cost_quote_atomic))
    if min(values) <= 0 or not 0 <= fee_bps < 10_000:
        return ExitQuote(max(0, acquired_token_atomic), 0, 0, cost, 0, False, "UNSUPPORTED_CURVE_MATH")
    entry_fee = _ceil_div(hypothetical_quote_in_atomic * fee_bps, 10_000)
    entry_net = hypothetical_quote_in_atomic - entry_fee
    token_reserves = current_virtual_token_reserves - acquired_token_atomic
    quote_reserves = current_virtual_quote_reserves + entry_net
    if token_reserves <= 0 or quote_reserves <= 0:
        return ExitQuote(acquired_token_atomic, 0, 0, cost, 0, False, "INSUFFICIENT_EXECUTABLE_CAPACITY")
    # Keep our hypothetical entry in the later observed world. The overlay has
    # its own invariant; reusing the pre-entry invariant algebraically cancels
    # every observed price move and pins all exits to the fee drag.
    invariant = token_reserves * quote_reserves
    post_sell_token = token_reserves + acquired_token_atomic
    quote_after_sell = _ceil_div(invariant, post_sell_token)
    gross = max(0, quote_reserves - quote_after_sell)
    exit_fee = _ceil_div(gross * fee_bps, 10_000)
    net = max(0, gross - exit_fee)
    if net <= cost:
        return ExitQuote(acquired_token_atomic, gross, exit_fee, cost, 0, False, "INSUFFICIENT_EXECUTABLE_CAPACITY")
    return ExitQuote(acquired_token_atomic, gross, exit_fee, cost, net, True)


def constant_product_clip_quote(
    *,
    virtual_token_reserves: int,
    virtual_quote_reserves: int,
    quote_in_atomic: int,
    fee_bps: int,
    network_cost_quote_atomic: int = 0,
    max_impact_bps: int = 5_000,
) -> ClipQuote:
    values = (virtual_token_reserves, virtual_quote_reserves, quote_in_atomic)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("reserves and quote input must be integers")
    if min(values) <= 0 or not 0 <= fee_bps < 10_000:
        return ClipQuote(
            quote_in_atomic=max(0, quote_in_atomic),
            tokens_out_atomic=0,
            immediate_exit_quote_atomic=0,
            entry_fee_atomic=0,
            exit_fee_atomic=0,
            network_cost_quote_atomic=max(0, network_cost_quote_atomic),
            entry_price_impact_bps=10_000,
            round_trip_return_bps=None,
            entry_virtual_token_reserves=max(0, virtual_token_reserves),
            entry_virtual_quote_reserves=max(0, virtual_quote_reserves),
            executable=False,
            blocker="UNSUPPORTED_CURVE_MATH",
        )
    entry_fee = _ceil_div(quote_in_atomic * fee_bps, 10_000)
    net_quote = quote_in_atomic - entry_fee
    if net_quote <= 0:
        return ClipQuote(
            quote_in_atomic, 0, 0, entry_fee, 0, network_cost_quote_atomic,
            10_000, None, virtual_token_reserves, virtual_quote_reserves,
            False, "INSUFFICIENT_EXECUTABLE_CAPACITY",
        )
    invariant = virtual_token_reserves * virtual_quote_reserves
    post_quote = virtual_quote_reserves + net_quote
    post_token = _ceil_div(invariant, post_quote)
    tokens_out = virtual_token_reserves - post_token
    ideal_out = net_quote * virtual_token_reserves // virtual_quote_reserves
    impact_bps = 10_000 if ideal_out <= 0 else max(0, 10_000 - tokens_out * 10_000 // ideal_out)
    if tokens_out <= 0 or impact_bps > max_impact_bps:
        return ClipQuote(
            quote_in_atomic, max(0, tokens_out), 0, entry_fee, 0,
            network_cost_quote_atomic, impact_bps, None, post_token, post_quote,
            False, "INSUFFICIENT_EXECUTABLE_CAPACITY",
        )
    post_sell_token = post_token + tokens_out
    quote_after_sell = _ceil_div(invariant, post_sell_token)
    gross_exit = max(0, post_quote - quote_after_sell)
    exit_fee = _ceil_div(gross_exit * fee_bps, 10_000)
    net_exit = max(0, gross_exit - exit_fee)
    net_after_cost = net_exit - max(0, network_cost_quote_atomic)
    return_bps = (net_after_cost - quote_in_atomic) * 10_000 // quote_in_atomic
    return ClipQuote(
        quote_in_atomic=quote_in_atomic,
        tokens_out_atomic=tokens_out,
        immediate_exit_quote_atomic=net_exit,
        entry_fee_atomic=entry_fee,
        exit_fee_atomic=exit_fee,
        network_cost_quote_atomic=max(0, network_cost_quote_atomic),
        entry_price_impact_bps=impact_bps,
        round_trip_return_bps=return_bps,
        entry_virtual_token_reserves=post_token,
        entry_virtual_quote_reserves=post_quote,
        executable=True,
    )
