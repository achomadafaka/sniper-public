"""The Solana arrival lane must fail over, back off, and count what it actually delivered.

Live failure this fixes: the lane hardcoded the CUSTODY rpc url. When that key's plan was spent it
could not fail over at all and sat in a flat 10s reconnect loop from 2026-08-25 11:31 — 21 hours
with no Solana ticks and 1,002 identical log lines, while three healthy endpoints went unused.
"""
from __future__ import annotations

import pathlib

import pytest

from sniper.services import memecoin_tick_stream as ts

LOOP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"


class _Stop(Exception):
    """Break the lane's infinite loop from inside the sleep."""


def _run(mints, *, fail_times, sleeps, sessions, max_iters=6, backoff_max_s=300.0):
    calls = {"n": 0}

    def sleep_fn(delay):
        sleeps.append(delay)
        if len(sleeps) >= max_iters:
            raise _Stop()

    def conn_factory():
        raise AssertionError("should not be reached; mints_fn is empty in these tests")

    def mints_fn():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise ConnectionError("server rejected WebSocket connection: HTTP 429")
        return mints

    with pytest.raises(_Stop):
        ts.run_solana_arrival_lane(
            ws_url="wss://example/rpc", ticks_conn_factory=conn_factory,
            mints_fn=mints_fn, now_fn=lambda: "2026-08-26T12:00:00+00:00",
            sleep_fn=sleep_fn, backoff_max_s=backoff_max_s,
            on_session=lambda ticks, watched, error: sessions.append((ticks, watched, error)))


def test_a_failing_lane_backs_off_instead_of_hammering():
    """1,002 identical failures in a day is a flat retry against a provider that already said no."""
    sleeps, sessions = [], []
    _run([], fail_times=99, sleeps=sleeps, sessions=sessions, max_iters=5)
    assert sleeps == [20.0, 40.0, 80.0, 160.0, 320.0][:5] or sleeps[0] < sleeps[-1]
    assert sleeps[0] == 20.0, "first retry doubles from the 10s base"
    assert all(b >= a for a, b in zip(sleeps, sleeps[1:])), "delay must be monotone"


def test_the_backoff_is_capped():
    sleeps, sessions = [], []
    _run([], fail_times=99, sleeps=sleeps, sessions=sessions, max_iters=6, backoff_max_s=60.0)
    assert max(sleeps) == 60.0, "an unbounded backoff becomes an outage nobody notices"


def test_a_successful_session_resets_the_backoff():
    """A lane that recovers must return to a fast cadence, not stay punished."""
    sleeps, sessions = [], []
    _run([], fail_times=3, sleeps=sleeps, sessions=sessions, max_iters=5)
    assert sleeps[:3] == [20.0, 40.0, 80.0]
    assert sleeps[3] == 10.0, "a completed session clears the backoff"


def test_every_retry_still_logs_so_a_dead_lane_is_never_silent(caplog):
    sleeps, sessions = [], []
    with caplog.at_level("WARNING"):
        _run([], fail_times=99, sleeps=sleeps, sessions=sessions, max_iters=4)
    lines = [r for r in caplog.records if "tickstream solana lane error" in r.getMessage()]
    assert len(lines) == 4, "backing off must reduce the RATE of retries, not stop reporting"


def test_the_session_hook_reports_delivery_and_errors():
    """A subscription id is not delivery. publicnode returns a healthy id and zero events forever
    (2026-08-07), so only a row count separates a dead endpoint from a quiet market."""
    sleeps, sessions = [], []
    _run([], fail_times=1, sleeps=sleeps, sessions=sessions, max_iters=3)
    assert sessions[0][2] is not None, "the failed session reports its error"
    assert sessions[1] == (0, 0, None), "an empty watch set delivers nothing, cleanly"


# ------------------------------------------------------------------ the wiring

def test_the_lane_no_longer_hardcodes_the_custody_key():
    src = LOOP.read_text()
    start = src.index("def _tickstream_sol_worker():")
    body = src[start:start + 3000]
    assert 'ws_url_from_read_rpc(solana_read_rpc())' in body, \
        "the lane must select a read endpoint, not the signing key"
    assert 'ws_url = (MEMECOIN_SOLANA_RPC_URL or "").replace' not in body, \
        "custody must not be the primary again"
    # custody may remain as a last resort
    assert "MEMECOIN_SOLANA_RPC_URL" in body


def test_the_wiring_reports_delivery_by_row_count():
    src = LOOP.read_text()
    start = src.index("def _tickstream_sol_worker():")
    body = src[start:start + 3000]
    assert "on_session=" in body
    assert "tickstream solana delivery" in body, "delivery must be observable, not inferred"
