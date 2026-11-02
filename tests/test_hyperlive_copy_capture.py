"""Tests for the copy-fidelity capture columns + open/close/excursion helpers (Task C)."""
import json
import os
import sqlite3

from sniper.hyperlive import intel_store as IS


def _db(tmp_path):
    c = IS.connect(os.path.join(str(tmp_path), "intel.sqlite"))
    IS.ensure_schema(c)
    return c


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(hl_copy_book)")}


def test_capture_columns_exist(tmp_path):
    c = _db(tmp_path)
    cols = _cols(c)
    for col in ("target_entry_px", "target_szi", "target_notional", "entry_spread_bps",
                "entry_depth_json", "exit_spread_bps", "exit_depth_json", "funding_bps",
                "funding_pnl_bps", "funding_provenance_status",
                "source_event_ts_ms", "source_received_ts_ms", "decision_ts_ms",
                "book_request_ts_ms", "book_received_ts_ms", "mirror_open_ts_ms",
                "source_latency_status", "source_event_tid",
                "mfe_bps", "mae_bps", "lag_bps", "is_holdout"):
        assert col in cols, f"missing capture column {col}"
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hl_copy_source_gaps'"
    ).fetchone()


def test_migration_is_idempotent(tmp_path):
    c = _db(tmp_path)
    IS.ensure_schema(c)          # run again — must not raise (duplicate ADD COLUMN would)
    IS.ensure_schema(c)
    assert "funding_bps" in _cols(c)


def test_v1_production_shaped_schema_migrates_source_capture_additively(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE hl_copy_book (id INTEGER PRIMARY KEY, target TEXT NOT NULL, "
        "coin TEXT NOT NULL, side TEXT, entry_mark REAL, detect_ts_ms INTEGER NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', exit_ts_ms INTEGER, exit_mark REAL, pnl_bps REAL, "
        "copy_tier TEXT DEFAULT 'unknown');"
        "CREATE TABLE hl_meta (key TEXT PRIMARY KEY, value TEXT, updated_ts_ms INTEGER);"
        "INSERT INTO hl_meta VALUES ('intel_schema_version','1',0);"
        "INSERT INTO hl_copy_book(id,target,coin,side,entry_mark,detect_ts_ms,status) "
        "VALUES (1,'0xlegacy','ETH','long',2000,1000,'open');"
    )
    legacy.commit()
    legacy.close()

    conn = IS.connect(str(path))
    IS.ensure_schema(conn)

    assert IS.get_meta(conn, "intel_schema_version") == "2"
    assert "source_event_ts_ms" in _cols(conn)
    assert conn.execute("SELECT COUNT(*) FROM hl_copy_source_events").fetchone()[0] == 0
    preserved = conn.execute(
        "SELECT target,coin,status,source_latency_status FROM hl_copy_book WHERE id=1"
    ).fetchone()
    assert tuple(preserved) == ("0xlegacy", "ETH", "open", "historical_not_captured")


def test_open_copy_backward_compatible(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.0, copy_tier="proven", ts_ms=1000)
    row = c.execute("SELECT * FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert row["entry_mark"] == 2000.0 and row["target_entry_px"] is None and row["is_holdout"] == 0


def test_open_copy_persists_capture(tmp_path):
    c = _db(tmp_path)
    depth = json.dumps({"mid": 2000.0, "bids": [[1999.0, 10.0]], "asks": [[2001.0, 10.0]]})
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.5, copy_tier="proven", ts_ms=1000,
                       target_entry_px=1999.0, target_szi=8.0, target_notional=15992.0,
                       entry_spread_bps=10.0, entry_depth_json=depth, lag_bps=7.5, is_holdout=1)
    r = c.execute("SELECT * FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert r["target_entry_px"] == 1999.0 and r["target_szi"] == 8.0
    assert r["target_notional"] == 15992.0 and r["entry_spread_bps"] == 10.0
    assert r["lag_bps"] == 7.5 and r["is_holdout"] == 1
    assert json.loads(r["entry_depth_json"])["asks"][0] == [2001.0, 10.0]


def test_update_copy_excursion_tracks_max_and_min(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1000)
    IS.update_copy_excursion(c, cid, gross_bps=30.0)
    IS.update_copy_excursion(c, cid, gross_bps=-15.0)
    IS.update_copy_excursion(c, cid, gross_bps=12.0)
    r = c.execute("SELECT mfe_bps, mae_bps FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert r["mfe_bps"] == 30.0 and r["mae_bps"] == -15.0


def test_close_copy_captures_exit_and_funding(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1000)
    xdepth = json.dumps({"mid": 2040.0, "bids": [[2039.0, 10.0]], "asks": [[2041.0, 10.0]]})
    IS.close_copy(c, cid, exit_mark=2040.0, pnl_bps=191.0, ts_ms=2000,
                  exit_spread_bps=9.8, exit_depth_json=xdepth, funding_bps=4.2)
    r = c.execute("SELECT * FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert r["status"] == "closed" and r["pnl_bps"] == 191.0
    assert r["exit_spread_bps"] == 9.8 and r["funding_bps"] == 4.2
    assert json.loads(r["exit_depth_json"])["mid"] == 2040.0


def test_close_copy_dual_writes_canonical_funding_and_legacy_cost_sign(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "short", 2000.0, ts_ms=1000)
    IS.close_copy(
        c, cid, exit_mark=1900.0, pnl_bps=491.0, ts_ms=2000,
        funding_pnl_bps=2.5, funding_provenance_status="complete",
    )
    row = c.execute("SELECT * FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert row["funding_pnl_bps"] == 2.5
    assert row["funding_bps"] == -2.5
    assert row["funding_provenance_status"] == "complete"


def test_close_copy_without_capture_still_works(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1000)
    IS.close_copy(c, cid, exit_mark=2010.0, pnl_bps=41.0, ts_ms=2000)
    r = c.execute("SELECT status, funding_bps FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert r["status"] == "closed" and r["funding_bps"] is None


def test_source_events_are_append_only_deduplicated_and_matchable(tmp_path):
    c = _db(tmp_path)
    event = {
        "wallet": "0xabc", "tid": "7", "coin": "kPEPE", "side": "A",
        "direction": "Open Short", "px": 1.0, "sz": 2.0, "start_position": 0.0,
        "oid": "3", "tx_hash": "0xh", "source_event_ts_ms": 1000,
        "received_ts_ms": 1100, "is_snapshot": 0, "is_recovery": 0,
        "connection_epoch": 1,
    }
    assert IS.record_copy_source_events(c, [event]) == 1
    assert IS.record_copy_source_events(c, [event]) == 0
    rows = IS.list_unmatched_copy_source_events(
        c, wallet="0xabc", after_ts_ms=900, before_ts_ms=1200,
    )
    assert [row["tid"] for row in rows] == ["7"]
    cid = IS.open_copy(c, "0xabc", "kPEPE", "short", 1.0, ts_ms=1200)
    IS.match_copy_source_events(c, copy_id=cid, tids=["7"], matched_ts_ms=1201)
    matched = c.execute("SELECT * FROM hl_copy_source_events").fetchone()
    assert matched["copy_id"] == cid and matched["match_status"] == "matched"


def test_historical_event_backfill_never_fabricates_receive_latency(tmp_path):
    c = _db(tmp_path)
    cid = IS.open_copy(
        c, "0xabc", "kPEPE", "short", 1.0, ts_ms=1200,
        target_szi=-3.0, source_latency_status="historical_not_captured",
    )

    assert IS.backfill_copy_source_event(
        c, copy_id=cid, source_event_ts_ms=1100, tids=["7"],
    ) is True
    assert IS.backfill_copy_source_event(
        c, copy_id=cid, source_event_ts_ms=1101, tids=["8"],
    ) is False
    row = c.execute("SELECT * FROM hl_copy_book WHERE id=?", (cid,)).fetchone()
    assert row["source_event_ts_ms"] == 1100
    assert row["source_event_tid"] == '["7"]'
    assert row["source_latency_status"] == "historical_event_reconstructed"
    assert row["source_received_ts_ms"] is None
    assert row["decision_ts_ms"] is None
    assert row["book_request_ts_ms"] is None
    assert row["book_received_ts_ms"] is None
    assert row["mirror_open_ts_ms"] is None

    future_id = IS.open_copy(
        c, "0xabc", "ETH", "long", 2_000.0, ts_ms=1200,
        target_szi=1.0, source_latency_status="historical_not_captured",
    )
    assert IS.backfill_copy_source_event(
        c, copy_id=future_id, source_event_ts_ms=1201, tids=["future"],
    ) is False


def test_source_overflow_gaps_persist_across_process_state(tmp_path):
    c = _db(tmp_path)
    IS.record_copy_source_gaps(c, [{
        "start_received_ts_ms": 1000, "end_received_ts_ms": 1200,
        "connection_epoch": 3, "dropped_count": 4, "reason": "queue_overflow",
    }], persisted_ts_ms=1300)
    c.close()
    reopened = IS.connect(str(tmp_path / "intel.sqlite"))
    assert IS.copy_source_gap_count(reopened) == 1
    assert IS.copy_source_gap_overlaps(reopened, after_ts_ms=900, before_ts_ms=1400)
