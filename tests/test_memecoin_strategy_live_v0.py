import inspect
import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.research.memecoin_strategy import live
from sniper.research.memecoin_strategy.live import CandidateLiveEngine
from sniper.research.memecoin_strategy.statistics import approve_first_class_paper


BORN = "2026-07-11T11:58:00+00:00"
NOW = "2026-07-11T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _feed(conn, obs_id, event_id, at, *, uid, mint, mcap=1_000_000, curated=1,
          raw_type="large_buy", side="buy", raw=None):
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(id,upstream_event_id,raw_type,normalized_side,"
        "source_user_id,network_id,mint,their_market_cap,their_size_usd,source_ts,observed_at,"
        "was_curated,was_real_approved,raw_json) VALUES (?,?,?,?,?,'solana',?,?,?,?,?,?,0,?)",
        (obs_id, event_id, raw_type, side, uid, mint, mcap, 5000, at, at, curated,
         json.dumps(raw or {})),
    )


def _quote(conn, obs_id, at, mint, price=1.0):
    sweep = f"s{obs_id}"
    conn.execute(
        "INSERT INTO memecoin_quote_sweeps_v2(sweep_id,observed_at,eligible_count,requested_count,"
        "captured_count,budget_skipped_count) VALUES (?,?,1,1,1,0)", (sweep, at),
    )
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(id,sweep_id,network_id,mint,observed_at,"
        "price,quality,contexts) VALUES (?,?,'solana',?,?,?,'ok','candidate')",
        (obs_id, sweep, mint, at, price),
    )


def test_live_engine_records_forward_cluster_control_and_opens_paired_paper_positions():
    conn = _conn(); engine = CandidateLiveEngine(conn, BORN)
    _feed(conn, 1, "control", "2026-07-11T11:58:30+00:00", uid="c", mint="CONTROL")
    _feed(conn, 2, "first", "2026-07-11T11:59:00+00:00", uid="u1", mint="SIGNAL")
    _feed(conn, 3, "second", NOW, uid="u2", mint="SIGNAL")
    conn.commit()

    result = engine.process_observations(NOW)
    assert result["matches"] == 1
    assert {(row["context"], row["mint"]) for row in result["capture_requests"]} == {
        ("candidate", "SIGNAL"), ("control", "CONTROL"),
    }
    signal = conn.execute(
        "SELECT * FROM memecoin_candidate_signals WHERE matched=1",
    ).fetchone()
    assert signal["control_observation_id"] == 1

    _quote(conn, 101, "2026-07-11T12:00:30+00:00", "SIGNAL")
    _quote(conn, 102, "2026-07-11T12:00:30+00:00", "CONTROL")
    conn.commit()
    swept = engine.sweep("2026-07-11T12:00:30+00:00")
    assert swept["opened"] == 2
    positions = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_candidate_positions ORDER BY position_id",
    )]
    assert {row["mint"] for row in positions} == {"SIGNAL", "CONTROL"}
    assert all(row["execution_mode"] == "paper" for row in positions)


def test_live_engine_enriches_thesis_from_raw_payload_and_never_uses_prebirth_rows():
    conn = _conn(); engine = CandidateLiveEngine(conn, BORN)
    _feed(conn, 1, "prebirth", "2026-07-11T11:57:59+00:00", uid="old", mint="OLD")
    raw = {"body": {"percentageUnrealizedPnl": 20, "marketCap": 1_000_000}}
    _feed(conn, 2, "thesis", NOW, uid="u", mint="THESIS", raw_type="thesis_created",
          side=None, raw=raw)
    conn.commit()
    result = engine.process_observations(NOW)
    names = {row[0] for row in conn.execute(
        "SELECT c.name FROM memecoin_candidate_signals s JOIN memecoin_strategy_candidates c "
        "ON c.candidate_id=s.candidate_id WHERE s.matched=1",
    )}
    assert names == {"thesis_momentum_v0"}
    assert result["matches"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_candidate_signals WHERE upstream_observation_id=1",
    ).fetchone()[0] == 0


def test_live_candidate_modules_have_no_execution_or_real_import_path():
    source = inspect.getsource(live)
    assert "sniper.execution" not in source and "memecoin_real" not in source


def test_coinless_rows_do_not_break_engine_and_stale_match_cannot_open_late():
    conn = _conn(); engine = CandidateLiveEngine(conn, BORN)
    _feed(conn, 1, "coinless", "2026-07-11T11:58:30+00:00", uid="x", mint="")
    _feed(conn, 2, "first", "2026-07-11T11:59:00+00:00", uid="u1", mint="M")
    _feed(conn, 3, "second", NOW, uid="u2", mint="M")
    conn.commit()
    assert engine.process_observations(NOW)["matches"] == 1
    _quote(conn, 101, "2026-07-11T12:03:00+00:00", "M")
    conn.commit()
    assert engine.sweep("2026-07-11T12:03:00+00:00")["opened"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_candidate_positions").fetchone()[0] == 0


def test_pending_open_query_is_recent_bounded_and_skips_fully_opened_signals():
    conn = _conn(); engine = CandidateLiveEngine(conn, BORN)
    candidate = conn.execute(
        "SELECT candidate_id FROM memecoin_strategy_candidates "
        "WHERE name='cluster_momentum_v0'",
    ).fetchone()[0]
    columns = (
        "signal_id,candidate_id,upstream_observation_id,episode_key,evidence_stage,network_id,"
        "mint,matched,reason,feature_json,source_observation_ids_json,params_hash,observed_at,"
        "created_at"
    )
    values = "(?,?,? ,?,'candidate','solana',?,1,'matched','{}','[]','hash',?,?)"
    conn.execute(
        f"INSERT INTO memecoin_candidate_signals({columns}) VALUES {values}",
        ("stale", candidate, 1, "stale", "STALE", "2026-07-11T11:00:00+00:00", NOW),
    )
    for index in range(1005):
        signal_id = f"fresh-{index:04d}"
        conn.execute(
            f"INSERT INTO memecoin_candidate_signals({columns}) VALUES {values}",
            (signal_id, candidate, index + 2, signal_id, f"M{index}", NOW, NOW),
        )
    conn.execute(
        "INSERT INTO memecoin_candidate_positions(candidate_id,signal_id,episode_key,network_id,"
        "mint,evidence_stage,execution_mode,timecap_s,entry_price,entry_notional,quantity,entry_cost,"
        "opened_at,status) VALUES (?,?,?,'solana',?,'candidate','paper',86400,1,100,100,0,?,'open')",
        (candidate, "fresh-0000", "fresh-0000", "M0", NOW),
    )
    conn.commit()

    rows = engine._pending_open_signals(
        {candidate: engine._active_candidates()[candidate]}, NOW,
    )

    assert len(rows) == 1000
    assert all(row["signal_id"] != "stale" for row in rows)
    assert all(row["signal_id"] != "fresh-0000" for row in rows)


def test_forged_ready_receipt_cannot_create_first_class_stage():
    conn = _conn(); engine = CandidateLiveEngine(conn, BORN)
    candidate = conn.execute(
        "SELECT * FROM memecoin_strategy_candidates WHERE name='cluster_momentum_v0'",
    ).fetchone()
    conn.execute(
        "INSERT INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,manifest_json,"
        "evidence_hash,created_at) VALUES ('ready',?,'fixed_candidate',?,'{\"status\":\"ready\"}',"
        "'h',?)", (candidate["candidate_id"], NOW, NOW),
    )
    conn.commit()
    cutoff = "2026-07-11T12:01:00+00:00"
    import pytest
    with pytest.raises(ValueError,match='unsupported memecoin performance'):
        approve_first_class_paper(conn,candidate['candidate_id'],cutoff,'ready')
    assert conn.execute("SELECT count(*) FROM memecoin_candidate_signals WHERE candidate_id=?",(candidate['candidate_id'],)).fetchone()[0]==0
