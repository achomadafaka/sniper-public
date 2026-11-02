"""72574/72575: receipt-bound Relay proof must survive the BSC V2 lane origin."""
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services.memecoin_source_initiation import (
    bsc_payload_initiation_refusal, source_initiation_refusal,
)
from sniper.settings import memecoin as cfg
from test_memecoin_bsc_fast_lane_live import live56, _payload

RECORDS = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_dante_deposit_lane_20261009.json").read_text())


def incident_payload(record):
    proof, post = record["proof"], record["post"]
    return dict(chain="56", tx_hash=proof["tx_hash"], seq=proof["block_number"],
                wallet=proof["beneficiary"], mint=proof["mint"],
                source_user_id=proof["source_user_id"], source_initiation=dict(proof),
                pool=post["pool"], r_quote=int(post["reserve0_atomic"]),
                r_coin=int(post["reserve1_atomic"]), src_in=int(post["pool_input_atomic"]),
                src_out=int(post["pool_output_atomic"]), paid_usd=record["source_size_usd"],
                source_price=record["source_price"], arrival_utc=record["detected_at"])


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: str(r["position_id"]))
def test_recorded_deposit_proof_passes_both_bsc_lane_guards(record, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    payload = incident_payload(record)
    assert bsc_payload_initiation_refusal(payload) is None
    event = bsc.BscLaneEvent(payload, market_cap=None)
    assert source_initiation_refusal(event, "56") is None


def test_disabled_bsc_deposit_authority_still_refuses(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"4663", "8453"}))
    assert bsc_payload_initiation_refusal(incident_payload(RECORDS[0])) == "wallet_initiation_unproved"


@pytest.mark.parametrize("field,value", [
    ("block_hash", None), ("acquired_atomic", None), ("block_number", 1),
    ("source_user_id", "other"), ("origin_identity_user_id", "other"),
    ("beneficiary", "0x" + "fa" * 20), ("mint", "0x" + "fb" * 20),
    ("tx_hash", "0x" + "fc" * 32), ("network_id", "8453"),
    ("origin_network_id", "56"), ("order_id", None), ("origin_amount_atomic", "0"),
    ("origin_tx", None), ("origin_wallet", None), ("origin_slot", 0),
])
def test_bsc_deposit_lane_retains_receipt_and_origin_binding(monkeypatch, field, value):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    payload = incident_payload(RECORDS[0])
    payload["source_initiation"][field] = value
    assert bsc_payload_initiation_refusal(payload) == "receipt_identity_mismatch"


def test_deposit_proof_reaches_native_bsc_signing_without_network_reads(live56, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    lane, ledger, net, sender, _prime = live56
    payload = _payload()
    # The existing synthetic successful receipt has the lane's identity; Relay origin fields
    # exercise that proof version through the real hook, reservation and native signer.
    proof = payload["source_initiation"]
    proof.update({k: v for k, v in RECORDS[0]["proof"].items()
                  if k.startswith("origin_") or k in {"order_id", "proof_version"}})
    proof.update(origin_identity_user_id=payload["source_user_id"],
                 acquired_atomic=str(payload["src_out"]))
    rec = lane["live"].handle(payload, recv_epoch=payload["sent_epoch"])
    assert sender.raws, rec
    assert all(sent for _name, sent in net.calls), net.calls
    assert rec["reason"].startswith("rejected:"), rec
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 1
