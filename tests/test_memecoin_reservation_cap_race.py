"""Two callers on DIFFERENT coins at cap-1: exactly one may reserve (Alpha, 2026-09-30).

The dip hit lane is a third concurrent caller of the funded hook, beside the main loop and the
on-chain lane, each on its own connection. The global slot and exposure check is atomic with the
reservation write because both reservation functions read the book AFTER `BEGIN IMMEDIATE`:
- EVM: `memecoin_evm_risk.reserve_entry_evm`
- Solana: `memecoin_risk.reserve_entry`
SQLite gives that write lock to one connection at a time, across threads and processes. The mint
UNIQUE index cannot help here: the two coins differ.

The race is forced: each caller stops inside the gate (after its book read) until the other caller
is there too, or 1 s passes. With the lock the second caller never gets there. The control runs the
same harness on a connection that ignores `BEGIN IMMEDIATE`, and both callers reserve, which is
what proves this check can fail.
"""
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_risk as sol_risk
from sniper.settings import memecoin as cfg

DAY, NOW = "2026-09-30", "2026-09-30T12:00:00Z"


class _NoImmediate(sqlite3.Connection):
    """The control: a connection on which the serialising statement does nothing."""

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            return super().execute("SELECT 1")
        return super().execute(sql, *args)


def _connect(path, factory=sqlite3.Connection):
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False, factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.sqlite")
    conn = _connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    # A flat $10 clip, so "room for one clip, not two" is exact. These are the REAL names; the
    # first version patched a setting that does not exist (raising=False), which did nothing.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {})
    return path


def _caps(monkeypatch, *, slots, exposure):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", slots)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", slots)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", exposure)


def _evm(conn, mint):
    return evm_risk.reserve_entry_evm(
        conn, mint=mint, source_user_id="u", strategy="alpha_b", network_id="8453", day=DAY,
        usdc_balance=300.0, native_balance=1.0, now_iso=NOW)


def _sol(conn, mint):
    return sol_risk.reserve_entry(
        conn, mint=mint, source_user_id="u", strategy="alpha_b", day=DAY, usdc_balance=300.0,
        sol_balance=1.0, now_iso=NOW)


def _race(monkeypatch, path, callers, factory=sqlite3.Connection):
    """Run each caller on its own thread and connection, meeting inside the gate. Returns the
    (decision, pending id) pairs."""
    meet = threading.Barrier(len(callers))

    def rendezvous(original):
        def gate(ctx, **kw):
            try:
                meet.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass                                  # the other caller never reached the gate
            return original(ctx, **kw)
        return gate
    monkeypatch.setattr(evm_risk, "evm_entry_allowed", rendezvous(evm_risk.evm_entry_allowed))
    monkeypatch.setattr(sol_risk, "memecoin_entry_allowed",
                        rendezvous(sol_risk.memecoin_entry_allowed))
    out, errors = [None] * len(callers), []

    def run(i, fn, mint):
        conn = _connect(path, factory)
        try:
            out[i] = fn(conn, mint)
        except Exception as exc:                      # surfaced below: never a silent pass
            errors.append(exc)
        finally:
            conn.close()
    threads = [threading.Thread(target=run, args=(i, fn, mint))
               for i, (fn, mint) in enumerate(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return out


def _reserved(path):
    conn = _connect(path)
    try:
        return m.pending_totals(conn)
    finally:
        conn.close()


CALLERS = {
    "evm_evm": [(_evm, "0x" + "a1" * 20), (_evm, "0x" + "b2" * 20)],
    "solana_evm": [(_sol, "SoLMintAAAA"), (_evm, "0x" + "c3" * 20)],
    "solana_solana": [(_sol, "SoLMintBBBB"), (_sol, "SoLMintCCCC")],
}


@pytest.mark.parametrize("pair", sorted(CALLERS))
@pytest.mark.parametrize("cap", ["slots", "exposure"])
def test_two_callers_at_cap_minus_one_reserve_exactly_once(monkeypatch, ledger, pair, cap):
    _caps(monkeypatch, slots=1 if cap == "slots" else 5,
          exposure=1_000.0 if cap == "slots" else 15.0)     # room for one $10 clip, not two
    out = _race(monkeypatch, ledger, CALLERS[pair])
    assert sorted(pid is not None for _d, pid in out) == [False, True], out
    assert _reserved(ledger) == (1, 10.0)
    refused = next(d for d, pid in out if pid is None)
    assert not refused.allowed and ("concurrent" in refused.reason or "exposure" in refused.reason)


def test_control_without_the_immediate_transaction_both_callers_take_the_last_slot(
        monkeypatch, ledger):
    """The harness reaches the race: drop the lock and the last slot is sold twice."""
    _caps(monkeypatch, slots=1, exposure=1_000.0)
    out = _race(monkeypatch, ledger, CALLERS["evm_evm"], factory=_NoImmediate)
    assert [pid is not None for _d, pid in out] == [True, True], out
    assert _reserved(ledger)[0] == 2
