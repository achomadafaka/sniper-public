"""A buy the confirmed watcher classifies but writes no detection row for gets a named decision
(Alpha GO 2026-10-16). 58Wk6q9f, JDQKD's Xsc9-funded creation buy: the slot lane refused
slot_lane_curve_quote_unread, the watcher valued route_non_quote_funding, proved no initiation and wrote
nothing, so no lane judged it and no table said why."""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_fill_timing as T


@pytest.fixture(autouse=True)
def fresh_writer(monkeypatch):
    """Each test its own queue and writer thread (an earlier test's writer keeps draining its own)."""
    monkeypatch.setitem(T._NO_ROW, "queue", None)
    monkeypatch.setitem(T._NO_ROW, "thread", None)
    monkeypatch.setitem(T._NO_ROW, "stats", {"queued": 0, "written": 0, "dropped_full": 0, "failed": 0})

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/watcher_no_row_xsc9_creation_20261016.json").read_text())
SIG = FX["transaction"]["transaction"]["signatures"][0]


def _run(monkeypatch, tmp_path, *, require_initiation=True):
    from scripts.research import onchain_wallet_watch as w
    monkeypatch.setattr(w, "_stage_write", lambda *a: None)
    monkeypatch.setattr(w, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(w, "send_wake", lambda *a: None)
    timing = tmp_path / "timing.db"
    monkeypatch.setattr(T, "timing_db_path", lambda: timing)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(w.SCHEMA)
    meta = {"uid": "pump:" + FX["owner"], "handle": "JDQKD", "status": "trial"}
    ok = w.process_transaction(SIG, FX["owner"], meta, c, tx=FX["transaction"],
                               require_initiation=require_initiation, signal_feed="shyft_confirmed:test")
    detections = c.execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0]
    assert T.flush_watcher_no_row()
    notes = []
    if timing.exists():
        with sqlite3.connect(timing) as t:
            notes = t.execute("SELECT signature, owner, source_user_id, feed, reason FROM memecoin_watcher_no_row").fetchall()
    return ok, detections, notes


def test_a_classified_buy_without_initiation_leaves_a_named_decision(monkeypatch, tmp_path):
    ok, detections, notes = _run(monkeypatch, tmp_path)
    assert ok is False and detections == 0
    assert notes == [(SIG, FX["owner"], "pump:" + FX["owner"], "shyft_confirmed:test",
                      "no_initiation:route_non_quote_funding")]


def test_a_row_written_needs_no_decision_note(monkeypatch, tmp_path):
    ok, detections, notes = _run(monkeypatch, tmp_path, require_initiation=False)   # the HTTP path
    assert detections == 1 and notes == []


def test_the_note_is_first_wins_and_never_raises(tmp_path):
    path = tmp_path / "t.db"
    assert T.note_watcher_no_row("s", "o", "u", "f", "first", path=path) is True
    assert T.note_watcher_no_row("s", "o", "u", "f", "second", path=path) is True
    assert T.flush_watcher_no_row()
    with sqlite3.connect(path) as t:
        assert t.execute("SELECT reason FROM memecoin_watcher_no_row").fetchall() == [("first",)]
    assert T.note_watcher_no_row("s", "o", "u", "f", "x", path=tmp_path) is True        # a directory: queued
    assert T.flush_watcher_no_row() and T.watcher_no_row_stats()["failed"] == 1        # the writer counts it


def test_a_held_file_never_blocks_the_caller(tmp_path):
    """Gamma B1: the decode thread pays the enqueue only, even while another writer holds the file."""
    path = tmp_path / "t.db"
    T.note_watcher_no_row("warm", "o", "u", "f", "r", path=path)
    assert T.flush_watcher_no_row()
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        for i in range(200):
            assert T.note_watcher_no_row("s%d" % i, "o", "u", "f", "r", path=path) is True
        per_note_ms = (time.perf_counter() - started) * 1000.0 / 200
        assert per_note_ms < 1.0, per_note_ms
    finally:
        holder.rollback()
        holder.close()
    assert T.flush_watcher_no_row(timeout_s=10.0)
    with sqlite3.connect(path) as t:
        assert t.execute("SELECT COUNT(*) FROM memecoin_watcher_no_row").fetchone()[0] == 201


def test_a_full_queue_drops_and_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "_NO_ROW_QUEUE_MAX", 2)
    path = tmp_path / "t.db"
    holder = sqlite3.connect(path, timeout=0)
    holder.execute(T.WATCHER_NO_ROW_DDL)
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")                   # the writer stalls on its first note
    try:
        results = [T.note_watcher_no_row("s%d" % i, "o", "u", "f", "r", path=path) for i in range(10)]
        assert results.count(False) >= 1 and T.watcher_no_row_stats()["dropped_full"] == results.count(False)
    finally:
        holder.rollback()
        holder.close()
    assert T.flush_watcher_no_row(timeout_s=10.0)


def test_the_timing_schema_carries_the_table(tmp_path):
    conn = T.connect(tmp_path / "t.db")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='memecoin_watcher_no_row'").fetchone()
