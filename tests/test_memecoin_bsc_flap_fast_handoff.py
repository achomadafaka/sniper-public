"""Captured Flap receipt reaches only the unchanged funded shared hook; no network/signing."""
import copy
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import decode, encode

from scripts.research import onchain_evm_wallet_watch as watcher
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_onchain_watch as watch
from sniper.services import memecoin_source_initiation as initiation
from sniper.settings import memecoin as cfg

FRAME = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_flap_source_73001.json").read_text())
MINT = "0xe832cde0fd4a50807099b28adf6641c71a257777"
PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
BOUGHT = "0xa800a2038683844fac66747f771bfdfae862eb28b16bcfa387afa9fbacce8ff7"
NOW = "2026-10-10T12:49:15.616315+00:00"
EPOCH = datetime.fromisoformat(NOW).timestamp()
AMOUNT = 19310927840580013240139818


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
    return dict(tx_hash=tx["hash"], wallet=tx["from"], source_user_id="china_sniper_1",
                handle="china_sniper_1", buy=buy, log=receipt["logs"][-1], now=NOW)


def _offer(frame, row=None, *, mode="live", clock=lambda: EPOCH + .1, send_ok=True):
    sent = []
    signal = bsc.make_watcher_signal(mode, "test", clock=clock,
                                   send_fn=lambda address, payload: sent.append(payload) or send_ok)
    result = watcher._signal_bsc_lane(signal, _row() if row is None else row,
        dict(_receipt=frame["receipt"], _transaction=frame["transaction"],
             evm_post={"reason": "no_venue_leg"}, decimals=18), 1000, .1)
    return result, sent


def _payload():
    result, sent = _offer(FRAME)
    assert result["sent"] is True, result
    return sent[0]


def _trade(frame):
    return next(log for log in frame["receipt"]["logs"] if log["address"] == PORTAL and log["topics"] == [BOUGHT])


def test_captured_73001_native_paid_aggregator_flap_buy_reaches_shared_lane():
    result, sent = _offer(FRAME)
    assert result["reason"] == "would_send" and result["sent"] is True, result
    payload, = sent
    assert payload["route_kind"] == "flap_shared"
    assert payload["pool"] == PORTAL and payload["quote"] is None
    assert payload["paid_usd"] == 100 and payload["source_price"] == _row()["buy"]["source_price"]
    assert payload["source_initiation"] == _row()["buy"]["source_initiation"]
    assert payload["payment_basis"] == "transaction_value_upper_bound"
    assert payload["arrival_utc"] == NOW and payload["tx_hash"] == FRAME["transaction"]["hash"]
    assert not {"post_ratio", "r_coin", "r_quote", "cap_num", "tax_num"}.intersection(payload)
    assert bsc.check_sender(payload, 123, os.getuid(), watcher_pid=123) is None


def test_direct_portal_owner_actor_needs_the_same_exact_wallet_payment_and_output():
    frame = copy.deepcopy(FRAME)
    tx, receipt = frame["transaction"], frame["receipt"]
    tx["to"] = receipt["to"] = PORTAL
    event = _trade(frame)
    words = list(decode(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"],
                        bytes.fromhex(event["data"][2:])))
    words[2] = tx["from"]
    event["data"] = "0x" + encode(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"], words).hex()
    outputs = [log for log in receipt["logs"] if log["address"] == MINT and log["topics"][0] == watch.TRANSFER_TOPIC]
    outputs[0]["topics"][2] = "0x" + "00" * 12 + tx["from"][2:]
    receipt["logs"].remove(outputs[1])
    result, sent = _offer(frame, _row(frame))
    assert result["sent"] is True and sent[0]["quote"] is None


def test_ws_source_log_optional_metadata_can_differ_from_the_same_receipt_event():
    row = _row(); row["log"] = dict(row["log"])
    row["log"].pop("blockTimestamp", None)
    row["log"]["subscription"] = "delivery-metadata"
    result, sent = _offer(FRAME, row)
    assert result["sent"] is True and sent[0]["arrival_utc"] == NOW


@pytest.mark.parametrize("field,value", [("logIndex", "0x999"), ("transactionHash", "0x"+"11"*32),
    ("blockHash", "0x"+"11"*32), ("blockNumber", "0x1"), ("address", "0x"+"11"*20),
    ("topics", [BOUGHT]), ("data", "0x01"), ("removed", True)])
def test_ws_source_log_event_identity_or_contents_cannot_disagree(field, value):
    row = _row(); row["log"] = dict(row["log"], **{field:value})
    result, sent = _offer(FRAME, row)
    assert not result["sent"] and sent == []


@pytest.mark.parametrize("field,value", [("payment_atomic", "1"), ("acquired_atomic", str(AMOUNT-1)),
    ("payment_token", bsc.WBNB), ("payment_basis", "wallet_quote_outflow"), ("payment_decimals", 6),
    ("token_decimals", None), ("receipt_proof_version", "unknown"), ("source_size_usd", float("nan")),
    ("source_price", float("inf")), ("source_price", 100), ("source_price", True)])
def test_cached_buy_quantities_units_and_average_cannot_disagree_with_the_receipt(field, value):
    row = _row(); row["buy"][field] = value
    result, sent = _offer(FRAME, row)
    assert not result["sent"] and sent == []


def test_boolean_zero_decimals_is_unknown_in_the_cached_buy():
    row = _row(); row["buy"]["token_decimals"] = False
    row["buy"]["source_price"] = row["buy"]["source_size_usd"] / AMOUNT
    signal = bsc.make_watcher_signal("live", "test", clock=lambda: EPOCH+.1,
                                    send_fn=lambda *a: pytest.fail("boolean decimals sent"))
    result = signal(row, FRAME["receipt"], {"reason":"no_venue_leg"}, transaction=FRAME["transaction"],
                    decimals=0, bnb_usd=1000, bnb_usd_age_s=.1)
    assert not result["sent"]


@pytest.mark.parametrize("case", ["reverted", "receipt_hash", "receipt_block", "receipt_block_hash",
    "tx_chain", "missing_chain", "tx_sender", "tx_target", "native_zero", "native_bool",
    "missing_proof", "foreign_proof", "wrong_row_mint", "wrong_row_tx", "wrong_row_block",
    "removed", "log_hash", "log_block", "log_block_hash", "unknown_portal", "sold_event",
    "duplicate_trade", "malformed_words", "wrong_actor", "wrong_event_amount", "wrong_flow_amount",
    "partial_owner", "foreign_owner", "extra_mint_transfer", "log_duplicate_index"])
def test_incoherent_or_ambiguous_receipt_keeps_ordinary_fallback(case):
    f, row = copy.deepcopy(FRAME), _row()
    tx, receipt, event = f["transaction"], f["receipt"], _trade(f)
    if case == "reverted": receipt["status"] = "0x0"
    elif case == "receipt_hash": receipt["transactionHash"] = "0x" + "11" * 32
    elif case == "receipt_block": receipt["blockNumber"] = "0x1"
    elif case == "receipt_block_hash": receipt["blockHash"] = "0x" + "11" * 32
    elif case == "tx_chain": tx["chainId"] = "0x1"
    elif case == "missing_chain": tx.pop("chainId")
    elif case == "tx_sender": tx["from"] = "0x" + "11" * 20
    elif case == "tx_target": tx["to"] = "0x" + "11" * 20
    elif case == "native_zero": tx["value"] = "0x0"
    elif case == "native_bool": tx["value"] = True
    elif case == "missing_proof": row["buy"].pop("source_initiation")
    elif case == "foreign_proof": row["buy"]["source_initiation"]["payer"] = "0x" + "11" * 20
    elif case == "wrong_row_mint": row["buy"]["mint"] = "0x" + "11" * 20
    elif case == "wrong_row_tx": row["tx_hash"] = "0x" + "11" * 32
    elif case == "wrong_row_block": row["log"] = dict(row["log"], blockNumber="0x1")
    elif case == "removed": event["removed"] = True
    elif case == "log_hash": event["transactionHash"] = "0x" + "11" * 32
    elif case == "log_block": event["blockNumber"] = "0x1"
    elif case == "log_block_hash": event["blockHash"] = "0x" + "11" * 32
    elif case == "unknown_portal": event["address"] = "0x" + "11" * 20
    elif case == "sold_event":
        from sniper.services.memecoin_bsc_volume_poll import FLAP_TOPICS
        event["topics"] = [FLAP_TOPICS[1]]
    elif case == "duplicate_trade": receipt["logs"].append(copy.deepcopy(event))
    elif case == "malformed_words": event["data"] = event["data"][:-64]
    elif case in {"wrong_actor", "wrong_event_amount"}:
        words = list(decode(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"], bytes.fromhex(event["data"][2:])))
        words[2 if case == "wrong_actor" else 3] = "0x" + "11" * 20 if case == "wrong_actor" else AMOUNT + 1
        event["data"] = "0x" + encode(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"], words).hex()
    elif case in {"wrong_flow_amount", "partial_owner", "foreign_owner", "extra_mint_transfer"}:
        flow = next(log for log in receipt["logs"] if log["address"] == MINT and log["topics"][0] == watch.TRANSFER_TOPIC
                    and log["topics"][2][-40:] == row["wallet"][2:])
        if case == "wrong_flow_amount": flow["data"] = "0x" + f"{AMOUNT + 1:064x}"
        elif case == "partial_owner": flow["data"] = "0x" + f"{AMOUNT - 1:064x}"
        elif case == "foreign_owner": flow["topics"][2] = "0x" + "00" * 12 + "11" * 20
        else:
            added = copy.deepcopy(flow); added["topics"][2] = "0x" + "00" * 12 + "11" * 20
            added["logIndex"] = "0x99"; receipt["logs"].append(added)
    elif case == "log_duplicate_index": event["logIndex"] = receipt["logs"][0]["logIndex"]
    result, sent = _offer(f, row)
    assert not result["sent"] and sent == [], (case, result)


@pytest.mark.parametrize("arrival", [None, "bad", "2026-10-10T12:48:00+00:00", "2026-10-10T12:50:00+00:00"])
def test_unknown_old_or_future_arrival_never_mints_a_new_clock(arrival):
    row = _row(); row["now"] = arrival
    result, sent = _offer(FRAME, row)
    assert not result["sent"] and sent == []


@pytest.mark.parametrize("mode", ["runner_shadow", "off"])
def test_flap_handoff_does_not_change_disabled_or_shadow_modes(mode):
    if mode == "off":
        assert bsc.make_watcher_signal(mode, "test") is None
    else:
        result, sent = _offer(FRAME, mode=mode)
        assert not result["sent"] and sent == []


def test_flap_disabled_flag_keeps_existing_fallback(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)
    result, sent = _offer(FRAME)
    assert not result["sent"] and sent == []


def test_shared_event_and_retry_preserve_original_source_average_and_clock():
    from sniper.services.memecoin_onchain_lane import retry_payload, event_from_retry_payload
    payload = _payload(); ev = bsc.BscLaneEvent(payload, market_cap=None)
    assert ev.signal_source == "onchain_watch" and ev.delivery_lane == "bsc_flap_fast_lane"
    assert ev.fomo_event_id == f"onchain:56:{payload['tx_hash']}:{MINT}" and not ev.no_revival
    assert ev.evm_chase_reference.post() == {"reason": "no_venue_leg"}
    assert ev.source_evidence["payment_basis"] == "transaction_value_upper_bound"
    assert ev.created_at == ev.received_at == ev.received_at_normalized == NOW
    assert initiation.source_initiation_refusal(ev, "56") is None
    restored = event_from_retry_payload(retry_payload(ev))
    assert restored.fomo_event_id == ev.fomo_event_id and restored.created_at == NOW
    assert restored.source_initiation == ev.source_initiation
    assert restored.evm_chase_reference.post() == {"reason": "no_venue_leg"}


def test_live_shared_hook_uses_untouched_context_and_warm_stats_before_any_v2_work(monkeypatch):
    payload = _payload(); called = []
    safety, executor = object(), object()
    ctx = {"token_safety_fn": safety, "execute_fn": executor}
    class Cache:
        def stats(self, mint, now): return ({"volume_m5": 123}, 0)
    def factory(contexts, stats_fn):
        assert contexts["56"] is ctx
        assert contexts["56"]["execute_fn"] is executor and contexts["56"]["token_safety_fn"] is safety
        assert stats_fn(MINT, network_id="56")["volume_m5"] == 123
        def hook(conn, ev):
            called.append(ev)
            return {"status": "rejected", "reason": "fixture"}
        return hook
    live = object.__new__(bsc.BscLive)
    live.stopped = False                 # LiveLane.__init__ sets it; object.__new__ skips __init__
    live.ctx, live.contexts, live.conn, live.cache = ctx, {"56": ctx}, None, Cache()
    live.host_clock, live.v3_hook_factory = lambda: EPOCH + .1, factory
    live.stats_fn = lambda *a: pytest.fail("warm stats fetched again")
    live.quotes = {}  # A shared hint must never use native V2 quote/cache/snapshot arithmetic.
    result = live.handle(payload, recv_epoch=EPOCH + .2)
    assert len(called) == 1 and called[0].delivery_lane == "bsc_flap_fast_lane"
    assert result["reason"].startswith("hook:") and result["quote"] is None


@pytest.mark.parametrize("route", ["flap_shared", "v3_shared"])
@pytest.mark.parametrize("decimals", [0, 18])
def test_proved_shared_buy_hands_decimals_to_the_original_funded_context_before_hook(
        monkeypatch, tmp_path, route, decimals):
    from test_memecoin_evm_metadata_cache import _real_context
    payload = _payload()
    payload["decimals"] = decimals
    payload["source_price"] = payload["paid_usd"] / (AMOUNT / 10 ** decimals)
    if route == "v3_shared":
        payload.update(route_kind=route, quote=bsc.WBNB, post_ratio=1.1)
    assert bsc.check_sender(payload, 123, os.getuid(), watcher_pid=123) is None
    ctx, calls = _real_context(monkeypatch, tmp_path)
    safety, execute = ctx["token_safety_fn"], ctx["execute_fn"]
    def factory(contexts, stats_fn):
        assert contexts["56"] is ctx
        assert ctx["token_safety_fn"] is safety and ctx["execute_fn"] is execute
        assert ctx["decimals_fn"](MINT) == decimals
        def hook(conn, ev):
            assert ev.source_initiation == payload["source_initiation"]
            assert ctx["token_fn"](MINT) == pytest.approx(3 * 10 ** (18 - decimals))
            assert ctx["token_fn"](MINT) == pytest.approx(3 * 10 ** (18 - decimals))
            return {"status": "rejected", "reason": "fixture"}
        return hook
    live = object.__new__(bsc.BscLive)
    live.stopped = False                 # LiveLane.__init__ sets it; object.__new__ skips __init__
    live.ctx, live.contexts, live.conn, live.cache = ctx, {"56": ctx}, None, None
    live.host_clock, live.v3_hook_factory = lambda: EPOCH + .1, factory
    assert live.handle(payload, recv_epoch=EPOCH+.2)["reason"].startswith("hook:")
    assert calls == {"decimals": 0, "balanceOf": 2}


@pytest.mark.parametrize("case", ["missing_proof", "wrong_proof_mint", "wrong_chain",
    "missing_decimals", "bool_decimals", "float_decimals", "high_decimals", "wrong_context"])
def test_direct_shared_helper_cannot_seed_unchecked_metadata(monkeypatch, case):
    payload, seeded = _payload(), []
    if case == "missing_proof": payload.pop("source_initiation")
    elif case == "wrong_proof_mint": payload["source_initiation"]["mint"] = "0x" + "11" * 20
    elif case == "wrong_chain": payload["chain"] = "8453"
    elif case == "missing_decimals": payload.pop("decimals")
    elif case == "bool_decimals": payload["decimals"] = False
    elif case == "float_decimals": payload["decimals"] = 18.0
    elif case == "high_decimals": payload["decimals"] = 37
    ctx = {"chain": "8453" if case == "wrong_context" else "56",
           "put_token_decimals_fn": lambda *a: seeded.append(a)}
    live = object.__new__(bsc.BscLive)
    live.stopped = False                 # LiveLane.__init__ sets it; object.__new__ skips __init__
    live.ctx, live.contexts, live.conn, live.cache = ctx, {"56": ctx}, None, None
    live.host_clock = lambda: EPOCH+.1
    live.v3_hook_factory = lambda *a: lambda *a: {"status": "rejected", "reason": "fixture"}
    live._handle_v3(payload, EPOCH+.2)
    assert seeded == []


def test_duplicate_and_foreign_datagrams_stop_before_the_shared_hook(monkeypatch):
    payload = _payload(); handled = []
    monkeypatch.setattr(bsc, "watcher_pid", lambda *a: 123)
    monkeypatch.setattr(bsc, "write_event", lambda *a: None)
    dispatch = bsc.BscDispatch(shadow=SimpleNamespace(handle=lambda *a, **kw: handled.append(a) or {}),
        db_path="unused", beat="unused")
    dispatch.on_datagram(payload, 999, os.getuid(), EPOCH + .2)
    dispatch.on_datagram(payload, 123, os.getuid()+1, EPOCH + .2)
    dispatch.on_datagram(payload, 123, os.getuid(), EPOCH + .2)
    dispatch.on_datagram(payload, 123, os.getuid(), EPOCH + .2)
    assert len(handled) == 1 and dispatch.drops["duplicate"] == 1
    assert dispatch.drops["not_the_watcher"] == dispatch.drops["foreign_uid"] == 1


@pytest.mark.parametrize("field,value", [("quote", bsc.WBNB), ("pool", "0x"+"11"*20),
    ("post_ratio", 1), ("tax_den", 1), ("cap_den", 1), ("r_coin", 1), ("bnb_usd", 1000),
    ("seq", True), ("decimals", False), ("source_user_id", None), ("paid_usd", float("nan")),
    ("source_price", 0), ("sent_epoch", True), ("arrival_utc", None)])
def test_flap_receiver_rejects_unknown_or_inapplicable_v2_payload_shapes(field, value):
    payload = _payload(); payload[field] = value
    assert bsc.check_sender(payload, 123, os.getuid(), watcher_pid=123) == "malformed"


def test_flap_receiver_rejects_a_relay_proof_even_if_ordinary_source_policy_accepts_it(monkeypatch):
    payload = _payload()
    payload["source_initiation"]["proof_version"] = "relay-origin-paid-v1"
    # A shared origin helper may approve a supported ordinary Relay proof; this new hint's
    # receiver must still enforce its narrower direct-signer contract before any hook work.
    monkeypatch.setattr(bsc, "bsc_payload_initiation_refusal", lambda _: None)
    assert bsc.check_sender(payload, 123, os.getuid(), watcher_pid=123) == "malformed"


@pytest.mark.parametrize("recv", [EPOCH+46, EPOCH-.1])
def test_expired_or_future_clock_delivery_stops_before_shared_hook_and_dedupe_claim(monkeypatch, recv):
    payload = _payload()
    monkeypatch.setattr(bsc, "watcher_pid", lambda *a: 123)
    dispatch = bsc.BscDispatch(shadow=SimpleNamespace(handle=lambda *a, **kw: pytest.fail("stale hook")),
                              db_path="unused", beat="unused", mode="live")
    assert dispatch.on_datagram(payload, 123, os.getuid(), recv) is None
    assert dispatch.drops["stale_flap"] == 1 and dispatch.seen.first(payload)


def test_shared_origin_blocks_concurrent_ordinary_delivery_but_preserves_explicit_retry(monkeypatch):
    from collections import OrderedDict
    from sniper.services import memecoin_entry_knobs as knobs
    monkeypatch.setattr(knobs, "_DELIVERED_ORIGINS", OrderedDict())
    ev = bsc.BscLaneEvent(_payload(), market_cap=None)
    claim = (ev.fomo_event_id, ev.source_user_id)
    with knobs.entry_decision(origin=claim, delivery=True) as first:
        assert first
        with knobs.entry_decision(origin=(f"onchain:56:{FRAME['transaction']['hash']}:{MINT}", ev.source_user_id),
                                  delivery=True) as ordinary:
            assert not ordinary
    with knobs.entry_decision(origin=claim, delivery=False) as retry:
        assert retry


def test_actual_shared_callback_and_ordinary_event_claim_the_same_source_and_keep_retry(monkeypatch):
    from collections import OrderedDict
    from sniper.services import memecoin_entry_knobs as knobs, memecoin_onchain_lane as lane
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import _arm, _approve, _conn, _evm_context
    monkeypatch.setattr(knobs, "_DELIVERED_ORIGINS", OrderedDict())
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    conn = _conn()
    payload = _payload()
    _approve(conn, uid=payload["source_user_id"], chains=("56",))
    ev = bsc.BscLaneEvent(payload, market_cap=11_000)
    ordinary = lane.OnchainEv(dict(mint=MINT, source_user_id=ev.source_user_id, address=ev.source_wallet,
        source_slot=ev.source_block_number, handle=ev.source_handle, network_id="56",
        source_valuation_json=json.dumps({"source_initiation":ev.source_initiation,
                                          "evm_post":{"reason":"no_venue_leg"}}),
        source_size_usd=ev.their_size_usd, source_price=ev.their_price,
        signature=f"56:{FRAME['transaction']['hash']}:{MINT}", detected_at=NOW))
    ordinary.their_market_cap = 11_000
    assert ordinary.fomo_event_id == ev.fomo_event_id and ordinary.created_at == ev.created_at
    called, recorded, queued = [], [], []
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **kw: called.append(a[1]) or
                        {"status":"aborted", "quote_miss_code":"provider_timeout"})
    monkeypatch.setattr(runner, "_record_real_decision", lambda *a, **kw: recorded.append(1))
    monkeypatch.setattr(runner, "_maybe_enqueue_retry", lambda *a, **kw: queued.append(1))
    shared = runner.make_real_hook(None, {"56":_evm_context({})}, now_fn=lambda:NOW, trade_size=25,
                                   max_age_s=45, stats_fn=lambda *a, **kw:None)
    fallback = runner.make_real_hook(None, {"56":_evm_context({})}, now_fn=lambda:NOW, trade_size=25,
                                     max_age_s=45, stats_fn=lambda *a, **kw:None)
    try:
        assert shared(conn, ev)["status"] == "aborted"
        assert fallback(conn, ordinary)["status"] == "duplicate_delivery"
        assert len(called) == len(recorded) == len(queued) == 1
        assert fallback.attempt(conn, ordinary)["status"] == "aborted"
        assert len(called) == len(recorded) == 2 and len(queued) == 1
        assert all(e.created_at == NOW for e in called)
    finally:
        conn.close()


def test_successful_offer_prioritizes_the_persisted_ordinary_row_with_the_same_arrival(monkeypatch):
    row, committed, offered = _row(), [], []
    monkeypatch.setattr(cfg, "memecoin_bsc_fast_lane_mode", lambda: "live")
    monkeypatch.setattr(watcher, "_commit_detections", lambda conn, rows: committed.extend(rows))
    signal = bsc.make_watcher_signal("live", "test", clock=lambda: EPOCH+.1,
                                    send_fn=lambda _, payload: offered.append(payload) or True)
    watcher._publish_detection(None, row, bsc_signal=signal,
        outcome=dict(_receipt=FRAME["receipt"], _transaction=FRAME["transaction"],
                     evm_post={"reason":"no_venue_leg"}, decimals=18), quote_usd=1000, price_age=.1)
    assert committed == [row] and row["valuation"]["bsc_fast_lane_sent"] is True
    assert offered[0]["arrival_utc"] == row["now"] == NOW


def test_offer_failure_retains_ordinary_row_without_priority_or_new_clock(monkeypatch):
    row = _row(); committed = []
    monkeypatch.setattr(cfg, "memecoin_bsc_fast_lane_mode", lambda: "live")
    monkeypatch.setattr(watcher, "_commit_detections", lambda conn, rows: committed.extend(rows))
    signal = bsc.make_watcher_signal("live", "test", clock=lambda: EPOCH+.1, send_fn=lambda *a: False)
    watcher._publish_detection(None, row, bsc_signal=signal,
        outcome=dict(_receipt=FRAME["receipt"], _transaction=FRAME["transaction"],
                     evm_post={"reason":"no_venue_leg"}, decimals=18), quote_usd=1000, price_age=.1)
    assert committed == [row] and row["now"] == NOW
    assert not (row.get("valuation") or {}).get("bsc_fast_lane_sent")
