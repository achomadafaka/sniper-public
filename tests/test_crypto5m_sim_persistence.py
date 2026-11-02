"""Phase 1A sim persistence — schema, idempotency, append-only."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import sim_persistence as SP


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "sim.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def test_sim_schema_creates_all_9_tables_idempotent(tmp_path):
    conn = _conn(tmp_path)
    SP.ensure_sim_schema(conn)  # twice
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in SP.SIM_TABLES:
        assert t in names


def test_feature_snapshot_idempotent_on_content_key(tmp_path):
    conn = _conn(tmp_path)
    snap = dict(condition_id="0xc", market_id="m", window_start_ts=100, window_end_ts=400,
                checkpoint=60, decision_ts=160, feature_cutoff_ts=160, feature_version="fv1",
                feature_status="ok", price_to_beat=63456.0, price_to_beat_quality="unavailable",
                book_integrity_status="usable", market_rule_hash="h", code_commit="abc",
                features={"distance_to_beat": -1.0})
    id1 = SP.upsert_feature_snapshot(conn, snap, now_ts=1)
    id2 = SP.upsert_feature_snapshot(conn, {**snap, "features": {"distance_to_beat": -2.0}}, now_ts=2)
    assert id1 == id2                                       # same content key -> one row
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots").fetchone()[0] == 1
    row = conn.execute("SELECT features_json FROM crypto5m_feature_snapshots").fetchone()
    assert json.loads(row[0])["distance_to_beat"] == -2.0  # updated in place


def test_prediction_and_decision_idempotent(tmp_path):
    conn = _conn(tmp_path)
    fid = SP.upsert_feature_snapshot(conn, dict(condition_id="0xc", checkpoint=60,
                                     feature_version="fv1"), now_ts=1)
    p = dict(condition_id="0xc", checkpoint=60, model_id="m", model_version="v1", calibrated=False,
             q_up=0.5, q_down=0.5, prediction_status="diagnostic_only", reason="", feature_version="fv1")
    pid1 = SP.upsert_prediction(conn, p, feature_snapshot_id=fid, now_ts=1)
    pid2 = SP.upsert_prediction(conn, p, feature_snapshot_id=fid, now_ts=2)
    assert pid1 == pid2
    d = dict(condition_id="0xc", checkpoint=60, strategy_version="s1", model_id="m",
             status="blocked", reason="boundary_rule_unvalidated")
    did1 = SP.upsert_decision(conn, d, now_ts=1)
    did2 = SP.upsert_decision(conn, d, now_ts=2)
    assert did1 == did2
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_shadow_decisions").fetchone()[0] == 1


def test_fee_configs_and_skips_are_append_only(tmp_path):
    conn = _conn(tmp_path)
    SP.record_fee_config(conn, dict(condition_id="0xc", fetched_ts=1, fees_enabled=True,
                         taker_base_fee=1000, fee_rate=None, fee_status="fee_conflict",
                         observed_fee_rate_bps=0, decoder_version="v0", source="clob",
                         raw_payload={"taker_base_fee": 1000}), now_ts=1)
    SP.record_fee_config(conn, dict(condition_id="0xc", fetched_ts=2, fees_enabled=True,
                         taker_base_fee=1000, fee_rate=None, fee_status="fee_conflict",
                         observed_fee_rate_bps=0, decoder_version="v0", source="clob",
                         raw_payload={}), now_ts=2)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_fee_configs").fetchone()[0] == 2  # append
    SP.record_skip(conn, condition_id="0xc", checkpoint=60, decision_ts=160,
                   reason="boundary_rule_unvalidated", strategy_version="s1", now_ts=1)
    SP.record_skip(conn, condition_id="0xc", checkpoint=90, decision_ts=190,
                   reason="boundary_rule_unvalidated", strategy_version="s1", now_ts=1)
    assert SP.sim_counts(conn)["crypto5m_paper_skips"] == 2


def test_skip_idempotent_on_identical_replay(tmp_path):
    conn = _conn(tmp_path)
    for _ in range(2):                                  # replay twice, identical params
        SP.record_skip(conn, condition_id="0xc", checkpoint=60, decision_ts=160,
                       reason="probability_uncalibrated", detail="x", strategy_version="s1", now_ts=1)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_paper_skips").fetchone()[0] == 1


def test_skip_reason_updated_in_place_on_rerun(tmp_path):
    conn = _conn(tmp_path)
    SP.record_skip(conn, condition_id="0xc", checkpoint=60, decision_ts=160,
                   reason="boundary_rule_unvalidated", strategy_version="s1", now_ts=1)
    SP.record_skip(conn, condition_id="0xc", checkpoint=60, decision_ts=160,
                   reason="probability_uncalibrated", strategy_version="s1", now_ts=2)
    rows = conn.execute("SELECT reason FROM crypto5m_paper_skips").fetchall()
    assert len(rows) == 1 and rows[0][0] == "probability_uncalibrated"
