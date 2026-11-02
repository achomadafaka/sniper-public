"""Phase 1D #UI — read-only forward-paper campaign report builder: aggregates the crypto5m_forward_*
tables into a display dict (manifest header, per-rule x fee-scenario PnL/WR, milestones, recents)."""
import os

from sniper.algos.polymarket_crypto5m import forward_harness as FH
from sniper.algos.polymarket_crypto5m import forward_persistence as FPP
from sniper.algos.polymarket_crypto5m import forward_report as FRPT
from sniper.algos.polymarket_crypto5m import forward_rules as FR
from sniper.algos.polymarket_crypto5m import persistence as P

UP_BOOK = {"best_bid": 0.60, "best_ask": 0.62, "bids": [[0.60, 800]], "asks": [[0.62, 1000]],
           "book_hash": "u", "book_integrity_status": "usable"}
DOWN_BOOK = {"best_bid": 0.38, "best_ask": 0.40, "bids": [[0.38, 200]], "asks": [[0.40, 1000]],
             "book_hash": "d", "book_integrity_status": "usable"}


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "fwd.sqlite"))
    FPP.ensure_forward_schema(conn)
    return conn


def _populate(conn):
    FPP.start_campaign(conn, campaign_id=FR.CAMPAIGN_ID, manifest=FR.RULE_MANIFEST,
                       manifest_hash=FR.manifest_hash(), git_commit="abc1234def", now_ts=1000,
                       feature_version="v0", host="testhost")
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    recs = FH.decide_market(condition_id="0x1", ctx=ctx, up_book=UP_BOOK, down_book=DOWN_BOOK,
                            up_token="UP", down_token="DOWN", decision_ts=1240, fill_book_ts=1241,
                            gross=10.0, min_order=5, latency_ms=500, book_quality="usable")
    FH.persist_records(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x1", rule_version="v0",
                       checkpoint=240, decision_ts=1240, records=recs, now_ts=1)
    FH.settle_market(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x1", winning_outcome="Up",
                     reconciliation_status="gamma_only", now_ts=3)


def test_report_campaign_header(tmp_path):
    conn = _conn(tmp_path)
    _populate(conn)
    c = FRPT.build_forward_report(conn)["campaign"]
    assert c["campaign_id"] == FR.CAMPAIGN_ID
    assert c["manifest_hash"] == FR.manifest_hash()
    assert c["git_commit"] == "abc1234def"
    assert c["settlement_rule"] == "up_when_chainlink_end_ge_start"
    assert c["config"]["gross_notional_usdc"] == 10.0
    assert c["config"]["primary_checkpoint_s"] == 240


def test_report_rule_leaderboard_and_pnl(tmp_path):
    conn = _conn(tmp_path)
    _populate(conn)
    r = FRPT.build_forward_report(conn)
    rules = {x["rule_id"]: x for x in r["rules"]}
    assert set(rules) == {"C0", "C1", "R1", "R2", "R3"}
    # all five rules selected Up in this fixture; Up won -> each is one resolved win, net positive at fees
    r1 = rules["R1"]
    assert r1["n_entries"] == 1 and r1["n_resolved"] == 1 and r1["n_skips"] == 0
    f1 = r1["scenarios"]["F1_documented"]
    assert f1["wins"] == 1 and f1["resolved"] == 1 and f1["win_rate"] == 1.0 and f1["total_pnl"] > 0
    f0 = r1["scenarios"]["F0_gross"]
    assert f0["total_pnl"] > f1["total_pnl"]            # gross PnL beats documented-fee PnL


def test_report_totals_milestones_recents_settlements(tmp_path):
    conn = _conn(tmp_path)
    _populate(conn)
    r = FRPT.build_forward_report(conn)
    assert r["totals"]["n_entries"] == 5 and r["totals"]["n_resolved"] == 5
    assert r["totals"]["by_scenario"]["F1_documented"]["wins"] == 5
    assert r["milestones"]["targets"] == [200, 500, 1000]
    assert r["milestones"]["resolved"] == 5 and r["milestones"]["next"] == 200
    assert len(r["recent_decisions"]) == 5
    assert r["settlements"] == {"total": 1, "resolved": 1, "quarantined": 0}
    assert r["fee_status"] == "conflict"


def test_report_quarantined_settlement_not_counted_resolved(tmp_path):
    conn = _conn(tmp_path)
    FPP.start_campaign(conn, campaign_id=FR.CAMPAIGN_ID, manifest=FR.RULE_MANIFEST,
                       manifest_hash=FR.manifest_hash(), git_commit="g", now_ts=1000)
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    FH.persist_records(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x9", rule_version="v0",
                       checkpoint=240, decision_ts=1240,
                       records=FH.decide_market(condition_id="0x9", ctx=ctx, up_book=UP_BOOK,
                                                down_book=DOWN_BOOK, up_token="UP", down_token="DOWN",
                                                decision_ts=1240, fill_book_ts=1241, gross=10.0,
                                                min_order=5, latency_ms=500, book_quality="usable"),
                       now_ts=1)
    FH.settle_market(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x9", winning_outcome=None,
                     reconciliation_status="conflict", now_ts=3)
    r = FRPT.build_forward_report(conn)
    assert r["totals"]["n_entries"] == 5 and r["totals"]["n_resolved"] == 0   # quarantined -> no PnL rows
    assert r["settlements"]["quarantined"] == 1


def test_report_empty_db(tmp_path):
    r = FRPT.build_forward_report(_conn(tmp_path))
    assert r["campaign"] is None
    assert r["rules"] == [] and r["totals"]["n_entries"] == 0
    assert r["milestones"]["resolved"] == 0 and r["milestones"]["next"] == 200
    assert r["recent_decisions"] == [] and r["settlements"]["total"] == 0
