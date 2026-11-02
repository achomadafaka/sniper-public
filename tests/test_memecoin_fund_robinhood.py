"""Pure-parsing guards for the Robinhood funding bridge script (2026-07-12). These functions turn
untrusted Relay API JSON into real transactions, so the value/hex parsing and the origin-chain guard
are safety-critical. No network — pure logic only."""
import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "memecoin_fund_robinhood.py")
_spec = importlib.util.spec_from_file_location("memecoin_fund_robinhood", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def test_to_int_handles_dec_hex_none():
    assert F._to_int("0") == 0
    assert F._to_int("50000000") == 50_000_000       # USDC amount as decimal string
    assert F._to_int("0x1237") == 4663               # hex string
    assert F._to_int(None, 7) == 7                    # missing -> default
    assert F._to_int(42) == 42


def test_steps_flatten_and_value_parse():
    # lowercase `to` (as Relay returns for the deposit router) must be accepted and checksummed
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    relay = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
    quote = {"steps": [
        {"id": "approve", "items": [{"data": {"chainId": 8453, "to": usdc,
                                              "data": "0x095ea7b3ff", "value": "0"}}]},
        {"id": "deposit", "items": [{"data": {"chainId": 8453, "to": relay,
                                              "data": "0xe8017952aa", "value": "1000"}}]},
    ]}
    from eth_utils import to_checksum_address
    txs = F._steps_as_txs(quote)
    assert [t["label"] for t in txs] == ["approve", "deposit"]
    assert txs[0]["to"] == F.BASE_USDC and txs[0]["value"] == 0        # checksummed
    assert txs[1]["to"] == to_checksum_address(relay)                 # lowercase -> checksummed
    assert txs[1]["value"] == 1000                    # decimal-string value -> int


def test_rejects_non_base_origin_chain():
    """A step that would execute on the WRONG chain must raise, never silently sign on Robinhood."""
    quote = {"steps": [{"id": "deposit", "items": [{"data": {"chainId": 4663, "to": "0xX",
                                                             "data": "0x", "value": "0"}}]}]}
    try:
        F._steps_as_txs(quote)
        assert False, "expected RuntimeError on non-Base origin chain"
    except RuntimeError as e:
        assert "chainId" in str(e)


def test_constants_are_the_verified_addresses():
    assert F.ROBINHOOD_CHAIN == 4663 and F.BASE_CHAIN == 8453
    assert F.BASE_USDC.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert F.ROBINHOOD_WETH.lower() == "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    assert F.NATIVE == "0x0000000000000000000000000000000000000000"
