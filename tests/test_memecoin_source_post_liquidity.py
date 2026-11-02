"""Receipt liquidity binds the native quote model to the final source-pool state."""
import copy

import pytest

from sniper.services import memecoin_evm_post_trade as post_decoder
from sniper.services.memecoin_source_fill import validate_source_fill
from test_memecoin_evm_post_trade import CASES, _keys, _owner, _swaps, _words
from test_memecoin_source_fill import proof, signal, source_post


@pytest.mark.parametrize("case_name", ["v4_hooked_usdg", "v4_hook_sells_after_buy"])
def test_receipt_preserves_the_last_same_pool_liquidity_with_its_sqrt(case_name):
    case = copy.deepcopy(CASES[case_name])
    initial = post_decoder.source_post_trade("4663", case["receipt"], case["mint"],
        owner=_owner(case), pool_key_fn=_keys)
    swaps = _swaps(case["receipt"], post_decoder.V4_SWAP, initial["pool"])
    final = swaps[-1][1]
    words = _words(final)
    words[3] = format(2**127 + 12345, "064x")
    final["data"] = "0x" + "".join(words)
    post = post_decoder.source_post_trade("4663", case["receipt"], case["mint"],
        owner=_owner(case), pool_key_fn=_keys)
    assert post["liquidity_atomic"] == "170141183460469231731687303715884118073"
    assert post["sqrt_price_x96"] == str(int(words[2], 16))


@pytest.mark.parametrize("value", ["1", str(2**128 - 1)])
def test_source_fill_retains_optional_canonical_uint128_liquidity(value):
    post = dict(source_post(), liquidity_atomic=value)
    validated = validate_source_fill(dict(proof(), source_post=post), signal())
    assert validated["source_post"] == post


@pytest.mark.parametrize("value", [None, 0, 1, True, "", "00", "01", "-1", "+1", " 1", "1.0", str(2**128)])
def test_malformed_provided_liquidity_invalidates_the_source_proof(value):
    post = dict(source_post(), liquidity_atomic=value)
    assert validate_source_fill(dict(proof(), source_post=post), signal()) is None


def test_missing_and_canonical_zero_liquidity_stay_unknown():
    legacy = dict(proof(), source_post=source_post())
    assert validate_source_fill(legacy, signal()) == legacy
    with_zero = dict(proof(), source_post=dict(source_post(), liquidity_atomic="0"))
    assert validate_source_fill(with_zero, signal()) == legacy
