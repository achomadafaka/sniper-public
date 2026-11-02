"""The automatic gas top-up must refuse far more often than it buys, and the cap must survive."""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_gas_topup as gt

PUBKEY = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
NOW = "2026-08-26T12:00:00+00:00"


_RESERVED = {"usdc": 0.0}


def _conn(reserved=0.0):
    _RESERVED["usdc"] = reserved
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_gas_topups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, day TEXT NOT NULL,
        usdc_spent REAL NOT NULL, sol_received REAL, sol_before REAL, tx_sig TEXT,
        status TEXT NOT NULL)""")
    m.ensure_memecoin_tables(c)
    return c


@pytest.fixture(autouse=True)
def _stub_reserved(monkeypatch):
    _RESERVED["usdc"] = 0.0
    monkeypatch.setattr(m, "pending_funding_totals",
                        lambda conn, network=None: (_RESERVED["usdc"], 0))


def _swap(out_lamports=30_000_000, status="confirmed"):
    return dict(
        quote_fn=lambda *_a, **_k: {"outAmount": str(out_lamports)},
        build_fn=lambda *_a, **_k: "tx-b64",
        sign_fn=lambda *_a, **_k: ("signed-b64", "SIG"),
        send_fn=lambda *_a, **_k: "SIG",
        confirm_fn=lambda *_a, **_k: status,
    )


def _run(conn, tmp_path, *, sol, usdc, low=0.02, usd=3.0, cap=10.0, sol_fn=None, **swap):
    return gt.gas_topup_once(
        conn, sol_balance=sol, usdc_balance=usdc, keypair=object(), pubkey=PUBKEY,
        rpc_url="https://rpc", now_iso=NOW, low_water_sol=low, topup_usd=usd,
        daily_cap_usd=cap, lock_dir=str(tmp_path),
        sol_balance_fn=(sol_fn if sol_fn is not None else (lambda: sol)),
        **(swap or _swap()))


# ------------------------------------------------------------------ refusals

def test_healthy_gas_buys_nothing():
    tally = gt.gas_topup_once(_conn(), sol_balance=0.15, usdc_balance=80.0, keypair=None,
                              pubkey=PUBKEY, rpc_url="r", now_iso=NOW, low_water_sol=0.02,
                              topup_usd=3.0, daily_cap_usd=10.0)
    assert tally["state"] == "above_low_water" and tally["spent_usd"] == 0.0


def test_an_unreadable_sol_balance_is_not_a_low_one():
    """Unknown must refuse. A failed read that reads as zero would buy gas forever."""
    tally = gt.gas_topup_once(_conn(), sol_balance=None, usdc_balance=80.0, keypair=None,
                              pubkey=PUBKEY, rpc_url="r", now_iso=NOW, low_water_sol=0.02,
                              topup_usd=3.0, daily_cap_usd=10.0)
    assert tally["state"] == "sol_unreadable" and tally["spent_usd"] == 0.0


def test_usdc_reserved_by_the_book_is_not_spendable(tmp_path):
    """$4 held but $3 encumbered by a live reservation leaves $1 free: refuse a $3 top-up rather
    than sell the bankroll out from under an in-flight entry."""
    tally = _run(_conn(reserved=3.0), tmp_path, sol=0.001, usdc=4.0)
    assert tally["state"] == "insufficient_free_usdc" and tally["spent_usd"] == 0.0


def test_an_unreadable_reservation_total_refuses(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("no such table")
    monkeypatch.setattr(m, "pending_funding_totals", boom)
    tally = _run(_conn(), tmp_path, sol=0.001, usdc=80.0)
    assert tally["state"] == "reserved_unreadable" and tally["spent_usd"] == 0.0


def test_gas_that_arrived_before_the_signature_cancels_the_swap(tmp_path):
    """An exit returning rent, or the rent sweep landing, between the outer read and the lock."""
    tally = _run(_conn(), tmp_path, sol=0.001, usdc=80.0, sol_fn=lambda: 0.15)
    assert tally["state"] == "recovered_before_swap" and tally["spent_usd"] == 0.0


def test_a_busy_wallet_yields_rather_than_queueing_behind_an_exit(tmp_path):
    import sniper.execution.exec_lock as exec_lock
    with exec_lock.wallet_exec_lock("sol-%s" % PUBKEY, lock_dir=str(tmp_path)) as held:
        assert held
        tally = _run(_conn(), tmp_path, sol=0.001, usdc=80.0)
    assert tally["state"] == "wallet_busy" and tally["spent_usd"] == 0.0


# ------------------------------------------------------------------ the cap

def test_the_daily_cap_blocks_the_pass_that_would_exceed_it():
    conn = _conn()
    m.record_gas_topup(conn, now_iso=NOW, day="2026-08-26", usdc_spent=9.0, sol_received=0.09,
                       sol_before=0.001, tx_sig="OLD", status="confirmed")
    tally = gt.gas_topup_once(conn, sol_balance=0.001, usdc_balance=80.0, keypair=None,
                              pubkey=PUBKEY, rpc_url="r", now_iso=NOW, low_water_sol=0.02,
                              topup_usd=3.0, daily_cap_usd=10.0)
    assert tally["state"] == "daily_cap" and tally["spent_usd"] == 0.0


def test_zero_disables_the_daily_cap(tmp_path):
    conn = _conn()
    m.record_gas_topup(conn, now_iso=NOW, day="2026-08-26", usdc_spent=100.0,
                       sol_received=1.0, sol_before=0.001, tx_sig="OLD", status="confirmed")

    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0, usd=1.0, cap=0.0)

    assert tally["state"] == "confirmed"
    assert tally["spent_usd"] == pytest.approx(1.0)


def test_an_ambiguous_submit_still_counts_against_the_cap(tmp_path):
    """Money that MAY have been spent is spent, for budget purposes. Otherwise a provider that
    times out on every attempt grants unlimited retries."""
    conn = _conn()
    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0, **_swap(status="unavailable"))
    assert tally["state"] == "unavailable"
    assert m.gas_topup_spent_today(conn, "2026-08-26") == pytest.approx(3.0)


def test_the_cap_is_durable_across_a_restart():
    """A cap held in memory resets exactly when a misbehaving loop restarts."""
    conn = _conn()
    m.record_gas_topup(conn, now_iso=NOW, day="2026-08-26", usdc_spent=4.0, sol_received=None,
                       sol_before=0.001, tx_sig="A", status="submitted")
    assert m.gas_topup_spent_today(conn, "2026-08-26") == pytest.approx(4.0)
    assert m.gas_topup_spent_today(conn, "2026-08-25") == 0.0


def test_a_refused_row_does_not_consume_budget():
    conn = _conn()
    m.record_gas_topup(conn, now_iso=NOW, day="2026-08-26", usdc_spent=3.0, sol_received=None,
                       sol_before=0.001, tx_sig=None, status="refused")
    assert m.gas_topup_spent_today(conn, "2026-08-26") == 0.0


# ------------------------------------------------------------------ the buy

def test_a_confirmed_topup_records_the_sol_it_bought(tmp_path):
    conn = _conn()
    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0, **_swap(out_lamports=30_000_000))
    assert tally["state"] == "confirmed"
    assert tally["spent_usd"] == pytest.approx(3.0)
    assert tally["sol_received"] == pytest.approx(0.03)
    rows = conn.execute("SELECT status, usdc_spent, sol_received FROM memecoin_gas_topups "
                        "ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["submitted", "confirmed"]
    assert m.gas_topup_spent_today(conn, "2026-08-26") == pytest.approx(3.0)


def test_the_write_ahead_row_lands_before_the_broadcast(tmp_path):
    """A crash between send and record must not hand the next pass a fresh budget."""
    conn = _conn()
    seen = {}

    def send(*_a, **_k):
        seen["at_send"] = m.gas_topup_spent_today(conn, "2026-08-26")
        return "SIG"

    swap = _swap()
    swap["send_fn"] = send
    _run(conn, tmp_path, sol=0.001, usdc=80.0, **swap)
    assert seen["at_send"] == pytest.approx(3.0)


def test_a_failed_quote_spends_nothing(tmp_path):
    conn = _conn()
    swap = _swap()
    swap["quote_fn"] = lambda *_a, **_k: None
    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0, **swap)
    assert tally["state"] == "no_quote" and tally["spent_usd"] == 0.0
    assert m.gas_topup_spent_today(conn, "2026-08-26") == 0.0


def test_a_signing_failure_spends_nothing(tmp_path):
    conn = _conn()
    swap = _swap()
    swap["sign_fn"] = lambda *_a, **_k: None
    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0, **swap)
    assert tally["state"] == "sign_failed" and tally["spent_usd"] == 0.0
    assert m.gas_topup_spent_today(conn, "2026-08-26") == 0.0


# ------------------------------------------------------------------ the production seam

def test_the_runner_seam_returns_a_read_object_not_a_number():
    """REGRESSION, caught in review 2026-08-26 and missed by every test above.

    The runner's solana context exposes `sol_fn`, which returns a NativeSolBalanceRead dataclass,
    and the wiring passed it straight in as `sol_balance`. The module's first live use is
    `float(sol_balance)`, which raises — so in production every pass fell into the broad except and
    tallied 'error'. The top-up would never have fired, including on the day it was needed.

    Every other test here passes raw floats, which is exactly why they were green: the bug lived at
    the seam the tests replace. This one pins the seam's SHAPE so the contract cannot drift.
    """
    from sniper.execution.solana_wallet import NativeSolBalanceRead
    read = NativeSolBalanceRead(True, 0.15, "ok")
    with pytest.raises(TypeError):
        float(read)                       # the module's contract is float-or-None, not this

    # what the wiring must do with it
    def unwrap(r):
        return r.balance if getattr(r, "ok", False) else None

    assert unwrap(read) == pytest.approx(0.15)
    assert unwrap(NativeSolBalanceRead(False, 0.0, "rpc_error")) is None, \
        "a FAILED read must become None (unreadable), never 0.0 (broke and needs gas)"


def test_the_wiring_unwraps_before_calling_the_module():
    """The runner file must not hand the read object to gas_topup_once."""
    import pathlib
    loop = (pathlib.Path(__file__).resolve().parents[1]
            / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "sol_balance=_sol_fn()" not in loop, "the raw read object is being passed again"
    assert "sol_balance=_sol_float()" in loop
    assert "sol_balance_fn=_sol_float" in loop


def test_a_failed_read_refuses_rather_than_buying(tmp_path):
    """The end the unwrap protects: a failed read must reach the module as None."""
    tally = _run(_conn(), tmp_path, sol=None, usdc=80.0, sol_fn=lambda: None)
    assert tally["state"] == "sol_unreadable" and tally["spent_usd"] == 0.0


# ------------------------------------------------------------------ reclaim before buy

def test_reclaimed_rent_covering_the_shortfall_spends_NOTHING(tmp_path, monkeypatch):
    """THE OPERATOR'S CATCH. A $3 buy fired while 0.0122 SOL sat unclaimed, because the sweep waits
    for 8 accounts (0.0163 parked) and the buy fires under 0.0200 — 0.0037 apart. Buying gas you
    already own."""
    conn = _conn()
    monkeypatch.setattr(gt, "close_batch", lambda *a, **k: "confirmed", raising=False)
    # Two reads happen: the in-lock re-check, then the post-reclaim re-read.
    balances = iter([0.001, 0.05])
    tally = gt.gas_topup_once(
        conn, sol_balance=0.001, usdc_balance=80.0, keypair=object(), pubkey=PUBKEY,
        rpc_url="https://rpc", now_iso=NOW, low_water_sol=0.02, topup_usd=3.0,
        daily_cap_usd=10.0, lock_dir=str(tmp_path),
        sol_balance_fn=lambda: next(balances),
        reclaim_fn=lambda: 6,
        **_swap())
    assert tally["state"] == "recovered_by_reclaim"
    assert tally["spent_usd"] == 0.0, "must not spend bankroll on gas we already owned"
    assert tally["reclaimed"] == 6
    assert m.gas_topup_spent_today(conn, "2026-08-26") == 0.0


def test_a_reclaim_that_falls_short_still_buys(tmp_path):
    """Reclaiming 2 accounts is not enough to clear the trigger; the buy must still happen."""
    conn = _conn()
    balances = iter([0.001, 0.005])
    tally = gt.gas_topup_once(
        conn, sol_balance=0.001, usdc_balance=80.0, keypair=object(), pubkey=PUBKEY,
        rpc_url="https://rpc", now_iso=NOW, low_water_sol=0.02, topup_usd=3.0,
        daily_cap_usd=10.0, lock_dir=str(tmp_path),
        sol_balance_fn=lambda: next(balances), reclaim_fn=lambda: 2, **_swap())
    assert tally["state"] == "confirmed" and tally["spent_usd"] == pytest.approx(3.0)
    assert tally["reclaimed"] == 2


def test_a_FAILED_reclaim_still_buys(tmp_path):
    """A housekeeping fault must never become a gas outage."""
    conn = _conn()

    def boom():
        raise RuntimeError("sweep exploded")
    tally = gt.gas_topup_once(
        conn, sol_balance=0.001, usdc_balance=80.0, keypair=object(), pubkey=PUBKEY,
        rpc_url="https://rpc", now_iso=NOW, low_water_sol=0.02, topup_usd=3.0,
        daily_cap_usd=10.0, lock_dir=str(tmp_path),
        sol_balance_fn=lambda: 0.001, reclaim_fn=boom, **_swap())
    assert tally["state"] == "confirmed" and tally["spent_usd"] == pytest.approx(3.0)


def test_a_reclaim_that_closed_nothing_does_not_re_read_or_block_the_buy(tmp_path):
    conn = _conn()
    reads = {"n": 0}

    def bal():
        reads["n"] += 1
        return 0.001
    tally = gt.gas_topup_once(
        conn, sol_balance=0.001, usdc_balance=80.0, keypair=object(), pubkey=PUBKEY,
        rpc_url="https://rpc", now_iso=NOW, low_water_sol=0.02, topup_usd=3.0,
        daily_cap_usd=10.0, lock_dir=str(tmp_path),
        sol_balance_fn=bal, reclaim_fn=lambda: 0, **_swap())
    assert tally["state"] == "confirmed"
    assert reads["n"] == 1, "no reclaim means no extra balance read"


def test_no_reclaim_hook_behaves_exactly_as_before(tmp_path):
    """The old path is untouched when the caller supplies no reclaim."""
    conn = _conn()
    tally = _run(conn, tmp_path, sol=0.001, usdc=80.0)
    assert tally["state"] == "confirmed" and tally["reclaimed"] == 0


def test_unresolved_withdrawal_defers_gas_purchase(tmp_path):
    c = _conn()
    m.ensure_memecoin_tables(c)
    iid = m.enqueue_withdraw(c, 'solana', 'DEST', 10, now_iso=NOW)
    m.resolve_withdraw(c, iid, 'submitted', now_iso=NOW, sig='WITHDRAW')
    tally = _run(c, tmp_path, sol=0.001, usdc=80.0)
    assert tally['state'] == 'withdrawal_unresolved'
    assert tally['spent_usd'] == 0
    assert c.execute('SELECT COUNT(*) FROM memecoin_gas_topups').fetchone()[0] == 0
