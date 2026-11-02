"""Task 2 — atomic block VWAP -> USD. The one place units convert; fail-closed everywhere."""
from sniper.analysis.tape_price import tape_price_usd


def _swap(block_ts, quote_atomic, base_atomic):
    """quote_side=0, so amount0 is quote and amount1 is base."""
    return {"kind": "v2_swap", "block_ts": block_ts,
            "amount0": str(quote_atomic), "amount1": str(-base_atomic)}


def test_converts_atomic_ratio_to_usd():
    # 1 WETH (18dp) buys 1000 tokens (9dp); WETH = $3000 -> $3.00 per token
    swaps = [_swap(10, 10**18, 1000 * 10**9)]
    px = tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0)
    assert abs(px - 3.0) < 1e-9


def test_uses_the_latest_block_not_the_first():
    swaps = [_swap(10, 10**18, 1000 * 10**9),      # $3.00
             _swap(11, 10**18, 500 * 10**9)]       # $6.00 — newer block wins
    px = tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0)
    assert abs(px - 6.0) < 1e-9


def test_fail_closed_on_every_missing_input():
    swaps = [_swap(10, 10**18, 1000 * 10**9)]
    assert tape_price_usd(swaps, quote_side=0, base_decimals=None, quote_decimals=18, quote_usd=3000.0) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=None, quote_usd=3000.0) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=None) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=0) is None
    assert tape_price_usd([], quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0) is None
