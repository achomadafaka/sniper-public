"""Chain 56 must build PancakeSwap SwapRouter calldata, and no other chain may change.

PancakeSwap V3 on BSC ships the ORIGINAL SwapRouter, not SwapRouter02, so both entry points carry
a `deadline` INSIDE the params struct. Its QuoterV2 is selector-identical to Uniswap's, and that is
what hid the defect: chain 56 quoted correctly, then every swap reverted with EMPTY revert data at
about 25,650 gas. An empty revert reads like a failed swap; it was a router refusing an unknown
selector. The operator's first funded BSC conversion died exactly this way on 2026-09-08, and every
funded BSC entry would have died the same way, because `evm_dex_quote` is the shared builder.

Verified on-chain 2026-09-08 against 0x1b81D678ffb9C0263b24A97847620C99d213eB14, the configured 56
router, by eth_call:

    0x04e45aaf exactInputSingle (no deadline) -> execution reverted: 0x
    0x414bf389 exactInputSingle (deadline)    -> ACCEPTED
    0xb858183f exactInput       (no deadline) -> execution reverted: 0x
    0xc04b8d59 exactInput       (deadline)    -> ACCEPTED

The blast radius is the reason for the second half of this file: 4663 and Base run genuine
SwapRouter02 deployments and trade every day on `04e45aaf`. A fix for 56 that changes their
calldata would break two live funded chains to repair a third that has never traded.
"""
import pytest

from sniper.execution import evm_dex_quote as Q
from sniper.settings import memecoin as cfg

TOKEN = "0x" + "11" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAKER = "0x" + "22" * 20


# --- the selectors are facts about deployed routers, not preferences -----------------------------

def test_the_selectors_match_what_the_live_router_accepts():
    assert Q.PANCAKE_EXACT_INPUT_SINGLE == "414bf389"
    assert Q.PANCAKE_EXACT_INPUT == "c04b8d59"
    # and the SwapRouter02 pair the other chains use is untouched
    assert Q.EXACT_INPUT_SINGLE == "04e45aaf"
    assert Q.EXACT_INPUT == "b858183f"


def test_chain_56_is_declared_pancake_and_others_are_not():
    dep = cfg.memecoin_evm_v3_addresses("56")
    assert dep and dep.get("protocol") == "pancake_v3"
    for chain in ("4663", "8453"):
        other = cfg.memecoin_evm_v3_addresses(chain)
        if other:                                     # unconfigured chains simply have no deployment
            assert other.get("protocol") in (None, "uniswap_v3"), chain


# --- encoding shape ------------------------------------------------------------------------------

def test_the_single_encoder_inserts_deadline_after_recipient():
    data = Q._encode_pancake_exact_input_single(TOKEN, WBNB, 100, TAKER, 1788900000, 10**18, 1)
    assert data.startswith("0x" + Q.PANCAKE_EXACT_INPUT_SINGLE)
    body = data[10:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    # (tokenIn, tokenOut, fee, recipient, deadline, amountIn, amountOutMinimum, sqrtPriceLimitX96)
    assert len(words) == 8, words
    assert words[0].endswith(TOKEN[2:])
    assert words[1].endswith(WBNB[2:])
    assert int(words[2], 16) == 100
    assert words[3].endswith(TAKER[2:])
    assert int(words[4], 16) == 1788900000, "deadline must sit between recipient and amountIn"
    assert int(words[5], 16) == 10**18
    assert int(words[7], 16) == 0


def test_the_single_encoder_is_one_word_longer_than_swaprouter02():
    """The whole defect in one assertion: same fields, one extra word."""
    pancake = Q._encode_pancake_exact_input_single(TOKEN, WBNB, 100, TAKER, 1, 10**18, 1)
    uniswap = Q._encode_exact_input_single(TOKEN, WBNB, 100, TAKER, 10**18, 1)
    assert len(pancake) - len(uniswap) == 64


def test_the_path_encoder_keeps_the_outer_struct_offset():
    """The struct holds `bytes path`, so it is DYNAMIC. Hand-rolling this without the outer offset
    is what produced a false 'the router rejects this selector too' reading during diagnosis."""
    path = Q._encode_path((TOKEN, WBNB), (100,))
    data = Q._encode_pancake_exact_input(path, TAKER, 1788900000, 10**18, 1)
    assert data.startswith("0x" + Q.PANCAKE_EXACT_INPUT)
    first = int(data[10:74], 16)
    assert first == 32, "a dynamic struct argument must be reached through an outer offset"
    assert path.hex() in data


@pytest.mark.parametrize("bad", [None, "not-an-int", float("nan")])
def test_a_malformed_deadline_never_reaches_the_chain(bad):
    """This builds calldata that spends real money; a bad clock must raise, never encode a zero
    deadline, which every router treats as already expired."""
    with pytest.raises(Exception):
        Q._encode_pancake_exact_input_single(TOKEN, WBNB, 100, TAKER, bad, 10**18, 1)


# --- blast radius: the two chains that trade today must not move ---------------------------------

def _quote(chain, protocol, descriptor_kind="single"):
    """Drive dex_v3_quote with a fake quoter so only the ENCODING path is under test."""
    dep = {"router": "0x" + "33" * 20, "quoter": "0x" + "44" * 20}
    if protocol:
        dep["protocol"] = protocol

    def call_fn(to, data):
        # QuoterV2 returns amountOut in the first word
        return "0x" + hex(10**17)[2:].rjust(64, "0") + "00" * 96

    return Q.dex_v3_quote(
        chain, TOKEN, WBNB, 10**18, TAKER, slippage_bps=300, cfg_addr=dep,
        call_fn=call_fn, fee_tiers=(100,), connectors=(), now_fn=lambda: 1788900000)


def test_a_uniswap_chain_still_emits_swaprouter02_calldata():
    q = _quote("4663", None)
    assert q is not None
    assert q["transaction"]["data"].startswith("0x" + Q.EXACT_INPUT_SINGLE), "4663 calldata changed"


def test_chain_56_emits_pancake_calldata_through_the_real_entry_point():
    q = _quote("56", "pancake_v3")
    assert q is not None
    assert q["transaction"]["data"].startswith("0x" + Q.PANCAKE_EXACT_INPUT_SINGLE)
    words = q["transaction"]["data"][10:]
    assert int(words[4 * 64:5 * 64], 16) == 1788900000 + Q._PANCAKE_DEADLINE_S


def test_the_route_reports_the_protocol_it_actually_built():
    """A PancakeSwap swap labelled `uniswap_v3` puts a wrong venue in every downstream record."""
    assert _quote("56", "pancake_v3")["route"]["protocol"] == "pancake_v3"
    assert _quote("4663", None)["route"]["protocol"] == "uniswap_v3"


# --- a coin whose only pool is quoted in its own pair token (BREW family, 2026-09-11) ------------
BREW = "0xfa6d9b504848606eb9aec04ccc161d169b3f2159"


def _pair_quote(sell, buy, pair_tokens):
    dep = {"router": "0x" + "33" * 20, "quoter": "0x" + "44" * 20, "protocol": "pancake_v3"}
    seen = []

    def call_fn(to, data):
        seen.append(data)
        # Only a multi-hop path through BREW prices; every direct and USDT candidate reverts.
        if data.startswith("0x" + Q.QUOTE_EXACT_INPUT) and BREW[2:] in data.lower():
            return "0x" + hex(10**17)[2:].rjust(64, "0") + "00" * 96
        return None

    q = Q.dex_v3_quote("56", sell, buy, 10**18, TAKER, slippage_bps=300, cfg_addr=dep,
                       call_fn=call_fn, fee_tiers=(10000, 2500), connectors=(),
                       now_fn=lambda: 1788900000, pair_tokens_fn=pair_tokens)
    return q, seen


def test_a_brew_quoted_coin_routes_through_brew_on_entry_and_exit():
    """0x77e2515c and 0xedd35445 refused no_route while BREW had a $1.1M V3 pool against WBNB."""
    buy, _ = _pair_quote(WBNB, TOKEN, lambda c, m: (BREW,))
    assert buy["route"]["tokens"] == [WBNB, BREW, TOKEN]
    assert buy["transaction"]["data"].startswith("0x" + Q.PANCAKE_EXACT_INPUT)
    sell, _ = _pair_quote(TOKEN, WBNB, lambda c, m: (BREW,))
    assert sell["route"]["tokens"] == [TOKEN, BREW, WBNB]


def test_pair_discovery_runs_only_after_a_full_miss():
    calls = []
    dep = {"router": "0x" + "33" * 20, "quoter": "0x" + "44" * 20, "protocol": "pancake_v3"}
    q = Q.dex_v3_quote("56", WBNB, TOKEN, 10**18, TAKER, slippage_bps=300, cfg_addr=dep,
                       call_fn=lambda to, data: "0x" + hex(10**17)[2:].rjust(64, "0") + "00" * 96,
                       fee_tiers=(10000,), connectors=(), now_fn=lambda: 1788900000,
                       pair_tokens_fn=lambda c, m: calls.append(m) or (BREW,))
    assert q["route"]["tokens"] == [WBNB, TOKEN]
    assert calls == [], "a direct pool answered; no DexScreener read"


def test_no_pair_token_leaves_a_real_no_route():
    q, _ = _pair_quote(WBNB, TOKEN, lambda c, m: ())
    assert q is None
