"""The shared native EVM fast lane learns its chain (plan 2026-10-14 Base 8453 step 1, part A).

The 4663 LiveLane already executes the BSC lane (CHAIN_ID 56). The Base lane reuses it as CHAIN_ID
8453 with signal_source rh_fast_lane and origin rhfast:8453:<tx>:<mint>. Every site that hard-coded
4663 now reads the lane's chain; 4663 itself is unchanged (its own suites).
"""
import sqlite3
from fractions import Fraction
from types import SimpleNamespace

import pytest

from sniper.execution import evm_swap_executor as executor
from sniper.execution.evm_v4_quote import V4
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_rh_fast_lane as lane
from sniper.services import memecoin_source_initiation as initiation

TX = "0x" + "ab" * 32
MINT = "0x" + "cd" * 20
WALLET = "0x" + "ef" * 20
BLOCK_HASH = "0x" + "12" * 32
NATIVE = "0x" + "00" * 20
KEY = {"currency0": NATIVE, "currency1": MINT, "fee": 0, "tick_spacing": 60, "hooks": NATIVE}


def _payload(**kw):
    sqrt = 1 << 96
    return dict(dict(tx_hash=TX, seq=5, wallet=WALLET, mint=MINT, source_user_id="u1", handle="h",
                     arrival_utc="2026-10-14T00:00:00+00:00", sent_epoch=1000.0, eth_usd=2500.0,
                     eth_usd_at=1000.0, post_sqrt=sqrt, src_in_wei=10 ** 15, src_out=10 ** 18,
                     paid_usd=2.5, key=KEY, cap_num=2, cap_den=1, clip_wei=10 ** 16,
                     post_wei_num=1, post_wei_den=1, route_kind="v4_native"), **kw)


def test_a_lane_event_names_its_own_chain_and_4663_stays_the_default():
    base = lane.LaneEvent(_payload(), decimals=18, supply=10 ** 27, chain="8453",
                          delivery_lane="base_fast_lane")
    assert base.network_id == "8453" and base.delivery_lane == "base_fast_lane"
    assert base.fomo_event_id == "rhfast:8453:%s:%s" % (TX, MINT)
    assert base.source_evidence["network_id"] == "8453"
    assert base.evm_chase_reference.chain == "8453"
    rh = lane.LaneEvent(_payload(), decimals=18, supply=10 ** 27)
    assert rh.network_id == "4663" and rh.delivery_lane == "rh_fast_lane"
    assert rh.fomo_event_id == "rhfast:4663:%s:%s" % (TX, MINT)


@pytest.mark.parametrize("kind", ["v4_native", "v4_weth"])
def test_the_lane_buy_goes_to_its_own_chain_s_universal_router(kind):
    payload = _payload(route_kind=kind, in_is_c0=True)
    if kind == "v4_weth":
        weth = "0x4200000000000000000000000000000000000006"
        payload["key"] = dict(KEY, currency0=weth)
    tx, _ = lane.lane_buy_tx(payload, 10 ** 16, 2_000_000_000, WALLET, chain="8453")
    assert tx["to"] == V4["8453"]["universal_router"]
    if kind == "v4_native":
        default, _ = lane.lane_buy_tx(payload, 10 ** 16, 2_000_000_000, WALLET)
        assert default["to"] == V4["4663"]["universal_router"]


def _direct_proof(chain):
    return dict(schema_version=1, proof_version=initiation.DIRECT_PROOF_VERSION, network_id=chain,
                tx_hash=TX, block_hash=BLOCK_HASH, block_number=5, mint=MINT, beneficiary=WALLET,
                payer=WALLET, source_user_id="u1")


@pytest.mark.parametrize("chain,admitted", [("8453", True), ("4663", True), ("56", False)])
def test_the_native_lane_origin_is_admitted_on_its_two_chains_only(chain, admitted):
    ev = SimpleNamespace(signal_source="rh_fast_lane", fomo_event_id="rhfast:%s:%s:%s" % (chain, TX, MINT),
                         mint=MINT, source_user_id="u1", source_wallet=WALLET, source_block_number=5,
                         source_initiation=_direct_proof(chain))
    got = initiation.source_initiation_refusal(ev, chain)
    assert (got is None) == admitted, got


def test_a_receipt_backed_deposit_proof_is_admitted_on_8453_with_its_chain_listed(monkeypatch):
    from sniper.settings import memecoin as cfg
    proof = dict(_direct_proof("8453"), proof_version=initiation.RELAY_DEPOSIT_PROOF_VERSION,
                 payer="0x" + "f7" * 20, acquired_atomic="1000", origin_network_id="solana",
                 origin_wallet="A" * 44, origin_tx="5" * 88, origin_slot=7, origin_amount_atomic="9",
                 order_id="0x" + "77" * 32, origin_identity_user_id="u1")
    ev = SimpleNamespace(signal_source="rh_fast_lane", fomo_event_id="rhfast:8453:%s:%s" % (TX, MINT),
                         mint=MINT, source_user_id="u1", source_wallet=WALLET, source_block_number=5,
                         source_initiation=proof)
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("8453",))
    assert initiation.source_initiation_refusal(ev, "8453") is None
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ())
    assert initiation.source_initiation_refusal(ev, "8453") is not None


@pytest.mark.parametrize("chain", ["8453", "4663"])
def test_a_lane_booking_reads_its_own_chain_s_pool_manager(monkeypatch, chain):
    seen = {}
    monkeypatch.setattr(executor, "_receipt_is_canonical", lambda *a: True)
    monkeypatch.setattr(executor, "lane_receipt_amounts",
                        lambda *a, pool_manager=None: seen.setdefault("pm", pool_manager) and None)
    pending = {"network_id": chain, "evm_quote_asset_usd": 2500.0, "mint": MINT}
    executor.recover_confirmed_lane_entry(None, pending, {}, {}, {}, owner=WALLET,
                                          decimals_fn=lambda _m: 18, network_id=chain)
    assert seen["pm"] == V4[chain]["pool_manager"]


def test_the_pilot_counts_one_chain_s_lane_fills_when_asked():
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    for chain in ("4663", "8453", "8453"):
        conn.execute("INSERT INTO memecoin_positions (mint, network_id, signal_source, execution_mode, "
                     "opened_at, entry_price, source_user_id) "
                     "VALUES (?, ?, 'rh_fast_lane', 'real', '2026-10-14', 1, 'u')",
                     ("0x%040x" % (conn.total_changes + 1), chain))
    assert lane._seed_pilot(conn)[0] == 3
    assert lane._seed_pilot(conn, network_id="4663")[0] == 1
    assert lane._seed_pilot(conn, network_id="8453")[0] == 2


class _Base(lane.LiveLane):
    CHAIN_ID = 8453
    DELIVERY_LANE = "base_fast_lane"
    SAFETY_LABEL = "base_fast_safety_cache"


def _base_live(transport=object()):
    return _Base(ctx={"address": WALLET, "submission_transport": transport}, conn=None, sender=None,
                 fanout_fn=lambda raw: None, rpc_read=None, facts_fn=lambda mint: (18, 10 ** 27),
                 lock_key="k", lock_dir=None, clip_usd=50.0, low_wei=0, generation_fn=lambda: 0.0,
                 host_clock=lambda: 1000.0, max_snapshot_age_s=30.0, eth_usd_max_age_s=120.0,
                 generation_margin_s=0.0, pilot_fills=20, max_sends_per_hour=6,
                 require_exact_state=False, lock_receipt_cap_s=1.0, contexts={"8453": {}})


def test_a_base_lane_submits_through_its_context_transport_and_runs_its_own_context():
    transport = object()
    live = _base_live(transport)
    assert live.submission_transport is transport and live.sender is None
    live.cache = SimpleNamespace(cached_answer=lambda mint, label: (True, label))
    ctx = live._lane_context(dict(_payload(), snapshot={"weth_wei": 0, "native_wei": 10 ** 17}),
                             execute_fn=lambda *a: None)
    assert set(ctx) == {"8453"}
    assert ctx["8453"]["token_safety_fn"](MINT) == (True, "base_fast_safety_cache")
    ev = live._lane_event(_payload(), decimals=18, supply=10 ** 27)
    assert ev.network_id == "8453" and ev.delivery_lane == "base_fast_lane"


def test_a_native_price_binding_reads_the_payload_chain_s_quote_token():
    weth = "0x4200000000000000000000000000000000000006"
    proof = {"source_post": {"mint_is_c1": True, "pool": None, "sqrt_price_x96": str(1 << 96)},
             "mint": MINT, "payment_token": weth, "payment_decimals": 18, "payment_atomic": "1000",
             "paid_usd": 2.5, "acquired_atomic": str(10 ** 18)}
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    proof["source_post"]["pool"] = v4_pool_id(KEY)
    payload = _payload(chain="8453", src_in_wei=1000)
    payload["cap_num"], payload["cap_den"] = 1, 1
    assert lane.native_source_price_matches(payload, proof) is True
    # The same WETH payment on a 4663 datagram is a foreign token: priced from USD, so 1000 wei
    # no longer binds.
    assert lane.native_source_price_matches(dict(payload, chain="4663"), proof) is False


def test_the_ordinary_lane_waits_while_the_base_lane_holds_the_same_buy(monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone
    from scripts.research.onchain_wallet_watch import SCHEMA
    from sniper.services import memecoin_onchain_lane as onchain
    now = datetime(2026, 10, 14, 6, 0, 0, tzinfo=timezone.utc)
    arrival = (now - timedelta(seconds=0.1)).isoformat()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,"
                 "network_id,mint,source_slot,source_size_usd,source_price,detected_at,created_at,"
                 "source_valuation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("8453:%s:%s" % (TX, MINT), "u1", "u1", WALLET, "8453", MINT, 16, 100.0, 1e-6,
                  arrival, arrival, json.dumps({})))
    held = lane.LaneHolds(bound_s=1.0, clock=lambda: 100.0, wake_fn=lambda: None)
    monkeypatch.setattr(lane, "LANE_HOLDS", held)
    held.hold(TX, MINT)
    calls = []
    _cursor, results = onchain.sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}, cursor=0,
        now_fn=lambda: now.isoformat(), max_age_s=45, network_id="8453", deferred={},
        approved_ids_fn=lambda _conn, chain: {"u1"} if chain == "8453" else set())
    assert calls == [] and results[0]["result"]["reason"] == "rh_fast_lane_in_flight"
