"""Paper cumulative accounting and admission/fill serialization over temporary ledgers."""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_paper as paper
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import BOUNDS, candidate

NOW = "2026-10-27T12:00:00+00:00"
THEN = "2026-10-26T12:00:00+00:00"
COHORT = "cohort:A"


@pytest.fixture
def book(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "desk.db")
    paper.ensure(conn)
    for cid in (COHORT, "cohort:B"):
        store.register_cohort(conn, cohort_id=cid, producer="U", prompt_version="desk-judge-v27",
                              policy={"cumulative_kill_usd": -200}, created_at=THEN)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", BOUNDS)
    yield conn
    conn.close()


def request(tid="desk:U:new", cohort=COHORT):
    return {"intent_id": "paper:" + tid, "requested_at": NOW, "payload": {
        "mint": candidate()["mint"], "network_id": "solana", "size_usd": 25.0,
        "rails": {"tp_pct": 60, "sl_pct": 20, "trail_pct": None}, "desk_ticket_id": tid,
        "desk_metadata": {"cohort_id": cohort, "producer": "U", "opportunity_id": tid,
                          "snapshot_hash": "a" * 64}}}


def closed(conn, tid, pnl, *, cohort=COHORT, at=THEN):
    r = request(tid, cohort)
    conn.execute("INSERT INTO desk_paper_positions(ticket_id,intent_id,mint,network_id,cohort_id,version,state,"
                 "requested_at,size_usd,rails_json,desk_metadata_json,closed_at,pnl_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (tid, r["intent_id"], r["payload"]["mint"], "solana", cohort, paper.VERSION, "closed", THEN, 25,
                  "{}", json.dumps(r["payload"]["desk_metadata"]), at, pnl))
    conn.commit()


def gate(conn, cohort=COHORT):
    fn = getattr(paper, "cohort_refusal", None)
    assert callable(fn), "paper cumulative gate is missing"
    return fn(conn, cohort, now_iso=NOW)


def test_cumulative_loss_across_days_latches_at_exact_limit(book):
    closed(book, "loss1", -120)
    closed(book, "loss2", -79.99, at="2026-10-27T10:00:00+00:00")
    assert gate(book) is None
    closed(book, "loss3", -.01)
    assert gate(book) == "desk_cohort_killed"
    trip = book.execute("SELECT known_at,limit_usd,pnl_usd FROM desk_paper_cohort_trips").fetchone()
    assert paper._ts(trip[0]) >= paper._ts(NOW) and tuple(trip)[1:] == (-200, -200)


def test_paper_loss_is_cohort_scoped_and_open_marks_are_not_realized(book):
    closed(book, "other", -500, cohort="cohort:B")
    assert paper.admit(book, request(), now_iso=NOW) is None
    book.execute("UPDATE desk_paper_positions SET state='open',pnl_usd=-999,last_mark_usd=0 WHERE ticket_id=?",
                 ("desk:U:new",))
    book.commit()
    assert gate(book) is None
    assert gate(book, "cohort:B") == "desk_cohort_killed"


def test_kill_survives_recovery_and_database_reopen(book):
    closed(book, "loss", -200)
    assert gate(book) == "desk_cohort_killed"
    closed(book, "later_winner", 1000)
    path = book.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as reopened:
        assert gate(reopened) == "desk_cohort_killed"
        assert reopened.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 1


@pytest.mark.parametrize("pnl", [None, float("inf"), float("-inf"), "unpriced"])
def test_unknown_closed_accounting_holds_without_fabricating_a_trip(book, pnl):
    closed(book, "unknown", pnl)
    assert gate(book) == "desk_cohort_accounting_unknown"
    assert book.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 0


@pytest.mark.parametrize("limit", [None, 0, 200, "-200", True, float("nan"), float("-inf"), 10**400])
def test_missing_or_malformed_registered_limit_never_defaults(book, limit):
    # Deliberately corrupt frozen policy bytes; register_cohort itself rejects NaN/Infinity.
    book.execute("INSERT INTO desk_cohorts(cohort_id,producer,prompt_version,policy_json,created_at) VALUES(?,?,?,?,?)",
                 ("cohort:bad", "U", "desk-judge-v27", json.dumps({"cumulative_kill_usd": limit}), THEN))
    book.commit()
    assert gate(book, "cohort:bad") == "desk_cohort_policy_unknown"


def test_unregistered_cohort_holds_and_idle_gate_never_commits_foreign_work(book):
    assert gate(book, "missing") == "desk_cohort_policy_unknown"
    book.execute("INSERT INTO desk_paper_cohort_trips VALUES('foreign',?,?,?,?)", (NOW, -1, -2, "foreign"))
    with pytest.raises(ValueError, match="transaction_active"):
        gate(book)
    book.rollback()
    assert book.execute("SELECT 1 FROM desk_paper_cohort_trips WHERE cohort_id='foreign'").fetchone() is None


def test_admission_refuses_cumulative_loss_without_changing_existing_daily_reason(book):
    closed(book, "oldloss", -200)
    assert paper.admit(book, request(), now_iso=NOW) == "desk_cohort_killed"
    assert book.execute("SELECT state FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()[0] == "refused"
    assert paper.admit(book, request(), now_iso=NOW) is None  # duplicate remains one refused row
    assert book.execute("SELECT count(*) FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()[0] == 1


def test_daily_halt_still_blocks_new_admission_and_cumulative_trip_is_recorded(book):
    closed(book, "todayloss", -200, at=NOW)
    assert paper.admit(book, request(), now_iso=NOW) == "desk_daily_loss"
    assert gate(book) == "desk_cohort_killed"


def test_concurrent_admissions_cannot_overbook(book, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    path = book.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)
    def admit(i):
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=5)
            return paper.admit(conn, request("desk:U:concurrent%d" % i), now_iso=NOW)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(admit, (1, 2)))
    assert sorted(str(x) for x in result) == ["None", "desk_max_concurrent"]
    assert book.execute("SELECT count(*) FROM desk_paper_positions WHERE state='pending_fill'").fetchone()[0] == 1


def test_pending_fill_cancelled_after_kill_without_quoting_or_backdating(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    closed(book, "loss", -200)
    def forbidden(*a, **kw):
        pytest.fail("known-killed paper request must not read a quote")
    monkeypatch.setattr(paper, "_cut", forbidden)
    now = paper._ts(NOW) + 10
    [result] = paper.fill_pending(book, desk_dir=tmp_path, now=now)
    assert (result["state"], result["refusal"]) == ("refused", "desk_cohort_killed")
    row = book.execute("SELECT requested_at,filled_at,decided_at,log_json FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()
    assert row[0] == NOW and row[1] is None and paper._ts(row[2]) >= now
    assert json.loads(row[3])["intended_fill_at"] == paper._iso(paper._ts(NOW) + paper.ENTRY_DELAY_S)


def test_close_during_quote_is_rechecked_atomically_before_pending_becomes_open(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    path = book.execute("PRAGMA database_list").fetchone()[2]
    def quote(*a, **kw):
        with sqlite3.connect(path) as other:
            closed(other, "raced_close", -200)
        return {"state": "open", "filled_at": paper._iso(paper._ts(NOW) + 1)}
    monkeypatch.setattr(paper, "_fill_ps", quote)
    [result] = paper.fill_pending(book, desk_dir=tmp_path, now=paper._ts(NOW) + 2,
                                  candidate_fn=lambda _: {"venue": "pumpswap"}, fx_at=lambda _: 100)
    assert (result["state"], result["refusal"]) == ("refused", "desk_cohort_killed")
    assert book.execute("SELECT filled_at FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()[0] is None
    assert paper._ts(book.execute("SELECT known_at FROM desk_paper_cohort_trips WHERE cohort_id=?", (COHORT,)).fetchone()[0]) >= paper._ts(NOW) + 2


def test_final_guard_knowledge_clock_includes_time_spent_quoting(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    def quote(*a, **kw):
        elapsed[0] = 4.0
        closed(book, "loss_after_sweep_start", -200)
        return {"state": "open", "filled_at": paper._iso(paper._ts(NOW) + 1)}
    monkeypatch.setattr(paper, "_fill_ps", quote)
    now = paper._ts(NOW) + 2
    paper.fill_pending(book, desk_dir=tmp_path, now=now,
                       candidate_fn=lambda _: {"venue": "pumpswap"}, fx_at=lambda _: 100)
    trip = book.execute("SELECT known_at FROM desk_paper_cohort_trips WHERE cohort_id=?", (COHORT,)).fetchone()[0]
    row = book.execute("SELECT decided_at,filled_at,requested_at FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()
    assert trip == row[0] == paper._iso(now + 4)
    assert row[1] is None and row[2] == NOW


def test_kill_knowledge_clock_includes_schema_lock_wait(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    closed(book, "loss", -200)
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    ensure = paper.ensure
    def wait_for_schema(conn):
        ensure(conn)
        elapsed[0] = 3.0
    monkeypatch.setattr(paper, "ensure", wait_for_schema)
    now = paper._ts(NOW) + 2
    paper.fill_pending(book, desk_dir=tmp_path, now=now)
    known = book.execute("SELECT known_at FROM desk_paper_cohort_trips WHERE cohort_id=?", (COHORT,)).fetchone()[0]
    assert known == paper._iso(now + 3)


@pytest.mark.parametrize("entrypoint", ["public", "admit"])
def test_pre_model_and_admission_latch_clock_includes_schema_and_write_lock_wait(book, monkeypatch, entrypoint):
    closed(book, "loss", -200)
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    ensure, write = paper.ensure, store._write
    def delayed_schema(conn):
        ensure(conn)
        elapsed[0] += 3
    @contextmanager
    def delayed_write(conn):
        with write(conn):
            elapsed[0] += 2
            yield
    monkeypatch.setattr(paper, "ensure", delayed_schema)
    monkeypatch.setattr(store, "_write", delayed_write)
    if entrypoint == "public":
        assert gate(book) == "desk_cohort_killed"
    else:
        assert paper.admit(book, request(), now_iso=NOW) == "desk_cohort_killed"
    known = book.execute("SELECT known_at FROM desk_paper_cohort_trips WHERE cohort_id=?", (COHORT,)).fetchone()[0]
    assert known == paper._iso(paper._ts(NOW) + 5)


def test_two_pending_fill_workers_commit_exactly_one_transition(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    path = book.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    barrier = threading.Barrier(2)
    def quote(*a, **kw):
        barrier.wait(timeout=5)
        return {"state": "open", "filled_at": paper._iso(paper._ts(NOW) + 1)}
    monkeypatch.setattr(paper, "_fill_ps", quote)
    def fill(_):
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return paper.fill_pending(conn, desk_dir=tmp_path, now=paper._ts(NOW) + 2,
                                      candidate_fn=lambda _: {"venue": "pumpswap"}, fx_at=lambda _: 100)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(fill, (1, 2)))
    assert sorted(len(x) for x in result) == [0, 1]
    assert book.execute("SELECT count(*) FROM desk_paper_positions WHERE state='open'").fetchone()[0] == 1


def test_duplicate_fill_transition_does_not_overwrite_a_concurrent_close(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    def quote(*a, **kw):
        book.execute("UPDATE desk_paper_positions SET state='closed',pnl_usd=1,closed_at=? WHERE ticket_id='desk:U:new'", (NOW,))
        book.commit()
        return {"state": "open", "filled_at": paper._iso(paper._ts(NOW) + 1)}
    monkeypatch.setattr(paper, "_fill_ps", quote)
    assert paper.fill_pending(book, desk_dir=tmp_path, now=paper._ts(NOW) + 2,
                              candidate_fn=lambda _: {"venue": "pumpswap"}, fx_at=lambda _: 100) == []
    assert book.execute("SELECT state FROM desk_paper_positions WHERE ticket_id='desk:U:new'").fetchone()[0] == "closed"


def test_killed_cohort_keeps_open_risk_exits_running(book, monkeypatch, tmp_path):
    assert paper.admit(book, request(), now_iso=NOW) is None
    book.execute("UPDATE desk_paper_positions SET state='open',filled_at=?,qty_atomic='1000000',"
                 "entry_price_usd=25,entry_cost_usd=25,peak_price_usd=25 WHERE ticket_id='desk:U:new'", (NOW,))
    book.commit()
    closed(book, "oldloss", -200)
    assert gate(book) == "desk_cohort_killed"
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "sell_proceeds", lambda *a: (250000000, "curve"))
    now = paper._ts(NOW) + 10
    [result] = paper.tick(book, desk_dir=tmp_path, now=now, fx_at=lambda _: 100,
                          risk_exits={"desk:U:new": paper.RISK_EXIT_REASON})
    assert result["state"] == "exit_requested"
    [result] = paper.tick(book, desk_dir=tmp_path, now=now + 2, fx_at=lambda _: 100)
    assert result["state"] == "closed" and result["reason"] == paper.RISK_EXIT_REASON
    assert gate(book) == "desk_cohort_killed"


def pending_exit(conn, tid, *, qty=1000000, entry_cost=50):
    assert paper.admit(conn, request(tid), now_iso=NOW) is None
    conn.execute("UPDATE desk_paper_positions SET state='open',filled_at=?,qty_atomic=?,entry_price_usd=25,"
                 "entry_cost_usd=?,peak_price_usd=25,exit_requested_at=?,exit_reason=? WHERE ticket_id=?",
                 (NOW, str(qty), entry_cost, NOW, paper.RISK_EXIT_REASON, tid))
    conn.commit()


def test_same_tick_ordinary_loss_and_gain_cannot_erase_a_cumulative_crossing(book, monkeypatch, tmp_path):
    # One in-memory cohort: -190 -> -215 -> -165 in a single ordinary tick, with no intervening entry gate.
    conn = store.connect(":memory:")
    paper.ensure(conn)
    store.register_cohort(conn, cohort_id=COHORT, producer="U", prompt_version="desk-judge-v27",
                          policy={"cumulative_kill_usd": -200}, created_at=THEN)
    closed(conn, "seed", -190)
    pending_exit(conn, "loss", qty=1000000, entry_cost=50)
    pending_exit(conn, "gain", qty=2000000, entry_cost=25)
    elapsed = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "EXEC_COST_SOL", {"entry": 0, "exit": 0})
    def quote(_dir, _mint, qty, _at):
        assert not conn.in_transaction, "quote must stay outside the paper writer lock"
        elapsed[0] += 3
        return (250000000 if qty == 1000000 else 750000000), "curve"
    monkeypatch.setattr(paper, "sell_proceeds", quote)
    now = paper._ts(NOW) + 10
    out = paper.tick(conn, desk_dir=tmp_path, now=now, fx_at=lambda _: 100)
    assert [(r["ticket_id"], r["state"], r["pnl_usd"]) for r in out] == [
        ("loss", "closed", -25), ("gain", "closed", 50)]
    assert conn.execute("SELECT SUM(pnl_usd) FROM desk_paper_positions WHERE state='closed'").fetchone()[0] == -165
    trip = conn.execute("SELECT known_at,pnl_usd FROM desk_paper_cohort_trips WHERE cohort_id=?", (COHORT,)).fetchone()
    assert trip is not None, "ordinary close missed the temporary cumulative loss crossing"
    assert tuple(trip) == (paper._iso(now + 3), -215)
    assert paper.cohort_refusal(conn, COHORT, now_iso=paper._iso(now + 20)) == "desk_cohort_killed"
    assert conn.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 1
    conn.close()


def test_priced_close_and_trip_roll_back_together_if_latch_write_fails(book, monkeypatch, tmp_path):
    closed(book, "seed", -190)
    pending_exit(book, "loser")
    book.execute("CREATE TRIGGER fail_trip BEFORE INSERT ON desk_paper_cohort_trips "
                 "BEGIN SELECT RAISE(ABORT,'trip_write_failed'); END")
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "EXEC_COST_SOL", {"entry": 0, "exit": 0})
    monkeypatch.setattr(paper, "sell_proceeds", lambda *a: (250000000, "curve"))
    with pytest.raises(sqlite3.IntegrityError, match="trip_write_failed"):
        paper.tick(book, desk_dir=tmp_path, now=paper._ts(NOW) + 10, fx_at=lambda _: 100)
    assert book.execute("SELECT state,closed_at,pnl_usd FROM desk_paper_positions WHERE ticket_id='loser'").fetchone()[:] == (
        "open", None, None)
    assert book.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 0


def test_ordinary_close_does_not_turn_unknown_accounting_into_a_zero_or_stop_exits(book, monkeypatch, tmp_path):
    pending_exit(book, "exit")
    closed(book, "unknown", None)
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "EXEC_COST_SOL", {"entry": 0, "exit": 0})
    monkeypatch.setattr(paper, "sell_proceeds", lambda *a: (250000000, "curve"))
    [out] = paper.tick(book, desk_dir=tmp_path, now=paper._ts(NOW) + 10, fx_at=lambda _: 100)
    assert out["state"] == "closed"
    assert gate(book) == "desk_cohort_accounting_unknown"
    assert book.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 0


def test_concurrent_ordinary_close_commits_and_latches_only_once(book, monkeypatch, tmp_path):
    closed(book, "seed", -190)
    pending_exit(book, "loser")
    path = book.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)
    local = threading.local()
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "EXEC_COST_SOL", {"entry": 0, "exit": 0})
    def quote(*args):
        assert not local.conn.in_transaction
        barrier.wait(timeout=5)
        return 250000000, "curve"
    monkeypatch.setattr(paper, "sell_proceeds", quote)
    def close(_):
        local.conn = sqlite3.connect(path, timeout=5)
        local.conn.row_factory = sqlite3.Row
        try:
            return paper.tick(local.conn, desk_dir=tmp_path, now=paper._ts(NOW)+10, fx_at=lambda _: 100)
        finally:
            local.conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(close, (1, 2)))
    assert sorted(map(len, out)) == [0, 1]
    assert book.execute("SELECT SUM(pnl_usd) FROM desk_paper_positions WHERE state='closed'").fetchone()[0] == -215
    assert book.execute("SELECT count(*) FROM desk_paper_cohort_trips").fetchone()[0] == 1


def test_a_preceding_mark_does_not_hold_the_writer_while_another_exit_quotes(book, monkeypatch, tmp_path):
    closed(book, "seed", -190)
    pending_exit(book, "mark", entry_cost=25)
    book.execute("UPDATE desk_paper_positions SET exit_requested_at=NULL,exit_reason=NULL WHERE ticket_id='mark'")
    book.commit()
    pending_exit(book, "loser")
    monkeypatch.setattr(paper, "_cut", lambda _d, now, _v: now)
    monkeypatch.setattr(paper, "EXEC_COST_SOL", {"entry": 0, "exit": 0})
    def quote(*args):
        assert not book.in_transaction, "a preceding mark leaked its writer lock into the next quote"
        return 250000000, "curve"
    monkeypatch.setattr(paper, "sell_proceeds", quote)
    [out] = paper.tick(book, desk_dir=tmp_path, now=paper._ts(NOW)+10, fx_at=lambda _: 100)
    assert (out["ticket_id"], out["state"]) == ("loser", "closed")


def test_tick_schema_setup_cannot_commit_foreign_work(book, tmp_path):
    book.execute("INSERT INTO desk_paper_cohort_trips VALUES('foreign',?,?,?,?)", (NOW, -1, -2, "foreign"))
    with pytest.raises(ValueError, match="transaction_active"):
        paper.tick(book, desk_dir=tmp_path, now=paper._ts(NOW)+10, fx_at=lambda _: 100)
    book.rollback()
    assert book.execute("SELECT 1 FROM desk_paper_cohort_trips WHERE cohort_id='foreign'").fetchone() is None
