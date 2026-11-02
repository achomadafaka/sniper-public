"""Research-only historical Relay destination proof; no live reads or origin claim.

The envelope/receipt fixture is an existing full public 4663 transaction.
Code RPC answers are offline doubles with test-only pinned hashes.
"""
from copy import deepcopy
import gzip
import importlib
import json
from pathlib import Path

from eth_utils import keccak
import pytest


@pytest.fixture
def relay():
    return importlib.import_module("sniper.services.wallet_universe_relay")


@pytest.fixture
def purchase():
    buys = json.load(gzip.open(Path(__file__).parent / "fixtures" /
        "relay_other_target_4663_2026-09-28.json.gz", "rt"))["buys"]
    frame = deepcopy(buys[0])
    receipt = frame["receipt"]
    unit = dict(chain=frame["chain"], uid="historical-beneficiary", address=frame["wallet"],
        mint=frame["mint"], signature=frame["tx"]["hash"],
        source_slot=int(receipt["blockNumber"],16),
        t0=int(receipt["logs"][0]["blockTimestamp"],16), source_initiation=None)
    return unit, receipt, frame["tx"]


def reader(relay, purchase, monkeypatch, *, code="0x6000"):
    unit, receipt, transaction = purchase
    digest = "0x" + keccak(bytes.fromhex(code[2:])).hex()
    monkeypatch.setattr(relay, "CODE_HASHES", tuple((a,digest) for a,_ in relay.CODE_HASHES))
    calls = []
    def call(method, params):
        calls.append((method, deepcopy(params)))
        if method == "eth_getTransactionByHash":
            assert params == [unit["signature"]]
            return deepcopy(transaction)
        assert method == "eth_getCode"
        assert params[1] == receipt["blockNumber"] and params[1] != "latest"
        assert params[0] in dict(relay.CODE_HASHES)
        return code
    return call, calls


def test_real_envelope_gets_destination_only_proof_at_exact_receipt_block(relay,purchase,monkeypatch):
    unit,receipt,_ = purchase
    call,calls = reader(relay,purchase,monkeypatch)
    state = {}
    proof = relay.recover_beneficiary(unit,receipt,call,state=state)
    assert proof["version"] == relay.VERSION and proof["code_basis"] == "receipt_block"
    assert proof["beneficiary_only"] is True and proof["initiating_wallet_verified"] is False
    assert proof["third_party_payment_possible"] is True
    assert proof["source_initiation"]["source_user_id"] == unit["uid"]
    assert proof["source_initiation"]["beneficiary"] == unit["address"]
    assert proof["source_initiation"]["mint"] == unit["mint"]
    assert proof["source_initiation"]["proof_version"] == "relay-envelope-other-target-v1"
    assert len(calls) == 3 and state["proof"] == proof
    json.dumps(state)


@pytest.mark.parametrize("answer", [None,"0x","0x00","0x6001","not-bytecode"])
def test_unknown_empty_malformed_or_wrong_code_cannot_use_head_fallback(relay,purchase,monkeypatch,answer):
    unit,receipt,transaction=purchase
    _,calls=reader(relay,purchase,monkeypatch)
    def call(method,params):
        calls.append((method,params))
        if method=="eth_getTransactionByHash": return deepcopy(transaction)
        assert params[1]==receipt["blockNumber"]
        return answer
    with pytest.raises(relay.RelayResearchError): relay.recover_beneficiary(unit,receipt,call)
    assert all(params[-1]!="latest" for method,params in calls if method=="eth_getCode")


@pytest.mark.parametrize("field,value", [("chainId","0x38"),("hash","0x"+"77"*32),
    ("blockHash","0x"+"77"*32),("blockNumber","0x1"),("transactionIndex","0x99"),
    ("from","0x"+"77"*20),("to","0x"+"77"*20),("value","0x1"),("input","0x0a2b8f36")])
def test_wrong_raw_transaction_identity_or_envelope_is_refused(relay,purchase,monkeypatch,field,value):
    unit,receipt,transaction=purchase
    transaction[field]=value
    call,calls=reader(relay,purchase,monkeypatch)
    with pytest.raises(relay.RelayResearchError): relay.recover_beneficiary(unit,receipt,call)
    assert not any(method=="eth_getCode" for method,_ in calls)


@pytest.mark.parametrize("mutation", ["chain","mint","beneficiary","slot","failed","removed",
    "log_block","log_index","log_tx","airdrop"])
def test_wrong_unit_anchor_or_delivery_is_not_a_purchase(relay,purchase,monkeypatch,mutation):
    unit,receipt,transaction=purchase
    if mutation=="chain": unit["chain"]="1"
    if mutation=="mint": unit["mint"]="0x"+"77"*20
    if mutation=="beneficiary": unit["address"]="0x"+"77"*20
    if mutation=="slot": unit["source_slot"]+=1
    if mutation=="failed": receipt["status"]="0x0"
    if mutation=="removed": receipt["logs"][0]["removed"]=True
    if mutation=="log_block": receipt["logs"][0]["blockNumber"]="0x1"
    if mutation=="log_index": receipt["logs"][0]["transactionIndex"]="0x99"
    if mutation=="log_tx": receipt["logs"][0]["transactionHash"]="0x"+"77"*32
    if mutation=="airdrop":
        delivered=next(l for l in receipt["logs"] if l["address"]==unit["mint"] and
            len(l["topics"])==3 and l["topics"][2].lower().endswith(unit["address"][2:]))
        delivered["topics"][1]="0x"+"00"*12+"77"*20
    call,calls=reader(relay,purchase,monkeypatch)
    with pytest.raises(relay.RelayResearchError): relay.recover_beneficiary(unit,receipt,call)
    assert not any(method=="eth_getCode" for method,_ in calls)


def test_budget_stop_preserves_completed_exact_block_reads(relay,purchase,monkeypatch):
    unit,receipt,_=purchase
    base,calls=reader(relay,purchase,monkeypatch)
    state={}
    def call(method,params):
        if method=="eth_getCode" and len(calls)==2: raise ValueError("budget_exhausted")
        return base(method,params)
    with pytest.raises(ValueError,match="budget_exhausted"):
        relay.recover_beneficiary(unit,receipt,call,state=state)
    assert len(state["code"])==1 and "transaction" in state
    proof=relay.recover_beneficiary(unit,receipt,base,state=json.loads(json.dumps(state)))
    assert proof["beneficiary_only"] is True and len(calls)==3


@pytest.mark.parametrize("mutation", ["version","uid","t0","payer","code_flag","block","input"])
def test_resume_rejects_forged_obsolete_or_rebound_proof_cache(relay,purchase,monkeypatch,mutation):
    unit,receipt,_=purchase
    call,_=reader(relay,purchase,monkeypatch)
    state={};relay.recover_beneficiary(unit,receipt,call,state=state)
    if mutation=="version": state["version"]="old-proof"
    if mutation=="uid": unit["uid"]="someone-else"
    if mutation=="t0": unit["t0"]+=1
    if mutation=="payer": state["proof"]["source_initiation"]["payer"]="0x"+"77"*20
    if mutation=="code_flag": state["code"]={a:True for a,_ in relay.CODE_HASHES}
    if mutation=="block": receipt["blockHash"]="0x"+"77"*32
    if mutation=="input":
        from eth_abi import decode,encode
        from sniper.services.memecoin_evm_onchain_watch import PERMIT2_TYPES
        transaction=state["transaction"]
        values=list(decode(PERMIT2_TYPES,bytes.fromhex(transaction["input"][10:])))
        signature=values[-1];values[-1]=bytes([signature[0]^1])+signature[1:]
        transaction["input"]="0x0a2b8f36"+encode(PERMIT2_TYPES,values).hex()
    with pytest.raises(relay.RelayResearchError):
        relay.recover_beneficiary(unit,receipt,lambda *_:pytest.fail("must use or reject bound cache"),state=state)


def test_cached_proof_rechecks_fresh_receipt_attribution(relay,purchase,monkeypatch):
    unit,receipt,_=purchase
    call,_=reader(relay,purchase,monkeypatch)
    state={};relay.recover_beneficiary(unit,receipt,call,state=state)
    receipt["logs"]=[]
    with pytest.raises(relay.RelayResearchError):
        relay.recover_beneficiary(unit,receipt,lambda *_:pytest.fail("must not reuse old delivery"),state=state)


def unknown_slot_resume(relay,purchase,monkeypatch,*,limit=20):
    from test_wallet_universe_recovery import Reader
    from sniper.services.wallet_universe_recovery import SharedResearchRead
    from sniper.services.wallet_universe_source import SourceRead,SOURCE_VIEW_VERSION
    unit,receipt,transaction=purchase
    unit.update(chain="56",source_slot=None);transaction["chainId"]="0x38"
    first,_=reader(relay,purchase,monkeypatch)
    state={}
    def stopped(method,params):
        if method=="eth_getCode" and params[0]==relay.ROUTER:raise ValueError("budget")
        return first(method,params)
    with pytest.raises(ValueError,match="budget"):
        relay.recover_beneficiary(unit,receipt,stopped,state=state)
    assert len(state["code"])==1
    primary,archive=Reader(),Reader()
    primary.values.update(eth_getTransactionByHash=deepcopy(transaction),eth_getCode="0x",
        eth_getBlockByNumber=dict(number=receipt["blockNumber"],hash=receipt["blockHash"],timestamp=hex(unit["t0"])))
    archive.values["eth_getCode"]="0x6000"
    shared=SharedResearchRead(primary,lambda:archive,limit)
    view=dict(receipt,source_view_version=SOURCE_VIEW_VERSION,observation_source="canonical_full_block_and_logs",
        source_unit_binding={k:unit.get(k) for k in ("chain","uid","address","mint","signature","source_slot","t0")})
    source=SourceRead(unit,shared.for_unit(unit),cached=view)
    fresh=source("eth_getTransactionReceipt",[unit["signature"]])
    return unit,fresh,state,source,shared,primary,archive


def test_unknown_slot_partial_resume_independently_learns_archive_code_permission(relay,purchase,monkeypatch):
    unit,receipt,state,source,shared,primary,archive=unknown_slot_resume(relay,purchase,monkeypatch)
    proof=relay.recover_beneficiary(unit,receipt,source,state=state)
    assert proof["beneficiary_only"] is True and proof["initiating_wallet_verified"] is False
    assert state["binding"]["unit"]["source_slot"] is None
    assert [method for method,_,_ in primary.calls]==["eth_getBlockByNumber","eth_getTransactionByHash","eth_getCode"]
    assert [method for method,_,_ in archive.calls]==["eth_chainId","eth_getCode"]
    assert archive.calls[-1][1]==[relay.ROUTER,receipt["blockNumber"]]
    assert shared.used==5
    # Fully proved cache needs neither another transaction nor more code reads.
    assert relay.recover_beneficiary(unit,receipt,source,state=state)==proof and shared.used==5


@pytest.mark.parametrize("mutation",["hash","blockHash","from","input"])
def test_unknown_slot_resume_rejects_changed_fresh_transaction_before_code(relay,purchase,monkeypatch,mutation):
    unit,receipt,state,source,shared,primary,archive=unknown_slot_resume(relay,purchase,monkeypatch)
    transaction=primary.values["eth_getTransactionByHash"]
    if mutation in ("hash","blockHash"):transaction[mutation]="0x"+"77"*32
    if mutation=="from":transaction[mutation]="0x"+"77"*20
    if mutation=="input":
        transaction["input"]=transaction["input"][:-2]+("01" if transaction["input"][-2:]!="01" else "02")
    with pytest.raises(relay.RelayResearchError,match="research_relay_transaction_(identity|cache)_mismatch"):
        relay.recover_beneficiary(unit,receipt,source,state=state)
    assert shared.used==2 and len(state["code"])==1 and not archive.calls
    assert primary.calls[-1][0]=="eth_getTransactionByHash"


@pytest.mark.parametrize("limit",[2,3,4])
def test_unknown_slot_resume_transaction_and_archive_code_share_total_cap(relay,purchase,monkeypatch,limit):
    unit,receipt,state,source,shared,primary,archive=unknown_slot_resume(relay,purchase,monkeypatch,limit=limit)
    with pytest.raises(ValueError,match="budget"):
        relay.recover_beneficiary(unit,receipt,source,state=state)
    assert shared.used==limit and len(state["code"])==1 and "proof" not in state
