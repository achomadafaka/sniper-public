"""Bound receipt initiation to the automatic BSC event before funded provider work."""
import gzip
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_real_runner as runner
from test_memecoin_entry_identity import NOW, _arm, _approve, _conn, _event

WALLET = "0x696d1265c8fc4f14797abebfae3c43ebfa9d8e28"
MINT = "0x" + "72" * 20
QUOTE = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TX = "0x" + "42" * 32
BLOCK = "0x" + "83" * 32
RUGS = json.load(gzip.open(Path(__file__).parent / "fixtures/memecoin/bsc_source_rugs_20261004.json.gz", "rt"))["frames"]


def direct_frame():
    router = "0x" + "b3" * 20
    tx = dict(hash=TX, blockHash=BLOCK, blockNumber="0x10", chainId="0x38",
              **{"from": WALLET, "to": router, "value": hex(10 ** 17)})
    receipt = dict(transactionHash=TX, blockHash=BLOCK, blockNumber="0x10", status="0x1",
                   **{"from": WALLET, "to": router}, logs=[{
                       "address": MINT, "topics": [watch.TRANSFER_TOPIC,
                         "0x" + router[2:].rjust(64, "0"), "0x" + WALLET[2:].rjust(64, "0")],
                       "data": hex(10 ** 24)}])
    return receipt, tx


def expected_proof(*, mint=MINT, tx_hash=TX, wallet=WALLET, uid="u1", block_number=16):
    return dict(schema_version=1, proof_version="direct-wallet-paid-v1", network_id="56",
                tx_hash=tx_hash, block_hash=BLOCK, block_number=block_number, mint=mint,
                beneficiary=wallet, payer=wallet, source_user_id=uid)


def source_event(proof=None, *, source="onchain_watch", chain="56"):
    ev = _event(network_id=chain, mint=MINT, source=source,
                event_id=f"onchain:{chain}:{TX}:{MINT}", market_cap=11_000.0)
    ev.source_wallet, ev.source_block_number = WALLET, 16
    ev.source_initiation = proof
    return ev


def test_direct_decoder_retains_bound_payment_evidence():
    receipt, tx = direct_frame()
    # The decoder's existing spend proof supplies initiation; receipt identity must survive.
    buy = watch.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=780.0,
                                        decimals_fn=lambda _: 18)
    assert buy["source_initiation"] == {k: v for k, v in expected_proof().items()
                                       if k != "source_user_id"}


def test_foreign_sender_spending_wallet_allowance_does_not_prove_wallet_initiation():
    receipt, tx = direct_frame()
    foreign = "0x" + "af" * 20
    tx.update(value="0x0", **{"from": foreign})
    receipt.update(**{"from": foreign})
    receipt["logs"].insert(0, dict(address=QUOTE, topics=[watch.TRANSFER_TOPIC,
        "0x" + WALLET[2:].rjust(64, "0"), "0x" + tx["to"][2:].rjust(64, "0")], data=hex(10 ** 18)))
    buy = watch.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=780.0,
                                        decimals_fn=lambda _: 18, chain="56")
    assert buy and buy["source_size_usd"] == 780.0
    assert buy["source_initiation"] is None


@pytest.mark.parametrize("frame", RUGS, ids=lambda f: str(f["position_id"]))
def test_incident_relay_payment_preserves_beneficiary_only_evidence(frame):
    proof = watch.relayer_purchase_from_receipt(frame["receipt"], frame["tx"], WALLET,
                                               chain="56", contracts_verified=True, any_route=True)
    assert proof and proof["payer"] != WALLET
    buy = watch.price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1.0)
    initiation = buy["source_initiation"]
    assert initiation["payer"] == proof["payer"]
    assert initiation["beneficiary"] == WALLET
    assert initiation["proof_version"] == proof["proof_version"]
    assert initiation["network_id"] == "56" and initiation["tx_hash"] == frame["tx"]["hash"]


@pytest.mark.parametrize("proof,reason", [
    (None, "missing_receipt_proof"), (True, "missing_receipt_proof"),
    ({}, "wallet_initiation_unproved"),
    (dict(expected_proof(), payer="0x" + "94" * 20), "receipt_identity_mismatch"),
])
@pytest.mark.parametrize("chain", ["solana", "1", "56", "8453", "4663", "5042", "9999"])
def test_unknown_initiation_stops_actual_shared_hook_before_provider_or_reservation(monkeypatch, proof, reason, chain):
    conn = _conn()
    _arm(monkeypatch)
    monkeypatch.setattr(runner.cfg, "MEMECOIN_EVM_ALLOWLIST", {chain})
    _approve(conn, chains=(chain,))
    def forbidden(*a, **k):
        pytest.fail("unknown source initiation reached a provider")
    monkeypatch.setattr(runner, "_start_evm_supply_read", forbidden)
    ctx = dict(usdc_fn=forbidden, native_fn=forbidden, token_safety_fn=forbidden,
               execute_fn=forbidden, kill_path=None)
    ev = source_event(proof, chain=chain)
    if chain == 'solana':
        from test_memecoin_solana_source_initiation import receipt
        tx, ev.source_wallet = receipt()
        ev.mint = tx['meta']['postTokenBalances'][0]['mint']
        ev.source_block_number = tx['slot']
        ev.fomo_event_id = 'onchain:' + tx['transaction']['signatures'][0]
    ev.override_risk = True
    hook = runner.make_real_hook(None, {chain: ctx}, now_fn=lambda: NOW,
                                 trade_size=30.0, max_age_s=45)
    assert hook.raw(conn, ev)["status"] == "source_initiation_unknown"
    assert conn.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    attempt = conn.execute("SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts").fetchone()
    assert attempt["terminal_status"] == "refused"
    assert attempt["terminal_reason"] == reason


def test_source_initiation_trace_is_an_explicit_refusal():
    assert runner._trace_terminal({"status": "source_initiation_unknown",
                                   "reason": "missing_receipt_proof"}) == ("refused", "missing_receipt_proof")


def test_onchain_retry_roundtrip_preserves_source_identity():
    proof = expected_proof()
    row = dict(mint=MINT, source_user_id="u1", handle="h", address=WALLET,
               network_id="56", source_size_usd=78.0, source_price=0.000078,
               signature=f"56:{TX}:{MINT}", detected_at=NOW, source_slot=16,
               source_valuation_json=json.dumps({"source_initiation": proof}))
    first = lane.OnchainEv(row)
    retry = lane.event_from_retry_payload(json.loads(json.dumps(lane.retry_payload(first))))
    assert retry.source_initiation == proof
    assert retry.source_wallet == WALLET and retry.source_block_number == 16
    assert retry.source_evidence["source_initiation"] == proof


def test_bsc_v2_datagram_preserves_source_identity():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from test_memecoin_bsc_fast_lane import ROW, _decide, _synthetic
    receipt, mint, post = _synthetic()
    row = dict(ROW, source_user_id="u1", wallet=WALLET, tx_hash=TX,
               buy=dict(ROW["buy"], mint=mint, source_initiation=expected_proof(mint=mint)))
    payload = bsc.lane_payload(row, _decide(receipt, mint, post), decimals=18,
                               bnb_usd=780.0, bnb_usd_at=1.0, sent_epoch=1.0)
    ev = bsc.BscLaneEvent(json.loads(json.dumps(payload)), market_cap=11_000.0)
    assert ev.source_initiation == row["buy"]["source_initiation"]
    assert ev.source_wallet == WALLET
    assert ev.source_block_number == payload["seq"]


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("network_id", "8453"),
    ("tx_hash", "0x" + "23" * 32), ("mint", "0x" + "95" * 20),
    ("beneficiary", "0x" + "96" * 20), ("source_user_id", "other-user"),
    ("block_number", 17), ("block_hash", "wrong"), ("block_number", None),
])
def test_proof_cannot_replay_across_an_event_identity(field, value):
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    proof = dict(expected_proof(), **{field: value})
    assert source_initiation_refusal(source_event(proof), "56") == "receipt_identity_mismatch"


@pytest.mark.parametrize("change", [
    lambda receipt, tx: tx.update(hash="0x" + "99" * 32),
    lambda receipt, tx: receipt.update(blockHash="0x" + "99" * 32),
    lambda receipt, tx: tx.update(chainId="0x2105"),
    lambda receipt, tx: receipt.update(blockNumber="0x11"),
    lambda receipt, tx: receipt.pop("from"),
])
def test_incomplete_or_mismatched_receipt_stays_observed_but_initiation_unknown(change):
    receipt, tx = direct_frame()
    change(receipt, tx)
    buy = watch.direct_buy_from_receipt(receipt, tx, WALLET, weth=QUOTE, quote_usd=780.0,
                                        decimals_fn=lambda _: 18, chain="56")
    assert buy and buy["source_initiation"] is None


@pytest.mark.parametrize("source,chain", [("manual", "56"), ("global_feed", "56")])
def test_other_authoritative_paths_do_not_require_bsc_receipt_initiation(source, chain):
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    ev = source_event(None, source=source, chain=chain)
    if source == "global_feed":
        ev.fomo_event_id = "j7-event-1"
    assert source_initiation_refusal(ev, chain) is None


@pytest.mark.parametrize("market_cap", [11_000.0, 100.0, 10_000_000.0])
@pytest.mark.parametrize("chain", ["1", "56", "8453", "4663", "5042"])
def test_direct_wallet_paid_lowcap_buy_reaches_shared_funded_hook(monkeypatch, market_cap, chain):
    conn = _conn()
    _arm(monkeypatch)
    monkeypatch.setattr(runner.cfg, "MEMECOIN_EVM_ALLOWLIST", {chain})
    _approve(conn, chains=(chain,))
    reached = []
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **k: reached.append(k) or {"status": "fixture_admitted"})
    ctx = dict(usdc_fn=lambda: 100.0, native_fn=lambda: 1.0,
               token_safety_fn=lambda *a, **k: (True, None), execute_fn=lambda *a: None)
    ev = source_event(dict(expected_proof(), network_id=chain), chain=chain)
    ev.their_market_cap = market_cap
    hook = runner.make_real_hook(None, {chain: ctx}, now_fn=lambda: NOW,
                                 trade_size=30.0, max_age_s=45)
    assert hook.raw(conn, ev)["status"] == "fixture_admitted"
    assert reached


@pytest.mark.parametrize("frame", RUGS, ids=lambda f: str(f["position_id"]))
def test_actual_rug_receipt_is_retained_and_refused_before_provider_work(monkeypatch, frame):
    proof = watch.relayer_purchase_from_receipt(frame["receipt"], frame["tx"], WALLET,
                                               chain="56", contracts_verified=True, any_route=True)
    buy = watch.price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1.0)
    initiation = dict(buy["source_initiation"], source_user_id="u1")
    row = dict(mint=buy["mint"], source_user_id="u1", handle="Frank", address=WALLET,
               network_id="56", source_size_usd=buy["source_size_usd"], source_price=buy["source_price"],
               signature=f"56:{frame['tx']['hash']}:{buy['mint']}", detected_at=NOW,
               source_slot=int(frame["receipt"]["blockNumber"], 16),
               source_valuation_json=json.dumps({"source_initiation": initiation}))
    ev = lane.OnchainEv(row)
    assert ev.source_initiation["payer"] != ev.source_wallet
    def forbidden(*a, **k):
        pytest.fail("beneficiary-only receipt reached funded provider work")
    monkeypatch.setattr(runner, "_start_evm_supply_read", forbidden)
    ctx = dict(usdc_fn=forbidden, native_fn=forbidden, token_safety_fn=forbidden, execute_fn=forbidden)
    conn = _conn()
    hook = runner.make_real_hook(None, {"56": ctx}, now_fn=lambda: NOW, trade_size=30.0, max_age_s=45)
    assert hook.attempt(conn, ev)["status"] == "source_initiation_unknown"
    assert conn.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    attempt = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert attempt["terminal_status"] == "refused" and attempt["network_id"] == "56"
    assert attempt["mint"] == buy["mint"] and attempt["origin_event_id"] == ev.fomo_event_id
    assert json.loads(attempt["trace_json"])["source_evidence"]["source_initiation"] == initiation


def test_watch_detection_binds_uid_and_persists_proof_without_new_provider_reads(monkeypatch):
    import sqlite3
    import scripts.research.onchain_evm_wallet_watch as script
    from scripts.research.onchain_wallet_watch import SCHEMA
    from sniper.services import memecoin_evm_post_trade as post
    receipt, tx = direct_frame()
    log = dict(receipt["logs"][0], transactionHash=TX, blockNumber="0x10", blockTimestamp="0x5")
    calls = []
    def read(method, params):
        calls.append(method)
        return {"eth_getTransactionByHash": tx, "eth_getTransactionReceipt": receipt}[method]
    monkeypatch.setattr(script, "CHAIN", "56")
    monkeypatch.setattr(script, "WETH", QUOTE)
    monkeypatch.setattr(post, "source_post_trade", lambda *a, **k: {})
    row = script.push_detection("unused", log, {WALLET: ("u1", "h")},
                               decimals_cache={MINT: 18}, quote_usd=780.0, now=NOW, read_fn=read)
    assert row["buy"]["source_initiation"] == expected_proof()
    assert sorted(calls) == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    script._record(conn, **row, valuation={"evm_post": {"venue": "v2"}})
    stored = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert json.loads(stored["source_valuation_json"])["source_initiation"] == expected_proof()
    ev = lane.OnchainEv(stored)
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    assert source_initiation_refusal(ev, "56") is None


@pytest.mark.parametrize("frame", RUGS, ids=lambda f: str(f["position_id"]))
def test_relay_observation_is_still_persisted(frame, monkeypatch):
    import sqlite3
    import scripts.research.onchain_evm_wallet_watch as script
    from scripts.research.onchain_wallet_watch import SCHEMA
    monkeypatch.setattr(script, "CHAIN", "56")
    proof = watch.relayer_purchase_from_receipt(frame["receipt"], frame["tx"], WALLET,
                                               chain="56", contracts_verified=True, any_route=True)
    buy = watch.price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1.0)
    buy["source_initiation"]["source_user_id"] = "u1"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    script._record(conn, tx_hash=frame["tx"]["hash"], wallet=WALLET, source_user_id="u1",
                   handle="Frank", buy=buy, log={"blockNumber": frame["receipt"]["blockNumber"]}, now=NOW)
    row = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert row["mint"] == buy["mint"] and row["source_size_usd"] > 0
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    assert source_initiation_refusal(lane.OnchainEv(row), "56") == "wallet_initiation_unproved"


def test_v3_datagram_retry_roundtrip_keeps_bound_initiation():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    payload = dict(route_kind="v3_shared", mint=MINT, tx_hash=TX, pool="0x" + "c3" * 20,
                   wallet=WALLET, source_user_id="u1", seq=16, post_ratio=1.1,
                   paid_usd=78.0, source_price=.000078, arrival_utc=NOW,
                   source_initiation=expected_proof())
    fast = bsc.BscLaneEvent(json.loads(json.dumps(payload)), market_cap=None)
    retried = runner._retry_event_from_json(json.dumps(runner._retry_payload(fast)))
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    assert retried.source_initiation == expected_proof()
    assert source_initiation_refusal(retried, "56") is None
    retried.source_wallet = "0x" + "fe" * 20
    assert source_initiation_refusal(retried, "56") == "receipt_identity_mismatch"


@pytest.mark.parametrize("fast", [False, True])
def test_sicat_70895_beneficiary_delivery_stops_before_funded_work(monkeypatch, fast):
    from sniper.services import memecoin_rh_fast_lane as rh
    wallet = "0x92bff72e6c943a1348aedf15d68e758e811dec64"
    mint = "0x6535b6f6e925801a820d9e403e118ebe2ea9dc14"
    tx = "0x948aa3b74437ea801c96928fd64549aa147eeab35cf772da57ca6098a7b0092c"
    uid = "2e955ffc-6fac-578a-93d8-3324605f6bed"
    proof = dict(schema_version=1, proof_version="relay-envelope-other-target-v1",
        network_id="4663", tx_hash=tx,
        block_hash="0x5368ae97d5abc0c3c7ee94e6602152dd3a5c75fe37d6dfd3a2a360b4497858a5",
        block_number=81674717, mint=mint, beneficiary=wallet,
        payer="0xf70da97812cb96acdf810712aa562db8dfa3dbef",
        acquired_atomic="2064719132493901", source_user_id=uid)
    row = dict(mint=mint, source_user_id=uid, handle="change", address=wallet,
        network_id="4663", source_size_usd=233.48110355460003, source_price=1.1308129027340706e-05,
        signature=f"4663:{tx}:{mint}", detected_at=NOW, source_slot=81674717,
        source_valuation_json=json.dumps({"source_initiation": proof}))
    ev = lane.OnchainEv(row)
    if fast:
        fill = dict(proof, proof_version="wallet-net-fill-v1", receipt_proof_version=proof["proof_version"],
                    wallet=wallet, token_decimals=8, paid_usd=row["source_size_usd"])
        ev = rh.LaneEvent(dict(tx_hash=tx, seq=81674717, wallet=wallet, mint=mint,
            source_user_id=uid, handle="change", arrival_utc=NOW, source_fill=fill,
            route_kind="v3_shared"), decimals=8, supply=None)
    def forbidden(*a, **k):
        pytest.fail("SICAT beneficiary delivery reached funded provider work")
    monkeypatch.setattr(runner, "_start_evm_supply_read", forbidden)
    ctx = dict(usdc_fn=forbidden, native_fn=forbidden, token_safety_fn=forbidden, execute_fn=forbidden)
    conn = _conn()
    hook = runner.make_real_hook(None, {"4663": ctx}, now_fn=lambda: NOW, trade_size=50., max_age_s=45)
    result = hook.attempt(conn, ev)
    assert result["status"] == "source_initiation_unknown"
    assert result["reason"] == "wallet_initiation_unproved"
    assert conn.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_new_evm_chain_inherits_the_same_receipt_binding():
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    proof=dict(expected_proof(), network_id="9999")
    assert source_initiation_refusal(source_event(proof, chain="9999"), "9999") is None
