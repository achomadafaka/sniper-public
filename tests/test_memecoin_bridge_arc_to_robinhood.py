"""Guards for the ARC(5042) USDC -> Robinhood(4663) WETH bridge script (2026-09-25).

The quote fixture is a REAL LI.FI /v1/quote answer captured read-only at 2026-09-25T13:59:52Z for
our custody wallet, 30 USDC, tool relaydepository. The diamond is LI.FI's /v1/chains
diamondAddress for 5042 read at the same time. No network: pure validation and settlement logic.
"""
import copy
import importlib.util
import json
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "memecoin_bridge_arc_to_robinhood", os.path.join(_ROOT, "scripts", "memecoin_bridge_arc_to_robinhood.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

with open(os.path.join(_ROOT, "tests", "fixtures", "lifi", "quote_arc_usdc_to_robinhood_weth_2026-09-25.json")) as fh:
    QUOTE = json.load(fh)
WALLET = "0x898BE9c27f8B391C47A3d9F2dA17781aa8aA3A1d"
DIAMOND = "0xa4072583658fae592a3506a42431cb6316a8d40b"
ATOMS = 30_000_000
ETH_USD = 2697.41          # LI.FI's WETH priceUSD in the same capture; stands in for our live mark


def _validate(quote=None, **kw):
    args = dict(wallet=WALLET, atoms=ATOMS, minimum_pct="98.5", eth_usd=ETH_USD, diamond=DIAMOND)
    args.update(kw)
    return F.validate_quote(quote or QUOTE, args.pop("wallet"), args.pop("atoms"), args.pop("minimum_pct"),
                            args.pop("eth_usd"), args.pop("diamond"))


def _mutated(path, value):
    q = copy.deepcopy(QUOTE)
    node = q
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return q


def test_the_captured_quote_passes_and_returns_the_wei_minimum():
    assert _validate() == int(QUOTE["estimate"]["toAmountMin"]) == 10_998_493_892_504_963


@pytest.mark.parametrize("path,value", [
    (("action", "toToken", "decimals"), 6),                                   # USDC-shaped destination
    (("action", "toToken", "address"), "0x4200000000000000000000000000000000000006"),  # Base WETH
    (("action", "toToken", "chainId"), 8453),
    (("action", "fromToken", "decimals"), 18),                                # an 18-dec "USDC" on Arc
    (("action", "fromToken", "address"), "0x0000000000000000000000000000000000000001"),
])
def test_token_identity_and_decimals_refuse(path, value):
    with pytest.raises(ValueError, match="token identity"):
        _validate(_mutated(path, value))


@pytest.mark.parametrize("path,value", [
    (("action", "toAddress"), "0x000000000000000000000000000000000000dEaD"),
    (("action", "fromAddress"), "0x000000000000000000000000000000000000dEaD"),
    (("action", "toChainId"), 8453),
    (("action", "fromAmount"), str(ATOMS + 1)),
])
def test_destination_and_amount_refuse(path, value):
    with pytest.raises(ValueError, match="transfer identity"):
        _validate(_mutated(path, value))


def test_the_floor_is_in_dollars_at_our_price():
    minimum_usd = 10_998_493_892_504_963 / 1e18 * ETH_USD          # ~$29.667
    assert _validate(minimum_pct="98.8") > 0                      # floor $29.64 < $29.667
    with pytest.raises(ValueError, match="dollar floor"):
        _validate(minimum_pct="99.0")                             # floor $29.70 > $29.667
    # The same quote refuses when OUR price says the WETH is worth less than LI.FI claims.
    with pytest.raises(ValueError, match="dollar floor"):
        _validate(eth_usd=ETH_USD * 0.98)
    assert 29.6 < minimum_usd < 29.7


@pytest.mark.parametrize("price", [None, 0, float("nan")])
def test_an_unknown_eth_price_refuses(price):
    with pytest.raises(ValueError, match="ETH/USD"):
        _validate(eth_usd=price)


def test_spender_and_target_must_be_the_published_diamond():
    with pytest.raises(ValueError, match="diamond"):
        _validate(diamond="0xb477751b76cf82d00a686a1232f5fcd772414af3")    # 4663's diamond, not ARC's
    with pytest.raises(ValueError, match="diamond"):
        _validate(_mutated(("estimate", "approvalAddress"), "0x000000000000000000000000000000000000dEaD"))
    with pytest.raises(ValueError, match="diamond"):
        _validate(_mutated(("transactionRequest", "to"), "0x000000000000000000000000000000000000dEaD"))


def test_native_value_on_arc_refuses():
    with pytest.raises(ValueError, match="native value"):
        _validate(_mutated(("transactionRequest", "value"), "0x1"))


def test_lifi_diamond_reads_the_chains_answer():
    chains = {"chains": [{"id": 4663, "diamondAddress": "0xB477751B76CF82d00a686A1232f5fCD772414Af3"},
                         {"id": 5042, "diamondAddress": "0xA4072583658Fae592A3506A42431cb6316a8d40b"}]}
    assert F.lifi_diamond(chains, 5042) == DIAMOND
    with pytest.raises(ValueError):
        F.lifi_diamond({"chains": [{"id": 4663, "diamondAddress": "0xB4"}]}, 5042)


def _done(chain=4663, sig="0xabc"):
    return {"status": "DONE", "substatus": "COMPLETED", "sending": {"txHash": sig},
            "receiving": {"chainId": chain, "txHash": "0xdef"}}


def test_settlement_needs_the_weth_rise_and_a_real_balance_read():
    receipt = {"bridge_sig": "0xABC", "weth_before_wei": 5 * 10**15, "minimum_wei": 10_998_493_892_504_963}
    enough = receipt["weth_before_wei"] + receipt["minimum_wei"]
    assert F.settlement_complete(receipt, _done(), enough)
    assert not F.settlement_complete(receipt, _done(), enough - 1)          # delivered as native ETH
    assert not F.settlement_complete(receipt, _done(), None)                # failed read is not a zero
    assert not F.settlement_complete(receipt, _done(chain=8453), enough)
    assert not F.settlement_complete(receipt, _done(sig="0xother"), enough)
    assert not F.settlement_complete(receipt, {"status": "PENDING"}, enough)
