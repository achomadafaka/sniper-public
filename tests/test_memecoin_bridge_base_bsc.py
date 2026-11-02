"""Pure-parsing guards for the Base -> BSC gas/bankroll bridge script (2026-09-06). These functions
turn untrusted Relay API JSON into real transactions, so the value/hex parsing and the origin-chain
guard are safety-critical. No network — pure logic only."""
import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "memecoin_bridge_base_bsc.py")
_spec = importlib.util.spec_from_file_location("memecoin_bridge_base_bsc", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def test_to_int_handles_dec_hex_none():
    assert F._to_int("0") == 0
    assert F._to_int("50000000") == 50_000_000
    assert F._to_int("0x38") == 56                   # BSC chain id, hex string
    assert F._to_int(None, 7) == 7
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
    """A step that would execute on the WRONG chain (e.g. BSC itself) must raise, never silently
    sign on the destination chain."""
    quote = {"steps": [{"id": "deposit", "items": [{"data": {"chainId": 56, "to": "0xX",
                                                             "data": "0x", "value": "0"}}]}]}
    try:
        F._steps_as_txs(quote)
        assert False, "expected RuntimeError on non-Base origin chain"
    except RuntimeError as e:
        assert "chainId" in str(e)


def test_constants_are_the_verified_addresses():
    assert F.BASE_CHAIN == 8453 and F.BSC_CHAIN == 56
    assert F.BASE_USDC.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    # WBNB, the same address CHAIN_QUOTE["56"] already carries — never a second, competing source.
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    assert F.BSC_WBNB.lower() == CHAIN_QUOTE["56"]["token"].lower()
    assert F.NATIVE == "0x0000000000000000000000000000000000000000"


def test_dest_choices_are_gas_and_bankroll_only():
    import ast
    tree = ast.parse(open(_p).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument":
            for kw in node.keywords:
                if kw.arg == "choices" and isinstance(kw.value, ast.Tuple):
                    values = {elt.value for elt in kw.value.elts if isinstance(elt, ast.Constant)}
                    if values == {"bnb", "wbnb"}:
                        return
    raise AssertionError("--dest must offer exactly bnb (gas) and wbnb (bankroll)")
