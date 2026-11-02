import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_reserve_counts_and_dedups_by_mint():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z")
    assert pid is not None
    assert m.pending_totals(c) == (1, 10.0)
    assert m.pending_open_mints(c) == {"M1"}
    assert m.pending_open_coin_ids(c) == {("solana", "M1")}
    # same mint while first is still reserved -> rejected by the partial unique index
    assert m.reserve_pending(c, mint="M1", source_user_id="u2", strategy="control",
                             notional=10.0, reserved_at="t") is None
    assert m.pending_totals(c) == (1, 10.0)


def test_release_frees_mint_and_reservation():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="t")
    m.release_pending(c, pid, reason="tx_failed")
    assert m.pending_totals(c) == (0, 0.0)
    assert m.pending_open_mints(c) == set()
    assert m.pending_open_coin_ids(c) == set()
    # mint is free again after release
    assert m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                             notional=10.0, reserved_at="t") is not None


def test_confirm_clears_reservation_but_keeps_row():
    c = _c()
    pid = m.reserve_pending(c, mint="M1", source_user_id="u1", strategy="control",
                            notional=10.0, reserved_at="t")
    m.set_pending_sig(c, pid, "SIG123", "2026-07-09T00:00:01Z")
    m.confirm_pending(c, pid)
    assert m.pending_totals(c) == (0, 0.0)      # no longer 'reserved'
    row = c.execute("SELECT tx_sig, status FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()
    assert row["tx_sig"] == "SIG123" and row["status"] == "confirmed"
