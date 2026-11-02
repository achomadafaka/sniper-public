"""Every funded exit decision records WHICH LOOP decided it.

The decision-latency map (2026-08-29) could name a p50 of 0.29s but could not split it by lane,
because nothing recorded the deciding code path. The mark's `source` is not a substitute: it says
where the PRICE came from, and a periodic sweep and a websocket wake can both read the same mark.

These fail quietly if broken. A missing lane is a NULL, not an error, and the split would silently
become "everything is periodic_sweep" — which reads as a finding rather than as absent data.
"""
import ast
import pathlib
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from _source_guard import code_of

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "sniper/services/memecoin_real_runner.py"
LOOP = pathlib.Path(__file__).resolve().parents[1] / "scripts/memecoin_runner_loop.py"


def _audit_calls(path):
    tree = ast.parse(path.read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "record_exit_trigger_audit"]


def test_every_audit_write_names_its_lane():
    """Enumerated from the source, so a call site added later cannot quietly omit it."""
    calls = _audit_calls(RUNNER)
    assert calls, "no audit writes found — the guard is looking at the wrong file"
    missing = [n.lineno for n in calls if "lane" not in {k.arg for k in n.keywords}]
    assert not missing, f"audit writes with no lane at lines {missing}"


def test_the_lane_is_threaded_and_not_hardcoded_inside_the_sweep():
    """A hardcoded 'periodic_sweep' inside the sweep would mislabel every fastlane-wake decision
    as periodic — the exact confusion the marker exists to remove, made permanent in the data.
    The final execution check has its own lane, separate from the decision loop."""
    tree = ast.parse(code_of(RUNNER))
    for function in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "record_exit_trigger_audit"):
                continue
            lane = next(k.value for k in node.keywords if k.arg == "lane")
            if isinstance(lane, ast.Constant):
                expected = ("execution" if function.name in ("confirm_execution_stop", "confirm_execution_target")
                            else "mirror_on_sell")
                assert lane.value == expected, (
                    f"{function.name} hardcodes lane={lane.value!r}; it must receive the caller's")


def test_the_four_loops_each_declare_a_distinct_lane():
    """If two loops shared a label the split would still produce numbers, and they would be a
    weighted average of two different things presented as one."""
    src = LOOP.read_text()
    for lane in ("fastlane_poll", "fastlane_wake", "hot_pull_fallback", "periodic_sweep"):
        assert f'lane="{lane}"' in src, f"{lane} is not declared by any loop"


def test_the_pull_fallback_is_not_filed_as_a_wake():
    """It runs precisely when the websocket did NOT wake us, so counting it as a wake would put a
    fallback's latency into the wake lane's p50 and make the wake look slower than it is."""
    src = LOOP.read_text()
    i = src.index("def pull_fallback")
    assert 'lane="hot_pull_fallback"' in src[i:i + 500]


# ---------------------------------------------------------------- the column

def test_the_column_exists_and_round_trips():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.record_exit_trigger_audit(
        conn, position_id=1, reason="real_tp", display_price=1.0, preview_usdc=2.0,
        allowed=True, detail="{}", checked_at="2026-08-29T10:00:00+00:00", lane="fastlane_wake")
    row = conn.execute("SELECT lane FROM memecoin_exit_trigger_audit").fetchone()
    assert row["lane"] == "fastlane_wake"


def test_an_upsert_replaces_the_lane_rather_than_keeping_a_stale_one():
    """The table keeps one row per position/reason/minute. If a wake and a sweep both decide inside
    one minute the row must carry the loop that decided LAST, not the first one to arrive."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    for lane in ("periodic_sweep", "fastlane_wake"):
        m.record_exit_trigger_audit(
            conn, position_id=1, reason="b_stop", display_price=1.0, preview_usdc=2.0,
            allowed=False, detail="{}", checked_at="2026-08-29T10:00:30+00:00", lane=lane)
    rows = conn.execute("SELECT lane FROM memecoin_exit_trigger_audit").fetchall()
    assert len(rows) == 1 and rows[0]["lane"] == "fastlane_wake"


def test_lane_is_optional_so_an_existing_caller_cannot_break():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.record_exit_trigger_audit(
        conn, position_id=1, reason="real_tp", display_price=1.0, preview_usdc=2.0,
        allowed=True, detail="{}", checked_at="2026-08-29T10:00:00+00:00")
    assert conn.execute("SELECT lane FROM memecoin_exit_trigger_audit").fetchone()["lane"] is None


def test_the_migration_adds_the_column_to_an_existing_table():
    """9,044 rows already exist without it. The add must be an ALTER, not a fresh CREATE."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE memecoin_exit_trigger_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, reason TEXT NOT NULL,
        checked_minute TEXT NOT NULL, display_price REAL, preview_usdc REAL,
        allowed INTEGER NOT NULL, detail TEXT, checked_at TEXT NOT NULL,
        UNIQUE(position_id, reason, checked_minute))""")
    conn.execute("INSERT INTO memecoin_exit_trigger_audit"
                 "(position_id,reason,checked_minute,allowed,checked_at)"
                 " VALUES (7,'real_tp','2026-08-01T00:00',1,'2026-08-01T00:00:00+00:00')")
    conn.commit()          # the migration refuses to run inside an open transaction
    m.ensure_memecoin_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_exit_trigger_audit)")}
    assert "lane" in cols
    assert conn.execute("SELECT COUNT(*) FROM memecoin_exit_trigger_audit").fetchone()[0] == 1
