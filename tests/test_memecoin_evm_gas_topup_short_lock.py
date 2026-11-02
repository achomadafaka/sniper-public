"""The EVM gas top-up holds the 4663 wallet lock only to decide, sign and send (2026-09-27, B2).

The pass took wallet_exec_lock("evm-4663-<addr>") and THEN read native (up to three hedged reads, 12 s
each) and WETH (12 s). Funded 4663 entries and exits take the same flock BLOCKING
(serialize_executor), so a slow top-up read made a funded exit wait behind it. Now the reads that
decide WHETHER to try run outside the lock; inside, native (checked against the agreed outside value)
and WETH are re-read with a 2 s wall-clock deadline and decide the amount; the nonce is read inside;
the receipt check runs after the lock is released and still settles the write-ahead row.
"""
import sqlite3
import threading
import time

import pytest

from sniper.execution.exec_lock import wallet_exec_lock
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G

E = 10 ** 18
RUN = dict(low_water_wei=int(0.0020 * E), target_wei=int(0.0030 * E),
           min_weth_reserve_wei=int(0.010 * E), daily_cap_wei=0)
NOW = "2026-09-27T13:40:00+00:00"
KEY = "evm-4663-0xabc"


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _context(calls, *, native_fn=None, weth_fn=None, nonce_fn=None, confirm_once=None):
    return {"chain": "4663", "address": "0xabc", "gas_topup": {
        "weth_token": "0xWETH", "rpc_url": "https://rpc",
        "native_balance_raw_fn": native_fn or (lambda: int(0.0005 * E)),
        "weth_balance_raw_fn": weth_fn or (lambda: int(0.05 * E)),
        "nonce_fn": nonce_fn or (lambda: 12),
        "gas_price_fn": lambda: 1_000_000_000,
        "sign_fn": lambda tx: (calls["signed"].append(tx), ("0xsigned", "0xhash"))[1],
        "send_fn": lambda raw: (calls["sent"].append(raw), "0xhash")[1],
        "confirm_once_fn": confirm_once or (lambda _h: "confirmed"),
    }}


def _pass(conn, ctx, tmp_path):
    return G.run_evm_gas_topup_pass(conn, context=ctx, now_iso=NOW, lock_dir=str(tmp_path), **RUN)


def _held_by_someone_else(tmp_path, chain="4663"):
    with wallet_exec_lock(f"evm-{chain}-0xabc", lock_dir=str(tmp_path), blocking=False) as held:
        return not held


def test_a_funded_4663_exit_gets_the_wallet_lock_while_a_top_up_read_hangs(tmp_path):
    conn, calls, out = _conn(), {"signed": [], "sent": []}, []
    entered, release = threading.Event(), threading.Event()

    def native():
        entered.set()
        release.wait(10)
        return int(0.0005 * E)
    t = threading.Thread(target=lambda: out.append(_pass(conn, _context(calls, native_fn=native),
                                                         tmp_path)))
    t.start()
    try:
        assert entered.wait(5)
        blocked = _held_by_someone_else(tmp_path)
    finally:
        release.set()
        t.join(15)
    assert not blocked, "a funded 4663 entry or exit would wait behind the top-up's balance read"


def test_the_weth_read_inside_the_lock_decides_the_spend(tmp_path):
    conn, calls = _conn(), {"signed": [], "sent": []}
    reads = iter([int(0.05 * E), int(0.011 * E)])      # outside: plenty; inside: an entry spent it
    out = _pass(conn, _context(calls, weth_fn=lambda: next(reads)), tmp_path)
    assert out["state"] == "insufficient_weth_for_full_topup" and calls["sent"] == []


def test_native_that_moved_under_the_lock_is_not_acted_on(tmp_path):
    conn, calls = _conn(), {"signed": [], "sent": []}
    reads = iter([int(0.0005 * E)] * 3 + [int(0.0025 * E)])   # three agree outside, then it moved
    out = _pass(conn, _context(calls, native_fn=lambda: next(reads)), tmp_path)
    assert out["act"] is False and calls["sent"] == []
    assert out["state"] == "native_moved_or_unreadable_under_lock"


def test_the_nonce_is_read_under_the_lock_and_the_receipt_after_it(tmp_path):
    conn, calls, seen = _conn(), {"signed": [], "sent": []}, {}

    def nonce():
        seen["nonce_under_lock"] = _held_by_someone_else(tmp_path)
        return 12

    def receipt(_h):
        seen["receipt_under_lock"] = _held_by_someone_else(tmp_path)
        return "confirmed"
    out = _pass(conn, _context(calls, nonce_fn=nonce, confirm_once=receipt), tmp_path)
    assert seen == {"nonce_under_lock": True, "receipt_under_lock": False}
    assert out["status"] == "confirmed" and out["state"] == "topup"
    rows = conn.execute("SELECT status FROM memecoin_evm_gas_topups").fetchall()
    assert [r["status"] for r in rows] == ["confirmed"]      # the write-ahead row was settled


def test_an_unknown_receipt_leaves_the_row_for_the_next_pass(tmp_path):
    conn, calls = _conn(), {"signed": [], "sent": []}
    out = _pass(conn, _context(calls, confirm_once=lambda _h: "unknown"), tmp_path)
    assert out["status"] == "submitted_unknown"
    rows = conn.execute("SELECT status FROM memecoin_evm_gas_topups").fetchall()
    assert [r["status"] for r in rows] == ["submitted_unknown"]
    assert m.has_unresolved_evm_gas_topup(conn, "4663")


def test_a_read_under_the_lock_past_its_deadline_acts_on_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "INSIDE_READ_DEADLINE_S", 0.2)
    conn, calls = _conn(), {"signed": [], "sent": []}
    n = {"weth": 0}

    def weth():
        n["weth"] += 1
        if n["weth"] > 1:                                   # the inside read hangs
            time.sleep(3)
        return int(0.05 * E)
    started = time.monotonic()
    out = _pass(conn, _context(calls, weth_fn=weth), tmp_path)
    assert time.monotonic() - started < 1.5
    assert out["state"] == "weth_unreadable" and calls["sent"] == []
    assert not _held_by_someone_else(tmp_path)


def test_the_flag_off_keeps_the_old_lock_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", "0")
    conn, calls, out = _conn(), {"signed": [], "sent": []}, []
    entered, release = threading.Event(), threading.Event()

    def native():
        entered.set()
        release.wait(10)
        return int(0.0005 * E)
    t = threading.Thread(target=lambda: out.append(_pass(conn, _context(calls, native_fn=native),
                                                         tmp_path)))
    t.start()
    try:
        assert entered.wait(5)
        blocked = _held_by_someone_else(tmp_path)
    finally:
        release.set()
        t.join(15)
    assert blocked


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("state", ["submitted", "approval_submitted"])
@pytest.mark.parametrize("native", [int(0.0005 * E), int(0.02 * E)])
def test_unknown_holding_transaction_blocks_unwrap_and_reclaim_under_lock(
        tmp_path, monkeypatch, short_lock, state, native):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}
    conn.execute("INSERT INTO memecoin_holding_sell_intents "
                 "(intent_id, network_id, mint, requested_at, expires_at, state, sig) "
                 "VALUES('holding', '4663', '0xtoken', ?, ?, ?, '0xunknown')", (NOW, NOW, state))
    conn.commit()
    original = m.unresolved_holding_sell_count
    guards = []

    def guarded_count(c, chain):
        guards.append(_held_by_someone_else(tmp_path))
        return original(c, chain)

    monkeypatch.setattr(m, "unresolved_holding_sell_count", guarded_count)
    out = _pass(conn, _context(calls, native_fn=lambda: native), tmp_path)
    assert out["state"] == "holding_unresolved"
    assert calls == {"signed": [], "sent": []}
    assert guards == [True], "the nonce fence must be checked while holding the wallet lock"


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("network,state", [("56", "submitted"), ("4663", "sold")])
def test_other_chain_or_terminal_holding_does_not_block_gas(tmp_path, monkeypatch,
                                                          short_lock, network, state):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}
    conn.execute("INSERT INTO memecoin_holding_sell_intents "
                 "(intent_id, network_id, mint, requested_at, expires_at, state) "
                 "VALUES('holding', ?, '0xtoken', ?, ?, ?)", (network, NOW, NOW, state))
    conn.commit()
    out = _pass(conn, _context(calls), tmp_path)
    assert out["status"] == "confirmed" and out["state"] == "topup"
    assert len(calls["signed"]) == len(calls["sent"]) == 1


@pytest.mark.parametrize("short_lock", ["0", "1"])
def test_unreadable_holding_nonce_fence_never_signs_gas(tmp_path, monkeypatch, short_lock):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}

    def unreadable(*_):
        raise sqlite3.OperationalError("holding ledger unavailable")

    monkeypatch.setattr(m, "unresolved_holding_sell_count", unreadable)
    with pytest.raises(sqlite3.OperationalError, match="holding ledger unavailable"):
        _pass(conn, _context(calls), tmp_path)
    assert calls == {"signed": [], "sent": []}


def _exit_position(conn, state, *, network="1", mode="real"):
    """Persist the same claims and signed identities as the funded exit executors."""
    pid = conn.execute(
        "INSERT INTO memecoin_positions (mint, network_id, execution_mode, is_open, opened_at) "
        "VALUES('0xtoken', ?, ?, 1, ?)", (network, mode, NOW)).lastrowid
    conn.commit()
    if state.startswith("close"):
        claim = m.claim_position_close(conn, pid, NOW, "manual")
        assert claim
        if state != "close_unsigned":
            assert m.mark_position_close_submitted(conn, pid, claim, NOW, "0xprivate_exit")
    else:
        claim = m.claim_position_derisk(conn, pid, NOW)
        assert claim
        if not state.endswith("unsigned"):
            assert m.mark_position_derisk_submitted(conn, pid, claim, NOW, "0xprivate_derisk")
        if state.startswith("derisk_settled"):
            conn.execute("INSERT INTO memecoin_fills (position_id, kind, created_at) "
                         "VALUES(?, 'derisk', ?)", (pid, NOW))
    if "closed" in state:
        conn.execute("UPDATE memecoin_positions SET is_open=0, closed_at=? WHERE id=?", (NOW, pid))
    conn.commit()
    return pid


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("native", [int(0.0005 * E), int(0.02 * E)])
@pytest.mark.parametrize("state", ["close_signed", "close_unsigned", "derisk_signed",
                                   "derisk_closed_signed", "derisk_unsigned"])
def test_private_exit_debt_blocks_new_gas_signing_under_wallet_lock(
        tmp_path, monkeypatch, short_lock, native, state):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}
    pid = _exit_position(conn, state)
    guards = []

    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == "memecoin_positions" \
                and column in {"close_sig", "derisk_sig"}:
            guards.append(_held_by_someone_else(tmp_path, "1"))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    ctx = _context(calls, native_fn=lambda: native)  # Public pending nonce remains blind at 12.
    ctx["chain"] = "1"
    out = _pass(conn, ctx, tmp_path)
    conn.set_authorizer(None)
    assert out["state"] == "exit_unresolved"
    assert calls == {"signed": [], "sent": []}
    assert guards and all(guards), "exit debt must be read under the existing wallet lock"
    row = conn.execute("SELECT close_sig, derisk_sig FROM memecoin_positions WHERE id=?",
                       (pid,)).fetchone()
    if "signed" in state and "unsigned" not in state:
        assert row["close_sig"] == "0xprivate_exit" or row["derisk_sig"] == "0xprivate_derisk"


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("state,network,mode", [
    ("close_closed_signed", "1", "real"),
    ("derisk_settled", "1", "real"),
    ("derisk_settled_closed", "1", "real"),
    ("derisk_settled_unsigned", "1", "real"),
    ("derisk_closed_unsigned", "1", "real"),
    ("close_signed", "56", "real"),
    ("close_signed", "1", "paper"),
])
def test_settled_history_paper_and_other_chain_exits_do_not_block_gas(
        tmp_path, monkeypatch, short_lock, state, network, mode):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}
    _exit_position(conn, state, network=network, mode=mode)
    ctx = _context(calls)
    ctx["chain"] = "1"
    out = _pass(conn, ctx, tmp_path)
    assert out["status"] == "confirmed" and out["state"] == "topup"
    assert len(calls["signed"]) == len(calls["sent"]) == 1


@pytest.mark.parametrize("short_lock", ["0", "1"])
def test_unreadable_real_exit_nonce_fence_never_signs_gas(tmp_path, monkeypatch, short_lock):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}

    def unreadable(*_):
        raise sqlite3.OperationalError("exit ledger unavailable")

    monkeypatch.setattr(m, "has_unresolved_real_exit", unreadable, raising=False)
    with pytest.raises(sqlite3.OperationalError, match="exit ledger unavailable"):
        _pass(conn, _context(calls), tmp_path)
    assert calls == {"signed": [], "sent": []}


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("receipt,want", [("confirmed", "recovered_confirmed"),
                                          ("failed", "recovered_failed"),
                                          ("unknown", "unresolved")])
def test_existing_gas_identity_recovers_before_the_real_exit_write_gate(
        tmp_path, monkeypatch, short_lock, receipt, want):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    conn, calls = _conn(), {"signed": [], "sent": []}
    _exit_position(conn, "close_signed", network="4663")
    row_id = m.record_evm_gas_topup_submission(
        conn, now_iso=NOW, day=NOW[:10], chain_id="4663", native_amount_atomic="1000",
        native_before_atomic="500", tx_hash="0xexisting_gas", nonce=11)
    m.finish_evm_gas_topup(conn, row_id=row_id, status="submitted_unknown")

    def confirm(tx_hash):
        assert tx_hash == "0xexisting_gas"
        return receipt

    out = _pass(conn, _context(calls, confirm_once=confirm), tmp_path)
    assert out["state"] == want
    assert calls == {"signed": [], "sent": []}
    stored = conn.execute("SELECT status FROM memecoin_evm_gas_topups WHERE id=?", (row_id,)).fetchone()
    assert stored["status"] == (receipt if receipt != "unknown" else "submitted_unknown")


@pytest.mark.parametrize("state", ["close_signed", "derisk_signed"])
def test_chain_one_reservation_hold_also_fences_private_exit_debt(tmp_path, state):
    conn, calls = _conn(), {"signed": [], "sent": []}
    _exit_position(conn, state)
    ctx = _context(calls, confirm_once=lambda _h: "unknown")
    ctx["chain"] = "1"
    out = G.run_evm_gas_topup_pass(conn, context=ctx, now_iso=NOW, lock_dir=str(tmp_path),
                                   hold_under_reservation=True, **RUN)
    assert out["state"] == "exit_unresolved"
    assert calls == {"signed": [], "sent": []}


def test_terminal_full_exit_history_does_not_make_nonce_fence_scan_the_ledger():
    conn = _conn()
    conn.executemany(
        "INSERT INTO memecoin_positions (network_id, mint, execution_mode, is_open, closed_at, "
        "close_claim_id, close_sig) VALUES('1', '0xhistory', 'real', 0, ?, 'settled', '0xsettled')",
        [(NOW,)] * 5000)
    conn.commit()
    # A local nonce fence must not walk terminal positions while holding the funded wallet lock.
    conn.set_progress_handler(lambda: 1, 1000)
    try:
        assert m.has_unresolved_real_exit(conn, "1") is False
    finally:
        conn.set_progress_handler(None, 0)


@pytest.mark.parametrize("short_lock", ["0", "1"])
@pytest.mark.parametrize("mode", ["topup", "reclaim"])
def test_runner_pass_wires_nonce_claim_and_unsent_release(tmp_path, monkeypatch, short_lock, mode):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", short_lock)
    calls = {"signed": [], "sent": []}
    ctx = _context(calls, native_fn=lambda: int((0.0005 if mode == "topup" else 0.02) * E))
    claims, releases = [], []
    ctx["gas_topup"].update(
        claim_nonce_fn=lambda nonce: claims.append(nonce) or nonce + 1,
        release_nonce_fn=lambda nonce: releases.append(nonce), sign_fn=lambda tx: None)
    out = _pass(_conn(), ctx, tmp_path)
    assert out["state"] == "sign_failed"
    assert claims == [12] and releases == [13] and calls["sent"] == []
