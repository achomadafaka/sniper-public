"""Exit-sweep wall time in the heartbeat (2026-10-21, Gamma/Alpha): the 'before' half of the LP-exit
latency gate. Observation only: no knob, no decision reads it."""
from __future__ import annotations

import inspect
import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_runner as runner


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_two_sweeps_reach_the_heartbeat_with_n_and_quantiles(tmp_path):
    rr.reset_exit_sweep_timing()
    conn = _ledger()
    for _ in range(2):
        rr.real_exit_sweep(conn, {}, now_fn=lambda: "2026-10-21T12:00:00+00:00")
    stats = rr.exit_sweep_timing()
    lane = stats["periodic_sweep"]
    assert lane["n"] == 2 and lane["count"] == 2
    assert 0 <= lane["p50_ms"] <= lane["p90_ms"] <= lane["max_ms"]
    path = tmp_path / "hb.json"
    runner.write_heartbeat({}, auth_ok=True, path=str(path), exit_sweep_timing=stats)
    beat = json.loads(path.read_text())
    assert beat["exit_sweep_timing"]["periodic_sweep"]["n"] == 2


def test_a_sweep_that_raises_is_still_timed():
    rr.reset_exit_sweep_timing()
    try:
        rr.real_exit_sweep(None, {"solana": {}}, now_fn=lambda: "x")
    except Exception:
        pass
    assert rr.exit_sweep_timing()["periodic_sweep"]["count"] == 1


def test_lanes_are_never_pooled():
    rr.reset_exit_sweep_timing()
    conn = _ledger()
    rr.real_exit_sweep(conn, {}, now_fn=lambda: "2026-10-21T12:00:00+00:00", lane="hot_tp")
    rr.real_exit_sweep(conn, {}, now_fn=lambda: "2026-10-21T12:00:00+00:00")
    stats = rr.exit_sweep_timing()
    assert stats["hot_tp"]["n"] == 1 and stats["periodic_sweep"]["n"] == 1


def test_the_sweep_source_is_still_inspectable_and_both_heartbeat_sites_pass_it():
    assert "def real_exit_sweep(" in inspect.getsource(rr.real_exit_sweep)
    body = open("scripts/memecoin_runner_loop.py").read()
    assert body.count("exit_sweep_timing=exit_sweep_timing()") == 2


def test_quantiles_are_sorted_outside_the_lock(monkeypatch):
    """Owed nit (Gamma): exit_sweep_timing copies under the lock and sorts after releasing it, so a
    sweep thread recording its time never waits on a sort."""
    rr.reset_exit_sweep_timing()
    conn = _ledger()
    for _ in range(3):
        rr.real_exit_sweep(conn, {}, now_fn=lambda: "2026-10-21T12:00:00+00:00")

    held = []

    class Spy:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            held.pop()

    monkeypatch.setattr(rr, "_SWEEP_TIMING_LOCK", Spy())
    real_sorted = sorted

    class Window(list):
        def sort(self, *a, **kw):
            assert not held, "sorted while holding the sweep-timing lock"
            return super().sort(*a, **kw)

    monkeypatch.setattr(rr, "list", Window, raising=False)
    monkeypatch.setattr(rr, "sorted", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("sorted() inside the lock")) if held else real_sorted(*a, **kw), raising=False)
    assert rr.exit_sweep_timing()["periodic_sweep"]["n"] == 3
