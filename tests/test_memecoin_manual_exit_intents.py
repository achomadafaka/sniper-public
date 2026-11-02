"""Only the runner may sell (2026-09-05). The web's Close and Sell-a-fraction buttons write an
INTENT; the runner claims it and sells with its own venue executors and its own config.

Same reason as the buy intent (position 44706): the web process holds settings captured at import
and deploys do not restart it. The retry ladder the web used to run in-request (2/5/10/30 s on an
unsigned stuck ladder) is kept as RE-QUEUES across runner passes; a signed result is never retried.
"""
from __future__ import annotations
import inspect
import json
import pathlib
import sqlite3
import threading

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_exit_intents as xi
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client

T0 = "2026-09-05T09:00:00+00:00"


def _at(sec):
    return "2026-09-05T09:%02d:%02d+00:00" % (sec // 60, sec % 60)


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, mint="MintX", uid="u1", network="4663"):
    pid = m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                          entry_price=0.01, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at=T0,
                          strategy="alpha_b", execution_mode="real", network_id=network)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.01, qty=1000.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=T0, tx_sig="E")
    return pid


# --- the intent row ---

def test_a_close_intent_is_inputs_only_and_a_partial_needs_a_fraction_in_0_1():
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    row = xi.read_status(c, iid)
    assert row["state"] == "queued" and row["action"] == "close" and row["fraction"] == 1.0
    for bad in (None, 0.0, 1.0, 1.5, "x"):
        with pytest.raises(ValueError):
            xi.enqueue(c, position_id=pid, action="partial", fraction=bad, now_iso=T0)
    with pytest.raises(ValueError):
        xi.enqueue(c, position_id=pid, action="sell_everything", now_iso=T0)
    assert xi.read_status(c, xi.enqueue(c, position_id=pid, action="partial", fraction=0.25, now_iso=T0))["fraction"] == 0.25


def test_an_unclaimed_intent_expires_with_a_reason_and_a_dead_claim_is_released():
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0, ttl_s=60)
    assert xi.expire_stale(c, now_iso=_at(61))["expired"] == 1
    assert xi.read_status(c, iid)["state"] == "expired"
    iid2 = xi.enqueue(c, position_id=pid, action="close", now_iso=_at(0), ttl_s=600)
    assert xi.claim_next(c, now_iso=_at(1))["intent_id"] == iid2
    assert xi.expire_stale(c, now_iso=_at(1 + int(xi.CLAIM_STALE_S) + 1))["released"] == 1
    assert xi.read_status(c, iid2)["state"] == "queued"


# --- the drain: the runner's side ---

def _drain(c, exit_results=None, partial_results=None, now=None, demoted=None, notes=None):
    calls = {"exit": [], "partial": []}
    exit_results = list(exit_results or [])
    partial_results = list(partial_results or [])
    def exit_fn(conn, pos, reason, now_iso):
        calls["exit"].append((pos["id"], reason))
        return exit_results.pop(0) if exit_results else {"status": "closed", "realized": 1.0, "sig": "S"}
    def partial_fn(conn, pos, fraction, now_iso):
        calls["partial"].append((pos["id"], fraction))
        return partial_results.pop(0) if partial_results else {"status": "derisked", "sig": "P"}
    clock = {"t": 0}
    def now_fn():
        clock["t"] += 1
        return _at(clock["t"]) if now is None else now()
    done = xi.drain(c, exit_fn, partial_fn, now_fn=now_fn, position_fn=m.get_position,
                    notify_fn=(lambda a, d: notes.append((a, d))) if notes is not None else None,
                    demotion_fn=(lambda conn, uid, network_id=None: demoted.append((uid, network_id))) if demoted is not None else None)
    return done, calls


def test_a_clean_close_resolves_done_notifies_and_runs_the_demotion_check():
    c = _conn(); pid = _open_real(c); demoted = []; notes = []
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    done, calls = _drain(c, [{"status": "closed", "realized": -3.38, "sig": "SigOK"}], demoted=demoted, notes=notes)
    assert done == [(iid, "done")] and calls["exit"] == [(pid, "manual")]
    row = xi.read_status(c, iid)
    assert row["result_status"] == "closed" and abs(row["realized"] + 3.38) < 1e-9 and row["sig"] == "SigOK"
    assert demoted == [("u1", "4663")] and notes and "MANUAL CLOSE" in notes[0][0]


def test_an_unsigned_stuck_ladder_is_requeued_on_the_old_schedule_then_refused():
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0, ttl_s=3600)
    # four stuck answers -> four re-queues with 2/5/10/30 s holds; the fifth is refused as stuck
    seen = []
    for i, expect in enumerate((2.0, 5.0, 10.0, 30.0)):
        # each pass runs a full minute after the previous hold, so the hold has elapsed
        done, calls = _drain(c, [{"status": "stuck"}], now=lambda i=i: "2026-09-05T09:%02d:00+00:00" % (10 + i))
        row = xi.read_status(c, iid)
        assert done == [(iid, "queued")] and row["state"] == "queued" and row["attempts"] == i + 1
        assert row["not_before"] == "2026-09-05T09:%02d:%02d+00:00" % (10 + i, int(expect)), "hold = the web's old schedule"
        seen.append(row["not_before"])
    assert len(set(seen)) == 4, "each retry gets its own hold"
    # a pass BEFORE the hold elapses must not claim it
    assert _drain(c, [{"status": "stuck"}], now=lambda: "2026-09-05T09:13:10+00:00")[0] == []
    # the schedule is exhausted: the next stuck resolves as refused/stuck, never a fifth retry
    done, _ = _drain(c, [{"status": "stuck"}], now=lambda: "2026-09-05T09:20:00+00:00")
    assert done == [(iid, "refused")] and xi.read_status(c, iid)["result_status"] == "stuck"


def test_a_signed_or_unknown_result_is_never_retried():
    c = _conn(); pid = _open_real(c)
    for res in ({"status": "stuck", "sig": "SigMaybe"}, {"status": "unknown", "sig": "S2"}, {"status": "submitted_unknown"}):
        iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0, ttl_s=600)
        done, calls = _drain(c, [res, {"status": "closed", "sig": "never"}])
        assert done == [(iid, "done")] and len(calls["exit"]) == 1, res
        assert xi.read_status(c, iid)["result_status"] == res["status"]


def test_unsigned_pending_exit_resumes_until_closed_without_spending_stuck_retries():
    c = _conn(); pid = _open_real(c, network="56")
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    for sec in range(0, 12, 2):
        done, calls = _drain(c, [{"status": "pending", "reason": "flap_quote_exit: step budget spent"}],
                             now=lambda sec=sec: _at(sec))
        assert done == [(iid, "queued")]
        assert len(calls["exit"]) == 1
        assert xi.read_status(c, iid)["attempts"] == 0
    done, _ = _drain(c, [{"status": "closed", "sig": "S"}], now=lambda: _at(12))
    assert done == [(iid, "done")]


def test_unsigned_pending_exit_still_expires():
    c = _conn(); pid = _open_real(c, network="56")
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0, ttl_s=5)
    _drain(c, [{"status": "pending"}], now=lambda: _at(0))
    done, calls = _drain(c, now=lambda: _at(6))
    assert not done and not calls["exit"]
    assert xi.read_status(c, iid)["state"] == "expired"


def test_signed_pending_exit_is_handed_to_recovery_without_retry():
    c = _conn(); pid = _open_real(c, network="56")
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    done, calls = _drain(c, [{"status": "pending", "tx_hash": "Leg1"}])
    assert done == [(iid, "done")] and len(calls["exit"]) == 1
    row = xi.read_status(c, iid)
    assert row["result_status"] == "pending" and row["sig"] == "Leg1"


@pytest.mark.parametrize("closed,same_sig,action,realized,expected", [
    (True, True, "close", 2.0, "done"),
    (True, True, "close_any_price", None, "done"),
    (False, True, "close", 2.0, "refused"),
    (True, False, "close", 2.0, "refused"),
    (True, True, "partial", 2.0, "refused"),
])
def test_recovery_winning_the_close_race_reports_only_the_exact_confirmed_sale(
        closed, same_sig, action, realized, expected):
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action=action, now_iso=T0,
                     fraction=0.25 if action == "partial" else None)
    calls = []
    def raced(conn, pos, *args):
        calls.append(pos["id"])
        conn.execute("UPDATE memecoin_positions SET is_open=?,closed_at=?,close_sig=?,realized_pnl=? WHERE id=?",
                     (int(not closed), _at(5) if closed else None,
                      "S" if same_sig else "different", realized, pid))
        conn.commit()
        return {"status": "already_closing", "tx_hash": "S"}
    assert xi.drain(c, raced, raced, now_fn=lambda: _at(5), position_fn=m.get_position) == [(iid, expected)]
    row = xi.read_status(c, iid)
    assert calls == [pid], "a recovered signed sale must never be resent"
    assert row["result_status"] == ("closed" if expected == "done" else "already_closing")
    assert row["realized"] == (realized if expected == "done" else None)


@pytest.mark.parametrize("missing", ["is_open", "closed_at"])
def test_incomplete_recovery_state_cannot_report_a_successful_sale(missing):
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    reads = []
    def position(conn, position_id):
        reads.append(position_id)
        if len(reads) == 1:
            return m.get_position(conn, position_id)
        settled = {"is_open": 0, "closed_at": _at(5), "close_sig": "S", "realized_pnl": 1.0}
        settled.pop(missing)
        return settled
    assert xi.drain(c, lambda *a: {"status": "already_closing", "sig": "S"}, None,
                    now_fn=lambda: _at(5), position_fn=position) == [(iid, "refused")]


def test_enqueue_wakes_the_worker_only_after_the_intent_commits(monkeypatch):
    c = _conn(); pid = _open_real(c); calls = []
    def wake(address):
        assert not c.in_transaction
        assert xi.pending_count(c) == 1
        calls.append(address)
        raise OSError("lost wake")
    monkeypatch.setattr(xi, "send_wake", wake, raising=False)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    assert calls == [xi.wake_address()]
    assert xi.read_status(c, iid)["state"] == "queued"


def test_worker_drains_on_its_own_connection_without_a_main_loop_pass(tmp_path, monkeypatch):
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    db_path = str(tmp_path / "manual-exits.db")
    seed = _conn(); pid = _open_real(seed)
    target = sqlite3.connect(db_path); seed.backup(target); target.close(); seed.close()
    wake = threading.Event(); idle = threading.Event(); done = threading.Event(); writers = []
    def observe():
        c = get_readonly_connection(db_path)
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
        idle.set()
        return c
    def connect():
        assert threading.current_thread() is not threading.main_thread()
        c = get_connection(db_path); writers.append(c); return c
    def drain(c):
        out = xi.drain(c, lambda *a: {"status": "closed", "sig": "S"}, None,
                       now_fn=lambda: _at(5), position_fn=m.get_position)
        done.set()
        return out
    worker = xi.ManualExitWorker(connect=connect, observe=observe, drain_fn=drain, wake=wake)
    monkeypatch.setattr(xi, "send_wake", lambda address: wake.set())
    worker.start()
    try:
        assert idle.wait(2)
        assert not writers, "an idle observer must never open a writable ledger"
        c = get_connection(db_path)
        iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
        assert done.wait(2), "the worker must sell without waiting for the main loop"
        assert xi.read_status(c, iid)["state"] == "done"
        c.close()
    finally:
        worker.stop()
    assert len(writers) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        writers[0].execute("SELECT 1")


def test_unhealthy_worker_fallback_cannot_duplicate_an_inflight_drain():
    c = _conn(); calls = []
    worker = xi.ManualExitWorker(connect=lambda: c, observe=lambda: c,
                                 drain_fn=lambda conn: calls.append(conn))
    worker.error = "provider"
    worker.pass_lock.acquire()
    try:
        assert worker.fallback(c) == [] and not calls
    finally:
        worker.pass_lock.release()
    worker.fallback(c)
    assert calls == [c]


def test_manual_worker_health_is_visible_in_the_runner_heartbeat(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / "heartbeat.json"
    status = {"alive": True, "healthy": True, "busy": False, "error": None}
    write_heartbeat({}, True, path=str(path), manual_exits=status)
    assert json.loads(path.read_text())["manual_exits"] == status


def test_worker_closes_a_failed_connection_before_wait_and_recovers(monkeypatch):
    from types import SimpleNamespace
    connections = []; observes = []; waits = []; calls = []
    def connection():
        c = SimpleNamespace(closed=False, commit=lambda: None, rollback=lambda: None)
        c.close = lambda: setattr(c, "closed", True)
        connections.append(c)
        return c
    def observer():
        c = SimpleNamespace(close=lambda: None)
        observes.append(c)
        return c
    def wait(seconds):
        assert all(c.closed for c in connections)
        waits.append(seconds)
    def drain(c):
        calls.append(c)
        if len(calls) == 1:
            raise OSError("RPC unavailable")
        worker.stopping.set()
    monkeypatch.setattr(xi, "pending_count", lambda c: 1)
    worker = xi.ManualExitWorker(connect=connection, observe=observer, drain_fn=drain,
                                 wake=SimpleNamespace(wait=wait, clear=lambda: None))
    worker.run()
    assert len(calls) == len(connections) == len(observes) == 2
    assert waits == [1.0, 1.0] and worker.error is None


def test_a_healthy_worker_keeps_the_main_loop_out_of_the_manual_queue():
    from types import SimpleNamespace
    worker = xi.ManualExitWorker(connect=lambda: None, observe=lambda: None,
                                 drain_fn=lambda conn: pytest.fail("main loop touched manual exits"),
                                 clock=lambda: 10.0)
    worker.thread = SimpleNamespace(is_alive=lambda: True)
    worker.last_pass = 10.0
    assert worker.fallback(None) == []


def test_a_partial_routes_to_the_partial_executor_with_its_fraction():
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="partial", fraction=0.25, now_iso=T0)
    done, calls = _drain(c, partial_results=[{"status": "derisked", "sig": "P1"}])
    assert done == [(iid, "done")] and calls["partial"] == [(pid, 0.25)] and calls["exit"] == []


def test_no_venue_an_exception_a_closed_or_paper_position_all_refuse_without_selling():
    c = _conn(); pid = _open_real(c)
    a = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    done, _ = _drain(c, [{"status": "no_venue", "network": "4663"}])
    assert done == [(a, "refused")]
    b = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    def boom(conn, pos, reason, now_iso): raise RuntimeError("rpc")
    assert xi.drain(c, boom, boom, now_fn=lambda: _at(5), position_fn=m.get_position) == [(b, "refused")]
    m.claim_close(c, pid); m.finalize_close(c, pid, _at(6), 0.0, exit_reason="manual")
    d = xi.enqueue(c, position_id=pid, action="close", now_iso=_at(7))
    done, calls = _drain(c)
    assert done == [(d, "refused")] and calls["exit"] == [] and xi.read_status(c, d)["result_status"] == "not_open"


def test_the_drain_is_bounded_per_pass():
    c = _conn(); pid = _open_real(c)
    ids = [xi.enqueue(c, position_id=pid, action="partial", fraction=0.1, now_iso=T0) for _ in range(4)]
    done, _ = _drain(c)
    assert len(done) == 2 and xi.pending_count(c) == 2


# --- the routes: the web writes a row and reports queued ---

def _client(ctx_extra=None):
    c = _conn(); ctx = {"conn": c}; ctx.update(ctx_extra or {})
    app = Flask(__name__); app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def test_the_close_route_queues_an_intent_and_touches_no_executor():
    cl, c = _client({"real_exit_intent_fn": xi.make_enqueue_exit_fn(lambda: T0)})
    pid = _open_real(c)
    r = cl.post(f"/api/memecoin/positions/{pid}/close")
    j = r.get_json()
    assert r.status_code == 202 and j["ok"] is False and j["status"] == "queued" and j["intent_id"]
    assert m.get_position(c, pid)["is_open"] == 1
    assert xi.read_status(c, j["intent_id"])["action"] == "close"
    s = cl.get(f"/api/memecoin/real/exit_intent/{j['intent_id']}").get_json()
    assert s["ok"] and s["intent"]["state"] == "queued"
    assert cl.get("/api/memecoin/real/exit_intent/nope").status_code == 404


def test_the_partial_route_queues_a_partial_intent():
    cl, c = _client({"real_exit_intent_fn": xi.make_enqueue_exit_fn(lambda: T0)})
    pid = _open_real(c)
    r = cl.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.3})
    j = r.get_json()
    assert r.status_code == 202 and j["status"] == "queued"
    assert xi.read_status(c, j["intent_id"])["fraction"] == 0.3


def test_without_the_intent_fn_a_real_close_still_fails_loud():
    cl, c = _client({"real_exit_fn": lambda *a: {"status": "closed"}})   # the OLD executor key is ignored
    pid = _open_real(c)
    assert cl.post(f"/api/memecoin/positions/{pid}/close").status_code == 503
    assert m.get_position(c, pid)["is_open"] == 1


# --- source guards: no web path may sell; the runner drains ---

def test_the_web_server_binds_no_exit_executor():
    body = pathlib.Path("sniper/web/server.py").read_text()
    assert "make_venue_exit_fn(" not in body and "make_venue_partial_exit_fn(" not in body
    assert "make_enqueue_exit_fn(" in body


def test_the_lazy_installer_installs_intents_never_executors():
    from sniper.services.memecoin_real_runner import make_real_fns_installer
    src = inspect.getsource(make_real_fns_installer)
    assert "make_real_buy_fn(" not in src and "make_venue_exit_fn(" not in src
    assert "make_enqueue_exit_fn(" in src and "make_enqueue_buy_fn(" in src


def test_the_runner_drains_exit_intents_at_the_cycle_head_and_after_buy_intents():
    """ROOM 64049 (2026-09-23) waited 14.8 s for the one drain behind a 9 s bracket sweep. The
    runner drains sells at the head of every cycle and again right after the buy intents."""
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "make_venue_exit_fn(contexts), make_venue_partial_exit_fn(contexts)" in body
    head = body.index('_drain_manual_exits("head")')
    assert body.index("while True:\n        cycle = time.time()") < head
    # Since the intent worker (desk step 2), _mi_drain lives in the worker's drain function; the
    # main loop's ONE intent call is its fallback, still between the head and bracket exit drains.
    assert head < body.index("_manual_intents.fallback(conn)") < body.index('_drain_manual_exits("bracket")')
    assert "observe=get_readonly_connection" in body
    assert body.index("_manual_exits.start()") < body.index("while True:\n        cycle = time.time()")
    fallback = body[body.index("def _drain_manual_exits(where):"):head]
    assert "_manual_exits.fallback(conn)" in fallback and "_mei_drain(" not in fallback


@pytest.mark.parametrize("fill_case,expected", [
    ("exact", "derisked"), ("other_sig", "unknown"),
    ("other_position", "unknown"), ("unpriced", "unknown"),
    ("entry", "unknown"), ("missing", "unknown"),
    ("unpriced_price", "unknown"), ("zero", "derisked"), ("paper", "unknown"),
])
def test_recovered_partial_status_uses_only_its_priced_fill(fill_case, expected):
    c = _conn(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="partial", fraction=0.25, now_iso=T0)
    xi.resolve(c, iid, state=xi.DONE, now_iso=T0, result_status="unknown", sig="SellSig")
    if fill_case != "missing":
        other = _open_real(c, mint="Other") if fill_case == "other_position" else pid
        if fill_case == "paper":
            c.execute("UPDATE memecoin_positions SET execution_mode='paper' WHERE id=?", (pid,))
        m.insert_fill(c, position_id=other,
                      kind="entry" if fill_case == "entry" else "derisk",
                      price=None if fill_case == "unpriced_price" else 0.02, qty=250,
                      notional=None if fill_case == "unpriced" else (0 if fill_case == "zero" else 5),
                      fill_source="chain", fraction=0.25, created_at=T0,
                      tx_sig="Different" if fill_case == "other_sig" else "SellSig")
    before = c.total_changes
    assert xi.read_status(c, iid)["result_status"] == expected
    assert c.total_changes == before
    assert c.execute("SELECT result_status FROM memecoin_manual_exit_intents WHERE intent_id=?", (iid,)).fetchone()[0] == "unknown"
