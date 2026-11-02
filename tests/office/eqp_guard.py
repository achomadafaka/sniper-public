"""Shared EXPLAIN-QUERY-PLAN guard for Office tools that read the PumpSwap tape (operator order 2026-10-28: indexed bounded reads only).
`plans_of(conn, run)` traces every swaps_ps SELECT that `run(conn)` issues; `assert_indexed(plans)` fails on a full SCAN and requires swaps_ps_received or swaps_ps_pool_slot.
A tool test must also call `assert_guard_catches_a_scan()` once, so the guard itself is proven able to fail."""
import re
import sqlite3

SCHEMA = """
CREATE TABLE swaps_ps(pool TEXT, mint TEXT, slot INTEGER, signature TEXT, ordinal INTEGER, tx_index INTEGER, is_buy INTEGER, block_time INTEGER, received_at REAL, actor TEXT, map_status TEXT,
  post_base_reserve_atomic TEXT, post_quote_reserve_atomic TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, creator_fee_bps TEXT, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID;
CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool, slot); CREATE INDEX swaps_ps_slot ON swaps_ps(slot); CREATE INDEX swaps_ps_received ON swaps_ps(received_at);
CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER); CREATE INDEX gaps_ps_at ON gaps_ps(at);
CREATE TABLE ps_capture_intervals(session TEXT, start_at REAL, through_at REAL, protocol INTEGER, PRIMARY KEY(session, start_at)) WITHOUT ROWID;
"""


def tape():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def plans_of(conn, run):
    """-> [(sql, plan)] for every distinct swaps_ps SELECT issued while `run(conn)` executes."""
    seen = []
    conn.set_trace_callback(lambda s: seen.append(s) if re.search(r"\bswaps_ps\b", s) and s.lstrip().upper().startswith("SELECT") else None)
    try:
        run(conn)
    finally:
        conn.set_trace_callback(None)
    return [(s, " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + s))) for s in dict.fromkeys(seen)]


def assert_indexed(plans):
    assert plans, "no swaps_ps statement captured: the guard would pass vacuously"
    for sql, plan in plans:
        assert "SCAN swaps_ps" not in plan, (sql, plan)
        assert "swaps_ps_received" in plan or "swaps_ps_pool_slot" in plan, (sql, plan)


def assert_guard_catches_a_scan():
    c = tape()
    plans = plans_of(c, lambda con: con.execute("SELECT COUNT(*) FROM swaps_ps WHERE block_time >= 1").fetchall())
    try:
        assert_indexed(plans)
    except AssertionError:
        return
    raise AssertionError("the guard let a block_time-only scan through")
