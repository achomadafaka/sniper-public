"""Depth in USD for a coin, computed from OUR OWN pool store when the provider has no pair.

WHY THIS EXISTS (2026-09-22, operator: "fix the pool store thing so liquidity isnt unknown").
Measured that day on chain 4663: 2,071 of 2,254 funded entry attempts since 09-15 carried
`liquidity_class = unknown`, and on 1,982 of them the market-data provider returned NO pair at all
— no address, no count, no number. Our own `pool_meta` already knew pools for 2,060 of those
attempts, and for 1,392 (70.2%) it knew them BEFORE the decision, a median 14 hours before. The
classifier never asked.

WHY IT IS A CACHE AND NOT A LIVE READ ON THE ENTRY PATH. A 4663 coin carries dozens of pools and
almost all are empty: reading the first eight of them found a LOWER total than reading the single
best one (median 0.07 of the provider's number against 0.27), because the deep pool is usually not
in the first few. Finding it reliably means reading them all, which is seconds of RPC inside a
decision that is measured in the first 60 s of a coin's life. So a refresher fills this table off
the hot path and the entry path does one indexed SELECT.

WHAT THE NUMBER IS, AND IS NOT. For a Uniswap V3/V4 position the quote-side reserve of a
full-range position is `L * sqrt(P)` in atomic units, so the pool's two-sided value in quote terms
is `2 * L * sqrt(P)`. That is the same shape as the TVL a provider reports, but it is NOT the same
number: measured against the provider on coins where both existed, ours read a median 0.27 of
theirs (range 0.00-0.65), because the provider sums venues we have not crawled and because
concentrated positions break the full-range assumption. **It is NEITHER a floor nor the provider's TVL**
(Codex recheck 2026-09-23, F6; an earlier version of this docstring called it a floor): on a
narrow concentrated position the virtual figure sits ORDERS OF MAGNITUDE ABOVE the actual in-range
value -- 2,000 against an actual 1.999001 -- while against a provider summing venues we have not
crawled it reads far below. It is a number about our own cached pool state and nothing else, and
that is why every row it produces carries `liquidity_source`.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

SOURCE = "pool_store"
# Uniswap V4 PoolManager storage: pools mapping at slot 6, slot0 at +0, in-range liquidity at +3.
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3
_Q96 = 2 ** 96


def quote_tvl_usd(liquidity, sqrt_price_x96, *, quote_is_token0, quote_decimals, quote_usd):
    """Two-sided pool value in USD from in-range liquidity and the current price, or None.

    `2 * L * sqrt(P)` in quote atomic units for a full-range position, converted with the quote
    unit's own USD price. Returns None on anything non-finite or non-positive rather than a zero:
    an unreadable pool is unknown depth, and a zero would read as a measured empty pool.

    WHAT THIS NUMBER IS (Codex review 2026-09-23, F6): 2 * L * sqrt(P) is the VIRTUAL reserve
    of the liquidity active at the current tick, the quantity the constant-product identity
    x*y = L^2 describes. It equals real inventory only for a full-range position. A position of
    L=1,000 concentrated in sqrt-price [0.999, 1.001] really holds about 2.0 units and reads
    2,000 here. It is a local-liquidity signal for filling a hole the provider left, measured at
    a median 0.27 of the provider's TVL on coins where both existed, and it must never be
    presented as TVL, as depth across a range, or as the sole certificate of a conversion price.
    """
    try:
        liquidity = float(liquidity)
        sqrt_price_x96 = float(sqrt_price_x96)
        decimals = int(quote_decimals)
        usd = float(quote_usd)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (liquidity, sqrt_price_x96, usd)):
        return None
    if liquidity <= 0 or sqrt_price_x96 <= 0 or usd <= 0 or not 0 <= decimals <= 36:
        return None
    sqrt_p = sqrt_price_x96 / _Q96
    if sqrt_p <= 0:
        return None
    # token0 side holds L / sqrt(P); token1 side holds L * sqrt(P).
    quote_atomic = (liquidity / sqrt_p) if quote_is_token0 else (liquidity * sqrt_p)
    value = 2.0 * quote_atomic / (10 ** decimals) * usd
    return value if math.isfinite(value) and value > 0 else None


def pool_state_slots(pool_id):
    """(slot0_key, liquidity_key) hex strings for one V4 pool id, for `extsload`."""
    from eth_utils import keccak

    raw = str(pool_id)
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise ValueError("a V4 pool id is 32 bytes")
    root = int.from_bytes(keccak(bytes.fromhex(raw) + POOLS_SLOT.to_bytes(32, "big")), "big")
    return (root.to_bytes(32, "big").hex(),
            (root + LIQUIDITY_OFFSET).to_bytes(32, "big").hex())


def depth_for_pools(pools, *, state_fn, price_fn):
    """Best (deepest) USD depth over `pools`, and the pool that produced it.

    `pools` are dicts with pool/token0/token1/quote/quote_decimals. `state_fn(pool)` returns
    (sqrt_price_x96, liquidity) or (None, None). Never raises: a pool that cannot be read is
    skipped, and a run that reads nothing returns (None, None).
    """
    best_usd, best_pool = None, None
    for row in pools or ():
        try:
            sqrt_price_x96, liquidity = state_fn(row)
        except Exception:
            logger.exception("pool depth: state read failed for %s", row.get("pool"))
            continue
        if not sqrt_price_x96 or not liquidity:
            continue
        try:
            usd = price_fn(row["quote"])
        except Exception:
            usd = None
        value = quote_tvl_usd(liquidity, sqrt_price_x96,
                              quote_is_token0=(str(row.get("token0", "")).lower()
                                               == str(row.get("quote", "")).lower()),
                              quote_decimals=row.get("quote_decimals"), quote_usd=usd)
        if value is not None and (best_usd is None or value > best_usd):
            best_usd, best_pool = value, row.get("pool")
    return best_usd, best_pool


def clip_impact_pct(clip_usd, *, liquidity, sqrt_price_x96, quote_is_token0,
                    quote_decimals, quote_usd):
    """How far OUR OWN clip moves this pool, in percent, or None when it cannot be computed.

    WHY (2026-09-23, operator: "build the impact gate"). The five quiet-hours refusals of that night
    split on DEPTH, not on the hour: ARENA took 315 swaps in eleven minutes and a $37.50 clip moved
    it 0.32%, CSTR took 8 and the same clip moved it 13.32% (35.53% at $100). A clock blocks both.
    This is the number that separates them, and it is computable before we sign.

    THE RATIO IS DECIMALS-FREE. Effective price and spot price both carry the same
    10^(mint_dec - quote_dec) factor, so it cancels and the mint's decimals are never needed. That
    matters: an uncorrected atomic-unit price on this chain read seventeen orders of magnitude from
    the truth, and only the quote side is needed here to size the clip.

    THE MODEL IS ONE TICK OF CONSTANT LIQUIDITY, AND THAT IS NEITHER A FLOOR NOR A CEILING
    (Codex review 2026-09-23, F2; an earlier docstring called it a floor). A clip that crosses
    into a THINNER initialized tick does worse than this number; a clip that crosses into a
    DEEPER one does better, and by a lot: with L=1,000 up to sqrt(P)=1.001 and L=100,000 beyond,
    a $100 clip reads 10.00% here against an exact 0.30% (tests/test_memecoin_pool_depth.py).
    The number is exact only while the whole clip stays inside the current tick, which these
    inputs cannot prove (no tick bitmap is read). Consumers must say "single-tick model", never
    "at least".
    """
    try:
        liquidity = float(liquidity)
        sqrt_p = float(sqrt_price_x96) / _Q96
        decimals = int(quote_decimals)
        usd = float(quote_usd)
        clip = float(clip_usd)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (liquidity, sqrt_p, usd, clip)):
        return None
    if liquidity <= 0 or sqrt_p <= 0 or usd <= 0 or clip <= 0 or not 0 <= decimals <= 36:
        return None
    quote_in = (clip / usd) * (10 ** decimals)
    d = quote_in / liquidity
    # ALGEBRAICALLY STABLE FORMS. The direct expressions subtract two nearly equal square roots,
    # and on a deep pool the difference is entirely rounding error: the function then returns None
    # and the gate reads "cannot compute" for exactly the pools that are safest. Rearranged, both
    # branches are a product and a quotient, with no cancellation.
    if quote_is_token0:                       # quote is x: adding x lowers sqrt(P)
        #   L*(sqrt_p - 1/(1/sqrt_p + d)) == L * sqrt_p^2 * d / (1 + sqrt_p*d)
        denom = 1.0 + sqrt_p * d
        if denom <= 0:
            return None
        tokens_out = liquidity * (sqrt_p ** 2) * d / denom
        spot_atomic = 1.0 / (sqrt_p ** 2)     # quote per mint, atomic
    else:                                     # quote is y: adding y raises sqrt(P)
        #   L*(1/sqrt_p - 1/(sqrt_p + d)) == L * d / (sqrt_p * (sqrt_p + d))
        denom = sqrt_p * (sqrt_p + d)
        if denom <= 0:
            return None
        tokens_out = liquidity * d / denom
        spot_atomic = sqrt_p ** 2
    if tokens_out <= 0 or not math.isfinite(tokens_out):
        return None
    effective_atomic = quote_in / tokens_out
    if spot_atomic <= 0 or not math.isfinite(effective_atomic):
        return None
    impact = 100.0 * (effective_atomic / spot_atomic - 1.0)
    return impact if math.isfinite(impact) and impact >= 0 else None


def impact_verdict(clip_usd, inputs, max_pct):
    """(allow, reason, impact_pct). FAIL-OPEN: an impact we cannot compute never blocks an entry.

    Blocking on an unknown would turn every unreadable pool into a refusal, which is a bigger change
    than the gate itself and the opposite of today's behaviour. `max_pct` of 0 or less disables it.
    """
    if not max_pct or float(max_pct) <= 0:
        return True, None, None
    if not inputs:
        return True, "impact_unknown:no_pool_state", None
    impact = clip_impact_pct(
        clip_usd, liquidity=inputs.get("liquidity"),
        sqrt_price_x96=inputs.get("sqrt_price_x96"),
        quote_is_token0=bool(inputs.get("quote_is_token0")),
        quote_decimals=inputs.get("quote_decimals"), quote_usd=inputs.get("quote_usd"))
    if impact is None:
        return True, "impact_unknown:not_computable", None
    if impact > float(max_pct):
        return (False,
                "clip impact %.2f%% over ceiling %.2f%% (single-tick model, not a bound)"
                % (impact, float(max_pct)), impact)
    return True, None, impact


def pool_state_row(row, sqrt_price_x96, liquidity, quote_usd):
    """The impact gate's inputs for ONE pool, or None when any of them is missing.

    Built here rather than in the refresher so the shape has one definition and a test. Returning
    None on an incomplete set is deliberate: a half-filled row would read as answerable and the gate
    would refuse an entry on a number it could not actually compute.
    """
    try:
        sqrt_price_x96 = int(sqrt_price_x96)
        liquidity = int(liquidity)
        decimals = int(row.get("quote_decimals"))
        usd = float(quote_usd)
    except (TypeError, ValueError):
        return None
    if sqrt_price_x96 <= 0 or liquidity <= 0 or usd <= 0 or not 0 <= decimals <= 36:
        return None
    return {"liquidity": liquidity, "sqrt_price_x96": sqrt_price_x96,
            "quote_decimals": decimals,
            "quote_is_token0": (str(row.get("token0", "")).lower()
                                == str(row.get("quote", "")).lower()),
            "quote_usd": usd}
