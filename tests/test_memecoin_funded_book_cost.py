"""Cost-only reads retain the full funded book without computing executable marks."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


NOW = "2026-10-17T15:40:18+00:00"


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def _position(conn, mint, cost, *, network="solana", signal="global_feed", mode="real",
              is_open=1, attempt=None, mark=1.0, paused=None):
    conn.execute(
        "INSERT INTO memecoin_positions "
        "(mint, source_user_id, entry_notional, network_id, signal_source, execution_mode, "
        "is_open, attempt_id, entry_price, last_executable_price, last_executable_at, "
        "opened_at, tracking_paused_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mint, "wallet", cost, network, signal, mode, is_open, attempt, 1.0, mark, NOW,
         "2026-10-01T00:00:00+00:00", paused))


def _pending(conn, mint, cost, *, network="solana", signal="global_feed", attempt=None,
             status="reserved", sig=None, recovery=None):
    conn.execute(
        "INSERT INTO memecoin_pending_entries "
        "(mint, reserved_notional_usd, network_id, signal_source, attempt_id, status, "
        "tx_sig, recovery_status) VALUES (?,?,?,?,?,?,?,?)",
        (mint, cost, network, signal, attempt, status, sig, recovery))


def test_empty_cost_book(ledger):
    assert m.funded_book_cost_snapshot(ledger) == {
        "count": 0, "notional": 0.0, "open_count": 0, "open_notional": 0.0,
        "pending_count": 0, "pending_notional": 0.0,
    }


def test_cost_book_includes_manual_and_all_chains_without_valuing_marks(ledger, monkeypatch):
    from sniper.util import clock

    monkeypatch.setattr(clock, "real_utc_now_iso", lambda: NOW)
    _position(ledger, "dead", 100.0, mark=0.0, paused=NOW)
    _position(ledger, "winner", 40.0, network="8453", mark=5.0)
    _position(ledger, "manual", 60.0, network="4663", signal="manual")
    _position(ledger, "unknown-cost", None, network=None)
    _position(ledger, "paper", 1000.0, mode="paper")
    _position(ledger, "closed", 2000.0, is_open=0)
    full = m.funded_book_snapshot(ledger)
    assert full["mark_notional"] == 260.0

    def no_mark_valuation(*args):
        pytest.fail("cost-only book must not evaluate executable marks")

    monkeypatch.setattr(m, "_mark_value_usd", no_mark_valuation)
    expected = {
        "count": 4, "notional": 200.0, "open_count": 4, "open_notional": 200.0,
        "pending_count": 0, "pending_notional": 0.0,
    }
    assert m.funded_book_cost_snapshot(ledger) == expected
    assert {key: full[key] for key in expected} == expected


def test_cost_book_keeps_retained_claims_and_counts_filled_attempts_once(ledger):
    _pending(ledger, "unsigned", 10.0)
    _pending(ledger, "signed", 20.0, network="8453", sig="sig-signed")
    _pending(ledger, "orphaned", 30.0, network="4663", sig="sig-unknown", recovery="orphaned")
    _pending(ledger, "booked", 40.0, attempt="filled-open", sig="sig-filled")
    _position(ledger, "booked", 40.0, attempt="filled-open")
    _pending(ledger, "confirmed", 50.0, status="confirmed", sig="sig-confirmed")
    _pending(ledger, "released", 60.0, status="released")
    _pending(ledger, "closed-match", 70.0, attempt="filled-closed")
    _position(ledger, "closed-match", 70.0, is_open=0, attempt="filled-closed")
    _pending(ledger, "paper-match", 80.0, attempt="paper-open", signal="manual")
    _position(ledger, "paper-match", 80.0, mode="paper", attempt="paper-open")
    _pending(ledger, "null-attempt", 90.0)
    _position(ledger, "null-attempt", 5.0)

    expected = {
        "count": 8, "notional": 345.0, "open_count": 2, "open_notional": 45.0,
        "pending_count": 6, "pending_notional": 300.0,
    }
    assert m.funded_book_cost_snapshot(ledger) == expected
    full = m.funded_book_snapshot(ledger)
    assert {key: full[key] for key in expected} == expected


def test_cost_book_preserves_invalid_cost_failure(ledger):
    _position(ledger, "corrupt-cost", "unavailable")
    with pytest.raises(TypeError):
        m.funded_book_snapshot(ledger)
    with pytest.raises(TypeError):
        m.funded_book_cost_snapshot(ledger)


def test_cost_book_does_not_commit_the_callers_reservation(ledger):
    ledger.execute("BEGIN IMMEDIATE")
    _pending(ledger, "uncommitted", 10.0)

    assert m.funded_book_cost_snapshot(ledger)["notional"] == 10.0
    assert ledger.in_transaction
    ledger.rollback()
    assert m.funded_book_cost_snapshot(ledger)["count"] == 0
