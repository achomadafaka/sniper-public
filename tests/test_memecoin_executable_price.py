"""Real executable exit price — the number the funded rails should decide on.

The critical property is the LAST test: an unquotable bag must return None and never fall back to
mid. A coin whose chart shows a price but whose bag cannot be sold is exactly the rug the current
rails miss (catastrophe only fires while quotable).
"""
from sniper.services.memecoin_executable_price import executable_exit_price_usd, v2_amount_out


def test_v2_constant_product_is_exact_and_charges_the_fee():
    # r_in = 1000, r_out = 1000, in = 100 -> 100*0.997=99 (int) -> 1000*99/(1000+99) = 90
    assert v2_amount_out(1000, 1000, 100) == (1000 * 99) // (1000 + 99)
    # a bigger bag eats more depth: price per unit must be strictly worse
    small = v2_amount_out(10**18, 10**18, 10**15) / 10**15
    big = v2_amount_out(10**18, 10**18, 10**17) / 10**17
    assert big < small


def test_v2_guards():
    assert v2_amount_out(0, 1000, 100) is None
    assert v2_amount_out(1000, 0, 100) is None
    assert v2_amount_out(1000, 1000, 0) is None
    assert v2_amount_out("x", 1000, 100) is None


def test_v3_quote_converts_to_usd_per_token():
    # sell 1000 tokens (9dp); quoter returns 1 WETH (18dp); WETH = $3000 -> $3.00 per token
    q = lambda bag: {"buyAmount": str(10**18)}          # noqa: E731 — injected, no network
    px = executable_exit_price_usd("4663", "0xbase", 1000 * 10**9, base_decimals=9,
                                   quote_decimals=18, quote_usd=3000.0, quote_fn=q)
    assert abs(px - 3.0) < 1e-9


def test_bigger_bag_prices_worse_through_the_v2_path():
    """Depth is priced, not assumed: the same pool must quote a large bag at a worse unit price."""
    reserves = (10**18, 10**18)     # (base, quote)
    small = executable_exit_price_usd("4663", "0xbase", 10**14, base_decimals=18,
                                      quote_decimals=18, quote_usd=1.0, v2_reserves=reserves)
    large = executable_exit_price_usd("4663", "0xbase", 10**17, base_decimals=18,
                                      quote_decimals=18, quote_usd=1.0, v2_reserves=reserves)
    assert small > large > 0


def test_unquotable_returns_none_and_never_falls_back_to_a_chart_price():
    """THE load-bearing test. A bag that cannot be sold has NO executable value. Returning mid here
    would recreate the exact alias that made 1.1M marks lie."""
    for bad in ({"buyAmount": "0"}, {}, None, "not-a-dict"):
        assert executable_exit_price_usd("4663", "0xbase", 1000 * 10**9, base_decimals=9,
                                         quote_decimals=18, quote_usd=3000.0,
                                         quote_fn=lambda bag, b=bad: b) is None
    # a raising quoter is also unquotable, not an exception escaping into the exit sweep
    def boom(bag):
        raise RuntimeError("rpc down")
    assert executable_exit_price_usd("4663", "0xbase", 1000 * 10**9, base_decimals=9,
                                     quote_decimals=18, quote_usd=3000.0, quote_fn=boom) is None


def test_missing_inputs_fail_closed():
    q = lambda bag: {"buyAmount": str(10**18)}          # noqa: E731
    assert executable_exit_price_usd("4663", "0xb", 0, base_decimals=9, quote_decimals=18,
                                     quote_usd=3000.0, quote_fn=q) is None
    assert executable_exit_price_usd("4663", "0xb", 10**9, base_decimals=9, quote_decimals=18,
                                     quote_usd=0, quote_fn=q) is None
    assert executable_exit_price_usd("4663", "0xb", 10**9, base_decimals=None, quote_decimals=18,
                                     quote_usd=3000.0, quote_fn=q) is None
