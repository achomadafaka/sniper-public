"""Keep a research signal available after a provider rate limit."""
import sqlite3

from scripts.research import memecoin_curve_phase_watch_loop as watch


def test_rate_limit_preserves_cursor_and_retries_signal(monkeypatch, tmp_path):
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, mint TEXT, "
                   "decision_started_at TEXT, terminal_status TEXT, terminal_reason TEXT, network_id TEXT)")
    ledger.execute("INSERT INTO memecoin_entry_attempts VALUES (?, ?, ?, ?, ?, ?)",
                   ("attempt-1", "0x" + "11" * 20, "2026-09-17T10:00:00Z", "refused", "no_quote", "4663"))
    conn = watch.connect(tmp_path / "memecoin_curve_phase_watch.db")
    watch.set_meta(conn, "after_rowid", 0)
    conn.close()
    monkeypatch.setattr(watch, "state_path", lambda *parts: tmp_path / parts[-1])
    monkeypatch.setattr(watch, "acquire_run_lock", lambda *_: object())
    monkeypatch.setattr(watch, "get_readonly_connection", lambda *_: ledger)
    monkeypatch.setattr(watch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(watch.sys, "argv", ["watch", "--once"])

    def limited(_mint):
        raise watch.Rate429()

    monkeypatch.setattr(watch, "curve_state", limited)
    assert watch.main() == 0
    conn = watch.connect(tmp_path / "memecoin_curve_phase_watch.db")
    assert watch.get_meta(conn, "after_rowid") == "0"
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0

    monkeypatch.setattr(watch, "curve_state", lambda _: None)
    assert watch.main() == 0
    assert watch.get_meta(conn, "after_rowid") == "1"
    assert conn.execute("SELECT attempt_id FROM signals").fetchone()[0] == "attempt-1"
    conn.close()
    ledger.close()
