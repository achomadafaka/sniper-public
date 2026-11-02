"""Exact ALON source receipt: PumpSwap's bound buyback fee belongs to its swap."""
import copy
import json
from pathlib import Path

import pytest

from sniper.services.memecoin_source_valuation import route_payment_evidence, value_source_buy
from sniper.services.memecoin_source_initiation import solana_receipt_initiation

OWNER = "DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y"
NET_PAID = 1_500_000_000
SWAP_PAID = 1_497_150_270


def receipt():
    return json.loads((Path(__file__).parent / "fixtures/memecoin/alon_routed_buy_20261011.json").read_text())


def test_alon_binds_pool_and_buyback_payment_and_excludes_platform_fees():
    tx = receipt()
    assert route_payment_evidence(tx, OWNER, NET_PAID) == (SWAP_PAID, None)


def test_alon_confirmed_proof_keeps_whole_acquisition_and_exact_swap_payment():
    tx = receipt()
    facts = value_source_buy(tx, OWNER, sol_usd=None, now=0)
    proof = solana_receipt_initiation(tx, OWNER, facts)
    assert proof["acquired_atomic"] == "10908646100860"
    assert proof["payment_atomic"] == str(NET_PAID)
    assert proof["route_payment_atomic"] == str(SWAP_PAID)


@pytest.mark.parametrize("wrong", ["amount", "fee_account", "fee_owner", "quote_mint",
                                    "actor", "instruction", "event", "unrelated_outflow",
                                    "decimals", "failed_transaction", "fee_token_program"])
def test_alon_cannot_bind_a_contradictory_or_extra_pumpswap_payment(wrong):
    tx = receipt()
    children = tx["meta"]["innerInstructions"][0]["instructions"]
    buy, fee, emit = children[3], children[6], children[7]
    info = fee["parsed"]["info"]
    if wrong == "amount":
        info["tokenAmount"]["amount"] = "237411"
    elif wrong == "fee_account":
        info["destination"] = children[0]["parsed"]["info"]["destination"]
    elif wrong == "fee_owner":
        index = next(i for i, k in enumerate(tx["transaction"]["message"]["accountKeys"])
                     if k["pubkey"] == info["destination"])
        for side in ("preTokenBalances", "postTokenBalances"):
            next(b for b in tx["meta"][side] if b["accountIndex"] == index)["owner"] = OWNER
    elif wrong == "quote_mint":
        buy["accounts"][4] = buy["accounts"][3]
    elif wrong == "actor":
        buy["accounts"][1] = buy["accounts"][0]
    elif wrong == "instruction":
        buy["data"] = emit["data"]
    elif wrong == "event":
        emit["data"] = "1"
    elif wrong == "decimals":
        info["tokenAmount"]["decimals"] = 9
    elif wrong == "failed_transaction":
        tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    elif wrong == "fee_token_program":
        fee["programId"] = buy["accounts"][9]
    else:
        extra = copy.deepcopy(fee)
        extra["parsed"]["info"]["destination"] = children[0]["parsed"]["info"]["destination"]
        children.insert(7, extra)
    paid, reason = route_payment_evidence(tx, OWNER, NET_PAID)
    assert paid is None and reason


@pytest.mark.parametrize("amount", [237410.9, 237410.0, True, False, "237410.9", "", None, [], {}])
def test_alon_rejects_inexact_or_malformed_atomic_payment(amount):
    tx = receipt()
    tx["meta"]["innerInstructions"][0]["instructions"][6]["parsed"]["info"]["tokenAmount"]["amount"] = amount
    assert route_payment_evidence(tx, OWNER, NET_PAID) == (None, "pumpswap_payment_unbound")


def test_alon_accepts_an_exact_integer_atomic_payment():
    tx = receipt()
    tx["meta"]["innerInstructions"][0]["instructions"][6]["parsed"]["info"]["tokenAmount"]["amount"] = 237410
    assert route_payment_evidence(tx, OWNER, NET_PAID) == (SWAP_PAID, None)


def test_alon_rejects_contradictory_dual_field_atomic_payment():
    tx = receipt()
    info = tx['meta']['innerInstructions'][0]['instructions'][6]['parsed']['info']
    info['amount'] = 237410
    info['tokenAmount']['amount'] = '237411'
    assert route_payment_evidence(tx, OWNER, NET_PAID) == (None, 'pumpswap_payment_unbound')


@pytest.mark.parametrize("container", ["transfer_info", "token_amount", "balance_row", "balance_amount"])
def test_alon_malformed_receipt_containers_stay_typed_unknown(container):
    tx = receipt()
    parsed = tx["meta"]["innerInstructions"][0]["instructions"][6]["parsed"]
    if container == "transfer_info":
        parsed["info"] = ["bad"]
    elif container == "token_amount":
        parsed["info"]["tokenAmount"] = ["bad"]
    elif container == "balance_row":
        tx["meta"]["postTokenBalances"].append(None)
    else:
        tx["meta"]["postTokenBalances"][0]["uiTokenAmount"] = ["bad"]
    assert route_payment_evidence(tx, OWNER, NET_PAID) == (None, "pumpswap_payment_unbound")
