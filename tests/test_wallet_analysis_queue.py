"""Universal analyzer intake queue — idempotent enqueue (one live row per wallet), atomic
lease with heartbeat + expiry reclaim, retry-with-backoff then terminal failure. This is how
'every wallet gets a full read' is enforced rather than hoped."""
from __future__ import annotations

import sqlite3

from sniper.persistence import db_wallet_analysis_queue as Q


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    Q.ensure_queue_tables(c)
    return c


def test_enqueue_dedup_live():
    c = _conn()
    assert Q.enqueue(c, "0xW", "manual", now=1) is True
    assert Q.enqueue(c, "0xW", "discovery", now=2) is False  # a live row already exists


def test_enqueue_again_after_done():
    c = _conn()
    Q.enqueue(c, "0xW", "manual", now=1)
    row = Q.lease(c, "w1", now=10)
    Q.complete(c, row["id"], now=11)
    assert Q.enqueue(c, "0xW", "manual", now=20) is True  # done -> no live row -> re-enqueue ok


def test_lease_orders_by_priority_then_age():
    c = _conn()
    Q.enqueue(c, "0xW", "manual", now=1, priority=100)
    Q.enqueue(c, "0xX", "favorite", now=2, priority=50)
    row = Q.lease(c, "w1", now=10)
    assert row["wallet"] == "0xx"  # lower priority number first; wallets normalize at enqueue
    assert row["status"] == "leased" and row["lease_owner"] == "w1"


def test_lease_none_when_empty():
    assert Q.lease(_conn(), "w1", now=10) is None


def test_expired_lease_reclaimable():
    c = _conn()
    Q.enqueue(c, "0xW", "manual", now=1)
    r1 = Q.lease(c, "w1", now=10, ttl_s=100)   # expires at 110
    assert Q.lease(c, "w2", now=50) is None     # still leased
    r2 = Q.lease(c, "w2", now=200)              # lease expired -> reclaimable
    assert r2 is not None and r2["id"] == r1["id"] and r2["lease_owner"] == "w2"


def test_heartbeat_extends_lease():
    c = _conn()
    Q.enqueue(c, "0xW", "manual", now=1)
    r = Q.lease(c, "w1", now=10, ttl_s=100)
    Q.heartbeat(c, r["id"], now=50, ttl_s=100)  # extend expiry to 150
    assert Q.lease(c, "w2", now=120) is None      # still leased (extended)


def test_fail_retries_with_backoff_then_terminal():
    c = _conn()
    Q.enqueue(c, "0xW", "manual", now=1)
    r = Q.lease(c, "w1", now=10)
    Q.fail(c, r["id"], "boom", now=10, backoff_s=100, max_attempts=2)
    assert Q.lease(c, "w2", now=50) is None         # before next_retry_ts
    r2 = Q.lease(c, "w2", now=200)                    # after backoff
    assert r2 is not None and r2["attempt_count"] == 1
    Q.fail(c, r2["id"], "boom2", now=200, backoff_s=100, max_attempts=2)
    row = c.execute("SELECT status, attempt_count FROM analyzer_read_queue WHERE id=?",
                    (r["id"],)).fetchone()
    assert row["status"] == "failed" and row["attempt_count"] == 2
    assert Q.lease(c, "w3", now=99999) is None       # terminal: never re-leased


def test_enqueue_normalizes_wallet_case_and_dedups():
    c = _conn()
    assert Q.enqueue(c, "0xAbC", "manual", now=1) is True
    assert Q.enqueue(c, "0xabc", "manual", now=2) is False
    row = c.execute("SELECT wallet FROM analyzer_read_queue").fetchone()
    assert row["wallet"] == "0xabc"


def test_enqueue_respects_legacy_mixed_case_live_row():
    c = _conn()
    assert Q.enqueue(c, "0xabc", "manual", now=1) is True
    c.execute("UPDATE analyzer_read_queue SET wallet='0xABC'")
    c.commit()
    assert Q.enqueue(c, "0xabc", "manual", now=2) is False


def test_allocate_test_index_is_monotone():
    c = _conn()
    assert Q.allocate_test_index(c) == 1
    assert Q.allocate_test_index(c) == 2
