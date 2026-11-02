"""BSC flap.sh coins pair with tokenized stocks and other tokens, not WBNB (2026-09-11).

0x7e15…7777 refused `no_route` against a $49,647 PancakeSwap V2 pool quoted in SKHYB; all 9
BSC no-route coins in 14 days had a Pancake pool. The V2 path search now also routes through the
coin's own pair token, read from its pair list. Proven read-only on BSC mainnet: 9 of 9 routable.
"""
from sniper.execution import evm_v2_exit_quote as V

ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
COIN = "0x7e15837daf327b65f571a008b2d5dd6c5e407777"
SKHYB = "0xca750ef65f295bbecd685abf54e82caf297bdb61"
TAKER = "0x" + "11" * 20


def _abi_amounts(amounts):
    words = [32, len(amounts)] + list(amounts)
    return "0x" + "".join("%064x" % w for w in words)


def _chain(live_paths):
    """A fake router: answers getAmountsOut only for the listed paths, reverts for the rest."""
    def call_fn(to, data):
        for path in live_paths:
            for amount_in in (10 ** 17, 10 ** 20):
                if data == V._encode_get_amounts_out(amount_in, path):
                    return _abi_amounts([amount_in] + [amount_in * 2] * (len(path) - 1))
        raise V.V2Reverted("no such pair")
    return call_fn


def _quote(sell, buy, amount, live_paths, pair_tokens_fn):
    return V.dex_v2_exit_quote(
        "56", sell, buy, amount, TAKER, cfg_router=ROUTER, connector_tokens=(WBNB, USDT),
        slippage_bps=1500, tax_buffer_bps=0, call_fn=_chain(live_paths),
        now_fn=lambda: 1_789_000_000, pair_tokens_fn=pair_tokens_fn)


def test_the_exit_routes_through_the_coins_own_pair_token():
    path = (COIN, SKHYB, WBNB)
    assert _quote(COIN, WBNB, 10 ** 20, [path], lambda c, m: ()) is None, "old search: no route"
    q = _quote(COIN, WBNB, 10 ** 20, [path], lambda c, m: (SKHYB,))
    assert tuple(q["route"]["path"]) == path


def test_the_entry_routes_the_same_way_in_reverse():
    path = (WBNB, SKHYB, COIN)
    q = _quote(WBNB, COIN, 10 ** 17, [path], lambda c, m: (SKHYB,))
    assert tuple(q["route"]["path"]) == path


def test_a_pair_token_reached_only_through_usdt_gets_a_four_token_path():
    path = (COIN, SKHYB, USDT, WBNB)
    q = _quote(COIN, WBNB, 10 ** 20, [path], lambda c, m: (SKHYB,))
    assert tuple(q["route"]["path"]) == path


def test_pair_lookup_ranks_by_liquidity_takes_either_side_and_caches():
    """龙虾 0xf72c2c31 (d3d7ed87, 2026-09-22): DexScreener listed its only pair with the coin on the
    QUOTE side (oANTHROPIC / 龙虾) and the lookup dropped it. The counter-token is whichever side
    is not the coin; a pair that does not involve the coin at all is still ignored."""
    calls = []
    OTHER = "0x" + "9" * 40

    def fetch(chain, mint):
        calls.append(mint)
        return [
            {"baseToken": {"address": COIN}, "quoteToken": {"address": USDT}, "liquidity": {"usd": 10}},
            {"baseToken": {"address": COIN}, "quoteToken": {"address": SKHYB}, "liquidity": {"usd": 49647}},
            {"baseToken": {"address": OTHER}, "quoteToken": {"address": COIN}, "liquidity": {"usd": 1e9}},
            {"baseToken": {"address": OTHER}, "quoteToken": {"address": USDT}, "liquidity": {"usd": 1e12}},
        ]
    V._PAIR_CACHE.clear()
    clock = [100.0]
    assert V.pair_quote_tokens("56", COIN, fetch=fetch, clock=lambda: clock[0]) == (OTHER, SKHYB)
    assert V.pair_quote_tokens("56", COIN, fetch=fetch, clock=lambda: clock[0]) == (OTHER, SKHYB)
    assert len(calls) == 1, "cached"
    clock[0] += V._PAIR_TTL_S + 1
    V.pair_quote_tokens("56", COIN, fetch=fetch, clock=lambda: clock[0])
    assert len(calls) == 2, "expires"


def test_a_failed_pair_lookup_adds_no_paths():
    V._PAIR_CACHE.clear()

    def broken(chain, mint):
        raise RuntimeError("provider down")
    assert V.pair_quote_tokens("56", COIN, fetch=broken, clock=lambda: 1.0) == ()
    assert _quote(COIN, WBNB, 10 ** 20, [(COIN, SKHYB, WBNB)],
                  lambda c, m: V.pair_quote_tokens(c, m, fetch=broken, clock=lambda: 1.0)) is None
