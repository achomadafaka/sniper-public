"""Pending cash reads stay bounded as the append-only Solana topup ledger grows."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


NOW = "2026-10-19T12:00:00+00:00"
CUTOFF = "2026-10-19T11:50:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"


@pytest.fixture
def ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    conn.commit()
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: NOW)
    yield conn
    conn.close()


def topups(conn, rows):
    conn.executemany(
        "INSERT INTO memecoin_gas_topups "
        "(tx_sig,purpose,status,created_at,usdc_spent,day) VALUES (?,?,?,?,?,'2026-10-19')",
        rows,
    )


@pytest.mark.parametrize("purpose,created_at,spent,want", [
    ("buffer", OLD, 11, 11.0),
    ("gas", OLD, 11, 0.0),
    ("gas", "2026-10-19T11:49:59+00:00", 11, 0.0),
    ("gas", CUTOFF, 11, 11.0),
    ("gas", "2026-10-20T00:00:00+00:00", 11, 11.0),
    ("other", OLD, 11, 0.0),
    ("other", NOW, 11, 11.0),
    ("buffer", OLD, 0, 0.0),
    ("buffer", OLD, -4.5, -4.5),
])
def test_pending_topup_retains_existing_purpose_age_and_amount_rules(
        ledger, purpose, created_at, spent, want):
    topups(ledger, [("sig", purpose, "submitted", created_at, spent)])
    assert m.pending_funding_totals(ledger, "solana") == (want, 0)


@pytest.mark.parametrize("sig,submitted_status,terminal_purpose,terminal_status,copies,want", [
    ("sig", "submitted", "buffer", "confirmed", 1, 0.0),
    ("sig", "submitted", "buffer", "failed", 1, 0.0),
    ("sig", "submitted", "buffer", "dropped", 1, 0.0),
    ("sig", "submitted", "gas", "confirmed", 1, 11.0),
    ("sig", "submitted", "buffer", "Confirmed", 1, 11.0),
    ("sig", "Submitted", "buffer", "confirmed", 1, 0.0),
    ("sig", "submitted", "buffer", "confirmed", 3, 0.0),
    ("", "submitted", "buffer", "confirmed", 1, 0.0),
    (None, "submitted", "buffer", "confirmed", 1, 11.0),
])
def test_only_same_signature_and_purpose_terminal_rows_release_cash(
        ledger, sig, submitted_status, terminal_purpose, terminal_status, copies, want):
    topups(ledger, [(sig, "buffer", submitted_status, OLD, 11)]
           + [(sig, terminal_purpose, terminal_status, OLD, 999)] * copies)
    assert m.pending_funding_totals(ledger, "solana") == (want, 0)


def test_each_submitted_row_counts_and_new_terminal_is_visible_on_next_read(ledger):
    topups(ledger, [("sig", "buffer", "submitted", OLD, 11)] * 2)
    assert m.pending_funding_totals(ledger, "solana") == (22.0, 0)
    topups(ledger, [("sig", "buffer", "failed", NOW, 0)])
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 0)


@pytest.mark.parametrize("rows,want", [
    ([("sig", None, "submitted", NOW, 11),
      ("sig", None, "confirmed", NOW, 0)], 11.0),
    ([("sig", None, "submitted", OLD, 11)], 0.0),
    ([("sig", "buffer", None, NOW, 11)], 0.0),
    ([("sig", "buffer", "submitted", OLD, 11),
      ("sig", "buffer", None, NOW, 0)], 11.0),
    ([("sig", "buffer", "submitted", None, 11)], 11.0),
    ([("sig", "gas", "submitted", None, 11)], 0.0),
    ([("sig", "buffer", "submitted", OLD, None)], 0.0),
])
def test_legacy_nullable_topup_fields_keep_sql_null_semantics(ledger, rows, want):
    # Only this isolated legacy fixture permits fields that today's schema makes NOT NULL.
    ledger.execute("DROP TABLE memecoin_gas_topups")
    ledger.execute("CREATE TABLE memecoin_gas_topups "
                   "(tx_sig TEXT,purpose TEXT,status TEXT,created_at TEXT,usdc_spent REAL,day TEXT)")
    topups(ledger, rows)
    assert m.pending_funding_totals(ledger, "solana") == (want, 0)


def test_topups_affect_only_solana_and_reads_preserve_the_callers_transaction(ledger):
    topups(ledger, [("sig", "buffer", "submitted", OLD, 11)])
    changes = ledger.total_changes
    assert ledger.in_transaction
    assert m.pending_funding_totals(ledger, "base") == (0.0, 0)
    assert m.pending_funding_totals(ledger, "solana") == (11.0, 0)
    assert ledger.in_transaction and ledger.total_changes == changes
    ledger.rollback()
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 0)


def test_topup_query_errors_propagate_instead_of_reporting_free_cash(ledger):
    ledger.execute("DROP TABLE memecoin_gas_topups")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        m.pending_funding_totals(ledger, "solana")


def test_pending_funding_read_has_bounded_work_with_terminal_topup_history(ledger):
    topups(ledger, [(f"sig-{i}", "buffer", "submitted", OLD, 100) for i in range(600)]
           + [(f"sig-{i}", "buffer", "confirmed", OLD, 0) for i in range(600)]
           + [("unresolved", "buffer", "submitted", OLD, 7)])
    ledger.commit()
    instructions = 0
    budget = 200_000

    def stop_excess_work():
        nonlocal instructions
        instructions += 1_000
        return instructions >= budget

    # Count actual SQLite work, independent of host load and SQL/query-plan spelling.
    ledger.set_progress_handler(stop_excess_work, 1_000)
    try:
        try:
            result = m.pending_funding_totals(ledger, "solana")
        except sqlite3.OperationalError:
            if instructions >= budget:
                pytest.fail(f"pending funding read exceeded {budget:,} SQLite VM instructions")
            raise
    finally:
        ledger.set_progress_handler(None, 0)
    assert result == (7.0, 0)
    assert not ledger.in_transaction
