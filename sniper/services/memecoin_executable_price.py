"""What a position's WHOLE bag would actually sell for. Free — no paid provider, no API key.

`memecoin_position_marks.executable_exit_value` has been an alias for `mid` on every one of 1.1M
rows, and the funded trail compares `mid` against the peak. So every exit ever taken was decided on
a chart price nobody can sell into. Measured 2026-07-24 on the 12 busiest 4663 pools: the median
coin returns ~8% LESS than the chart, and the worst returned $2.82 on a $300 chart-implied bag.
Two pools quoted slightly POSITIVE — so the gap is per-pool depth, not a constant fee. Only a real
quote reveals it.

Two routes, both free:
  * V3 -> `dex_v3_quote` (QuoterV2 `quoteExactInputSingle` over eth_call on the EXISTING ingest RPC;
    it scans fee tiers and returns the exact whole-bag amountOut).
  * V2 -> exact constant-product math from the `v2_sync` reserves already in the tape. Zero RPC.

FAIL-CLOSED, and this is the whole point: an unquotable bag returns None. It must NEVER fall back to
`mid` — a coin that cannot be sold while its chart still shows a price is precisely the rug the
current rails cannot see (catastrophe only fires while quotable).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memecoin")

_V2_FEE_BPS = 30          # Uniswap V2 standard 0.30%; V3 fees come from the quoted tier itself


def v2_amount_out(reserve_in_atomic, reserve_out_atomic, amount_in_atomic, *, fee_bps=_V2_FEE_BPS):
    """Exact Uniswap-V2 constant-product output. Pure integer math, no RPC, no approximation."""
    try:
        r_in, r_out, ain = int(reserve_in_atomic), int(reserve_out_atomic), int(amount_in_atomic)
    except (TypeError, ValueError):
        return None
    if r_in <= 0 or r_out <= 0 or ain <= 0:
        return None
    ain_after_fee = ain * (10_000 - int(fee_bps)) // 10_000
    if ain_after_fee <= 0:
        return None
    return (r_out * ain_after_fee) // (r_in + ain_after_fee)


def executable_exit_price_usd(chain_id, base_token, bag_atomic, *, base_decimals, quote_decimals,
                              quote_usd, quote_token=None, v2_reserves=None, quote_fn=None):
    """USD per BASE token that selling `bag_atomic` would actually realize, or None.

    `v2_reserves` = (reserve_base_atomic, reserve_quote_atomic) uses the free constant-product path.
    Otherwise the V3 quoter is used. `quote_fn` is injected for tests so no network is touched.
    """
    try:
        bag = int(bag_atomic)
        bd, qd = int(base_decimals), int(quote_decimals)
        q_usd = float(quote_usd)
    except (TypeError, ValueError):
        return None
    if bag <= 0 or q_usd <= 0:
        return None

    out_atomic = None
    if v2_reserves:
        out_atomic = v2_amount_out(v2_reserves[0], v2_reserves[1], bag)
    else:
        fn = quote_fn
        if fn is None:
            from sniper.execution.evm_dex_quote import dex_v3_quote
            from sniper.execution.evm_wallet import CHAIN_QUOTE
            quote_token = quote_token or (CHAIN_QUOTE.get(str(chain_id)) or {}).get("token")
            if not quote_token:
                return None

            def fn(_bag):
                # taker affects only the swap calldata, never the quoted amountOut
                return dex_v3_quote(str(chain_id), base_token, quote_token, _bag, "0x" + "0" * 40)
        try:
            q = fn(bag)
        except Exception:
            logger.exception("executable quote failed chain=%s token=%s", chain_id, base_token)
            return None
        if isinstance(q, dict):
            try:
                out_atomic = int(q.get("buyAmount") or 0)
            except (TypeError, ValueError):
                out_atomic = None

    if not out_atomic or out_atomic <= 0:
        return None                      # unquotable -> None. NEVER mid.
    proceeds_usd = (out_atomic / (10 ** qd)) * q_usd
    tokens = bag / (10 ** bd)
    if tokens <= 0:
        return None
    price = proceeds_usd / tokens
    return price if price > 0 else None
