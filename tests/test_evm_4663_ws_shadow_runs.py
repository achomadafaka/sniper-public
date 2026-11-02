"""WS uptime belongs to its own run (2026-10-08 review SF01).

up_intervals ignored run_id and closed an interval left open at MAX(stopped_at) over EVERY run, so
on a reused DB socket A's session from run 1 ran on through run 2 (where A was never opened) and a
run-2 log read as "MISSED while up" for A. Now each run's sessions close inside that run: at its
stopped_at, at `now` while it is the live run, or at its last recorded event when it stopped without
a record (a crash: nothing after that is known). A normal session end also writes its own `down`.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import evm_4663_ws_shadow as sh  # noqa: E402
from test_evm_4663_ws_shadow import Clock, FakeWS, PM, INIT, TRADER, Pacer, log  # noqa: E402


def _db(runs, events, logs=()):
    conn = sqlite3.connect(":memory:")
    conn.executescript(sh.SCHEMA)
    for run_id, started, ends, stopped in runs:
        conn.execute("INSERT INTO runs(run_id, started_at, ends_at, stopped_at) VALUES (?,?,?,?)",
                     (run_id, started, ends, stopped))
    for run_id, socket, at, kind, epoch in events:
        conn.execute("INSERT INTO events(run_id, socket, at, kind, epoch) VALUES (?,?,?,?,?)",
                     (run_id, socket, at, kind, epoch))
    for tx, at, epoch in logs:
        conn.execute("INSERT INTO logs(tx, log_index, sub, epoch, first_at) VALUES (?,0,'transfers',?,?)",
                     (tx, epoch, at))
    return conn


TWO_RUNS = [("r1", 100.0, 300.0, 200.0), ("r2", 1000.0, 1200.0, 1100.0)]


def test_a_session_left_open_ends_with_its_own_run():
    conn = _db(TWO_RUNS, [("r1", "A", 100.0, "up", 1), ("r2", "B", 1000.0, "up", 1)])
    assert sh.up_intervals(conn, "A", now=5000.0) == [(100.0, 200.0, 1)]
    assert not sh.was_up(sh.up_intervals(conn, "A", now=5000.0), 1050.0, 1)


def test_a_later_runs_log_is_down_for_a_socket_that_run_never_opened():
    conn = _db(TWO_RUNS, [("r1", "A", 100.0, "up", 1), ("r2", "B", 1000.0, "up", 1)],
               logs=[("0xlate", 1050.0, 1)])
    out = "\n".join(sh.report(conn, ["A"]))
    assert "MISSED while up 0" in out and "socket down 1" in out, out


def test_the_same_sockets_over_two_runs_count_each_runs_uptime_only():
    conn = _db(TWO_RUNS, [("r1", "A", 110.0, "up", 1), ("r2", "A", 1010.0, "up", 1)])
    assert sh.up_intervals(conn, "A", now=5000.0) == [(110.0, 200.0, 1), (1010.0, 1100.0, 1)]


def test_a_repeated_epoch_number_never_joins_two_runs():
    conn = _db(TWO_RUNS, [("r1", "A", 110.0, "up", 1), ("r2", "A", 1010.0, "up", 1)])
    intervals = sh.up_intervals(conn, "A", now=5000.0)
    assert not sh.was_up(intervals, 500.0, 1)          # between the runs: no session


def test_a_crashed_run_ends_at_its_last_event_not_the_next_run():
    runs = [("r1", 100.0, 300.0, None), ("r2", 1000.0, 1200.0, 1100.0)]
    conn = _db(runs, [("r1", "A", 110.0, "up", 1), ("r1", "roster", 150.0, "epoch", 1),
                      ("r2", "B", 1000.0, "up", 1)])
    assert sh.up_intervals(conn, "A", now=5000.0) == [(110.0, 150.0, 1)]


def test_the_live_run_counts_up_to_now():
    runs = [("r1", 100.0, 300.0, 200.0), ("r2", 1000.0, 9000.0, None)]
    conn = _db(runs, [("r2", "A", 1010.0, "up", 1)])
    assert sh.up_intervals(conn, "A", now=1500.0) == [(1010.0, 1500.0, 1)]


def test_a_normal_session_end_writes_its_own_down():
    clock, pacer = Clock(), Pacer()
    shadow = sh.Shadow(db_path=":memory:", sockets={"drpc": "wss://x"}, roster=sh.Roster(lambda: [TRADER]),
                       pool_manager=PM, init_topic=INIT, pacer=pacer, clock=clock, run_id="t")
    shadow.connect_fn = lambda url, open_timeout=10: FakeWS(shadow, clock, [(1, 1, log("0xaa", 1))])
    shadow.run_socket("drpc", "wss://x")
    kinds = []
    while not shadow.q.empty():
        item = shadow.q.get()
        if item[0] == "event":
            kinds.append((item[3], item[5]))
    assert ("up", kinds[1][1]) in kinds and kinds[-1] == ("down", "session_end"), kinds
