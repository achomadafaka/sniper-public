"""The receipt flush runs AFTER the funded work of a cycle, rate-limited, and cannot reach the loop.

A measurement must never delay or break an entry: the flush is paced, its failure is counted, and
its exception never leaves the helper (CLAUDE.md: research writes run after the funded hooks).
"""
from pathlib import Path

import sqlite3

import scripts.memecoin_runner_loop as loop


def test_the_flush_is_paced_and_only_fires_once_per_interval():
    calls = []
    clock = {"t": 1000.0}
    feed = type("F", (), {"flush_receipts": lambda self: calls.append(1) or 1})()
    state = {"at": 0.0}
    for _ in range(5):
        loop.flush_j7_receipts(feed, state, now_fn=lambda: clock["t"], interval_s=5.0)
    assert len(calls) == 1
    clock["t"] += 5.1
    loop.flush_j7_receipts(feed, state, now_fn=lambda: clock["t"], interval_s=5.0)
    assert len(calls) == 2


def test_a_flush_failure_is_counted_and_never_raises():
    def boom(self):
        raise sqlite3.OperationalError("database is locked")

    feed = type("F", (), {"flush_receipts": boom})()
    state = {"at": 0.0}
    loop.flush_j7_receipts(feed, state, now_fn=lambda: 10.0, interval_s=5.0)
    assert state["failed"] == 1
    loop.flush_j7_receipts(feed, state, now_fn=lambda: 100.0, interval_s=5.0)
    assert state["failed"] == 2


def test_no_feed_is_a_no_op():
    state = {"at": 0.0}
    loop.flush_j7_receipts(None, state, now_fn=lambda: 10.0, interval_s=5.0)
    assert state == {"at": 0.0}


def test_the_flush_runs_after_the_cycle_work_not_before_it():
    """Source guard with a REASON: a write before run_once would sit in front of entry submission."""
    text = Path("scripts/memecoin_runner_loop.py").read_text()
    body = text.split("    while True:")[-1]
    assert "flush_j7_receipts(" in body
    assert body.index("res = run_once(") < body.index("flush_j7_receipts(")
