"""Roster history (cohort C prereg Amendment 1 226c0144e): baseline at T, a row only on change, as-of reads
see only rows at <= cut, and every unknown case is a reason, never the current state."""
from sniper.services import memecoin_desk_roster_history as R

A = (0, 0, 1, "solana")
B = (1, 1, 0, None)
T = 1000.0


def store(tmp_path):
    return R.open_store(tmp_path / "roster.db")


def test_baseline_then_rows_only_on_change(tmp_path):
    s = store(tmp_path)
    last = R.poll_once(s, {"u1": A, "u2": A}, T)
    last = R.poll_once(s, {"u1": A, "u2": A}, T + 30, last)
    last = R.poll_once(s, {"u1": B, "u2": A}, T + 60, last)
    kinds = s.execute("SELECT source_user_id, at, kind FROM roster_events ORDER BY rowid").fetchall()
    assert kinds == [("u1", T, "baseline"), ("u2", T, "baseline"), ("u1", T + 60, "change")]


def test_as_of_reads_never_see_a_later_state(tmp_path):
    s = store(tmp_path)
    last = R.poll_once(s, {"u1": A}, T)
    for k in range(1, 5):
        last = R.poll_once(s, {"u1": A if k < 3 else B}, T + 30 * k, last)
    assert R.state_at(s, "u1", T + 89)[0]["excluded"] == 0          # change observed at T+90 is not seen
    assert R.state_at(s, "u1", T + 100) == (None, "roster_unknown")  # within LAG_S after the observed change
    assert R.state_at(s, "u1", T + 120)[0]["excluded"] == 1


def test_before_baseline_heartbeat_gap_and_unknown_wallet(tmp_path):
    s = store(tmp_path)
    R.poll_once(s, {"u1": A}, T)
    assert R.state_at(s, "u1", T - 1) == (None, "grade_asof_unknown")
    assert R.state_at(s, "u1", T + 91) == (None, "roster_unknown")  # no poll for over GAP_S
    assert R.state_at(s, "nobody", T + 10) == (None, "not_on_roster")


def test_a_removed_wallet_is_not_on_the_roster_after_its_row(tmp_path):
    s = store(tmp_path)
    last = R.poll_once(s, {"u1": A, "u2": A}, T)
    R.poll_once(s, {"u1": A}, T + 30, last)
    assert R.state_at(s, "u2", T + 31) == (None, "not_on_roster")
    assert R.state_at(s, "u2", T + 29)[0] is not None


def test_worker_once_writes_baseline_and_heartbeat_and_a_failed_read_writes_nothing(tmp_path):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_desk_roster_history_worker as Wk
    assert Wk.main(["--state-dir", str(tmp_path), "--once"], clock=lambda: T, read=lambda: {"u1": A}) == 0
    assert json.loads((tmp_path / "roster_history.heartbeat").read_text()) == {"ts": T, "wallets": 1}

    def boom():
        raise OSError("ledger busy")
    assert Wk.main(["--state-dir", str(tmp_path), "--once"], clock=lambda: T + 30, read=boom) == 0
    s = R.open_store(tmp_path / "roster_history.db")
    assert s.execute("SELECT COUNT(*) FROM roster_polls").fetchone()[0] == 1
    assert json.loads((tmp_path / "roster_history.heartbeat").read_text())["ts"] == T


def test_the_real_reader_opens_the_ledger_read_only(tmp_path, monkeypatch):
    import sqlite3
    from sniper.persistence import db_core, db_memecoin
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE memecoin_wallets(source_user_id TEXT, paused INT, excluded INT, real_approved INT, real_chains TEXT)")
        c.execute("INSERT INTO memecoin_wallets VALUES('u1',0,0,1,'solana')")
    conn = db_core.get_readonly_connection(str(path))
    assert db_memecoin.roster_state_rows(conn) == {"u1": (0, 0, 1, "solana")}
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO memecoin_wallets VALUES('u2',0,0,0,NULL)")



def test_a_slow_read_is_stamped_when_it_returns_never_before(tmp_path):
    # Delta witness on 2233b7723: the poll time was taken before the read
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_desk_roster_history_worker as Wk
    clock = [T]

    def read():
        clock[0] += 10
        return {"u1": A}
    assert Wk.main(["--state-dir", str(tmp_path), "--once"], clock=lambda: clock[0], read=read) == 0
    s = R.open_store(tmp_path / "roster_history.db")
    assert R.state_at(s, "u1", T + 5) == (None, "grade_asof_unknown")
    assert R.state_at(s, "u1", T + 10)[0] is not None
