"""A curve-phase flap.sh Portal sell (position 73815) books its native proceeds from the receipt.

The Portal pays the seller from its own BNB balance (no WBNB Withdrawal; the fee is a WBNB
Deposit), so only the Portal's TokenSold event and the emitter's V2-shaped Swap name the payout.
"""
import copy
import json
from pathlib import Path

import pytest

from sniper.execution import evm_real_seams as S

RECEIPT = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_flap_curve_sell_73815.json").read_text())
HASH = RECEIPT["transactionHash"]
OWNER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
PROCEEDS = 61000668703544502            # TokenSold eth word == the emitter Swap's amount out to us
SOLD = "0x03a4693e592f5e75dc7c136acb39b146d2b4966c0e509c34f362dee02b3b861a"
SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


def _proceeds(receipt):
    return S.exit_proceeds_atomic(HASH, "https://rpc", owner=OWNER, quote_token=WBNB,
                                  native=True, receipt=receipt)


def _only(receipt, address, topic0):
    log, = [l for l in receipt["logs"] if l["address"] == address and l["topics"][0] == topic0]
    return log


def test_captured_73815_curve_sell_books_the_native_the_portal_paid_us():
    assert _proceeds(RECEIPT) == (PROCEEDS, "flap_portal_sell")


def _swap(receipt):
    log, = [l for l in receipt["logs"] if l["topics"][0] == SWAP]
    return log


@pytest.mark.parametrize("case", ["not_native_chain", "not_our_tx", "not_the_portal", "no_sold_event",
    "sold_by_someone_else", "two_sold_events", "sold_from_foreign_contract", "zero_eth",
    "malformed_sold", "no_swap", "swap_to_someone_else", "swap_amount_disagrees",
    "swap_from_someone_else", "portal_also_unwrapped"])
def test_a_curve_sell_without_its_exact_proof_stays_unknown(case):
    r = copy.deepcopy(RECEIPT)
    sold, native = _only(r, PORTAL, SOLD), True
    words = [sold["data"][2 + 64 * i:2 + 64 * (i + 1)] for i in range(7)]
    if case == "not_native_chain":
        native = False
    elif case == "not_our_tx":
        r["from"] = "0x" + "11" * 20
    elif case == "not_the_portal":
        r["to"] = "0x" + "11" * 20
    elif case == "no_sold_event":
        r["logs"].remove(sold)
    elif case == "sold_by_someone_else":
        words[2] = "00" * 12 + "11" * 20
    elif case == "two_sold_events":
        added = copy.deepcopy(sold); added["logIndex"] = "0x99"; r["logs"].append(added)
    elif case == "sold_from_foreign_contract":
        sold["address"] = "0x" + "11" * 20
    elif case == "zero_eth":
        words[4] = "0" * 64
    elif case == "malformed_sold":
        sold["data"] = sold["data"][:-64]
    elif case == "no_swap":
        r["logs"].remove(_swap(r))
    elif case == "swap_to_someone_else":
        _swap(r)["topics"][2] = "0x" + "00" * 12 + "11" * 20
    elif case == "swap_amount_disagrees":
        swap = _swap(r)
        swap["data"] = swap["data"][:-64] + f"{PROCEEDS + 1:064x}"
    elif case == "swap_from_someone_else":
        _swap(r)["topics"][1] = "0x" + "00" * 12 + "11" * 20
    elif case == "portal_also_unwrapped":
        r["logs"].append({"address": WBNB, "topics": [S.WRAPPED_NATIVE_WITHDRAWAL_TOPIC,
                          "0x" + "00" * 12 + PORTAL[2:]], "data": "0x" + f"{5:064x}", "logIndex": "0x98"})
    if case in {"sold_by_someone_else", "zero_eth"}:
        sold["data"] = "0x" + "".join(words)
    answer = S.exit_proceeds_atomic(HASH, "https://rpc", owner=OWNER, quote_token=WBNB,
                                    native=native, receipt=r)
    if case == "portal_also_unwrapped":
        # The existing unwrap proof keeps precedence; the curve-sell proof never runs beside it.
        assert answer[1] != "flap_portal_sell", answer
    else:
        assert answer == (None, "unknown"), case
