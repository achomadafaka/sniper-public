"""A confirmed buy with an unreadable receipt is re-read soon, not at the next reconcile (G1).

Operator order 2026-09-27: "a confirmed purchase must become a tracked position with working
exits". A receipt_unreadable anomaly left the reservation for the stale-reservation recovery, which
looks at it no earlier than 90 s after broadcast and runs every 120 s: up to 210 s with no rails and
no mirror exit. ReceiptRetry re-runs recover_confirmed_entry at +5/+15/+30 s on its own thread.
"""
import sqlite3
import threading
import time

import pytest

from sniper.execution.solana_swap_executor import recover_confirmed_entry
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_receipt_retry import ReceiptRetry
from sniper.settings import memecoin as cfg

FAST = (0.02, 0.05, 0.08)


def _receipt(sig):
    return {
        "blockTime": 1790521200,
        "transaction": {"signatures": [sig], "message": {"accountKeys": [
            {"pubkey": "owner", "signer": True}, {"pubkey": "coin-ata", "signer": False}]}},
        "meta": {"err": None, "fee": 100_000,
                 "preBalances": [2_000_000_000, 0],
                 "postBalances": [1_600_000_000, 2_039_280],
                 "preTokenBalances": [],
                 "postTokenBalances": [{"accountIndex": 1, "mint": "coin", "owner": "owner",
                                        "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]},
    }


@pytest.fixture(autouse=True)
def _close_every_retry(monkeypatch):
    """Each test's ReceiptRetry worker stops at its teardown (conftest _no_leaked_threads)."""
    made = []
    real_init = ReceiptRetry.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(ReceiptRetry, "__init__", init)
    yield
    for retry in made:
        assert retry.close()


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.db")

    def connect():
        c = sqlite3.connect(path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    c = connect()
    m.ensure_memecoin_tables(c)
    m.start_entry_attempt(c, attempt_id="a1", decision_started_at="2026-09-27T15:00:00Z",
                          network_id="solana", mint="coin")
    pid = m.reserve_pending(c, mint="coin", source_user_id="wallet",
                            strategy=cfg.MEMECOIN_REAL_POLICY, notional=50.0,
                            reserved_at="2026-09-27T15:00:00Z", network_id="solana",
                            signal_source="slot_lane", attempt_id="a1", funding_asset="sol",
                            funding_amount_atomic=500_000_000, funding_sol_usd=100.0)
    m.set_pending_broadcast(c, pid, "sig", None, "2026-09-27T15:00:01Z")
    c.close()
    return connect, pid


def _recover_after(misses, calls):
    """recover_fn that sees no receipt `misses` times, then the real one."""
    def recover(conn, row, now_iso):
        calls.append(row["id"])
        receipt = None if len(calls) <= misses else _receipt(row["tx_sig"])
        return recover_confirmed_entry(conn, row, receipt, owner="owner", now_iso=now_iso)
    return recover


def _wait(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _book(connect):
    c = connect()
    try:
        return ([dict(p) for p in m.open_positions_by_strategy(c, cfg.MEMECOIN_REAL_POLICY,
                                                                mode="real")],
                c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='entry'").fetchone()[0])
    finally:
        c.close()


def test_an_unreadable_receipt_is_booked_by_the_next_retry_into_the_exit_and_mirror_book(ledger):
    connect, pid = ledger
    calls = []
    retry = ReceiptRetry(recover_fn=_recover_after(1, calls), conn_factory=connect,
                         now_fn=lambda: "2026-09-27T15:00:10Z", delays=FAST)
    assert retry.after({"status": "anomaly", "reason": "receipt_unreadable", "sig": "sig"}, pid)
    assert _wait(lambda: _book(connect)[0])
    positions, fills = _book(connect)
    # open_positions_by_strategy(MEMECOIN_REAL_POLICY, mode=real) is the mirror's want-list reader.
    assert [(p["mint"], p["network_id"], p["source_user_id"]) for p in positions] == [
        ("coin", "solana", "wallet")]
    assert fills == 1 and calls == [pid, pid]
    assert m.pending_row(connect(), pid)["status"] == "confirmed"


def test_at_most_three_reads_per_anomaly_then_the_reconcile_owns_it(ledger):
    connect, pid = ledger
    calls = []
    retry = ReceiptRetry(recover_fn=_recover_after(99, calls), conn_factory=connect,
                         now_fn=lambda: "2026-09-27T15:00:10Z", delays=FAST)
    retry.after({"status": "anomaly", "sig": "sig"}, pid)
    time.sleep(0.4)
    assert calls == [pid, pid, pid]
    assert m.pending_row(connect(), pid)["status"] == "reserved"
    assert retry.after({"status": "anomaly", "sig": "sig"}, pid), "a later anomaly may schedule again"


def test_only_a_signed_anomaly_is_scheduled(ledger):
    connect, pid = ledger
    retry = ReceiptRetry(recover_fn=lambda *a: None, conn_factory=connect,
                         now_fn=lambda: "t", delays=FAST)
    for result in ({"status": "filled", "sig": "sig"}, {"status": "anomaly"},
                   {"status": "unknown", "sig": "sig"}, None):
        assert not retry.after(result, pid), result


def test_a_reservation_already_resolved_is_not_read_again(ledger):
    connect, pid = ledger
    c = connect()
    m.release_pending(c, pid, "released_elsewhere")
    c.close()
    calls = []
    retry = ReceiptRetry(recover_fn=_recover_after(0, calls), conn_factory=connect,
                         now_fn=lambda: "t", delays=FAST)
    retry.after({"status": "anomaly", "sig": "sig"}, pid)
    time.sleep(0.2)
    assert calls == []


def test_the_retry_and_the_reconcile_racing_book_one_position(ledger):
    connect, pid = ledger
    gate = threading.Barrier(2, timeout=5)

    def recover(conn, row, now_iso):
        gate.wait()
        return recover_confirmed_entry(conn, row, _receipt(row["tx_sig"]), owner="owner",
                                       now_iso=now_iso)

    retry = ReceiptRetry(recover_fn=recover, conn_factory=connect,
                         now_fn=lambda: "2026-09-27T15:02:00Z", delays=FAST)
    retry.after({"status": "anomaly", "sig": "sig"}, pid)
    c = connect()
    try:
        out = R.recover_stale_reservations(
            c, confirm_for=lambda _n, _s: "confirmed", now_iso="2026-09-27T15:02:00Z",
            expiry_proof_s=90, confirmed_entry_for=recover)
    finally:
        c.close()
    assert _wait(lambda: _book(connect)[0])
    time.sleep(0.2)
    positions, fills = _book(connect)
    assert len(positions) == 1 and fills == 1, (out, positions, fills)


def test_the_executor_schedules_its_own_signed_anomaly(monkeypatch):
    """The builder hands every signed anomaly of execute_fn to the retry, keyed by the pending id."""
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams
    from sniper.execution import solana_swap_executor as ex

    seen = []
    monkeypatch.setattr(ex.m, "pending_row", lambda *_: {"strategy": "alpha_b",
                                                          "source_user_id": "u1"})
    monkeypatch.setattr(ex, "execute_entry", lambda *a, **k: {"status": "anomaly", "sig": "S1"})
    monkeypatch.setattr("sniper.services.memecoin_receipt_retry.ReceiptRetry.after",
                        lambda self, result, pid: seen.append((result["sig"], pid)) or True)
    executor = seams.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc")
    executor["execute_fn"](None, 7, "MINT", 10, "2026-09-27T15:00:00Z")
    assert seen == [("S1", 7)]
