"""Phase 2A — quant shadow cohort persistence: dedicated tables (separate from forward/sim), frozen on
config hash, idempotent matrix/decision records, restart-safe settlement recovery."""
import os

import pytest

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import quant_persistence as QPP


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "quant.sqlite"))
    QPP.ensure_quant_schema(conn)
    return conn


def test_schema_creates_all_tables(tmp_path):
    conn = _conn(tmp_path)
    QPP.ensure_quant_schema(conn)  # idempotent
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in QPP.QUANT_TABLES:
        assert t in names


def test_campaign_frozen_refuses_changed_config(tmp_path):
    conn = _conn(tmp_path)
    QPP.start_campaign(conn, campaign_id="q", config={"a": 1}, git_commit="g", now_ts=1)
    QPP.start_campaign(conn, campaign_id="q", config={"a": 1}, git_commit="g", now_ts=2)  # same -> ok
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_quant_campaigns").fetchone()[0] == 1
    with pytest.raises(QPP.QuantFrozenError):
        QPP.start_campaign(conn, campaign_id="q", config={"a": 2}, git_commit="g", now_ts=3)


def test_matrix_and_decision_idempotent(tmp_path):
    conn = _conn(tmp_path)
    m1 = QPP.record_matrix(conn, campaign_id="q", condition_id="0x1", checkpoint=240, decision_ts=1240,
                           matrix={"q_trade": 0.64, "actionable_side": "Up"}, now_ts=1)
    m2 = QPP.record_matrix(conn, campaign_id="q", condition_id="0x1", checkpoint=240, decision_ts=1240,
                           matrix={"q_trade": 0.64, "actionable_side": "Up"}, now_ts=2)
    assert m1 == m2
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_quant_matrix").fetchone()[0] == 1
    d1 = QPP.record_decision(conn, campaign_id="q", condition_id="0x1", checkpoint=240, decision_ts=1240,
                             actionable_side="Up", q_trade=0.64, net_edge=0.03, status="shadow_prediction",
                             reason="edge", now_ts=1)
    d2 = QPP.record_decision(conn, campaign_id="q", condition_id="0x1", checkpoint=240, decision_ts=1240,
                             actionable_side="Up", q_trade=0.64, net_edge=0.03, status="shadow_prediction",
                             reason="edge", now_ts=2)
    assert d1 == d2
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_quant_decisions").fetchone()[0] == 1


def test_settlement_upsert_and_pending(tmp_path):
    conn = _conn(tmp_path)
    # two decided markets; one settled final, one quarantined-unresolved -> pending should return the latter
    for cid, dts in (("0xA", 1240), ("0xB", 1540)):
        QPP.record_decision(conn, campaign_id="q", condition_id=cid, checkpoint=240, decision_ts=dts,
                            actionable_side="Up", q_trade=0.6, net_edge=0.02, status="shadow_prediction",
                            reason="x", now_ts=1)
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xA", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xB", winning_outcome=None,
                          reconciliation_status="unresolved", now_ts=2)
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xB", winning_outcome=None,
                          reconciliation_status="unresolved", now_ts=3)  # upsert, no dup
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_quant_settlements").fetchone()[0] == 2
    pend = {p["condition_id"] for p in QPP.pending_settlements(conn, "q", checkpoint=240)}
    assert pend == {"0xB"}
    assert QPP.pending_settlements(conn, "q", checkpoint=240)[0]["slug"] == "btc-updown-5m-1300"
