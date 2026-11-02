"""Relay envelope buys whose swap call targets something other than the Relay adapter (2026-09-28).

Spec: docs/superpowers/specs/2026-09-28-memecoin-relay-other-target-decoder-design.md. Operator order
(direct). Every J7 4663 fill since 09-21 is a Relay envelope; 47 of 51 call the adapter, 4 call the
0x Settler, the Kyber router directly, or the 0x AllowanceHolder, and the watcher refused those as
unsupported_relayer_swap (2 of its 15 misses since 06:37Z). In those frames the tokens do not pass
through the call target, so the adapter's four fixed legs cannot hold. The watcher's decode
(any_route) now accepts such a target when the approval names it, the wallet's OWN delta is exactly
the router's cleanup leg of the coin, and the router nets to zero in both tokens. Every frame here is
a real receipt; mutated ones are marked.
"""
import copy
import gzip
import json
from pathlib import Path

import pytest
from eth_abi import decode

from sniper.execution.evm_relay_quote import RELAY_ROUTER
from sniper.services import memecoin_evm_onchain_watch as M

_DIR = Path(__file__).parent / "fixtures"
_OLD = json.load(gzip.open(_DIR / "relay_purchases_2026-09-28.json.gz", "rt"))["buys"]
_NEW = json.load(gzip.open(_DIR / "relay_other_target_4663_2026-09-28.json.gz", "rt"))["buys"]
OTHER = [b for b in _OLD if b["shape"] == "other_target"] + _NEW
ADAPTER = [b for b in _OLD if b["shape"] in ("kyber", "no_kyber", "fee_gainer")]


def _id(b):
    return "%s-%s-%s" % (b["chain"], b["handle"], b["tx"]["hash"][:10])


def _calls(b):
    return decode(M.PERMIT2_TYPES, bytes.fromhex(b["tx"]["input"][10:]))[2]


def _router_to_wallet(b):
    mint, wallet = _mint(b), b["wallet"].lower()
    return sum(int(lg["data"], 16) for lg in b["receipt"]["logs"]
               if lg["address"].lower() == mint and len(lg["topics"]) == 3
               and lg["topics"][0] == M.TRANSFER_TOPIC
               and M._topic_address(lg["topics"][1]) == RELAY_ROUTER
               and M._topic_address(lg["topics"][2]) == wallet)


def _mint(b):
    tokens = decode(["address[]", "address[]", "uint256[]", "bytes"], _calls(b)[2][3][4:])[0]
    return tokens[0].lower()


def _decode(b, *, any_route=True):
    return M.relayer_purchase_from_receipt(b["receipt"], b["tx"], b["wallet"], chain=b["chain"],
                                           contracts_verified=True, any_route=any_route)


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_an_other_target_buy_decodes_to_the_wallets_own_delta(buy):
    proof = _decode(buy)
    assert proof is not None, "real Relay buy through %s refused" % _calls(buy)[1][0]
    assert proof["proof_version"] == "relay-envelope-other-target-v1"
    assert proof["beneficiary"] == buy["wallet"].lower() and proof["mint"] == _mint(buy)
    assert proof["output_atomic"] == _router_to_wallet(buy) > 0
    assert proof["swap_target"] == str(_calls(buy)[1][0]).lower()
    assert proof["swap_selector"] == _calls(buy)[1][3][:4].hex()


def test_the_census_shapes_are_all_covered():
    targets = {(str(_calls(b)[1][0]).lower()[:10], _calls(b)[1][3][:4].hex()) for b in OTHER if b["chain"] == "4663"}
    assert {("0x0005ea38", "bb66eb41"), ("0x6131b5fa", "e21fd0e9"), ("0x00000000", "2213bc0b")} <= targets


@pytest.mark.parametrize("buy", ADAPTER, ids=_id)
def test_an_adapter_buy_keeps_its_proof(buy):
    proof = _decode(buy)
    assert proof is not None and proof["proof_version"] == M.RELAY_ENVELOPE_PROOF_VERSION
    assert "swap_target" not in proof


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_the_address_verifier_keeps_its_kyber_only_proof(buy):
    assert _decode(buy, any_route=False) is None


# --- fabricated negatives, each from a real other-target frame ---------------------------------------

def _mutated(b):
    b = copy.deepcopy(b)
    return b


def _set_approve_spender(b, spender):
    from eth_abi import encode
    raw = bytes.fromhex(b["tx"]["input"][10:])
    values = list(decode(M.PERMIT2_TYPES, raw))
    calls = [list(c) for c in values[2]]
    amount = decode(["address", "uint256"], calls[0][3][4:])[1]
    calls[0][3] = calls[0][3][:4] + encode(["address", "uint256"], [spender, amount])
    values[2] = [tuple(c) for c in calls]
    b["tx"]["input"] = "0x0a2b8f36" + encode(M.PERMIT2_TYPES, values).hex()
    return b


def _add_transfer(b, token, sender, receiver, amount):
    logs = b["receipt"]["logs"]
    top = max(int(lg["logIndex"], 16) for lg in logs)
    logs.append({"address": token, "topics": [M.TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:],
                                               "0x" + "0" * 24 + receiver[2:]],
                 "data": "0x%064x" % amount, "logIndex": hex(top + 1),
                 "transactionHash": b["receipt"]["transactionHash"], "blockHash": b["receipt"]["blockHash"],
                 "removed": False})
    return b


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_an_approval_to_another_spender_refuses(buy):
    b = _set_approve_spender(_mutated(buy), "0x" + "77" * 20)          # MUTATED
    assert _decode(b) is None


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_a_wallet_that_also_pays_the_payment_token_refuses(buy):
    payment = decode(M.PERMIT2_TYPES, bytes.fromhex(buy["tx"]["input"][10:]))[1][0][0][0].lower()
    b = _add_transfer(_mutated(buy), payment, buy["wallet"].lower(), "0x" + "66" * 20, 1)   # MUTATED
    assert _decode(b) is None


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_a_router_that_keeps_part_of_the_coin_refuses(buy):
    b = _add_transfer(_mutated(buy), _mint(buy), "0x" + "55" * 20, RELAY_ROUTER, 7)          # MUTATED
    assert _decode(b) is None


@pytest.mark.parametrize("buy", OTHER, ids=_id)
def test_a_router_that_pays_the_coin_from_its_own_balance_refuses(buy):
    """The coin into the router falls 7 units short of what it paid the wallet: the difference came
    from the router's own balance, so the swap did not produce the wallet's coin."""
    b = _mutated(buy)                                                                       # MUTATED
    mint = _mint(b)
    leg = next(lg for lg in b["receipt"]["logs"]
               if lg["address"].lower() == mint and len(lg["topics"]) == 3
               and lg["topics"][0] == M.TRANSFER_TOPIC and M._topic_address(lg["topics"][2]) == RELAY_ROUTER)
    leg["data"] = "0x%064x" % (int(leg["data"], 16) - 7)
    assert _decode(b) is None


@pytest.mark.parametrize("chain", ["56", "8453"])
def test_an_other_target_proof_pins_the_relay_code_only(chain, monkeypatch):
    """Off 4663 the Kyber router carries different code, so an other-target proof (which, like the
    envelope proof, never relies on Kyber) must not require the 4663 Kyber hash. Real code reads."""
    monkeypatch.setattr(M, "_RELAY_CODE_CACHE", {}, raising=False)
    monkeypatch.setattr(M, "_CHAIN_ID_PINNED", set(), raising=False)
    old = json.load(gzip.open(_DIR / "relay_purchases_2026-09-28.json.gz", "rt"))
    buy = next(b for b in OTHER if b["chain"] == chain)
    code = old["code_" + chain]

    def read(method, params):
        if method == "eth_chainId":
            return hex(int(chain))
        return code.get(params[0], "0xdeadbeef")       # any other contract answers the wrong code
    evidence = {}
    proof = M.read_relayer_purchase(buy["receipt"], buy["tx"], buy["wallet"], chain=chain, read_fn=read,
                                    any_route=True, evidence=evidence, live=True)
    assert proof is not None, evidence
    assert proof["proof_version"] == M.RELAY_OTHER_TARGET_PROOF_VERSION
