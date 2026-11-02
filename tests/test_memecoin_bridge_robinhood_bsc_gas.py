"""Pure-parsing guards for the Robinhood(4663) -> BSC native-gas/bankroll bridge script
(2026-09-06). Same family as test_memecoin_bridge_robinhood_bsc.py; this is the EXACT_INPUT
sibling (native ETH/WETH origin) built for the chicken-and-egg case: BSC gas can run out before
there is enough of it to pay for the on-chain swap that would make more gas from BSC's own
stablecoin, so this sources gas from Robinhood's native ETH instead. No network — pure logic
only."""
import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "memecoin_bridge_robinhood_bsc_gas.py")
_spec = importlib.util.spec_from_file_location("memecoin_bridge_robinhood_bsc_gas", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def test_to_int_handles_dec_hex_none():
    assert F._to_int("0") == 0
    assert F._to_int("50000000") == 50_000_000
    assert F._to_int("0x1237") == 4663
    assert F._to_int(None, 7) == 7
    assert F._to_int(42) == 42


def test_steps_flatten_and_value_parse():
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    relay = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
    quote = {"steps": [
        {"id": "deposit", "items": [{"data": {"chainId": 4663, "to": relay,
                                              "data": "0x49290c1caa", "value": "200000000000000"}}]},
    ]}
    from eth_utils import to_checksum_address
    txs = F._steps_as_txs(quote)
    assert txs[0]["to"] == to_checksum_address(relay)   # lowercase -> checksummed
    assert txs[0]["value"] == 200000000000000


def test_rejects_non_robinhood_origin_chain():
    quote = {"steps": [{"id": "deposit", "items": [{"data": {"chainId": 56, "to": "0xX",
                                                             "data": "0x", "value": "0"}}]}]}
    try:
        F._steps_as_txs(quote)
        assert False, "expected RuntimeError on non-Robinhood origin chain"
    except RuntimeError as e:
        assert "chainId" in str(e)


def test_constants_are_the_verified_addresses():
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    assert F.ROBINHOOD_CHAIN == 4663 and F.BSC_CHAIN == 56
    assert F.ROBINHOOD_WETH.lower() == CHAIN_QUOTE["4663"]["token"].lower()
    assert F.BSC_WBNB.lower() == CHAIN_QUOTE["56"]["token"].lower()
    assert F.NATIVE == "0x0000000000000000000000000000000000000000"


def test_the_quote_is_exact_input_not_exact_output():
    """The whole reason this script exists beside memecoin_bridge_robinhood_bsc.py: this one
    already knows its origin amount (an explicit --amount of Robinhood's own ETH/WETH), so unlike
    that EXACT_OUTPUT sibling, this one asks Relay for an exact INPUT."""
    src = open(_p).read()
    assert '"tradeType": "EXACT_INPUT"' in src
    assert '"tradeType": "EXACT_OUTPUT"' not in src


def test_native_origin_reserve_is_checked_after_subtracting_the_bridged_amount():
    """The bridged ETH and the gas that pays for the bridge come out of the SAME balance."""
    src = open(_p).read()
    assert "robin_gas - args.amount" in src or "robin_gas - MIN_ROBINHOOD_GAS_ETH" in src
