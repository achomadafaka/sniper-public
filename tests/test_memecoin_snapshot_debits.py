"""Dispatched entries keep their cash debit until the caller's balance generation catches up."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_risk import build_memecoin_risk_context, reserve_entry
from sniper.settings import memecoin as cfg


@pytest.fixture
def ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    yield conn
    conn.close()


def reserve(conn, mint, *, asset=None, overhead=0, network="solana", notional=50):
    return m.reserve_pending(
        conn, mint=mint, source_user_id="wallet", strategy="alpha_b", notional=notional,
        reserved_at="2026-10-09T15:00:00Z", network_id=network, attempt_id="attempt-" + mint,
        **({"funding_asset": "sol", "funding_amount_atomic": 500_000_000,
            "funding_sol_usd": 100.0, "funding_overhead_atomic": overhead}
           if asset == "sol" else {}))


def test_active_debits_count_once_even_when_ids_repeat(ledger):
    usdc = reserve(ledger, "usdc")
    sol = reserve(ledger, "sol", asset="sol", overhead=2_000_000)
    assert m.pending_funding_totals(ledger, "solana") == (50.0, 502_000_000)
    assert m.pending_funding_totals(
        ledger, "solana", snapshot_debits=(usdc, sol, usdc)) == (50.0, 502_000_000)


def test_confirmed_debits_hold_original_cash_without_double_counting_book(ledger):
    usdc = reserve(ledger, "usdc")
    sol = reserve(ledger, "sol", asset="sol", overhead=2_000_000)
    m.open_position(
        ledger, source_user_id="wallet", source_handle="wallet", mint="sol", ticker="S",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-10-09T15:00:01Z", execution_mode="real",
        network_id="solana", attempt_id="attempt-sol")
    m.confirm_pending(ledger, usdc)
    m.confirm_pending(ledger, sol)
    ordinary = build_memecoin_risk_context(
        ledger, usdc_balance=100, sol_balance=1, day="2026-10-09")
    stale = build_memecoin_risk_context(
        ledger, usdc_balance=100, sol_balance=1, day="2026-10-09",
        snapshot_debits=(usdc, sol))
    assert ordinary.effective_pending_usdc == 0 and ordinary.pending_sol_lamports == 0
    assert stale.effective_pending_usdc == 50.0 and stale.pending_sol_lamports == 502_000_000
    assert stale.effective_concurrent == ordinary.effective_concurrent == 1
    assert stale.effective_exposure_usd == ordinary.effective_exposure_usd == 50.0
    assert stale.effective_pending_notional_usd == ordinary.effective_pending_notional_usd == 0


def test_open_position_match_does_not_erase_a_snapshot_debit(ledger):
    pending = reserve(ledger, "coin")
    m.open_position(
        ledger, source_user_id="wallet", source_handle="wallet", mint="coin", ticker="S",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-10-09T15:00:01Z", execution_mode="real",
        network_id="solana", attempt_id="attempt-coin")
    assert m.pending_totals(ledger, "solana") == (0, 0.0)
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 0)
    assert m.pending_funding_totals(
        ledger, "solana", snapshot_debits=(pending,)) == (50.0, 0)


def test_failed_signed_sol_entry_keeps_overhead_until_a_fresh_snapshot(ledger):
    pending = reserve(ledger, "failed", asset="sol", overhead=2_000_000)
    m.set_pending_sig(ledger, pending, "signed-sig", "2026-10-09T15:00:00Z")
    m.release_pending(ledger, pending, "BuySlippageBelowMinBaseAmountOut")
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 0)
    assert m.pending_funding_totals(
        ledger, "solana", snapshot_debits=(pending,)) == (0.0, 502_000_000)
    params = dict(source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
                  usdc_balance=200, sol_balance=0.7, now_iso="2026-10-09T15:00:02Z",
                  size_override=50, funding_asset="sol", funding_sol_usd=100,
                  sol_buffer_cap_usd=175)
    decision, second = reserve_entry(ledger, mint="next", snapshot_debits=(pending,), **params)
    assert not decision.allowed and second is None and "SOL buffer" in decision.reason
    decision, second = reserve_entry(ledger, mint="next", **params)
    assert decision.allowed and second is not None


def test_atomic_reservation_cannot_respend_confirmed_usdc_from_an_old_snapshot(ledger):
    pending = reserve(ledger, "confirmed")
    m.confirm_pending(ledger, pending)
    params = dict(source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
                  usdc_balance=75, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
                  size_override=50)
    decision, second = reserve_entry(ledger, mint="next", snapshot_debits=(pending,), **params)
    assert not decision.allowed and second is None
    decision, second = reserve_entry(ledger, mint="next", **params)
    assert decision.allowed and second is not None


@pytest.mark.parametrize("ids", [None, (True,), (0,), (-1,), ("1",), tuple(range(1, 258))])
def test_invalid_snapshot_debit_ids_fail_closed(ledger, ids):
    with pytest.raises(ValueError, match="snapshot debit"):
        m.pending_funding_totals(ledger, "solana", snapshot_debits=ids)


def test_missing_or_wrong_chain_shadow_never_reads_as_zero(ledger):
    other = reserve(ledger, "other", network="8453")
    for pending in (other, other + 1):
        with pytest.raises(ValueError, match="snapshot debit"):
            m.pending_funding_totals(ledger, "solana", snapshot_debits=(pending,))


@pytest.mark.parametrize("column,value", [
    ("reserved_notional_usd", -1), ("reserved_notional_usd", "unknown"),
    ("funding_amount_atomic", "-1"), ("funding_overhead_atomic", "-1"),
    ("funding_asset", "unknown"),
])
def test_malformed_settled_debit_fails_closed(ledger, column, value):
    pending = reserve(ledger, "bad", asset="sol" if column.startswith("funding_") else None)
    m.confirm_pending(ledger, pending)
    ledger.execute("UPDATE memecoin_pending_entries SET " + column + "=? WHERE id=?", (value, pending))
    ledger.commit()
    with pytest.raises(ValueError):
        m.pending_funding_totals(ledger, "solana", snapshot_debits=(pending,))


def test_unknown_snapshot_debit_refuses_before_another_reservation(ledger):
    decision, pending = reserve_entry(
        ledger, mint="next", source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
        usdc_balance=100, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
        snapshot_debits=(123,), size_override=50)
    assert not decision.allowed and pending is None and "snapshot debit" in decision.reason
    assert m.pending_totals(ledger, "solana") == (0, 0.0)


def test_shadow_selection_does_not_add_an_entry_query_or_change_gas_topups(ledger):
    pending = reserve(ledger, "confirmed")
    m.confirm_pending(ledger, pending)
    m.record_gas_topup(
        ledger, usdc_spent=7, sol_received=None, sol_before=.1, status="submitted",
        purpose="buffer", tx_sig="topup", day="2026-10-09", now_iso="2026-10-09T15:00:00Z")
    selects = []
    ledger.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
    assert m.pending_funding_totals(ledger, "solana") == (7.0, 0)
    baseline = len(selects)
    selects.clear()
    assert m.pending_funding_totals(
        ledger, "solana", snapshot_debits=(pending,)) == (57.0, 0)
    assert len(selects) == baseline == 2


def test_debit_callback_runs_inside_reservation_transaction_after_fill(ledger):
    pending = reserve(ledger, "earlier", notional=100)
    m.confirm_pending(ledger, pending)
    called = []

    def debits():
        called.append(ledger.in_transaction)
        return (pending,)

    decision, second = reserve_entry(
        ledger, mint="next", source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
        usdc_balance=200, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
        size_override=150, snapshot_debits_fn=debits)
    assert called == [True]
    assert not decision.allowed and second is None
    assert m.pending_totals(ledger, "solana") == (0, 0.0)


def test_debit_callback_stale_history_refuses_without_reservation(ledger):
    def debits():
        assert ledger.in_transaction
        raise ValueError("snapshot debit history expired")

    decision, pending = reserve_entry(
        ledger, mint="next", source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
        usdc_balance=200, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
        size_override=150, snapshot_debits_fn=debits)
    assert not decision.allowed and pending is None
    assert "invalid_funded_contract: snapshot debit history expired" in decision.reason
    assert not ledger.in_transaction


def test_manual_equity_debit_counts_settled_usdc_only(ledger):
    active = reserve(ledger, "active", notional=40)
    confirmed = reserve(ledger, "confirmed", notional=100)
    linked = reserve(ledger, "linked", notional=20)
    failed = reserve(ledger, "failed", notional=10)
    sol = reserve(ledger, "sol", asset="sol")
    m.confirm_pending(ledger, confirmed)
    m.confirm_pending(ledger, sol)
    m.release_pending(ledger, failed, "chain_error")
    m.open_position(
        ledger, source_user_id="wallet", source_handle="wallet", mint="linked", ticker="L",
        entry_price=1.0, entry_notional=20.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-10-09T15:00:01Z", execution_mode="real",
        network_id="solana", attempt_id="attempt-linked")
    assert m.settled_snapshot_usdc_debit(
        ledger, "solana", snapshot_debits=(active, confirmed, linked, failed, sol, confirmed)) == 130
    assert m.settled_snapshot_usdc_debit(ledger, "solana") == 0
    with pytest.raises(ValueError, match="snapshot debit"):
        m.settled_snapshot_usdc_debit(ledger, "solana", snapshot_debits=(sol + 1,))


def test_callback_extra_sol_cost_stays_chain_local_and_refuses_insufficient_gas(ledger):
    pending = reserve(ledger, "earlier", notional=100)
    m.confirm_pending(ledger, pending)
    evidence = {"pending_ids": (pending,), "extra_sol_atomic": 990_000_000}
    ctx = build_memecoin_risk_context(
        ledger, usdc_balance=200, sol_balance=1, day="2026-10-09", snapshot_debits=evidence)
    assert ctx.pending_sol_lamports == 990_000_000
    assert ctx.effective_pending_usdc == 100 and ctx.effective_pending_notional_usd == 0
    decision, second = reserve_entry(
        ledger, mint="next", source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
        usdc_balance=200, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
        size_override=50, snapshot_debits_fn=lambda: evidence)
    assert not decision.allowed and second is None and "insufficient SOL" in decision.reason


@pytest.mark.parametrize("evidence", [
    {"pending_ids": (), "extra_sol_atomic": -1},
    {"pending_ids": (), "extra_sol_atomic": True},
    {"pending_ids": (), "extra_sol_atomic": "10"},
    {"pending_ids": ()},
    {"pending_ids": (), "extra_sol_atomic": 0, "extra": 1},
])
def test_malformed_callback_funding_evidence_refuses(ledger, evidence):
    decision, pending = reserve_entry(
        ledger, mint="next", source_user_id="wallet", strategy="alpha_b", day="2026-10-09",
        usdc_balance=200, sol_balance=1, now_iso="2026-10-09T15:00:02Z",
        size_override=50, snapshot_debits_fn=lambda: evidence)
    assert not decision.allowed and pending is None and "snapshot" in decision.reason
