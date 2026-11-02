"""Base 8453 step 1 part C: the 8453 watcher's datagram, from a real Base source buy, through the
runner's lane to a booked fill (plan 2026-10-14; Alpha's preconf ruling 2026-10-14).

Fixture: 0xda538110 (degen_ak), a Relay buy proved from the user's Solana deposit, v4_native on
pool 0xaf861cf0, key from the store; receipt and transaction as Base served them.
"""
import copy
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.execution.evm_v4_quote import V4
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionTransport
from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_base_fast_decide as decide
from sniper.services import memecoin_base_fast_lane as base
from sniper.services import memecoin_base_preconf as preconf
from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.services import memecoin_rh_fast_lane as rh
from sniper.services.memecoin_evm_post_trade import source_post_trade
from sniper.settings import memecoin as cfg

F = json.loads((Path(__file__).parent / "fixtures/memecoin/base_relay_v4_native_da5381.json").read_text())
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ZERO = "0x" + "00" * 32


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "runner_shadow")
    monkeypatch.setattr(cfg, "MEMECOIN_BASE_FAST_ROUTES", ("v4_native", "v4_weth"))
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("8453",))


def _frame(preconf_read=False):
    receipt, tx = copy.deepcopy(F["receipt"]), copy.deepcopy(F["transaction"])
    init = dict(F["source_initiation"])
    if preconf_read:                       # the Flashblocks shape: zero hash, null tx block hash
        receipt["blockHash"] = ZERO
        for log in receipt["logs"]:
            log["blockHash"] = ZERO
        tx["blockHash"] = None
        init["block_hash"] = ZERO
    proof = watch.relayer_purchase_from_receipt(receipt, tx, F["wallet"], chain="8453",
                                                contracts_verified=True, any_route=True,
                                                preconfirmed=preconf_read)
    buy = watch.price_relayer_purchase(proof, decimals_fn=lambda t: 6 if t.lower() == USDC else 18,
                                       price_fn=lambda t: 1.0)
    buy["source_initiation"] = init
    buy["source_post"] = dict(source_post_trade("8453", receipt, F["mint"], owner=F["wallet"],
                                                pool_key_fn=lambda chain, pool: F["key"]))
    row = dict(tx_hash=tx["hash"], wallet=F["wallet"], source_user_id=F["source_user_id"],
               handle=F["handle"], now=F["detected_at"], buy=buy)
    return row, tx, receipt


def _hook(sent, retracted=None):
    writer = type("W", (), {"put": lambda self, rec: None})()
    return decide.make_hook(writer, key_fn=lambda chain, pool: F["key"], clip_usd_fn=lambda: F["clip_usd"],
                            send_fn=lambda p: sent.append(json.loads(json.dumps(p))) or True,
                            host_clock=lambda: 1000.0, retracted_fn=lambda p: retracted)


@pytest.mark.parametrize("preconf_read", [False, True])
def test_a_real_base_relay_buy_becomes_a_datagram_the_runner_admits(preconf_read):
    sent = []
    row, tx, receipt = _frame(preconf_read)
    rec = _hook(sent)(row, tx, receipt, F["eth_usd"], 1.0, "cache")
    assert rec["reason"] == "would_send" and rec.get("lane_sent") is True, rec
    payload, = sent
    assert payload["preconf"] is preconf_read and payload["route_kind"] == "v4_native"
    assert payload["source_fill"]["block_hash"] == (ZERO if preconf_read else F["receipt"]["blockHash"])
    assert base.check_sender(payload, 7, os.getuid(), watcher_pid=7) is None
    assert rh.receipt_refusal(payload, "v4_native") is None
    ev = rh.LaneEvent(payload, decimals=18, supply=10 ** 27, chain="8453", delivery_lane="base_fast_lane")
    assert ev.fomo_event_id == "rhfast:8453:%s:%s" % (F["transaction"]["hash"], F["mint"])
    assert ev.source_evidence.get("source_observation_state") == (
        "preconfirmed" if preconf_read else None)


@pytest.mark.parametrize("mode,routes,retracted,skip", [
    ("step0", ("v4_native",), None, None), ("runner_shadow", ("v4_weth",), None, "route_off:v4_native"),
    ("runner_shadow", ("v4_native",), "source_retracted", "source_retracted")])
def test_nothing_is_sent_off_a_runner_mode_an_enabled_route_or_for_a_proven_phantom(
        monkeypatch, mode, routes, retracted, skip):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", mode)
    monkeypatch.setattr(cfg, "MEMECOIN_BASE_FAST_ROUTES", routes)
    sent = []
    row, tx, receipt = _frame(preconf_read=True)
    rec = _hook(sent, retracted=retracted)(row, tx, receipt, F["eth_usd"], 1.0, "cache")
    assert sent == [] and rec.get("lane_skip") == skip and "lane_sent" not in rec


def test_a_foreign_user_s_deposit_proof_never_reaches_the_runner():
    sent = []
    row, tx, receipt = _frame()
    row["source_user_id"] = "someone-else"
    rec = _hook(sent)(row, tx, receipt, F["eth_usd"], 1.0, "cache")
    assert sent == [] and rec["lane_skip"] == "source_fill_unproved"


def test_the_lane_s_presign_check_refuses_a_retracted_preconfirmed_source(tmp_path):
    """Alpha's ruling: a retraction observed before the send aborts it, the ordinary path's rule."""
    from scripts.research.onchain_wallet_watch import SCHEMA
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    signature = "8453:%s:%s" % (F["transaction"]["hash"], F["mint"])
    conn.execute("INSERT INTO onchain_wallet_detections(signature, network_id, mint, source_user_id, "
                 "address, detected_at, created_at, source_observation_state) "
                 "VALUES (?, '8453', ?, 'u', ?, 'x', 'x', 'retracted')", (signature, F["mint"], F["wallet"]))
    conn.commit()
    check = preconf.presign_check("rhfast:" + signature, ledger_path=str(path))
    assert check is not None and check() == "source_retracted"
    conn.execute("UPDATE onchain_wallet_detections SET source_observation_state='canonical'")
    conn.commit()
    assert preconf.presign_check("rhfast:" + signature, ledger_path=str(path))() is None
    assert preconf.presign_check("rhfast:4663:%s:%s" % (F["transaction"]["hash"], F["mint"])) is None


# --- the live lane, from the real datagram to a booked 8453 fill -----------------------------------

ACCT = Account.from_key(b"\x07" * 32)
PM, UR = V4["8453"]["pool_manager"].lower(), V4["8453"]["universal_router"].lower()
BLOCK = "0x" + "be" * 32
SNAP = dict(native_wei=10 ** 17, weth_wei=0, nonce=11, gas_price_wei=5_000_000, read_at=1000.5)


class Chain:
    """Receipt, transaction and block answers for whatever raw the transport carried."""

    def __init__(self, got=4 * 10 ** 23):
        self.raw, self.got = None, got

    def read(self, method, params):
        if self.raw is None:
            return None
        h = "0x" + keccak(hexstr=self.raw).hex()
        if params[0] not in (h, "0x2100000"):
            return None
        transfer = {"address": F["mint"], "topics": [watch.TRANSFER_TOPIC, "0x" + "0" * 24 + PM[2:],
                                                     "0x" + "0" * 24 + ACCT.address.lower()[2:]],
                    "data": hex(self.got), "transactionHash": h, "blockHash": BLOCK, "logIndex": "0x3"}
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": h, "blockHash": BLOCK, "blockNumber": "0x2100000", "status": "0x1",
                    "transactionIndex": "0x9", "from": ACCT.address.lower(), "to": UR, "logs": [transfer]}
        if method == "eth_getTransactionByHash":
            return {"hash": h, "from": ACCT.address.lower(), "blockHash": BLOCK, "blockNumber": "0x2100000",
                    "to": UR, "value": hex(rh.signed_value(self.raw))}
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK, "number": "0x2100000", "timestamp": hex(1790900000)}
        return None


class Provider:
    def __init__(self, chain):
        self.provider_id, self.chain, self.requests = "official-8453", chain, []

    def submit(self, request):
        self.requests.append(request)
        self.chain.raw = request.signed_payload
        return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED, request.tx_identity, 1.0, "200")


def _live(conn, transport, chain, tmp_path, **kw):
    from sniper.execution.evm_real_seams import sign_tx
    claimed, released = [], []
    args = dict(
        ctx={"address": ACCT.address, "sign_tx_fn": lambda tx: sign_tx(tx, ACCT),
             "lane_claim_nonce_fn": lambda n: claimed.append(n) or n,
             "lane_release_nonce_fn": released.append, "gasprice_buffer": 1.2,
             "submission_transport": transport},
        conn=conn, sender=None, fanout_fn=lambda raw: None, rpc_read=chain.read,
        facts_fn=lambda mint: (18, 10 ** 27), lock_key="base-test", lock_dir=str(tmp_path),
        clip_usd=50.0, low_wei=10 ** 15, generation_fn=lambda: 900.0, host_clock=lambda: 1001.0,
        max_snapshot_age_s=30.0, eth_usd_max_age_s=60.0, generation_margin_s=1.0, pilot_fills=20,
        max_sends_per_hour=6, require_exact_state=False, receipt_timeout_s=1.0, receipt_poll_s=0.01,
        lock_receipt_cap_s=0.2, sleep=lambda s: None, alert_fn=lambda t: None)
    args.update(kw)
    live = base.BaseLive(**args)
    live._claimed, live._released = claimed, released
    return live


def _reserve(conn, payload):
    return db.reserve_pending(conn, mint=payload["mint"], source_user_id=payload["source_user_id"],
                              strategy="alpha_b", notional=50.0, reserved_at="2026-10-14T00:00:00+00:00",
                              network_id="8453", signal_source="rh_fast_lane",
                              origin_event_id="rhfast:8453:%s:%s" % (payload["tx_hash"], payload["mint"]))


@pytest.mark.parametrize("retracted", [False, True])
def test_the_live_base_lane_books_its_8453_fill_or_aborts_a_phantom_before_signing(tmp_path, retracted):
    sent = []
    row, tx, receipt = _frame(preconf_read=True)
    _hook(sent)(row, tx, receipt, F["eth_usd"], 1.0, "cache")
    payload = dict(sent[0], snapshot=SNAP, eth_usd_at=1000.0)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    chain = Chain()
    provider = Provider(chain)
    transport = SubmissionTransport([provider], deadline_s=.5)
    live = _live(conn, transport, chain, tmp_path)
    try:
        pending_id = _reserve(conn, payload)
        rec = {"_perf0": time.perf_counter()}
        result = live.execute_reserved(payload, rec, conn, pending_id, 50.0, decimals=18,
                                       presign_check_fn=lambda: "source_retracted" if retracted else None)
        if retracted:
            assert result["status"] == "aborted" and result["reason"] == "source_retracted"
            assert provider.requests == [] and live._claimed == []
            assert db.pending_row(conn, pending_id)["status"] != "reserved"
            return
        assert result["status"] == "filled", (result, rec)
        request = provider.requests[0]
        assert request.chain_id == "8453" and live._claimed == [11]
        pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
        assert pos["network_id"] == "8453" and pos["signal_source"] == "rh_fast_lane"
        assert pos["entry_notional"] == pytest.approx(50.0, rel=1e-6)
        assert rec["_outcome"] == "filled"
        assert (rec["execution_evidence"]["public_transaction"]["to"] or "").lower() == UR
    finally:
        transport.close()
        conn.close()


def test_the_funded_leg_hands_a_base_lane_event_s_executor_the_presign_check(monkeypatch, tmp_path):
    from sniper.services import memecoin_evm_leg as leg
    from test_evm_early_cached_sizing import Marks
    from test_memecoin_evm_leg import _c, _Ev
    c, kill = _c(monkeypatch, tmp_path)
    got = []

    def execute_fn(conn, pending_id, mint, trade_size, now_iso, trace=None, presign_check_fn=None):
        got.append(presign_check_fn)
        return {"status": "filled", "position_id": pending_id}
    ev = _Ev()
    ev.fomo_event_id = "rhfast:8453:%s:%s" % (F["transaction"]["hash"], ev.mint)
    trace = Marks()
    trace.mark = lambda *a, **k: None
    leg.evm_real_entry(c, ev, route="evm:8453", day="2026-10-14", usdc_balance=300.0, native_balance=1.0,
                       now_iso="2026-10-14T00:00:00Z", trade_size=10.0,
                       token_safety_fn=lambda mint, **k: (True, "safe"), execute_fn=execute_fn,
                       kill_path=kill, trace=trace)
    assert len(got) == 1 and callable(got[0])


def test_cached_only_is_4663_alone_a_base_miss_still_reads_inline(monkeypatch, tmp_path):
    """Gamma LOW 1 on 96bcf06de: nothing pinned cached-only to 4663. BaseLive inherits
    LiveLane.handle and has an inline host, so with the knob on a Base cache miss still reads inline."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)
    sent = []
    row, tx, receipt = _frame(preconf_read=True)
    _hook(sent)(row, tx, receipt, F["eth_usd"], 1.0, "cache")
    payload = dict(sent[0], snapshot=SNAP, eth_usd_at=1000.0)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    chain = Chain()
    transport = SubmissionTransport([Provider(chain)], deadline_s=.5)
    read = []
    cache = rh.SafetyCache(lambda m, usd=None: (True, None), ttl_s=60, usd=50)
    live = _live(conn, transport, chain, tmp_path, cache=cache,
                 inline_safety_fn=lambda m, usd=None: read.append("safety") or (False, "test stops here"),
                 inline_facts_fn=lambda m: read.append("facts") or (18, 10 ** 27),
                 stats_fn=lambda m: {"price": 1.0},
                 routes=(payload.get("route_kind") or "v4_native",))
    try:
        assert live._cached_only() is False
        rec = live.handle(payload, recv_epoch=1001.0, safety_reason="no_safety_verdict", stats=None)
        assert "facts" in read, ("a Base miss must still read its inputs inline", rec.get("reason"))
    finally:
        live._safety_pool.shutdown(wait=True)
        live._inputs_pool.shutdown(wait=True)
        transport.close()
        conn.close()
