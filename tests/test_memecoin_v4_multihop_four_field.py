"""The V4 MULTIHOP ExactInputParams layout follows the chain's deployed Universal Router (Gamma LOW 1
on Alpha's 1192ad309, 2026-10-01).

Ethereum (1) and Base (8453) run upstream v4-periphery's FOUR fields
(currencyIn, PathKey[] path, uint128 amountIn, uint128 amountOutMinimum). 4663, 56 and 5042 run the
FIVE-field form with an extra uint256[] between the path and the amounts. The single-hop path
encoder in evm_v4_quote already splits on `_EXACT_INPUT_FOUR_FIELD_CHAINS`; the multihop encoder was
still five-field everywhere, and Base runs v4_multihop in every ladder.
"""
import hashlib

import pytest
from eth_abi import decode

from sniper.execution import evm_v4_multihop_tx as T
from sniper.execution import evm_v4_quote as Q

CIN = "0x" + "11" * 20
BR = "0x" + "22" * 20
COIN = "0x" + "33" * 20
HOOK = "0x" + "44" * 20
PATH = [(BR, 3000, 60, HOOK), (COIN, 10000, 200, "0x" + "00" * 20)]
AMOUNT, MIN_OUT = 10 ** 18, 5 * 10 ** 17
# Today's bytes (main 1192ad309, five-field everywhere), captured before the change.
GOLDEN = {True: "8d8d81cbc687559317b475debbb948202be14e701e71d75124fb24aa3931c9d8",
          False: "d2d5d9e2dcbcfab07bd34e38459e9d476c1494abc788e076030741e5130fd3c8"}
PATH_KEY = "(address,uint24,int24,address,bytes)[]"


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _p0(chain):
    return bytes.fromhex(T._encode_exact_input(CIN, PATH, AMOUNT, MIN_OUT, chain=chain))


@pytest.mark.parametrize("chain", ["4663", "56", "5042"])
@pytest.mark.parametrize("take_all", [True, False])
def test_the_five_field_chains_keep_todays_bytes(chain, take_all):
    swap, actions = T._v4_multihop_swap_input(CIN, COIN, PATH, AMOUNT, MIN_OUT,
                                             take_all_to_sender=take_all, chain=chain)
    assert _sha(swap) == GOLDEN[take_all]
    (currency_in, path, _min_hop, amount_in, min_out), = decode(
        ["(address,%s,uint256[],uint128,uint128)" % PATH_KEY], _p0(chain))
    assert (currency_in, amount_in, min_out, len(path)) == (CIN, AMOUNT, MIN_OUT, 2)


@pytest.mark.parametrize("chain", ["1", "8453"])
def test_base_and_ethereum_take_upstreams_four_fields(chain):
    (currency_in, path, amount_in, min_out), = decode(["(address,%s,uint128,uint128)" % PATH_KEY], _p0(chain))
    assert (currency_in, amount_in, min_out) == (CIN, AMOUNT, MIN_OUT)
    assert [(p[0], p[1], p[2], p[3]) for p in path] == [(BR, 3000, 60, HOOK), (COIN, 10000, 200, "0x" + "00" * 20)]
    # read as the five-field form, the router would see the path offset where amountIn sits
    assert _p0(chain) != _p0("4663")


def test_the_encoder_needs_the_chain():
    with pytest.raises(TypeError):
        T._encode_exact_input(CIN, PATH, AMOUNT, MIN_OUT)


def test_one_set_decides_both_encoders(monkeypatch):
    """The multihop encoder reads evm_v4_quote's own set, so a chain moves in one place."""
    four = _p0("8453")
    monkeypatch.setattr(Q, "_EXACT_INPUT_FOUR_FIELD_CHAINS", frozenset({"1"}))
    assert _p0("8453") == _p0("4663") != four


def test_the_builders_pass_their_chain(monkeypatch):
    """The production exit builder, end to end: Base sells four-field, 4663 five-field."""
    monkeypatch.setattr(T, "_permit2_input", lambda *a, **k: "00" * 32)
    route = {"legs_path": [{"currency": BR, "key": {"fee": 3000, "tick_spacing": 60, "hooks": HOOK}},
                           {"currency": COIN, "key": {"fee": 10000, "tick_spacing": 200,
                                                      "hooks": "0x" + "00" * 20}}]}
    # the exit replays the entry reversed: coin -> bridge on the coin's pool, bridge -> cash on the bridge's
    exit_path = [(BR, 10000, 200, "0x" + "00" * 20), (Q._NATIVE, 3000, 60, HOOK)]
    swaps = {}
    for chain in ("8453", "4663"):
        built = T.build_multihop_exit(chain, COIN, Q._NATIVE, route, AMOUNT, MIN_OUT,
                                      taker="0x" + "55" * 20, acct=object(), now_fn=lambda: 1_800_000_000)
        swaps[chain] = built["swap_input"]
        assert swaps[chain] == T._v4_multihop_swap_input(COIN, Q._NATIVE, exit_path, AMOUNT, MIN_OUT,
                                                         take_all_to_sender=False, chain=chain)[0]
    assert swaps["8453"] != swaps["4663"]


def test_the_entry_builder_passes_its_chain(monkeypatch):
    """Gamma LOW 1 on 02095dcd1: the entry twin. A native-cash buy needs no permit2."""
    route = {"legs_path": [{"currency": BR, "key": {"fee": 3000, "tick_spacing": 60, "hooks": HOOK}},
                           {"currency": COIN, "key": {"fee": 10000, "tick_spacing": 200,
                                                      "hooks": "0x" + "00" * 20}}]}
    swaps = {}
    for chain in ("8453", "4663"):
        built = T.build_multihop_entry(chain, Q._NATIVE, COIN, route, AMOUNT, MIN_OUT,
                                       taker="0x" + "55" * 20, now_fn=lambda: 1_800_000_000)
        swaps[chain] = built["swap_input"]
        assert swaps[chain] == T._v4_multihop_swap_input(Q._NATIVE, COIN, PATH, AMOUNT, MIN_OUT,
                                                         chain=chain)[0]
    assert swaps["8453"] != swaps["4663"]
