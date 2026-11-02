"""Funded entries recheck unresolved wallet transactions under the execution lock."""
import sqlite3
import threading

import pytest

from sniper.execution import exec_lock
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_bsc_fast_lane import BscLive
from sniper.services.memecoin_rh_fast_lane import LiveLane


NOW = "2026-10-07T20:00:00+00:00"


def _ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _pending(conn, chain, *, signed=False):
    pid = m.reserve_pending(conn, mint="0x" + "11" * 20, source_user_id="u",
                            strategy="alpha_b", notional=25, reserved_at=NOW,
                            network_id=chain)
    if signed:
        conn.execute("UPDATE memecoin_pending_entries SET tx_sig='0xentry' WHERE id=?", (pid,))
        conn.commit()
    return pid


def _unresolved_holding(conn, chain, state):
    intent = m.enqueue_holding_sell(conn, chain, "0x" + "22" * 20, now_iso=NOW)
    m.resolve_holding_sell(conn, intent, state, now_iso=NOW, sig="0xholding",
                           transaction_phase="approval" if state == "approval_submitted" else "swap")


BLOCKERS = [("holding", "submitted"), ("holding", "approval_submitted"),
            ("gas", "submitted"), ("gas", "submitted_unknown")]


def _unresolved_transaction(conn, chain, kind, state):
    if kind == "holding":
        _unresolved_holding(conn, chain, state)
        return
    row = m.record_evm_gas_topup_submission(
        conn, now_iso=NOW, day=NOW[:10], chain_id=chain, native_amount_atomic=100,
        native_before_atomic=1000, tx_hash="0xmaintenance", nonce=7, direction="wrap")
    if state != "submitted":
        m.finish_evm_gas_topup(conn, row_id=row, status=state)


def _lane(lane_type, tmp_path):
    live = object.__new__(lane_type)
    live.clip_usd, live.low_wei = 25.0, 0
    live.lock_key, live.lock_dir = "wallet", str(tmp_path)
    live.host_clock, live.generation_fn = lambda: 1005.0, lambda: 900.0
    live.max_snapshot_age_s = live.eth_usd_max_age_s = 30
    live.generation_margin_s = 1.0
    live._post_peak_nominated = lambda *a: False
    live._copy_floor = lambda *a: (0, None)
    live._buy_tx = lambda *a, **k: ({"data": "0x12", "gas": 100000}, 1)
    live._send = lambda *a, **k: pytest.fail("fast lane claimed a nonce or signed")
    payload = dict(eth_usd=2000, bnb_usd=600, eth_usd_at=1000,
                   cap_num=1, cap_den=1, tax_num=0, tax_den=1, r_quote=10**18, r_coin=10**18,
                   snapshot=dict(read_at=1000, native_wei=10**18, gas_price_wei=1, nonce=7))
    return live, payload, {}


@pytest.mark.parametrize("chain", ["1", "56", "8453", "4663", "57073", "5042"])
@pytest.mark.parametrize("kind,state", BLOCKERS)
def test_production_evm_entry_wrapper_enables_its_chain_fence(
        tmp_path, monkeypatch, chain, kind, state):
    from test_memecoin_evm_context_keys import _production_context

    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    ctx = _production_context(monkeypatch, tmp_path, chain)
    conn = _ledger()
    pid = _pending(conn, chain)
    _unresolved_transaction(conn, chain, kind, state)
    result = ctx["execute_fn"](conn, pid, "mint", 25, NOW)
    assert result == {"status": "deferred", "reason": kind + "_unresolved"}
    assert isinstance(result, exec_lock.UnchangedWalletResult)
    assert m.pending_row(conn, pid)["status"] == "failed"


@pytest.mark.parametrize("kind,state", BLOCKERS)
@pytest.mark.parametrize("signed", [False, True])
def test_waiting_entry_rechecks_holding_after_lock_and_preserves_signed_claim(
        tmp_path, monkeypatch, kind, state, signed):
    conn = _ledger()
    pid = _pending(conn, "56", signed=signed)
    assert m.unresolved_signed_pending_count(conn, "56") == int(signed)  # admission already passed
    waiting, outputs, errors = threading.Event(), [], []
    real_lock = exec_lock.wallet_exec_lock
    helper = "unresolved_holding_sell_count" if kind == "holding" else "has_unresolved_evm_gas_topup"
    real_count = getattr(m, helper)

    def locked_count(conn, chain):
        with real_lock("wallet", lock_dir=str(tmp_path), blocking=False) as held:
            assert not held, "transaction fence must run while the wallet lock is held"
        return real_count(conn, chain)

    monkeypatch.setattr(m, helper, locked_count)
    raw = lambda *a, **k: pytest.fail("entry signed after an unresolved wallet transaction")
    wrapped = exec_lock.serialize_executor(raw, "wallet", lock_dir=str(tmp_path),
                                           holding_entry_chain="56")

    def entry():
        waiting.set()
        try:
            outputs.append(wrapped(conn, pid, "mint", 25, NOW))
        except BaseException as exc:
            errors.append(exc)

    with real_lock("wallet", lock_dir=str(tmp_path)):
        worker = threading.Thread(target=entry)
        worker.start()
        assert waiting.wait(1)
        _unresolved_transaction(conn, "56", kind, state)
    worker.join(2)
    assert not worker.is_alive() and not errors
    assert outputs == [{"status": "deferred", "reason": kind + "_unresolved"}]
    assert isinstance(outputs[0], exec_lock.UnchangedWalletResult)
    assert m.pending_row(conn, pid)["status"] == ("reserved" if signed else "failed")
    assert exec_lock.exec_generation("wallet", lock_dir=str(tmp_path)) == 0


@pytest.mark.parametrize("lane_type,chain", [(LiveLane, "4663"), (BscLive, "56")])
@pytest.mark.parametrize("kind,state", BLOCKERS)
@pytest.mark.parametrize("signed", [False, True])
def test_fresh_fast_lane_snapshot_cannot_bypass_unresolved_holding(
        tmp_path, monkeypatch, lane_type, chain, kind, state, signed):
    conn = _ledger()
    pid = _pending(conn, chain, signed=signed)
    _unresolved_transaction(conn, chain, kind, state)
    live, payload, rec = _lane(lane_type, tmp_path)
    helper = "unresolved_holding_sell_count" if kind == "holding" else "has_unresolved_evm_gas_topup"
    real_count = getattr(m, helper)

    def locked_count(conn, network):
        with exec_lock.wallet_exec_lock("wallet", lock_dir=str(tmp_path), blocking=False) as held:
            assert not held, "fast-lane transaction fence must run under its existing wallet lock"
        return real_count(conn, network)

    monkeypatch.setattr(m, helper, locked_count)
    result = live.execute_reserved(payload, rec, conn, pid, 25, decimals=18)
    assert result["status"] == "deferred" and result["reason"] == kind + "_unresolved"
    assert rec["_outcome"] == kind + "_unresolved"
    assert m.pending_row(conn, pid)["status"] == ("reserved" if signed else "failed")
    assert exec_lock.exec_generation("wallet", lock_dir=str(tmp_path)) == 0


def test_unknown_private_maintenance_blocks_the_next_entry_after_wallet_unlock(tmp_path):
    from sniper.services.memecoin_evm_gas_topup import run_evm_gas_topup_pass
    conn = _ledger()
    address = "0x" + "33" * 20
    key = exec_lock.evm_exec_lock_key("1", address)
    sends = []
    context = {"chain": "1", "address": address, "gas_topup": {
        "weth_token": "0xWETH", "rpc_url": "https://offline.invalid",
        "native_balance_raw_fn": lambda: 40_162_197_932_195_810,
        "weth_balance_raw_fn": lambda: 0,
        "nonce_fn": lambda: 7, "gas_price_fn": lambda: 10**9,
        "sign_fn": lambda tx: ("offline-signed", "0xmaintenance"),
        "send_fn": lambda raw: sends.append(raw) or "0xmaintenance",
        "confirm_once_fn": lambda tx: "unknown"}}
    result = run_evm_gas_topup_pass(
        conn, context=context, now_iso=NOW, low_water_wei=7_700_000_000_000_000,
        target_wei=20_900_000_000_000_000, min_weth_reserve_wei=0, daily_cap_wei=0,
        hold_under_reservation=True, hold_until_receipt_s=0, lock_dir=str(tmp_path))
    assert result["status"] == "submitted_unknown" and result["direction"] == "wrap"
    assert sends == ["offline-signed"]
    assert m.has_unresolved_evm_gas_topup(conn, "1")
    assert m.unresolved_signed_pending_count(conn, "1") == 0
    with exec_lock.wallet_exec_lock(key, lock_dir=str(tmp_path), blocking=False) as held:
        assert held, "the maintenance receipt budget has released this wallet"
    pid = _pending(conn, "1")
    wrapped = exec_lock.serialize_executor(
        lambda *a: pytest.fail("entry executed across an unknown maintenance transaction"),
        key, lock_dir=str(tmp_path), holding_entry_chain="1")
    assert wrapped(conn, pid, "mint", 25, NOW) == {
        "status": "deferred", "reason": "gas_unresolved"}
    assert m.pending_row(conn, pid)["status"] == "failed"


@pytest.mark.parametrize("state,gas_chain", [
    ("confirmed", "1"), ("failed", "1"), ("not_sent", "1"),
    ("submitted", "56"), ("submitted_unknown", "56"),
])
def test_terminal_or_other_chain_gas_transfer_does_not_block_entry(tmp_path, state, gas_chain):
    conn = _ledger()
    _unresolved_transaction(conn, gas_chain, "gas", state)
    pid = _pending(conn, "1")
    seen = []
    wrapped = exec_lock.serialize_executor(
        lambda *a: seen.append(True) or {"status": "probe"},
        "wallet", lock_dir=str(tmp_path), holding_entry_chain="1")
    assert wrapped(conn, pid, "mint", 25, NOW) == {"status": "probe"}
    assert seen == [True] and m.pending_row(conn, pid)["status"] == "reserved"


@pytest.mark.parametrize("signed", [False, True])
def test_unreadable_maintenance_ledger_never_reaches_entry(tmp_path, monkeypatch, signed):
    conn = _ledger()
    pid = _pending(conn, "1", signed=signed)

    def unreadable(*args):
        raise sqlite3.OperationalError("maintenance ledger unreadable")

    monkeypatch.setattr(m, "has_unresolved_evm_gas_topup", unreadable)
    wrapped = exec_lock.serialize_executor(
        lambda *a: pytest.fail("entry executed with unknown maintenance state"),
        "wallet", lock_dir=str(tmp_path), holding_entry_chain="1")
    with pytest.raises(sqlite3.OperationalError, match="maintenance ledger unreadable"):
        wrapped(conn, pid, "mint", 25, NOW)
    assert m.pending_row(conn, pid)["status"] == "reserved"


@pytest.mark.parametrize("lane_type,chain", [(LiveLane, "4663"), (BscLive, "56")])
@pytest.mark.parametrize("state,same_chain", [
    ("confirmed", True), ("failed", True), ("not_sent", True),
    ("submitted", False), ("submitted_unknown", False),
])
def test_terminal_or_other_chain_maintenance_does_not_block_fast_lane(
        tmp_path, lane_type, chain, state, same_chain):
    conn = _ledger()
    _unresolved_transaction(conn, chain if same_chain else "1", "gas", state)
    pid = _pending(conn, chain)
    live, payload, rec = _lane(lane_type, tmp_path)
    seen = []
    live._send = lambda *a: seen.append(True) or {"status": "probe"}
    assert live.execute_reserved(payload, rec, conn, pid, 25, decimals=18) == {"status": "probe"}
    assert seen == [True] and m.pending_row(conn, pid)["status"] == "reserved"


@pytest.mark.parametrize("lane_type,chain", [(LiveLane, "4663"), (BscLive, "56")])
@pytest.mark.parametrize("signed", [False, True])
def test_unreadable_maintenance_ledger_never_reaches_fast_lane(
        tmp_path, monkeypatch, lane_type, chain, signed):
    conn = _ledger()
    pid = _pending(conn, chain, signed=signed)
    live, payload, rec = _lane(lane_type, tmp_path)

    def unreadable(*args):
        raise sqlite3.OperationalError("maintenance ledger unreadable")

    monkeypatch.setattr(m, "has_unresolved_evm_gas_topup", unreadable)
    with pytest.raises(sqlite3.OperationalError, match="maintenance ledger unreadable"):
        live.execute_reserved(payload, rec, conn, pid, 25, decimals=18)
    assert m.pending_row(conn, pid)["status"] == "reserved"


@pytest.mark.parametrize("chain", ["1", "56", "8453", "4663", "57073", "5042"])
@pytest.mark.parametrize("action", ["exit", "derisk"])
@pytest.mark.parametrize("state", ["submitted", "submitted_unknown"])
@pytest.mark.parametrize("signed_claim", [False, True])
def test_production_exit_and_derisk_wait_for_unknown_maintenance_without_claim_changes(
        tmp_path, monkeypatch, chain, action, state, signed_claim):
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    ctx = _production_context(monkeypatch, tmp_path, chain)
    conn = _ledger()
    position_id = m.open_position(
        conn, source_user_id="u", source_handle="u", mint="0x" + "11" * 20,
        ticker="T", entry_price=1, entry_notional=25, entry_fill_source="test",
        entry_lag_s=0, their_size_usd=25, opened_at=NOW, network_id=chain,
        execution_mode="real")
    # Neither an absent claim nor an existing signed claim is changed by this refusal.
    if signed_claim:
        claim, sig = ("close_claim_id", "close_sig") if action == "exit" else (
            "derisk_claim_id", "derisk_sig")
        conn.execute(f"UPDATE memecoin_positions SET {claim}='claim',{sig}='0xsigned' "
                     "WHERE id=?", (position_id,))
        conn.commit()
    monkeypatch.setattr(m, "release_unsigned_blocked_entry",
                        lambda *a: pytest.fail("an exit guard tried to release an entry"))
    before = m.get_position(conn, position_id)
    _unresolved_transaction(conn, chain, "gas", state)
    result = (ctx["exit_fn"](conn, before, "manual", NOW) if action == "exit" else
              ctx["derisk_fn"](conn, before, 0.5, NOW))
    assert result == {"status": "deferred", "reason": "gas_unresolved"}
    assert isinstance(result, exec_lock.UnchangedWalletResult)
    assert m.get_position(conn, position_id) == before
