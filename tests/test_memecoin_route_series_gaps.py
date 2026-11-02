"""Route series gaps and the writer thread (2026-10-16, Alpha; Gamma's PASS conditions).

Pins: a miss is a gap row whose reason is the failure class and the provider group; a full buffer
leaves buffer_drop rows per position with the count and the first/last observed_at it lost; the
writer thread drains on its own autocommit connection in one BEGIN IMMEDIATE ... COMMIT and leaves no
transaction open; it refuses on any other thread, and the maintenance flush stands down while it
runs; the restart marker is written by the writer, stamped with the runner start, one per open real
position; and no path puts a NULL or zero expected_usdc in the price table."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution.exit_preview import ExitPreview, PreviewResult
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as runner
from sniper.services import memecoin_route_series as rs

NOW = "2026-10-16T08:00:00+00:00"
STARTED = "2026-10-16T07:59:00+00:00"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean():
    rs.reset_state()
    yield
    w = rs._WRITER
    if w is not None:
        w.stop_event.set()
        w.join(timeout=5)
    rs.reset_state()


def _pos(pid=1, qty=1000.0, net="solana"):
    return {"id": pid, "mint": "MINT%d" % pid, "network_id": net, "open_qty": qty}


def _ok(expected=50.0):
    return PreviewResult.succeeded(ExitPreview(expected, 10 ** 9, "jupiter", NOW),
                                   provider_group="solana:exact_quote")


def _rec(preview, pid=1, seam=rs.SEAM_EXIT_DECISION, now=NOW, attempted=True):
    return rs.record(_pos(pid), preview, seam=seam, now_iso=now, value_fn=runner._preview_value,
                     attempted=attempted)


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.execute("PRAGMA journal_mode=WAL")
    c.commit()
    c.close()
    return path


# --- the reason ----------------------------------------------------------------------------------

@pytest.mark.parametrize("preview, reason", [
    (None, "no_preview|unknown"),
    (PreviewResult.failed(QuoteFailure.RATE_LIMIT, provider_group="solana:exact_quote",
                          provider_id="jupiter"), "rate_limit|solana:exact_quote"),
    (PreviewResult.failed(QuoteFailure.NO_ROUTE, provider_id="0x"), "no_route|0x"),
    ({"expected_usdc": 0, "provider_group": "g"}, "zero_value|g"),
    ({"expected_usdc": float("nan"), "provider_group": "g"}, "bad_value|g"),
    ({"expected_usdc": -1.0}, "bad_value|unknown"),
    ({}, "no_value|unknown"),
])
def test_a_miss_is_a_gap_row_named_by_failure_class_and_provider_group(preview, reason):
    assert _rec(preview) is False
    assert rs._BUFFER == []
    (gap,) = rs._GAPS
    assert gap["kind"] == "quote_miss" and gap["reason"] == reason
    assert (gap["position_id"], gap["seam"], gap["observed_at"], gap["count"]) == (
        1, "exit_decision", NOW, 1)


def test_not_asked_is_not_failed():
    """Gamma B1: a None preview from a site that never asked for a quote is no miss."""
    assert _rec(None, attempted=False) is False
    assert rs._GAPS == [] and rs._BUFFER == []
    _rec(PreviewResult.failed(QuoteFailure.NO_ROUTE, provider_id="j"), attempted=False)
    assert [g["reason"] for g in rs._GAPS] == ["no_route|j"]      # a failure object is an answer


def test_the_gap_buffer_is_bounded_and_counts_what_it_drops(monkeypatch):
    monkeypatch.setattr(rs, "_MAX_BUFFER", 3)
    for i in range(5):
        _rec(None, pid=i)
    assert [g["position_id"] for g in rs._GAPS] == [2, 3, 4]
    assert rs.snapshot()["gaps_dropped_full"] == 2


def test_a_priced_quote_writes_no_gap():
    assert _rec(_ok()) is True
    assert rs._GAPS == [] and len(rs._BUFFER) == 1


# --- buffer drops --------------------------------------------------------------------------------

def test_a_full_buffer_leaves_drop_rows_per_position_with_count_and_span(monkeypatch):
    monkeypatch.setattr(rs, "_MAX_BUFFER", 4)
    stamps = ["2026-10-16T08:00:0%d+00:00" % i for i in range(7)]
    for i, t in enumerate(stamps):
        _rec(_ok(), pid=1 if i % 2 == 0 else 2, now=t)
    batch, gaps = rs._take(500)
    assert len(batch) == 4
    drops = {g["position_id"]: g for g in gaps if g["kind"] == "buffer_drop"}
    assert drops[1]["count"] == 2 and drops[2]["count"] == 1
    assert (drops[1]["observed_at"], drops[1]["last_observed_at"]) == (stamps[0], stamps[2])
    assert drops[2]["observed_at"] == drops[2]["last_observed_at"] == stamps[1]
    assert rs._take(500) == ([], [])                                # reported once


# --- the price table -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["maintenance", "writer", "direct"])
def test_no_path_writes_a_null_or_zero_price(ledger, path):
    bad = [None, {}, {"expected_usdc": None}, {"expected_usdc": 0}, {"expected_usdc": -2},
           {"expected_usdc": float("nan")}, {"expected_usdc": float("inf")}]
    if path == "direct":
        conn = sqlite3.connect(ledger)
        rows = [dict(position_id=1, seam="x", observed_at=NOW, expected_usdc=v)
                for v in (None, 0, -1.0, float("nan"), float("inf"), "x", 5.0)]
        m.record_route_series(conn, rows)
        conn.close()
    else:
        for p in bad:
            _rec(p)
        _rec(_ok(7.0))
        if path == "maintenance":
            conn = sqlite3.connect(ledger)
            rs.flush(conn)
            conn.close()
        else:
            w = rs.start_writer(started_at=STARTED, db_path=ledger, drain_s=0.02,
                                open_positions_fn=lambda c: [])
            _wait(lambda: rs.snapshot()["flushed"] == 1)
            w.stop_event.set()
            w.join(5)
    conn = sqlite3.connect(ledger)
    prices = [r[0] for r in conn.execute("SELECT expected_usdc FROM memecoin_route_series")]
    assert prices == ([5.0] if path == "direct" else [7.0])
    if path != "direct":
        n = conn.execute("SELECT COUNT(*) FROM memecoin_route_series_gaps WHERE kind='quote_miss'")
        assert n.fetchone()[0] == len(bad)


# --- the writer thread -----------------------------------------------------------------------------

def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in %.1fs" % timeout)


def _traced(statements):
    def connect(path):
        conn = rs._connect_writer(path)
        conn.set_trace_callback(lambda sql: statements.append(
            sql.strip().upper() if sql.strip().upper().startswith("BEGIN") else sql.split()[0].upper()))
        return conn
    return connect


def test_the_writer_drains_in_one_short_immediate_transaction_and_leaves_none_open(ledger):
    seen = []
    w = rs.start_writer(started_at=STARTED, db_path=ledger, drain_s=0.05, connect=_traced(seen),
                        open_positions_fn=lambda c: [])
    _wait(lambda: w.conn is not None)
    seen.clear()
    _rec(_ok(9.0))
    _rec(None, pid=2)
    _wait(lambda: rs.snapshot()["flushed"] == 1 and rs.snapshot()["gaps_flushed"] == 1)
    w.stop_event.set()
    w.join(5)
    assert seen == ["BEGIN IMMEDIATE", "INSERT", "INSERT", "COMMIT"]
    assert w.conn.isolation_level is None and not w.conn.in_transaction
    snap = rs.snapshot()
    assert snap["lock_hold_ms_p50"] is not None and snap["lock_wait_ms_p90"] is not None


def test_the_drain_refuses_off_the_writer_thread_and_the_maintenance_flush_stands_down(ledger):
    w = rs.start_writer(started_at=STARTED, db_path=ledger, drain_s=30.0,
                        open_positions_fn=lambda c: [])
    _wait(lambda: w.conn is not None)
    _rec(_ok())
    conn = sqlite3.connect(ledger)
    assert rs._drain(conn) == 0 and rs.flush(conn) == 0
    assert len(rs._BUFFER) == 1 and rs.snapshot()["refused_off_thread"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_route_series").fetchone()[0] == 0
    w.stop_event.set()
    w.join(5)                                                    # the final drain on stop
    assert conn.execute("SELECT COUNT(*) FROM memecoin_route_series").fetchone()[0] == 1


def test_recording_from_the_funded_path_never_touches_the_database(monkeypatch):
    monkeypatch.setattr(m, "record_route_series", lambda *a, **k: pytest.fail("wrote"))
    assert _rec(_ok()) is True and _rec(None) is False


def test_the_restart_marker_is_the_writers_one_row_per_open_real_position_at_runner_start(ledger):
    conn = sqlite3.connect(ledger)
    for pid, mode, is_open in ((11, "real", 1), (12, "real", 1), (13, "real", 0), (14, "paper", 1)):
        conn.execute("INSERT INTO memecoin_positions (id, mint, network_id, execution_mode, is_open) "
                     "VALUES (?, ?, 'solana', ?, ?)", (pid, "M%d" % pid, mode, is_open))
    conn.commit()
    marker_thread = []
    keys = m.open_real_position_keys

    def open_fn(c):
        marker_thread.append(threading.current_thread())
        return keys(c)
    w = rs.start_writer(started_at=STARTED, db_path=ledger, drain_s=30.0, open_positions_fn=open_fn)
    _wait(lambda: rs.snapshot()["restart_markers"] == 2)
    rows = conn.execute("SELECT position_id, kind, reason, seam, observed_at FROM "
                        "memecoin_route_series_gaps ORDER BY position_id").fetchall()
    assert rows == [(11, "restart", "runner_start", "*", STARTED),
                    (12, "restart", "runner_start", "*", STARTED)]
    assert marker_thread == [w]


def test_one_writer_per_process():
    a = rs.start_writer(started_at=STARTED, db_path=":memory:", drain_s=30.0,
                        open_positions_fn=lambda c: [])
    assert rs.start_writer(started_at=STARTED, db_path=":memory:") is a


# --- the knob ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, on", [(None, True), ("1", True), ("on", True), ("0", False),
                                     ("", False), ("maybe", False)])
def test_the_knob_defaults_on_and_anything_else_is_off(raw, on):
    env = dict(os.environ)
    env.pop("MEMECOIN_ROUTE_SERIES_WRITER_THREAD", None)
    if raw is not None:
        env["MEMECOIN_ROUTE_SERIES_WRITER_THREAD"] = raw
    out = subprocess.run([sys.executable, "-c", "from sniper.settings import memecoin as c; "
                          "print(c.MEMECOIN_ROUTE_SERIES_WRITER_THREAD)"],
                         cwd=REPO, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == str(on)


def test_the_runner_starts_the_writer_only_under_the_knob_with_its_start_time():
    src = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    block = src.split("if MEMECOIN_ROUTE_SERIES_WRITER_THREAD:", 1)[1].split("\n    # ", 1)[0]
    assert "start_writer(started_at=runner_started_at)" in block
    assert src.count("start_writer(") == 1


# --- the writer releases the lock when a write fails (Gamma B2) ----------------------------------------

def test_a_failed_drain_rolls_back_and_frees_the_ledger_at_once(ledger):
    w = rs.start_writer(started_at=STARTED, db_path=ledger, drain_s=0.05,
                        open_positions_fn=lambda c: [])
    _wait(lambda: w.conn is not None)
    rs._append_gap(dict(rs._gap(_pos(), seam=None, kind="quote_miss", reason="x",
                                observed_at=NOW), seam=None))         # NOT NULL: the insert fails
    _rec(_ok())
    _wait(lambda: rs.snapshot()["errors"] >= 1)
    other = sqlite3.connect(ledger, timeout=0.05, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")                                    # no busy wait
    other.execute("ROLLBACK")
    assert not w.conn.in_transaction
    w.stop_event.set()
    w.join(5)
    assert other.execute("SELECT COUNT(*) FROM memecoin_route_series").fetchone()[0] == 0


def test_the_lock_timing_reports_wait_then_hold(ledger, monkeypatch):
    clock = iter([10.0, 10.004, 10.010])                                # t0, BEGIN done, COMMIT done
    monkeypatch.setattr(m.time, "perf_counter", lambda: next(clock))
    conn = sqlite3.connect(ledger, isolation_level=None)
    got = []
    m.record_route_series(conn, [dict(position_id=1, seam="s", observed_at=NOW, expected_usdc=1.0)],
                          on_lock=lambda wait, hold: got.append((wait, hold)))
    assert got == [(pytest.approx(4.0), pytest.approx(6.0))]


# --- through the real exit sweep (Gamma B1) ----------------------------------------------------------

def _sweep(monkeypatch, *, price, preview_fn):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_EXACT_QUOTE_ALWAYS", False, raising=False)
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.open_position(c, source_user_id="u1", source_handle="h", mint="So1", ticker="T",
                    entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at=NOW,
                    strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    ctx = {"solana": {"exit_fn": lambda *a, **k: pytest.fail("no exit"),
                      "preview_exit_fn": preview_fn}}
    runner.real_exit_sweep(c, ctx, now_fn=lambda: NOW,
                           price_fn=lambda mints: ({("solana", "So1"): {"price": price}}
                                                   if price else {}))
    return [(g["kind"], g["reason"]) for g in rs._GAPS]


def test_a_sweep_that_never_asks_writes_no_gap(monkeypatch):
    calls = []
    assert _sweep(monkeypatch, price=1.05, preview_fn=lambda pos: calls.append(pos)) == []
    assert calls == []                                                  # it really did not ask


@pytest.mark.parametrize("answer, reason", [
    ("raise", "no_preview|unknown"),
    (None, "no_preview|unknown"),
    (PreviewResult.failed(QuoteFailure.RATE_LIMIT, provider_group="solana:exact_quote"),
     "rate_limit|solana:exact_quote"),
])
def test_a_sweep_that_asks_and_fails_writes_one_miss(monkeypatch, answer, reason):
    def preview_fn(pos):
        if answer == "raise":
            raise RuntimeError("provider down")
        return answer
    assert _sweep(monkeypatch, price=None, preview_fn=preview_fn) == [("quote_miss", reason)]


@pytest.mark.parametrize("fn, want", [
    (None, []),                                                         # no preview fn: not asked
    (lambda pos: None, [("quote_miss", "no_preview|unknown")]),
])
def test_the_batch_prepass_records_only_what_it_asked_for(monkeypatch, fn, want):
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="So1", ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at=NOW,
                          strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="solana")
    positions = m.open_positions_by_strategy(c, cfg.MEMECOIN_REAL_POLICY, mode="real")
    ctx = {"solana": {"preview_exit_fn": fn}}
    runner._wo4_miss_prepass(positions, conn=c, contexts=ctx,
                             price_map={("solana", "So1"): {"price": 1.0}},
                             frozen_by_id={pid: None}, engine_ids={pid}, cfg=cfg,
                             preview_by_position=None, now_iso=NOW)
    assert [(g["kind"], g["reason"]) for g in rs._GAPS] == want


# --- the remaining asked-for misses (Gamma LOW A4-A6) -----------------------------------------------

def _marks(fn):
    pos = dict(_pos(), open_qty=1000.0)
    out = runner.make_real_mark_fn({"solana": {"preview_exit_fn": fn}})([pos])
    return out, [(g["seam"], g["reason"]) for g in rs._GAPS]


def test_an_equity_mark_with_no_value_is_one_miss_not_two():
    out, gaps = _marks(lambda pos: {"expected_usdc": None})
    assert out == {} and gaps == [("equity_mark", "no_value|unknown")]


@pytest.mark.parametrize("exc", [RuntimeError("down"), ValueError("bad")])
def test_an_equity_mark_that_raises_is_one_miss(exc):
    def fn(pos):
        raise exc
    out, gaps = _marks(fn)
    assert out == {} and gaps == [("equity_mark", "no_preview|unknown")]


def test_an_exit_confirmation_quote_that_raises_is_one_miss(monkeypatch):
    """The TP fires on the display mid; its exact confirmation asks for a quote and gets an error."""
    from sniper.settings import memecoin as cfg
    asked = []

    def preview_fn(pos):
        asked.append(pos["id"])
        raise RuntimeError("provider down")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_EXACT_QUOTE_ALWAYS", False, raising=False)
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.open_position(c, source_user_id="u1", source_handle="h", mint="So1", ticker="T",
                    entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at=NOW,
                    strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    ctx = {"solana": {"exit_fn": lambda *a, **k: pytest.fail("no exit without a confirmation"),
                      "preview_exit_fn": preview_fn}}
    runner.real_exit_sweep(c, ctx, now_fn=lambda: NOW,
                           price_fn=lambda mints: {("solana", "So1"): {"price": 1.30}})
    assert asked                                                        # the confirmation asked
    assert [(g["seam"], g["reason"]) for g in rs._GAPS if g["seam"] == "exit_audit"] == [
        ("exit_audit", "no_preview|unknown")]
