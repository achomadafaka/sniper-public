"""A deposit-proved Relay Flap buy (position 73827) reaches the shared BSC lane (operator option b,
2026-10-14). The lane fell back on `post:no_venue_leg` because flap_shared admitted only a direct,
native-paid Portal buy; the ordinary path then bought the same source 2.3 s later.
"""
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
from eth_utils import keccak

from scripts.research import onchain_evm_wallet_watch as watcher
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.settings import memecoin as cfg

FRAME = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_flap_relay_buy_73827.json").read_text())
MINT = "0x6246ab8300f3e070193dfef50688e987b2097777"
OWNER = "0x6feae02e6fd4f43387b2ef2b772a04e2039b0a0a"
PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
AMOUNT = 18492946671105466468577783
EPOCH = datetime.fromisoformat(FRAME["row"]["now"]).timestamp()
BOUGHT = "0x" + keccak(text="TokenBought(uint256,address,address,uint256,uint256,uint256,uint256)").hex()
FOREIGN = "0x" + "00" * 12 + "11" * 20


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENTRY_MAX_AGE_S", 45)
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("56",))


def _offer(frame, row=None):
    sent = []
    signal = bsc.make_watcher_signal("live", "test", clock=lambda: EPOCH + .1,
                                    send_fn=lambda address, payload: sent.append(payload) or True)
    result = watcher._signal_bsc_lane(signal, copy.deepcopy(frame["row"] if row is None else row),
        dict(_receipt=frame["receipt"], _transaction=frame["transaction"],
             evm_post={"reason": "no_venue_leg"}, decimals=18), 742.24, .1)
    return result, sent


def _mint_transfers(frame):
    return [log for log in frame["receipt"]["logs"]
            if log["address"] == MINT and log["topics"][0] == watch.TRANSFER_TOPIC]


def test_captured_73827_relay_flap_buy_reaches_the_shared_lane_and_the_runner_accepts_it():
    result, sent = _offer(FRAME)
    assert result["reason"] == "would_send" and result["sent"] is True, result
    payload, = sent
    assert payload["route_kind"] == "flap_shared" and payload["mint"] == MINT and payload["wallet"] == OWNER
    assert payload["pool"] == PORTAL and payload["quote"] is None
    assert payload["payment_basis"] == "relay_receipt_outflow"
    assert payload["source_initiation"]["proof_version"] == "relay-deposit-processed-v1"
    assert payload["paid_usd"] == FRAME["row"]["buy"]["source_size_usd"]
    assert bsc.check_sender(payload, 7, __import__("os").getuid(), watcher_pid=7) is None


@pytest.mark.parametrize("case", ["beneficiary_only_proof", "deposit_chain_off", "taxed_hop",
    "extra_flow", "to_foreign_wallet", "acquired_mismatch", "two_token_bought", "price_mismatch",
    "foreign_origin_user", "not_relay_envelope", "native_basis_on_relay", "another_relay_order"])
def test_only_an_exact_deposit_proved_relay_flap_buy_is_admitted(monkeypatch, case):
    f, row = copy.deepcopy(FRAME), copy.deepcopy(FRAME["row"])
    buy, transfers = row["buy"], _mint_transfers(f)
    if case == "beneficiary_only_proof":
        buy["source_initiation"] = {k: v for k, v in buy["source_initiation"].items()
                                    if not k.startswith("origin") and k != "order_id"}
        buy["source_initiation"]["proof_version"] = "relay-envelope-other-target-v1"
    elif case == "deposit_chain_off":
        monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ())
    elif case == "taxed_hop":
        transfers[2]["data"] = "0x%064x" % (AMOUNT - 1)
    elif case == "extra_flow":
        added = copy.deepcopy(transfers[-1]); added["topics"][2] = FOREIGN; added["logIndex"] = "0x999"
        f["receipt"]["logs"].append(added)
    elif case == "to_foreign_wallet":
        transfers[-1]["topics"][2] = FOREIGN
    elif case == "acquired_mismatch":
        buy["acquired_atomic"] = str(AMOUNT - 1)
    elif case == "two_token_bought":
        trade = next(log for log in f["receipt"]["logs"] if log["topics"][0] == BOUGHT)
        added = copy.deepcopy(trade); added["logIndex"] = "0x999"; f["receipt"]["logs"].append(added)
    elif case == "price_mismatch":
        buy["source_price"] *= 0.5
    elif case == "foreign_origin_user":
        buy["source_initiation"]["origin_identity_user_id"] = "someone-else"
    elif case == "not_relay_envelope":
        f["transaction"]["to"] = "0x" + "11" * 20
    elif case == "native_basis_on_relay":
        buy["payment_basis"] = "transaction_value_upper_bound"
    elif case == "another_relay_order":
        f["transaction"]["input"] = f["transaction"]["input"][:-64] + "77" * 32
    result, sent = _offer(f, row)
    assert not result["sent"] and sent == [], (case, result)


def test_the_runner_keeps_each_proof_on_its_own_payment_basis():
    _, (payload,) = _offer(FRAME)
    uid = __import__("os").getuid()
    assert bsc.check_sender(dict(payload, payment_basis="transaction_value_upper_bound"), 7, uid,
                            watcher_pid=7) == "malformed"
    direct = dict(payload["source_initiation"], proof_version="direct-wallet-paid-v1")
    assert bsc.check_sender(dict(payload, source_initiation=direct), 7, uid, watcher_pid=7) == "malformed"
