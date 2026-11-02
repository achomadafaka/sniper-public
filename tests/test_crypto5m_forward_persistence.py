"""Phase 1D #4 — forward paper persistence: dedicated tables, frozen campaign, idempotent decisions."""
import os

import pytest

from sniper.algos.polymarket_crypto5m import forward_persistence as FPP
from sniper.algos.polymarket_crypto5m import persistence as P


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "fwd.sqlite"))
    FPP.ensure_forward_schema(conn)
    return conn


def test_schema_creates_all_tables(tmp_path):
    conn = _conn(tmp_path)
    FPP.ensure_forward_schema(conn)  # idempotent
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in FPP.FORWARD_TABLES:
        assert t in names


def test_campaign_frozen_refuses_changed_manifest(tmp_path):
    conn = _conn(tmp_path)
    FPP.start_campaign(conn, campaign_id="c", manifest={"a": 1}, manifest_hash="h1", git_commit="g",
                       now_ts=1)
    FPP.start_campaign(conn, campaign_id="c", manifest={"a": 1}, manifest_hash="h1", git_commit="g",
                       now_ts=2)  # idempotent same hash -> ok, no error
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_forward_campaigns").fetchone()[0] == 1
    with pytest.raises(FPP.FrozenCampaignError):
        FPP.start_campaign(conn, campaign_id="c", manifest={"a": 2}, manifest_hash="h2", git_commit="g",
                           now_ts=3)


def test_decision_idempotent_on_key(tmp_path):
    conn = _conn(tmp_path)
    d1 = FPP.record_decision(conn, campaign_id="c", condition_id="0x1", rule_id="R1", rule_version="v0",
                             checkpoint=240, decision_ts=100, selected_outcome="Up", status="paper_entry",
                             reason="momentum_up", now_ts=1)
    d2 = FPP.record_decision(conn, campaign_id="c", condition_id="0x1", rule_id="R1", rule_version="v0",
                             checkpoint=240, decision_ts=100, selected_outcome="Up", status="paper_entry",
                             reason="momentum_up", now_ts=2)
    assert d1 == d2                                              # same decision -> one row (restart-safe)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_forward_decisions").fetchone()[0] == 1


def test_entry_and_fee_scenarios_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    did = FPP.record_decision(conn, campaign_id="c", condition_id="0x1", rule_id="R1", rule_version="v0",
                              checkpoint=240, decision_ts=100, selected_outcome="Up", status="paper_entry",
                              reason="momentum_up", now_ts=1)
    eid = FPP.record_entry(conn, decision_id=did, campaign_id="c", condition_id="0x1", rule_id="R1",
                           selected_token_id="UP", selected_outcome="Up", decision_ts=100,
                           feature_cutoff_ts=100, fill_book_ts=101, modeled_latency_ms=500, best_ask=0.50,
                           depth_weighted_fill_price=0.50, gross_trade_cost=10.0, shares=20.0,
                           levels_consumed=1, levels=[{"price": 0.5, "shares": 20.0}], book_hash="h",
                           book_age_ms=200, spread=0.02, slippage_vs_best_ask=0.0,
                           book_integrity_status="usable", now_ts=1)
    FPP.record_fee_scenario(conn, entry_id=eid, campaign_id="c", condition_id="0x1", rule_id="R1",
                            scenario="F1_documented", fee=0.35, pnl=9.65, won=True, now_ts=1)
    FPP.record_fee_scenario(conn, entry_id=eid, campaign_id="c", condition_id="0x1", rule_id="R1",
                            scenario="F1_documented", fee=0.35, pnl=9.65, won=True, now_ts=2)  # idempotent
    rows = conn.execute("SELECT scenario, pnl FROM crypto5m_forward_fee_scenarios").fetchall()
    assert len(rows) == 1 and rows[0]["scenario"] == "F1_documented" and rows[0]["pnl"] == 9.65


def test_pending_settlements_recovers_unsettled_and_quarantined(tmp_path):
    conn = _conn(tmp_path)
    FPP.start_campaign(conn, campaign_id="c", manifest={"a": 1}, manifest_hash="h", git_commit="g",
                       now_ts=1)

    def _dec(cid, dts):
        FPP.record_decision(conn, campaign_id="c", condition_id=cid, rule_id="C0", rule_version="v0",
                            checkpoint=240, decision_ts=dts, selected_outcome="Up", status="paper_entry",
                            reason="x", now_ts=1)

    _dec("0xA", 1240)   # final Up settlement -> NOT pending
    FPP.record_settlement(conn, campaign_id="c", condition_id="0xA", winning_outcome="Up",
                          reconciliation_status="gamma_only", now_ts=2)
    _dec("0xB", 1540)   # quarantined (unresolved) settlement -> pending (retry)
    FPP.record_settlement(conn, campaign_id="c", condition_id="0xB", winning_outcome=None,
                          reconciliation_status="unresolved", now_ts=2)
    _dec("0xC", 1840)   # no settlement at all -> pending

    pend = FPP.pending_settlements(conn, "c", checkpoint=240)
    by = {p["condition_id"]: p for p in pend}
    assert set(by) == {"0xB", "0xC"}                                   # final Up excluded; rest retried
    assert by["0xB"]["slug"] == "btc-updown-5m-1300"                   # window_start = 1540 - 240
    assert by["0xB"]["window_end_ts"] == 1300 + 300


def test_skip_recorded(tmp_path):
    conn = _conn(tmp_path)
    did = FPP.record_decision(conn, campaign_id="c", condition_id="0x2", rule_id="R2", rule_version="v0",
                              checkpoint=240, decision_ts=100, selected_outcome=None,
                              status="paper_no_trade", reason="insufficient_depth", now_ts=1)
    FPP.record_skip(conn, decision_id=did, campaign_id="c", condition_id="0x2", rule_id="R2",
                    reason="insufficient_depth", now_ts=1)
    assert conn.execute("SELECT reason FROM crypto5m_forward_skips").fetchone()["reason"] == "insufficient_depth"
