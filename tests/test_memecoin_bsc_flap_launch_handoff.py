"""A Flap create-and-buy source (position 73815) reaches the shared BSC lane; nothing else widens."""
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from scripts.research import onchain_evm_wallet_watch as watcher
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.settings import memecoin as cfg

FRAME = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_flap_create_buy_73815.json").read_text())
MINT = "0x1e2abe662b86ba6a171045f6b1e2746b62667777"
OWNER = "0xd1bee21a7898232c337e46bc399488be065bae4f"
PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
ZERO_TOPIC = "0x" + "00" * 32
CREATED = "0x" + keccak(text="TokenCreated(uint256,address,uint256,address,string,string,string)").hex()
NOW = "2026-10-13T15:00:28.008181+00:00"
EPOCH = datetime.fromisoformat(NOW).timestamp()
AMOUNT = 34245455080808080808080808
WORDS = ["uint256", "address", "uint256", "address", "string", "string", "string"]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENTRY_MAX_AGE_S", 45)


def _row(frame=None):
    frame = FRAME if frame is None else frame
    tx, receipt = frame["transaction"], frame["receipt"]
    buy = watch.direct_buy_from_receipt(receipt, tx, tx["from"], weth=bsc.WBNB,
        quote_usd=1000, decimals_fn=lambda _: 18, chain="56")
    assert buy is not None and buy["acquired_atomic"] == str(AMOUNT)
    buy["source_initiation"]["source_user_id"] = "china_sniper_1"
    trade = next(log for log in receipt["logs"] if log["address"] == PORTAL
                 and log["topics"][0] == "0x" + keccak(text="TokenBought(uint256,address,address,uint256,"
                                                            "uint256,uint256,uint256)").hex())
    return dict(tx_hash=tx["hash"], wallet=tx["from"], source_user_id="china_sniper_1",
                handle="china_sniper_1", buy=buy, log=trade, now=NOW)


def _offer(frame, row=None):
    sent = []
    signal = bsc.make_watcher_signal("live", "test", clock=lambda: EPOCH + .1,
                                    send_fn=lambda address, payload: sent.append(payload) or True)
    result = watcher._signal_bsc_lane(signal, _row(frame) if row is None else row,
        dict(_receipt=frame["receipt"], _transaction=frame["transaction"],
             evm_post={"reason": "no_venue_leg"}, decimals=18), 1000, .1)
    return result, sent


def _logs(frame, address, topic0):
    return [log for log in frame["receipt"]["logs"] if log["address"] == address and log["topics"][0] == topic0]


def _created(frame):
    created, = _logs(frame, PORTAL, CREATED)
    return created


def _rewrite_created(frame, **changes):
    event = _created(frame)
    words = list(decode(WORDS, bytes.fromhex(event["data"][2:])))
    for index, value in changes.items():
        words[int(index[1:])] = value
    event["data"] = "0x" + encode(WORDS, words).hex()


def test_captured_73815_create_and_buy_reaches_the_shared_lane():
    result, sent = _offer(FRAME)
    assert result["reason"] == "would_send" and result["sent"] is True, result
    payload, = sent
    assert payload["route_kind"] == "flap_shared" and payload["mint"] == MINT
    assert payload["wallet"] == OWNER and payload["pool"] == PORTAL and payload["quote"] is None
    assert payload["payment_basis"] == "transaction_value_upper_bound"


@pytest.mark.parametrize("case", ["no_created_event", "created_other_token", "created_other_creator",
    "created_by_foreign_contract", "created_twice", "two_genesis_mints", "genesis_mint_to_owner",
    "genesis_mint_from_foreign", "extra_foreign_flow", "malformed_created_words"])
def test_only_the_portal_genesis_mint_of_the_source_s_own_token_is_admitted(case):
    f = copy.deepcopy(FRAME)
    logs = f["receipt"]["logs"]
    mint_transfer, = [log for log in _logs(f, MINT, watch.TRANSFER_TOPIC) if log["topics"][1] == ZERO_TOPIC]
    if case == "no_created_event":
        logs.remove(_created(f))
    elif case == "created_other_token":
        _rewrite_created(f, w3="0x" + "11" * 20)
    elif case == "created_other_creator":
        _rewrite_created(f, w1="0x" + "11" * 20)
    elif case == "created_by_foreign_contract":
        _created(f)["address"] = "0x" + "11" * 20
    elif case == "created_twice":
        added = copy.deepcopy(_created(f)); added["logIndex"] = "0x99"; logs.append(added)
    elif case == "two_genesis_mints":
        added = copy.deepcopy(mint_transfer); added["logIndex"] = "0x99"; logs.append(added)
    elif case == "genesis_mint_to_owner":
        mint_transfer["topics"][2] = "0x" + "00" * 12 + OWNER[2:]
    elif case == "genesis_mint_from_foreign":
        mint_transfer["topics"][1] = "0x" + "00" * 12 + "11" * 20
    elif case == "extra_foreign_flow":
        owner_flow, = [log for log in _logs(f, MINT, watch.TRANSFER_TOPIC)
                       if log["topics"][2][-40:] == OWNER[2:]]
        added = copy.deepcopy(owner_flow); added["topics"][2] = "0x" + "00" * 12 + "11" * 20
        added["logIndex"] = "0x99"; logs.append(added)
    elif case == "malformed_created_words":
        _created(f)["data"] = _created(f)["data"][:-64]
    result, sent = _offer(f, _row(FRAME))
    assert not result["sent"] and sent == [], (case, result)
