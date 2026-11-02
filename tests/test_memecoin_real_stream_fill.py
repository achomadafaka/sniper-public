"""SSE 'fill' detection: the stream pushes a typed event when a REAL position opens or closes,
detected from two consecutive change-signature pos tuples (no extra scans). The client turns
these into the operator's entry/exit notifications."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import _cockpit_change_signature, _fill_events


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, ticker="COIN"):
    pid = m.open_position(c, source_user_id="u1", source_handle="fibs", mint="MINT-" + ticker,
                          ticker=ticker, entry_price=1e-5, entry_notional=18.75,
                          entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=500.0,
                          opened_at="2026-08-17T15:00:00+00:00", execution_mode="real",
                          network_id="solana", strategy="alpha_b")
    c.commit()
    return pid


def _close(c, pid, realized=-7.88, at="2026-08-17T15:05:00+00:00"):
    c.execute("UPDATE memecoin_positions SET is_open=0, realized_pnl=?, exit_reason='manual',"
              " closed_at=?, closed_recorded_at=? WHERE id=?", (realized, at, at, pid))
    c.commit()


def test_an_entry_between_two_signatures_yields_one_entry_fill():
    c = _conn()
    prev = _cockpit_change_signature(c)[0]
    _open_real(c)
    cur = _cockpit_change_signature(c)[0]
    fills = _fill_events(c, prev, cur)
    assert [f["kind"] for f in fills] == ["entry"]
    assert fills[0]["ticker"] == "COIN" and fills[0]["notional"] == 18.75
    assert fills[0]["handle"] == "fibs"


def test_an_exit_between_two_signatures_yields_one_exit_fill():
    c = _conn()
    pid = _open_real(c)
    prev = _cockpit_change_signature(c)[0]
    _close(c, pid)
    cur = _cockpit_change_signature(c)[0]
    fills = _fill_events(c, prev, cur)
    assert [f["kind"] for f in fills] == ["exit"]
    assert fills[0]["realized"] == -7.88 and fills[0]["exit_reason"] == "manual"


def test_paper_rows_never_fill():
    c = _conn()
    prev = _cockpit_change_signature(c)[0]
    m.open_position(c, source_user_id="u1", source_handle="fibs", mint="P", ticker="P",
                    entry_price=1e-5, entry_notional=100.0, entry_fill_source="quote",
                    entry_lag_s=1.0, their_size_usd=500.0, opened_at="t",
                    execution_mode="paper", network_id="solana", strategy="control")
    c.commit()
    cur = _cockpit_change_signature(c)[0]
    # a paper open moves neither the real MAX(id) nor MAX(closed_recorded_at)
    assert _fill_events(c, prev, cur) == []


def test_unchanged_signature_yields_nothing():
    c = _conn()
    _open_real(c)
    sig = _cockpit_change_signature(c)[0]
    assert _fill_events(c, sig, sig) == []
