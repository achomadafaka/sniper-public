"""Phase 1B Deliverable #10 — data-readiness + leakage audit.

Leakage checks are STRUCTURAL guards over the persisted data; any violation fails the dataset build
(export refuses). Readiness reports counts at each stage (captured -> resolved -> feature rows ->
eligible) and feature-group coverage.
"""
import os

import pytest

from sniper.algos.polymarket_crypto5m import features as FE
from sniper.algos.polymarket_crypto5m import dataset_export as DX
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import readiness_audit as RA
from sniper.algos.polymarket_crypto5m import sim_persistence as SP


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "ra.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def _seed(conn, cid, cp, *, decision_ts=None, feature_cutoff_ts=None, p2b_quality="unavailable",
          feature_status="ok", winner="Up", recon="gamma_only"):
    ws = 1000
    decision_ts = decision_ts if decision_ts is not None else ws + cp
    feature_cutoff_ts = feature_cutoff_ts if feature_cutoff_ts is not None else decision_ts
    SP.upsert_feature_snapshot(conn, dict(
        condition_id=cid, market_id="m", window_start_ts=ws, window_end_ts=1300, checkpoint=cp,
        decision_ts=decision_ts, feature_cutoff_ts=feature_cutoff_ts, feature_version=FE.FEATURE_VERSION,
        feature_status=feature_status, price_to_beat_quality=p2b_quality,
        book_integrity_status="usable", receive_time_missing=False, market_rule_hash="h",
        features={"binance_return_15s": 0.1}), now_ts=1)
    conn.execute("INSERT OR IGNORE INTO crypto5m_markets (condition_id, window_start_ts) VALUES (?,?)",
                 (cid, ws))
    conn.execute("INSERT OR IGNORE INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES (?,?,?)", (cid, winner, recon))
    conn.execute("INSERT OR IGNORE INTO crypto5m_market_rules (condition_id, rule_text_hash, "
                 "rule_supported) VALUES (?,?,?)", (cid, "h", 1))
    conn.commit()


def test_clean_db_passes_leakage_checks(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15)
    _seed(conn, "a", 30)
    res = RA.leakage_checks(conn)
    assert res["passed"] is True and res["violations"] == []


def test_cutoff_after_decision_detected(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, decision_ts=1060, feature_cutoff_ts=1065)     # cutoff AFTER decision (lookahead)
    res = RA.leakage_checks(conn)
    assert res["passed"] is False
    assert any(v["check"] == "cutoff_after_decision" for v in res["violations"])


def test_decision_misalignment_detected(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, decision_ts=1061)                            # 1061 != window_start(1000)+60
    res = RA.leakage_checks(conn)
    assert any(v["check"] == "decision_not_window_aligned" for v in res["violations"])


def test_unknown_price_to_beat_quality_detected(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15, p2b_quality="gamma_post_resolution")          # post-hoc oracle smell
    res = RA.leakage_checks(conn)
    assert any(v["check"] == "unknown_price_to_beat_quality" for v in res["violations"])


def test_price_to_beat_known_after_decision_detected(tmp_path):
    conn = _conn(tmp_path)
    SP.upsert_feature_snapshot(conn, dict(
        condition_id="a", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=60,
        decision_ts=1060, feature_cutoff_ts=1060, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="validated_rtds_reconstruction", price_to_beat_known_ts=1200,
        book_integrity_status="usable", receive_time_missing=False, market_rule_hash="h",
        features={}), now_ts=1)
    res = RA.leakage_checks(conn)
    assert any(v["check"] == "price_to_beat_known_after_decision" for v in res["violations"])


def test_export_refuses_on_leakage(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, decision_ts=1060, feature_cutoff_ts=1065)     # leaking
    with pytest.raises(RA.LeakageError):
        DX.export_dataset(conn, out_path=os.path.join(str(tmp_path), "x.parquet"),
                          enforce_leakage=True)


def test_export_allows_clean_with_enforcement(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15)
    res = DX.export_dataset(conn, out_path=os.path.join(str(tmp_path), "ok.parquet"),
                            enforce_leakage=True)
    assert res["row_count"] == 1


def test_readiness_report_counts(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15, winner="Up")
    _seed(conn, "b", 15, winner=None, recon="conflict")               # captured but not resolved
    rep = RA.readiness_report(conn)
    assert rep["windows_captured"] == 2
    assert rep["windows_resolved"] == 1
    assert rep["feature_rows"] == 2
    assert rep["model_eligible"] == 1                                 # only the resolved one
    assert 200 in rep["learning_curve_targets"]
    assert rep["resolved_meets_target"][200] is False
