"""Classify a fomo swap into buy / sell / route using the canonical cash-leg set.

fomo canonicalizes the USD leg to the Solana USDC mint on every chain (verified
2026-07-06 across Solana/Base/BSC/Ethereum), so direction detection is
chain-agnostic. USD amounts always come from humanUsdAmount* fields.
"""
from __future__ import annotations

CASH_TOKENS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC — canonical cash leg (all chains)
    "So11111111111111111111111111111111111111112",   # WSOL
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    # EVM cash/base legs (2026-07-10): traders route through these constantly; without them a
    # memecoin->WETH swap read as "buy of WETH" and we copied WETH/cbBTC as if they were
    # memecoins (paper positions 344/609/611, garbage entry prices). Lowercase — compare via
    # norm_cash_mint. These double as the NON-copyable denylist for the feed path.
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",     # WETH (Ethereum L1)
    "0x4200000000000000000000000000000000000006",     # WETH (Base / OP-stack)
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",     # WBTC (L1)
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",     # cbBTC (Base)
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",     # WBNB (BSC)
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",     # USDC (L1)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",     # USDC (Base)
    "0xdac17f958d2ee523a2206206994597c13d831ec7",     # USDT (L1)
}


def norm_cash_mint(a):
    """EVM addresses are case-insensitive (feed may checksum them); Solana is case-sensitive."""
    return a.lower() if isinstance(a, str) and a.startswith("0x") else a


def is_cash_asset(mint):
    """True for cash/base-leg assets (USDC/USDT/WSOL/WETH/WBTC/cbBTC/WBNB…) — routing legs,
    never copyable 'memecoin buys'. Shared by the swap classifier, the paper copy engine, and
    the real entry path."""
    return norm_cash_mint(mint) in CASH_TOKENS


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def classify_swap(swap):
    """Return (kind, mint, usd, token_qty).

    buy:  cash in, memecoin out  -> mint=outToken, usd=humanUsdAmountIn, qty=outHumanAmount
    sell: memecoin in, cash out  -> mint=inToken,  usd=humanUsdAmountOut, qty=inHumanAmount
    cash_route / token_route:    -> mint=None, usd=max(in,out), qty=0.0
    """
    inm = swap.get("inTokenAddress")
    outm = swap.get("outTokenAddress")
    in_cash = is_cash_asset(inm)
    out_cash = is_cash_asset(outm)
    usd_in = _f(swap.get("humanUsdAmountIn"))
    usd_out = _f(swap.get("humanUsdAmountOut"))
    if in_cash and out_cash:
        return ("cash_route", None, max(usd_in, usd_out), 0.0)
    if in_cash and not out_cash:
        return ("buy", outm, usd_in, _f(swap.get("outHumanAmount")))
    if out_cash and not in_cash:
        return ("sell", inm, usd_out, _f(swap.get("inHumanAmount")))
    return ("token_route", None, max(usd_in, usd_out), 0.0)
