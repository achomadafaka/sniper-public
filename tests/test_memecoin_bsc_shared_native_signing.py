"""Shared BSC quotes reuse only a fresh local native-lane nonce; all entry gates remain."""
import json
import sqlite3
import time

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as seams, exec_lock
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_evm_receipt_booking import Chain, MINT, POOL, _buy, _seq


def _entry(monkeypatch, tmp_path, *, snapshot_change=None, safety=(True, "ok"),
           source_price=None, route=None, generation_change=False, nonce=0,
           recheck_invalid=False, sign_failure=False, unknown_send=False,
           floor_ahead=False, no_snapshot=False, through_context=False,
           approval=False, unwrap=False, pending_intent=False):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "56")
    if unwrap:
        monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "")
        monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "")
    monkeypatch.setattr(seams, "quote_usd_price", lambda *_a, **_k: 1.0)
    from sniper.execution import evm_flap_quote_exit
    monkeypatch.setattr(evm_flap_quote_exit, "prewarm_after_fill", lambda *a: None)
    conn = sqlite3.connect(tmp_path / "book.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint=MINT, source_user_id="trader", strategy="alpha_b",
                            notional=10, reserved_at="2026-10-10T00:00:00Z", network_id="56",
                            source_price=source_price)
    acct = Account.create()
    key = exec_lock.evm_exec_lock_key("56", acct.address)
    net = Chain("56", acct.address, _buy(coin=4, chain="56"))
    snapshot = {"chain": "56", "address": acct.address.lower(), "nonce": nonce,
                "read_at": time.time(), "max_age_s": 30.0, "generation_margin_s": 0.0}
    if snapshot_change:
        snapshot.update(snapshot_change)
    callbacks, signed, gates, stages = [], [], [], []

    def snapshot_fn():
        with exec_lock.wallet_exec_lock(key, blocking=False) as held:
            assert not held, "snapshot must be consumed under the existing wallet lock"
        callbacks.append(True)
        if no_snapshot:
            return None
        if recheck_invalid and len(callbacks) == 2:
            return dict(snapshot, read_at=0)
        return dict(snapshot)

    original_http = net.http

    def http(*a, body=None, **kw):
        request = json.loads(body)
        if request["method"] == "eth_gasPrice" and generation_change:
            exec_lock.touch_generation(key)
        if request["method"] == "eth_sendRawTransaction" and unknown_send:
            net.calls.append(request["method"])
            return 200, {"error": {"code": -32000, "message": "provider busy"}}, None
        if unwrap and request["method"] == "eth_getBalance":
            net.calls.append(request["method"])
            native = 100 if net.calls.count("eth_getBalance") == 1 else 110
            return 200, {"result": hex(native * 10**18)}, None
        if unwrap and request["method"] == "eth_call" and request["params"][0]["data"] == "0x":
            net.calls.append(request["method"])
            return 200, {"result": "0x"}, None
        return original_http(*a, body=body, **kw)

    route = route or {"protocol": "pcs_mixed_v3_v2", "tokens": ["w", "base", MINT]}

    def quote(sell, buy, amount, *, slippage_bps=None):
        out = 4 * 10**18 if str(buy).lower() == MINT.lower() else int(10 * 10**18 * .97)
        q = {"sellAmount": str(amount), "buyAmount": str(out),
                "issues": {"allowance": {"spender": POOL} if approval else None}, "route": dict(route),
                "transaction": {"to": POOL, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}
        if unwrap and str(buy).lower() == MINT.lower():
            from sniper.execution.evm_wallet import CHAIN_QUOTE
            q["pre_transactions"] = [{"to": CHAIN_QUOTE["56"]["token"],
                "data": "0x2e1a7d4d%064x" % (10 * 10**18), "value": 0, "gas": 100000,
                "gasPrice": 1000000000}]
        return q

    real_sign = seams.sign_tx

    def sign(tx, account, **kw):
        signed.append(dict(tx))
        return None if sign_failure else real_sign(tx, account, **kw)

    monkeypatch.setattr(seams, "sign_tx", sign)
    ex = seams.build_real_evm_executor(
        acct=acct, chain_id="56", rpc_url="https://fixture.invalid", http=http,
        venue_quote_fn=quote, usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([(0.0, 18), (4.0, 18)]))
    if pending_intent:
        raw, _hash = real_sign({"to": POOL, "data": "0x", "value": 0, "gas": 21000,
                               "gasPrice": 100, "nonce": 0, "chainId": 56}, acct)
        exec_lock.intent_send(raw, "fixture", required=True)
    execute = exec_lock.serialize_executor(ex["execute_fn"], key)
    if floor_ahead:
        assert ex["lane_claim_nonce_fn"](0) == 0
    if unknown_send:
        monkeypatch.setattr(seams, "eth_confirm", lambda *a, **kw: "unknown")

    def gate():
        gates.append(True)
        return safety

    from types import SimpleNamespace
    trace = SimpleNamespace(mark=lambda *a, **kw: None,
                            mark_substage=lambda name, **kw: stages.append((name, kw)),
                            stage_completed_at=lambda _name: "2026-10-10T00:00:01Z")
    if through_context:
        from sniper.services import memecoin_evm_leg as leg, memecoin_real_runner as runner
        monkeypatch.setattr(leg, "real_entries_allowed", lambda *a: True)
        monkeypatch.setattr(leg._arming, "in_quiet_hours", lambda: False)
        monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", ("56",))
        monkeypatch.setattr(leg, "reserve_entry_evm", lambda *a, **kw: (
            SimpleNamespace(allowed=True, approved_size_usdc=10.0), pid))
        ctx = {"execute_fn": execute, "entry_signing_snapshot_fn": snapshot_fn,
               "token_safety_fn": lambda *a, **kw: gate(), "usdc_fn": lambda: 100,
               "native_fn": lambda: 1}
        ev = SimpleNamespace(mint=MINT, source_user_id="trader", network_id="56")
        result = runner._make_evm_entry_fn({"56": ctx})(
            conn, ev, route="evm:56", day="2026-10-10", now_iso="2026-10-10T00:00:01Z",
            trade_size=10, trace=trace, chase_max_pct=30)
    else:
        result = execute(conn, pid, MINT, 10, "2026-10-10T00:00:01Z",
                         entry_signing_snapshot_fn=snapshot_fn, safety_gate_fn=gate, trace=trace,
                         chase_max_pct=30)
    return conn, pid, result, net, signed, callbacks, gates, stages, ex


@pytest.mark.parametrize("route", [
    {"protocol": "pcs_mixed_v3_v2", "tokens": ["w", "base", MINT]},
    {"protocol": "flap_curve", "portal": POOL},
    {"protocol": "uniswap_v3", "source": "relay-origin-paid-v1"},
])
def test_ordinary_shared_entry_uses_snapshot_without_nonce_rpc_and_books_route(
        monkeypatch, tmp_path, route):
    conn, pid, result, net, signed, callbacks, gates, stages, _ex = _entry(
        monkeypatch, tmp_path, route=route)
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 0
    assert signed[0]["gasPrice"] == 1_000_000_000
    assert signed[0]["value"] == 0 and signed[0]["data"] == "0x"
    assert "eth_getTransactionCount" not in net.calls
    assert len(callbacks) == 2 and gates == [True]
    pending = m.pending_row(conn, pid)
    assert pending["nonce"] == 0 and pending["status"] == "confirmed" and pending["tx_sig"]
    pos = m.get_position(conn, result["position_id"])
    stored_route = json.loads(pos["evm_route_json"])
    assert {key: stored_route[key] for key in route} == route
    assert stored_route["round_trip_pct"] == pytest.approx(-3.0)
    assert m.open_qty(conn, pos["id"]) == 4.0 and pos["entry_notional"] == 10.0
    assert any(n == "evm.entry_signing_snapshot" and f["outcome"] == "used"
               for n, f in stages)


def test_refused_safety_never_reads_snapshot_or_signs(monkeypatch, tmp_path):
    _conn, _pid, result, net, signed, callbacks, gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, safety=(False, "unsafe token"))
    assert result["status"] == "unsafe_gate" and result["reason"] == "unsafe token"
    assert signed == callbacks == [] and gates == [True]
    assert "eth_sendRawTransaction" not in net.calls


def test_chase_refusal_still_precedes_snapshot_and_signing(monkeypatch, tmp_path):
    _conn, _pid, result, net, signed, callbacks, gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, source_price=1.0)
    assert result["status"] == "skipped_chase", result
    assert signed == callbacks == gates == []
    assert "eth_sendRawTransaction" not in net.calls


@pytest.mark.parametrize("change", [
    {"chain": "1"}, {"chain": 56}, {"address": "0x" + "11" * 20},
    {"nonce": None}, {"nonce": True}, {"nonce": -1}, {"nonce": "0"},
    {"read_at": 0}, {"read_at": float("inf")}, {"read_at": True},
    {"read_at": time.time() + 3600}, {"max_age_s": None}, {"max_age_s": 0},
    {"generation_margin_s": -1}, {"generation_margin_s": float("nan")},
])
def test_invalid_snapshot_keeps_ordinary_nonce_read(monkeypatch, tmp_path, change):
    _conn, _pid, result, net, signed, _callbacks, _gates, stages, _ex = _entry(
        monkeypatch, tmp_path, snapshot_change=change)
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 5 and net.calls.count("eth_getTransactionCount") == 1
    assert any(n == "evm.entry_signing_snapshot" and f["outcome"] == "fallback"
               for n, f in stages)


@pytest.mark.parametrize("option", ["no_snapshot", "generation_change", "floor_ahead"])
def test_unavailable_or_spend_invalidated_snapshot_uses_current_nonce(monkeypatch, tmp_path, option):
    _conn, _pid, result, net, signed, _callbacks, _gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, **{option: True})
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 5 and net.calls.count("eth_getTransactionCount") == 1


def test_snapshot_expiring_during_gas_read_releases_claim_and_rereads_nonce(monkeypatch, tmp_path):
    _conn, _pid, result, net, signed, callbacks, _gates, stages, _ex = _entry(
        monkeypatch, tmp_path, recheck_invalid=True)
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 5 and net.calls.count("eth_getTransactionCount") == 1
    assert len(callbacks) == 2
    assert any(n == "evm.entry_signing_snapshot" and f["outcome"] == "fallback_recheck"
               for n, f in stages)


def test_unsigned_sign_failure_releases_owned_snapshot_nonce(monkeypatch, tmp_path):
    _conn, _pid, result, net, signed, _callbacks, _gates, _stages, ex = _entry(
        monkeypatch, tmp_path, sign_failure=True)
    assert result["status"] == "aborted" and result["reason"] == "sign failed"
    assert signed[0]["nonce"] == 0 and "eth_sendRawTransaction" not in net.calls
    assert ex["lane_claim_nonce_fn"](0) == 0


def test_unknown_send_retains_nonce_floor_and_durable_submission(monkeypatch, tmp_path):
    conn, pid, result, net, signed, _callbacks, _gates, _stages, ex = _entry(
        monkeypatch, tmp_path, unknown_send=True)
    assert result["status"] == "unknown", result
    assert signed[0]["nonce"] == 0 and net.calls.count("eth_sendRawTransaction") == 1
    pending = m.pending_row(conn, pid)
    assert pending["status"] == "reserved" and pending["nonce"] == 0 and pending["tx_sig"]
    assert pending["signed_payload_b64"] and pending["signed_payload_sha256"]
    assert ex["lane_claim_nonce_fn"](0) is None


def test_original_context_callback_reaches_real_leg_signer(monkeypatch, tmp_path):
    _conn, _pid, result, net, signed, callbacks, gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, through_context=True)
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 0 and len(callbacks) == 2 and gates == [True]
    assert "eth_getTransactionCount" not in net.calls


@pytest.mark.parametrize("fault", ["latched", "inflight"])
def test_generation_fault_or_inflight_intent_keeps_ordinary_signer(monkeypatch, tmp_path, fault):
    if fault == "latched":
        monkeypatch.setattr(exec_lock, "generation_fault", lambda: True)
    _conn, _pid, result, net, signed, _callbacks, _gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, pending_intent=fault == "inflight")
    assert result["status"] == "filled", result
    assert signed[0]["nonce"] == 5 and net.calls.count("eth_getTransactionCount") == 1


@pytest.mark.parametrize("path", ["approval", "unwrap"])
def test_approval_next_nonce_and_unwrap_keep_existing_signer(monkeypatch, tmp_path, path):
    _conn, _pid, result, net, signed, callbacks, _gates, _stages, _ex = _entry(
        monkeypatch, tmp_path, **{path: True})
    assert result["status"] == "filled", json.dumps(result)
    assert [tx["nonce"] for tx in signed] == [5, 6]
    assert callbacks == []
    assert net.calls.count("eth_getTransactionCount") == (1 if path == "approval" else 2)


def test_gas_worker_exception_rewinds_only_unsigned_claim(monkeypatch, tmp_path):
    from test_memecoin_bsc_signing_reads import executor, TX
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "56")
    ex = executor(None)
    address = ex["address"]

    def gas_failure(*a, **kw):
        raise TimeoutError("gas worker unavailable")

    monkeypatch.setattr(seams, "eth_gas_price", gas_failure)
    snapshot = {"chain": "56", "address": address.lower(), "nonce": 0,
                "read_at": time.time(), "max_age_s": 30.0, "generation_margin_s": 0.0}
    with exec_lock.wallet_exec_lock(exec_lock.evm_exec_lock_key("56", address)):
        with pytest.raises(TimeoutError, match="gas worker unavailable"):
            ex["_sign"](TX, entry_signing_snapshot_fn=lambda: snapshot)
        assert ex["lane_claim_nonce_fn"](0) == 0
