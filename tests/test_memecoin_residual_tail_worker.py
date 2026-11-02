"""The per-close timer: it must measure, and it must never cost the funded path anything."""
import sqlite3

import pytest

from sniper.services import memecoin_residual_tail_worker as W


@pytest.fixture(autouse=True)
def _reset():
    W._ACTIVE.clear()
    for k in W.COUNTS:
        W.COUNTS[k] = 0
    yield
    W._ACTIVE.clear()


class Clock:
    """Monotonic that jumps instead of sleeping, so horizons are exercised in real time."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 0.001
        return self.t


def _conn():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


def _close(pid=1):
    return {"position_id": pid, "mint": "MINT", "network_id": "solana",
            "closed_at": "2026-08-27T12:00:00+00:00", "exit_qty": 1000.0,
            "entry_usdc": 20.0, "exit_usdc": 25.0}


def _inline(fn, close, args):
    fn(close, **args)


class _Keep:
    """The worker OWNS and closes its connection — condition 1, and correct. An in-memory database
    dies with that close, so the test hands it a handle whose close() is a no-op and inspects the
    same database afterwards. The worker's behaviour is unchanged; only the teardown is deferred.
    """
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _run_one(conn, quote_out, *, monkeypatch, close=None, fee=1.0):
    monkeypatch.setattr(W.time, "sleep", lambda s: None)      # horizons without wall-clock
    return W.spawn_for_close(
        close or _close(), connect_fn=lambda: _Keep(conn), decimals_fn=lambda m: 6,
        # the real seam's shape: Jupiter returns {"outAmount": <atomic USDC>} or None
        exit_quote_fn=lambda mint, atomic, slip: (
            None if quote_out(atomic) is None
            else {"outAmount": int(quote_out(atomic) * 1e6)}),
        now_fn=lambda: "2026-08-27T12:00:01+00:00", fee_buffer_pct=fee,
        monotonic=Clock(), spawn_fn=_inline)


def _samples(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_residual_tail_samples ORDER BY horizon_s")]


# ------------------------------------------------------------------ the cap

def test_at_the_cap_a_close_is_dropped_counted_and_never_queued():
    W._ACTIVE.update(range(W.MAX_WORKERS))
    assert W.spawn_for_close(_close(99), connect_fn=None, decimals_fn=None,
                             exit_quote_fn=None, now_fn=None, spawn_fn=_inline) is False
    assert W.COUNTS["dropped_at_cap"] == 1
    assert 99 not in W._ACTIVE, "a dropped close must not be left registered"


def test_the_same_close_never_gets_two_clocks():
    W._ACTIVE.add(7)
    assert W.spawn_for_close(_close(7), connect_fn=None, decimals_fn=None, exit_quote_fn=None,
                             now_fn=None, spawn_fn=_inline) is False
    assert W.COUNTS["spawned"] == 0


def test_a_worker_always_deregisters_even_when_it_dies(monkeypatch):
    def boom():
        raise RuntimeError("no connection")
    W.spawn_for_close(_close(3), connect_fn=boom, decimals_fn=lambda m: 6,
                      exit_quote_fn=lambda *a: None, now_fn=lambda: "x", spawn_fn=_inline)
    assert W._ACTIVE == set(), "a leaked registration would permanently consume a cap slot"
    assert W.COUNTS["died"] == 1


def test_a_dead_worker_never_retries(monkeypatch):
    calls = []

    def quote(atomic):
        calls.append(atomic)
        raise RuntimeError("provider down")
    conn = _conn()
    _run_one(conn, quote, monkeypatch=monkeypatch)
    # the resolve's first probe raises inside resolve_residual, which types it and stops
    assert len(calls) <= W.MAX_WORKERS, "a retry loop would show up as repeated probes"


# ------------------------------------------------------------------ the measurement

def test_it_records_one_sample_per_horizon_at_the_residual_quantity(monkeypatch):
    conn = _conn()
    # convex book: proceeds grow sublinearly, so the residual is a real partial quantity
    quoted = []

    def quote(atomic):
        quoted.append(atomic)
        f = atomic / 1_000_000_000
        return 40.0 * f * (1.0 - 0.4 * f)
    assert _run_one(conn, quote, monkeypatch=monkeypatch) is True
    rows = _samples(conn)
    assert [r["horizon_s"] for r in rows] == list(W.HORIZONS_S)
    assert all(r["status"] == W.STATUS_HEALTHY for r in rows)
    residual = int(rows[0]["residual_qty_atomic"])
    assert residual > 0
    # every horizon priced the RESIDUAL, not the full bag
    assert quoted[-len(W.HORIZONS_S):] == [residual] * len(W.HORIZONS_S)


def test_a_bag_that_never_covers_principal_records_one_row_not_six(monkeypatch):
    conn = _conn()
    _run_one(conn, lambda atomic: 0.01, monkeypatch=monkeypatch)   # never reaches 20 USDC
    rows = _samples(conn)
    assert len(rows) == 1 and rows[0]["horizon_s"] == 0.0
    assert rows[0]["status"] == W.STATUS_NO_RESIDUAL
    assert rows[0]["resolve_status"] == "unreachable"


def test_no_route_is_typed_and_never_a_zero(monkeypatch):
    conn = _conn()
    _run_one(conn, lambda atomic: None, monkeypatch=monkeypatch)
    rows = _samples(conn)
    assert len(rows) == 1 and rows[0]["status"] == W.STATUS_ROUTE_ABSENT
    assert rows[0]["residual_usdc"] is None


def test_a_horizon_is_never_overwritten(monkeypatch):
    """A restart may fill a gap; it may never replace a measurement with a later one."""
    conn = _conn()

    def quote(atomic):
        f = atomic / 1_000_000_000
        return 40.0 * f * (1.0 - 0.4 * f)
    _run_one(conn, quote, monkeypatch=monkeypatch)
    first = [r["residual_usdc"] for r in _samples(conn)]
    W._ACTIVE.clear()
    _run_one(conn, lambda a: 999.0, monkeypatch=monkeypatch)   # a wildly different second pass
    assert [r["residual_usdc"] for r in _samples(conn)] == first


def test_the_fee_buffer_raises_the_principal_target(monkeypatch):
    conn_a, conn_b = _conn(), _conn()

    def quote(atomic):
        f = atomic / 1_000_000_000
        return 40.0 * f * (1.0 - 0.4 * f)
    _run_one(conn_a, quote, monkeypatch=monkeypatch, fee=0.0)
    W._ACTIVE.clear()
    _run_one(conn_b, quote, monkeypatch=monkeypatch, fee=10.0)
    a, b = _samples(conn_a)[0], _samples(conn_b)[0]
    assert b["resolve_fraction"] > a["resolve_fraction"], "a bigger buffer must sell more"
    assert int(b["residual_qty_atomic"]) < int(a["residual_qty_atomic"])


def test_the_snapshot_reports_activity_so_a_stopped_collector_is_visible():
    s = W.snapshot()
    assert set(s) >= {"spawned", "dropped_at_cap", "completed", "died", "samples", "active"}
