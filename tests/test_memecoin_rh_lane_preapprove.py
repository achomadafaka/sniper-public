"""4663 fast-lane fills get the token pre-approval the ordinary V4 entry gets (Gamma 2026-10-08:
pos 71780's b_stop paid approve_n=1 / 450 ms on the exit; Alpha GO). Same knobs:
MEMECOIN_EVM_PREAPPROVE_CHAINS and the approval pipeline. The lane books from the receipt with no
evm_route_json, and its own buys are V4 swaps, so it names the route protocol itself."""
import sqlite3
import threading

import pytest

import test_memecoin_evm_preapprove as P
from test_memecoin_rh_fast_live import Chain, _accepting_sender, _live, _run  # noqa: F401
from sniper.persistence import db_memecoin as db


def test_a_lane_booked_position_without_a_route_is_approved_when_the_lane_names_v4(P_env, tmp_path):
    path, c = P._db(tmp_path)
    pid = P._open(c, route=None)                     # how the lane books: no evm_route_json
    http, seen = P._http()
    ex = P._ex(http)
    ex["preapprove_position_fn"](path, pid, route_protocol="uniswap_v4")
    assert len(seen["sends"]) == 1
    [row] = P._rows(c)
    assert row["send_status"] == "accepted" and row["spender"] == P.PERMIT2


@pytest.fixture
def P_env(monkeypatch):
    return P.env.__wrapped__(monkeypatch)


def _file_ledger(tmp_path):
    c = sqlite3.connect(str(tmp_path / "lane.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    return c


def _knobs(monkeypatch, *, preapprove="4663", pipeline="1"):
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", pipeline)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", preapprove)


def _recorder():
    calls, done = [], threading.Event()

    def preapprove(db_path, position_id, spender=None, route_protocol=None):
        calls.append((db_path, position_id, route_protocol, threading.current_thread().name))
        done.set()
    return calls, done, preapprove


def test_a_lane_fill_starts_the_pre_approval_for_its_position(monkeypatch, tmp_path):
    _knobs(monkeypatch)
    conn = _file_ledger(tmp_path)
    live = _live(conn, _accepting_sender(), Chain(), lock_dir=str(tmp_path))
    calls, done, live.ctx["preapprove_position_fn"] = _recorder()
    result, rec = _run(live, conn)
    assert result["status"] == "filled", result
    assert done.wait(2.0), "no pre-approval started"
    (db_path, pid, protocol, thread), = calls
    assert db_path == str(tmp_path / "lane.db") and pid == result["position_id"]
    assert protocol == "uniswap_v4" and thread.startswith("rh-lane-preapprove")


@pytest.mark.parametrize("knobs", [dict(preapprove=""), dict(preapprove="8453"), dict(pipeline="0")])
def test_no_pre_approval_without_both_knobs_on_this_chain(monkeypatch, tmp_path, knobs):
    _knobs(monkeypatch, **knobs)
    conn = _file_ledger(tmp_path)
    live = _live(conn, _accepting_sender(), Chain(), lock_dir=str(tmp_path))
    calls, done, live.ctx["preapprove_position_fn"] = _recorder()
    result, rec = _run(live, conn)
    assert result["status"] == "filled" and not done.wait(0.2) and calls == []


def test_a_failing_pre_approval_never_fails_the_fill(monkeypatch, tmp_path):
    _knobs(monkeypatch)
    conn = _file_ledger(tmp_path)
    live = _live(conn, _accepting_sender(), Chain(), lock_dir=str(tmp_path))
    ran = threading.Event()

    def boom(*a, **k):
        ran.set()
        raise RuntimeError("approval read failed")
    live.ctx["preapprove_position_fn"] = boom
    result, rec = _run(live, conn)
    assert result["status"] == "filled" and ran.wait(2.0)


def test_a_pre_approval_that_cannot_start_never_fails_the_fill(monkeypatch, tmp_path):
    from sniper.services import memecoin_rh_fast_lane as lane
    _knobs(monkeypatch)
    conn = _file_ledger(tmp_path)
    live = _live(conn, _accepting_sender(), Chain(), lock_dir=str(tmp_path))
    calls, done, live.ctx["preapprove_position_fn"] = _recorder()
    real = threading.Thread

    def no_preapproval_thread(*a, **k):
        if str(k.get("name", "")).startswith("rh-lane-preapprove"):
            raise RuntimeError("can't start new thread")
        return real(*a, **k)
    monkeypatch.setattr(lane.threading, "Thread", no_preapproval_thread)
    result, rec = _run(live, conn)
    assert result["status"] == "filled" and calls == [], result


def test_the_chain_context_carries_the_executors_pre_approval():
    import inspect
    from sniper.services import memecoin_evm_leg
    assert '"preapprove_position_fn": ex.get("preapprove_position_fn")' in inspect.getsource(
        memecoin_evm_leg)
