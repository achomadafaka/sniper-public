"""Operator order 2026-10-28: desk data accumulates; a reader reads only a bounded window through an index, never the whole tape. The census, its hourly roll-up and pool_mints must plan swaps_ps
reads on swaps_ps_received or swaps_ps_pool_slot (swaps_ps has NO block_time index: a block_time filter alone scanned 44 GB for 49 min)."""
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1] / "scripts" / "research"
sys.path.insert(0, str(R))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, R / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


C, HR, M = _load("pumpswap_census"), _load("pumpswap_census_hourly"), _load("pumpswap_pool_mints")
H = 1_800_000_000 // 3600 * 3600


def real_tape():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE swaps_ps(pool TEXT, mint TEXT, slot INTEGER, signature TEXT, ordinal INTEGER, tx_index INTEGER, block_time INTEGER, received_at REAL, actor TEXT, map_status TEXT,
      lp_fee_bps TEXT, protocol_fee_bps TEXT, creator_fee_bps TEXT, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID;
    CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool, slot); CREATE INDEX swaps_ps_slot ON swaps_ps(slot); CREATE INDEX swaps_ps_received ON swaps_ps(received_at);
    CREATE TABLE pools(pool TEXT, canonical INTEGER, block_time INTEGER); CREATE INDEX pools_pool ON pools(pool);
    CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER); CREATE INDEX gaps_ps_at ON gaps_ps(at);
    CREATE TABLE ps_grads(pool TEXT, received_at REAL, grad_class TEXT); CREATE INDEX ps_grads_received ON ps_grads(received_at);
    CREATE TABLE ps_pool_hours(pool TEXT, hour INTEGER, swaps INTEGER, PRIMARY KEY(pool, hour));""")
    for i in range(50):
        c.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("P%d" % (i % 5), "m", i, "s%d" % i, 0, 0, H + i, H + i + 1.0, "u", "unmapped", "20", "5", "95"))
    c.commit()
    return c


def swaps_plans(c, run):
    """Run `run(conn)` with a statement trace, then EXPLAIN QUERY PLAN every captured swaps_ps statement. -> [(sql, plan text)]."""
    seen = []
    c.set_trace_callback(lambda sql: seen.append(sql) if re.search(r"\bswaps_ps\b", sql) and sql.lstrip().upper().startswith("SELECT") and "EXPLAIN" not in sql.upper() else None)
    run(c)
    c.set_trace_callback(None)
    out = []
    for sql in dict.fromkeys(seen):
        plan = " | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
        out.append((sql, plan))
    return out


def assert_indexed(plans):
    assert plans, "no swaps_ps statement was captured: the regression would pass vacuously"
    for sql, plan in plans:
        assert "SCAN swaps_ps" not in plan, (sql, plan)
        assert "swaps_ps_received" in plan or "swaps_ps_pool_slot" in plan, (sql, plan)


def test_census_reads_swaps_ps_only_through_the_received_index_or_a_pool_probe():
    c = real_tape()
    assert_indexed(swaps_plans(c, lambda con: C.census(con, start=H, end=H + 3600)))


def test_hourly_facts_read_swaps_ps_only_through_an_index():
    c = real_tape()
    plans = swaps_plans(c, lambda con: (HR.hour_facts(con, H), HR.curve_coverage(con, H, H + 3600)))
    assert_indexed(plans)
    assert any("swaps_ps_received" in plan for _, plan in plans)


def test_pending_pools_reads_a_received_window_only():
    c = real_tape()
    side = sqlite3.connect(":memory:")
    side.execute(M.SCHEMA)
    plans = swaps_plans(c, lambda con: M.pending_pools(con, side, H - 300.0, H + 3600.0))
    assert_indexed(plans)
    assert M.pending_pools(c, side, H - 300.0, H + 3600.0) == ["P0", "P1", "P2", "P3", "P4"]
    assert M.pending_pools(c, side, H + 10_000.0, H + 20_000.0) == []                     # outside the window nothing is read


def test_the_guard_catches_a_block_time_only_scan():
    c = real_tape()
    plans = swaps_plans(c, lambda con: con.execute("SELECT COUNT(*) FROM swaps_ps WHERE block_time >= ? AND block_time < ?", (H, H + 3600)).fetchall())
    try:
        assert_indexed(plans)
    except AssertionError:
        return
    raise AssertionError("a block_time-only scan passed the guard")


def test_a_row_received_outside_the_window_is_a_stated_lower_bound_not_counted():
    c = real_tape()
    c.execute("INSERT INTO swaps_ps VALUES('LATE','m',999,'late',0,0,?,?,'u','mapped','20','5','95')", (H + 5, H + 7200 + 5.0))      # in the hour by chain time, received 2 h late
    out = C.census(c, start=H, end=H + 3600)
    assert out["received_window"] == [H - C.RECEIVED_PRE_S, H + 3600 + C.RECEIVED_MARGIN_S] and out["hours"][0]["pools"] == 5


def _null_history(c, n=5000):
    c.executemany("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [("OLD", "m", -i - 1, "old%05d" % i, 0, 0, None, H - 86400.0, "u", "mapped", "20", "5", "95") for i in range(n)])
    c.execute("INSERT INTO swaps_ps VALUES('OLD','m',1,'new',0,0,?,?,'u','mapped','20','5','95')", (H, H + 1.0))


def test_the_first_block_time_probe_is_bounded_and_unknown_is_never_classified_new():
    c = real_tape()
    _null_history(c)
    ticks = []
    c.set_progress_handler(lambda: ticks.append(1) or 0, 100)
    got = C._first_block_time(c, "OLD")
    c.set_progress_handler(None, 0)
    assert got is None and len(ticks) < 30, (got, len(ticks))                 # 64 rows by slot are all NULL: earliest UNKNOWN, no walk through the old history
    out = C.census(c, start=H, end=H + 3600)
    assert out["hours"][0]["new_pools_unknown"] == 1 and out["hours"][0]["new_pools"] == 5


def test_a_captured_create_pool_gives_the_earliest_time_without_touching_swaps():
    c = real_tape()
    _null_history(c)
    c.execute("INSERT INTO pools VALUES('OLD', 1, ?)", (H - 7200,))
    assert C._first_block_time(c, "OLD") == H - 7200
    assert C.census(c, start=H, end=H + 3600)["hours"][0]["new_pools"] == 5   # P0..P4 are new in the hour; OLD (created two hours before) is not


def test_each_page_starts_at_the_cursor_not_at_the_window_start():
    c = real_tape()
    stmts = []
    c.set_trace_callback(lambda s: stmts.append(s) if "ORDER BY received_at" in s and s.lstrip().startswith("SELECT") else None)
    pages = list(C._received_chunks(c, H - 300.0, H + 5400.0, step=10))
    c.set_trace_callback(None)
    assert len(pages) == 5 and len(stmts) >= 6
    assert "received_at >= %s" % float(H - 300) in stmts[0].replace("'", "")
    second = stmts[1].replace("'", "")
    assert "received_at >= %s" % float(H - 300) not in second and "received_at >= %s" % float(H + 10) in second, second       # 10 rows at H+1.0.. => the 10th has received_at H+10
