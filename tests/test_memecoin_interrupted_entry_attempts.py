import sqlite3

from sniper.persistence import db_memecoin as m

START = "2026-09-17T08:05:02+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _attempt(c, aid, started, **cols):
    names = ["attempt_id", "network_id", "mint", "decision_started_at", *cols]
    values = [aid, "solana", "Mint", started, *cols.values()]
    c.execute(f"INSERT INTO memecoin_entry_attempts({', '.join(names)}) "
              f"VALUES({', '.join('?' for _ in names)})", values)


def test_an_attempt_killed_by_a_restart_gets_a_terminal_status():
    # COINU 2026-09-17 08:04:49: the deploy stopped the runner 0.46s into the decision and the
    # Telegram card stayed at EVALUATING forever.
    c = _conn()
    _attempt(c, "coinu", "2026-09-17T08:04:49.249857+00:00")
    assert m.close_interrupted_entry_attempts(c, started_at=START, now_iso=START) == 1
    row = c.execute("SELECT terminal_status, terminal_reason, completed_at FROM memecoin_entry_attempts").fetchone()
    assert row["terminal_status"] == "aborted"
    assert row["terminal_reason"].startswith("interrupted")
    assert row["completed_at"] == START


def test_it_never_touches_a_reserved_finished_new_or_old_attempt():
    c = _conn()
    _attempt(c, "reserved", "2026-09-17T08:04:40+00:00")
    c.execute("INSERT INTO memecoin_pending_entries(mint, status, attempt_id, reserved_at) "
              "VALUES('Mint', 'reserved', 'reserved', '2026-09-17T08:04:41+00:00')")
    _attempt(c, "done", "2026-09-17T08:04:30+00:00", terminal_status="refused",
             terminal_reason="x", completed_at="2026-09-17T08:04:31+00:00")
    _attempt(c, "after", "2026-09-17T08:05:03+00:00")
    _attempt(c, "ancient", "2026-09-10T08:04:30+00:00")
    assert m.close_interrupted_entry_attempts(c, started_at=START, now_iso=START) == 0
    statuses = dict(c.execute("SELECT attempt_id, terminal_status FROM memecoin_entry_attempts"))
    assert statuses == {"reserved": None, "done": "refused", "after": None, "ancient": None}


def test_mixed_timestamp_formats_do_not_abort_the_restart_sweep():
    c = _conn()
    _attempt(c, "naive", "2026-09-17 08:04:45")
    _attempt(c, "offset", "2026-09-17T04:04:46-04:00")
    _attempt(c, "garbage", "not-a-time")
    _attempt(c, "new", "2026-09-17 08:05:03")
    assert m.close_interrupted_entry_attempts(c, started_at=START, now_iso=START) == 2
    statuses = dict(c.execute("SELECT attempt_id, terminal_status FROM memecoin_entry_attempts"))
    assert statuses == {"naive": "aborted", "offset": "aborted", "garbage": None, "new": None}


def test_naive_process_start_uses_the_same_utc_boundary():
    c = _conn()
    _attempt(c, "old", "2026-09-17T08:04:45Z")
    assert m.close_interrupted_entry_attempts(
        c, started_at="2026-09-17 08:05:02", now_iso=START) == 1
