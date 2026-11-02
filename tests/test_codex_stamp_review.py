"""Offline witnesses for U's real cache-to-snapshot boundary; no providers/models."""
import importlib.util
import json
import sqlite3
import statistics
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_crawler as C
from sniper.services import memecoin_desk_universe as U

SPEC = importlib.util.spec_from_file_location(
    "codex_u_fixtures", Path(__file__).with_name("test_memecoin_desk_universe_worker.py"))
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)
T = 1792584000.0
MINT = U.b58encode(H.MINT)
FLAGS = {k: False for k in C.FLAGS}


def cache(tmp_path):
    path = tmp_path / "crawler" / "crawls.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, C.connect(path)


def put(db, *, at=T - 1, intent="analysis", flags=FLAGS, raw_flags=None,
        error=None, version=None, network="solana"):
    db.execute("INSERT INTO crawls(network_id,mint,known_at,input_hash,inputs_json,intent,flags_json,"
               "call_error,prompt_version) VALUES(?,?,?,'h','[]',?,?,?,?)",
               (network, MINT, at, intent, raw_flags if raw_flags is not None else json.dumps(flags),
                error, C.CRAWLER_VERSION if version is None else version))
    db.commit()


def emit(tmp_path, monkeypatch):
    # Only the clock and external SOL-price/alert boundaries are fake. process(), fields(),
    # stamp_fields(), build_snapshot(), JSONL write and render_judge_sheet() are production code.
    monkeypatch.setattr(H.W.time, "time", lambda: T)
    w, _ = H.worker(tmp_path)
    try:
        w.process(H.frame_meta(10, 1700000000))
        w.process(H.frame_tx(10, 1, H.trade_event(*H.vq_for(20000))))
        w.process(H.frame_tx(11, 2, H.trade_event(*H.vq_for(40000))))
        row = json.loads((tmp_path / "candidate_emissions" / f"{H.W.utc_day(T)}.jsonl").read_text().splitlines()[0])
        return row, K.render_judge_sheet({**row, "opportunity_id": row["ticket_id"]})
    finally:
        for conn in w.files._open.values():
            conn.close()


@pytest.mark.parametrize("intent,flags,raw", [
    ("moon", FLAGS, None),
    ("analysis", [], None),
    ("analysis", {**FLAGS, "rug_history_claim": "false"}, None),
    ("analysis", FLAGS, "{not json"),
])
def test_bad_cache_does_not_poison_real_u_candidate(tmp_path, monkeypatch, intent, flags, raw):
    path, db = cache(tmp_path)
    put(db, intent=intent, flags=flags, raw_flags=raw)
    db.close()
    row, sheet = emit(tmp_path, monkeypatch)
    assert sheet["crawler_intent"] is None and sheet["crawler_flags"] is None
    for key in ("crawler_intent", "crawler_flags"):
        assert row["snapshot"]["fields"][key]["completeness"] == "missing"


def test_current_crawl_version_cannot_reuse_old_prompt_values(tmp_path, monkeypatch):
    path, db = cache(tmp_path)
    put(db, intent="warning", version="desk-crawler-v0")
    db.close()
    _, sheet = emit(tmp_path, monkeypatch)
    assert sheet["crawler_intent"] is None and sheet["crawler_flags"] is None


@pytest.mark.parametrize("state", ["absent", "corrupt", "failed", "stale", "future", "other_chain"])
def test_null_states_remain_valid_candidates(tmp_path, monkeypatch, state):
    if state == "corrupt":
        path = tmp_path / "crawler" / "crawls.db"
        path.parent.mkdir()
        path.write_bytes(b"not a sqlite database")
    elif state != "absent":
        _, db = cache(tmp_path)
        if state == "failed":
            put(db, intent=None, error="timeout")
        elif state == "stale":
            put(db, at=T - C.FRESH_S - 1)
        elif state == "future":
            put(db, at=T + 1, intent="warning")
        else:
            put(db, network="ethereum", intent="warning")
        db.close()
    row, sheet = emit(tmp_path, monkeypatch)
    expected = "missing" if state in ("corrupt", "failed", "stale") else "absent"
    assert sheet["crawler_intent"] is None and sheet["crawler_flags"] is None
    assert row["snapshot"]["fields"]["crawler_intent"]["completeness"] == expected


def test_latest_failure_keeps_prior_fresh_success_and_never_future(tmp_path, monkeypatch):
    _, db = cache(tmp_path)
    put(db, at=T - 30, intent="warning")
    put(db, at=T - 10, intent=None, error="timeout")
    put(db, at=T + 1, intent="promotion")
    db.close()
    row, sheet = emit(tmp_path, monkeypatch)
    assert sheet["crawler_intent"] == "warning"
    assert row["snapshot"]["fields"]["crawler_intent"]["known_at"] == T - 30


def test_locked_cache_keeps_real_u_emission_under_fault_ceiling(tmp_path, monkeypatch):
    path, db = cache(tmp_path)
    put(db)
    db.close()
    owner = sqlite3.connect(path)
    try:
        assert owner.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        owner.execute("PRAGMA locking_mode=EXCLUSIVE")
        owner.execute("BEGIN EXCLUSIVE")
        owner.execute("UPDATE crawls SET model='hold-lock'")
        start = time.perf_counter()
        row, sheet = emit(tmp_path, monkeypatch)
        elapsed = time.perf_counter() - start
        assert sheet["crawler_intent"] is None and sheet["crawler_flags"] is None
        for key in ("crawler_intent", "crawler_flags"):
            field = row["snapshot"]["fields"][key]
            assert field["source"] == "crawler:unreadable"
            assert field["completeness"] == "missing"
        # This is a generous failure-path budget, separate from measured normal-read p90.
        assert elapsed < 0.250, f"locked crawl cache stalled U emission for {elapsed * 1000:.1f}ms"
    finally:
        owner.rollback()
        owner.close()


def test_cache_read_latency_normal_and_exclusive_wal_lock(tmp_path):
    path, db = cache(tmp_path)
    put(db)
    db.close()
    def measure():
        start = time.perf_counter()
        f = C.stamp_fields(path, MINT, T)
        return time.perf_counter() - start, f
    normal = [measure()[0] for _ in range(50)]
    owner = sqlite3.connect(path)
    try:
        assert owner.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        owner.execute("PRAGMA locking_mode=EXCLUSIVE")
        owner.execute("BEGIN EXCLUSIVE")
        owner.execute("UPDATE crawls SET model='hold-lock'")
        locked = []
        for _ in range(3):
            elapsed, f = measure()
            locked.append(elapsed)
            assert f["crawler_intent"].value is None
            assert f["crawler_intent"].source == "crawler:unreadable"
    finally:
        owner.rollback()
        owner.close()
    metrics = {"normal_p50_ms": statistics.median(normal) * 1000,
               "normal_p90_ms": sorted(normal)[44] * 1000,
               "exclusive_lock_p50_ms": statistics.median(locked) * 1000,
               "exclusive_lock_max_ms": max(locked) * 1000}
    (tmp_path / "latency.json").write_text(json.dumps(metrics))
    print("STAMP_LATENCY", json.dumps(metrics))
