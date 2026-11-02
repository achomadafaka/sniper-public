"""Phase 2A — read-only quant shadow cohort report: campaign header, totals, and the residual split
(does the model's prediction beat the market when it AGREES vs DIFFERS from the market favorite)."""
import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import quant_persistence as QPP
from sniper.algos.polymarket_crypto5m import quant_report as QR


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "q.sqlite"))
    QPP.ensure_quant_schema(conn)
    return conn


def _record(conn, cid, side, q_market, cost, dts):
    matrix = {"q_market": q_market, "actionable_side": side, "q_trade": 0.6,
              f"{side.lower()}_fee_inclusive_cost_per_share": cost,
              f"{side.lower()}_net_edge_vs_executable": 0.03}
    QPP.record_matrix(conn, campaign_id="q", condition_id=cid, checkpoint=240, decision_ts=dts,
                      matrix=matrix, now_ts=1)
    QPP.record_decision(conn, campaign_id="q", condition_id=cid, checkpoint=240, decision_ts=dts,
                        actionable_side=side, q_trade=0.6, net_edge=0.03, status="shadow_prediction",
                        reason="edge", now_ts=1)


def test_report_header_and_totals(tmp_path):
    conn = _conn(tmp_path)
    QPP.start_campaign(conn, campaign_id="q", config={"model_version": "v1"}, git_commit="abc1234",
                       now_ts=100, model_version="v1")
    _record(conn, "0x1", "Up", 0.6, 0.55, 1240)
    r = QR.build_quant_report(conn)
    assert r["campaign"]["campaign_id"] == "q" and r["campaign"]["model_version"] == "v1"
    assert r["campaign"]["git_commit"] == "abc1234"
    assert r["totals"]["matrix"] == 1 and r["totals"]["decisions"] == 1 and r["totals"]["actionable"] == 1
    assert r["totals"]["resolved"] == 0


def test_report_eval_hit_rate_and_residual_split(tmp_path):
    conn = _conn(tmp_path)
    QPP.start_campaign(conn, campaign_id="q", config={}, git_commit="g", now_ts=100)
    _record(conn, "0xA", "Up", 0.7, 0.55, 1240)        # agree(fav Up), Up wins -> hit
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xA", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    _record(conn, "0xB", "Up", 0.4, 0.45, 1540)        # differ(fav Down), Up wins -> residual hit
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xB", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    _record(conn, "0xC", "Down", 0.3, 0.40, 1840)      # agree(fav Down), Up wins -> miss
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xC", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    e = QR.build_quant_report(conn)["eval"]
    assert e["n_resolved_actionable"] == 3 and e["hits"] == 2
    assert round(e["hit_rate"], 3) == 0.667
    assert e["agree_market"]["n"] == 2 and e["agree_market"]["hits"] == 1
    assert e["differ_market"]["n"] == 1 and e["differ_market"]["hits"] == 1
    # shadow net edge: A win(+0.45), B win(+0.55), C lose(-0.40) -> mean 0.20
    assert round(e["shadow_net_edge_mean"], 3) == 0.2


def test_report_recent_outcome_labels(tmp_path):
    conn = _conn(tmp_path)
    QPP.start_campaign(conn, campaign_id="q", config={}, git_commit="g", now_ts=1)
    # no-edge decision (actionable_side None) on a settled window -> "no_trade", not "pending"
    QPP.record_matrix(conn, campaign_id="q", condition_id="0xN", checkpoint=240, decision_ts=1240,
                      matrix={"q_market": 0.9}, now_ts=1)
    QPP.record_decision(conn, campaign_id="q", condition_id="0xN", checkpoint=240, decision_ts=1240,
                        actionable_side=None, q_trade=0.5, net_edge=None, status="shadow_prediction",
                        reason="no_edge", now_ts=1)
    QPP.record_settlement(conn, campaign_id="q", condition_id="0xN", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    rec = QR.build_quant_report(conn)["recent"][0]
    assert rec["actionable_side"] is None and rec["outcome"] == "no_trade"


def test_report_verdict_at_n_over_50_does_not_crash(tmp_path):
    # regression: differ/agree buckets must carry a Wilson CI so the n>=50 verdict path doesn't KeyError
    conn = _conn(tmp_path)
    QPP.start_campaign(conn, campaign_id="q", config={}, git_commit="g", now_ts=1)
    for i in range(60):
        cid = f"0x{i:03d}"
        _record(conn, cid, "Up", 0.7, 0.55, 1240 + i)
        QPP.record_settlement(conn, campaign_id="q", condition_id=cid,
                              winning_outcome=("Up" if i % 2 == 0 else "Down"),
                              reconciliation_status="gamma_only", now_ts=2)
    r = QR.build_quant_report(conn)              # must not raise
    assert r["eval"]["n_resolved_actionable"] == 60
    assert isinstance(r["eval"]["agree_market"]["ci"], list)
    assert r["edge_verdict"]["state"] in ("edge", "weak", "none")


def test_report_empty(tmp_path):
    r = QR.build_quant_report(_conn(tmp_path))
    assert r["campaign"] is None and r["totals"]["matrix"] == 0
    assert r["eval"]["n_resolved_actionable"] == 0 and r["recent"] == []
