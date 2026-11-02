import math
import os

import scripts.hl_microstructure_evidence_audit as CLI
from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure import evidence_audit as EA
from sniper.algos.hl_microstructure.evidence_audit import (
    build_evidence_audit,
    format_evidence_audit_markdown,
)
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision


def _conn(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_audit.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    return conn


def _position(conn, *, asset, side, pnl, close_reason, opened_ts, source_votes):
    pos_id = P.open_position(
        conn,
        policy_id="fusion_radar",
        asset=asset,
        side=side,
        entry_price=100.0,
        quantity=0.25,
        notional_usdc=25.0,
        leverage=1.0,
        fee_usdc=0.01,
        opened_ts_ms=opened_ts,
        source_votes=source_votes,
    )
    P.close_position(
        conn,
        position_id=pos_id,
        exit_fee_usdc=0.01,
        realized_pnl=pnl,
        closed_ts_ms=opened_ts + 30_000,
        reason=close_reason,
    )
    return pos_id


def _entry_decision(conn, *, asset, side, reason, ts, spread_bps, bid_depth, ask_depth, position_id):
    snapshot = FeatureSnapshot(
        asset,
        ts,
        "hlm_fv1",
        {
            "mid": 100.0,
            "spread_bps": spread_bps,
            "bid_depth_5": bid_depth,
            "ask_depth_5": ask_depth,
            "flow_imbalance_5s": 0.7 if side == "long" else -0.7,
            "book_imbalance_5": 0.4 if side == "long" else -0.4,
            "trade_count_10s": 8,
        },
        "ok",
        [],
    )
    snapshot_id = P.record_feature_snapshot(conn, snapshot)
    P.record_decision(
        conn,
        PolicyDecision("fusion_radar", "enter", side, reason, 0.8),
        snapshot,
        feature_snapshot_id=snapshot_id,
        status="entered",
        position_id=position_id,
        fee_rate=0.00045,
    )


def _snapshot(conn, *, asset, ts, mid, spread_bps=4.0, bid_depth=30_000, ask_depth=28_000):
    P.record_feature_snapshot(
        conn,
        FeatureSnapshot(
            asset,
            ts,
            "hlm_fv1",
            {
                "mid": mid,
                "spread_bps": spread_bps,
                "bid_depth_5": bid_depth,
                "ask_depth_5": ask_depth,
                "flow_imbalance_5s": 0.0,
                "book_imbalance_5": 0.0,
                "trade_count_10s": 4,
            },
            "ok",
            [],
        ),
    )


def _entry_with_future(conn, *, asset="BTC", ts, votes, side="long", future_mid=101.0,
                       spread_bps=4.0, bid_depth=30_000, ask_depth=28_000):
    pos = _position(
        conn,
        asset=asset,
        side=side,
        pnl=0.10,
        close_reason="take_profit",
        opened_ts=ts,
        source_votes=votes,
    )
    _entry_decision(
        conn,
        asset=asset,
        side=side,
        reason=f"fusion_2_source_{side}",
        ts=ts,
        spread_bps=spread_bps,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        position_id=pos,
    )
    _snapshot(conn, asset=asset, ts=ts + 5_000, mid=future_mid, spread_bps=spread_bps,
              bid_depth=bid_depth, ask_depth=ask_depth)
    _snapshot(conn, asset=asset, ts=ts + 10_000, mid=future_mid, spread_bps=spread_bps,
              bid_depth=bid_depth, ask_depth=ask_depth)
    _snapshot(conn, asset=asset, ts=ts + 30_000, mid=future_mid, spread_bps=spread_bps,
              bid_depth=bid_depth, ask_depth=ask_depth)
    return pos


def test_evidence_audit_summarizes_source_pairs_counts_exits_and_regimes(tmp_path):
    conn = _conn(tmp_path)
    long_pos = _position(
        conn,
        asset="BTC",
        side="long",
        pnl=0.15,
        close_reason="take_profit",
        opened_ts=10_000,
        source_votes={
            "flow": {"side": "long", "score": 0.9},
            "book": {"side": "long", "score": 0.6},
        },
    )
    short_pos = _position(
        conn,
        asset="ETH",
        side="short",
        pnl=-0.25,
        close_reason="signal_decay",
        opened_ts=20_000,
        source_votes={
            "flow": {"side": "short", "score": 0.8},
            "momentum": {"side": "short", "score": 0.5},
            "funding": {"side": "long", "score": 0.7},
        },
    )
    _entry_decision(conn, asset="BTC", side="long", reason="fusion_2_source_long", ts=10_000,
                    spread_bps=4.0, bid_depth=30_000, ask_depth=28_000, position_id=long_pos)
    _entry_decision(conn, asset="ETH", side="short", reason="fusion_2_source_short", ts=20_000,
                    spread_bps=24.0, bid_depth=2_000, ask_depth=1_500, position_id=short_pos)

    report = build_evidence_audit(conn)
    text = format_evidence_audit_markdown(report)

    assert report["summary"]["closed_positions"] == 2
    assert report["summary"]["realized_pnl"] == -0.10
    assert {r["pair"] for r in report["source_pairs"]} == {"book+flow", "flow+momentum"}
    assert report["source_counts"][2]["closed"] == 2
    assert report["exit_reasons"][0]["reason"] == "signal_decay"
    assert {r["bucket"] for r in report["regime_buckets"]} == {"tight_deep", "wide_thin"}
    assert "book+flow" in text
    assert "Data Gaps" in text


def test_evidence_audit_reports_expert_predictiveness_and_capture_gaps(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        """INSERT INTO hlm_expert_predictions
           (asset, expert_id, family, side, confidence, reliability, horizon_ms,
            decision_ts_ms, due_ts_ms, entry_mid, rationale, label_side,
            realized_return, result, settled_ts_ms, code_version)
           VALUES
           ('BTC','flow_5s','flow','long',0.8,0.7,5000,1000,6000,100.0,'flow','long',0.001,1,6000,'test'),
           ('BTC','flow_5s','flow','short',0.8,0.7,5000,7000,12000,100.0,'flow','long',0.001,0,12000,'test'),
           ('BTC','carry_funding','carry','short',0.8,0.7,60000,1000,61000,100.0,'funding','long',0.001,0,61000,'test')"""
    )
    conn.commit()

    report = build_evidence_audit(conn)

    assert report["expert_predictiveness"][0]["expert_id"] == "flow_5s"
    assert report["expert_predictiveness"][0]["settled"] == 2
    assert report["data_gaps"]["trade_ticks"] == "available_in_hlm_raw_events"
    assert report["data_gaps"]["mfe_mae"] == "not_persisted_reconstructable_from_snapshots"


def test_evidence_audit_labels_source_votes_against_forward_returns_after_costs(tmp_path):
    conn = _conn(tmp_path)
    long_pos = _position(
        conn,
        asset="BTC",
        side="long",
        pnl=0.20,
        close_reason="take_profit",
        opened_ts=1_000,
        source_votes={
            "flow": {"side": "long", "score": 0.9},
            "book": {"side": "long", "score": 0.7},
        },
    )
    short_pos = _position(
        conn,
        asset="BTC",
        side="short",
        pnl=0.10,
        close_reason="take_profit",
        opened_ts=20_000,
        source_votes={
            "flow": {"side": "short", "score": 0.8},
            "funding": {"side": "long", "score": 0.5},
        },
    )
    _entry_decision(conn, asset="BTC", side="long", reason="fusion_2_source_long", ts=1_000,
                    spread_bps=4.0, bid_depth=30_000, ask_depth=28_000, position_id=long_pos)
    _entry_decision(conn, asset="BTC", side="short", reason="fusion_2_source_short", ts=20_000,
                    spread_bps=4.0, bid_depth=30_000, ask_depth=28_000, position_id=short_pos)
    _snapshot(conn, asset="BTC", ts=6_000, mid=101.0)
    _snapshot(conn, asset="BTC", ts=11_000, mid=101.5)
    _snapshot(conn, asset="BTC", ts=25_000, mid=99.0)
    _snapshot(conn, asset="BTC", ts=30_000, mid=98.5)

    report = build_evidence_audit(conn)
    text = format_evidence_audit_markdown(report)

    flow_5s = next(
        r for r in report["source_forward_returns"]
        if r["source"] == "flow" and r["horizon_ms"] == 5_000
    )
    funding_5s = next(
        r for r in report["source_forward_returns"]
        if r["source"] == "funding" and r["horizon_ms"] == 5_000
    )
    assert flow_5s["observations"] == 2
    assert flow_5s["directional_accuracy"] == 1.0
    assert flow_5s["net_win_rate"] == 1.0
    assert flow_5s["avg_net_bps"] == 87.0
    assert funding_5s["directional_accuracy"] == 0.0
    assert "Source Forward Returns" in text
    assert "avg_net_bps" in text
    assert report["summary"]["source_label_population"] == "entered_only_source_votes"
    assert "entered-only source votes" in text


def test_evidence_audit_net_returns_include_entry_spread_cost(tmp_path):
    conn = _conn(tmp_path)
    _entry_with_future(
        conn,
        ts=1_000,
        votes={"flow": {"side": "long", "score": 0.9}},
        future_mid=100.2,
        spread_bps=20.0,
    )

    report = build_evidence_audit(conn)
    flow_5s = next(
        r for r in report["source_forward_returns"]
        if r["source"] == "flow" and r["horizon_ms"] == 5_000
    )

    assert report["summary"]["net_cost_model"] == "mid_return_minus_taker_round_trip_and_entry_spread"
    assert flow_5s["avg_signed_bps"] == 20.0
    assert flow_5s["avg_net_bps"] == -9.0


def test_evidence_audit_runs_mandatory_negative_controls(tmp_path):
    conn = _conn(tmp_path)
    pos = _position(
        conn,
        asset="BTC",
        side="long",
        pnl=0.20,
        close_reason="take_profit",
        opened_ts=1_000,
        source_votes={"flow": {"side": "long", "score": 0.9}},
    )
    _entry_decision(conn, asset="BTC", side="long", reason="fusion_2_source_long", ts=1_000,
                    spread_bps=4.0, bid_depth=30_000, ask_depth=28_000, position_id=pos)
    _snapshot(conn, asset="BTC", ts=6_000, mid=101.0)
    _snapshot(conn, asset="BTC", ts=61_000, mid=99.0)
    _snapshot(conn, asset="BTC", ts=66_000, mid=99.5)
    _snapshot(conn, asset="BTC", ts=71_000, mid=99.8)
    _snapshot(conn, asset="BTC", ts=91_000, mid=100.1)
    _snapshot(conn, asset="ETH", ts=1_000, mid=200.0)
    _snapshot(conn, asset="ETH", ts=6_000, mid=198.0)
    _snapshot(conn, asset="ETH", ts=61_000, mid=202.0)

    report = build_evidence_audit(conn)
    controls = {(r["control"], r["horizon_ms"]): r for r in report["negative_controls"]}

    assert set(c for c, _h in controls) == {
        "shuffled_signs",
        "random_entry_timestamps",
        "lagged_irrelevant_feature",
        "random_asset_assignment",
    }
    assert controls[("random_asset_assignment", 5_000)]["observations"] == 1
    assert controls[("random_asset_assignment", 5_000)]["directional_accuracy"] == 0.0


def test_evidence_audit_lagged_irrelevant_feature_side_is_sign_varying():
    assert EA._irrelevant_feature_side({"spread_bps": 5.0, "book_imbalance_5": 0.2}) == "long"
    assert EA._irrelevant_feature_side({"spread_bps": 5.0, "book_imbalance_5": -0.2}) == "short"
    assert EA._irrelevant_feature_side({"spread_bps": 5.0, "book_imbalance_5": 0.0}) is None


def test_evidence_audit_session_placebo_avoids_nearby_overlap():
    rows = [
        {"ts": 1_000, "mid": 100.0},
        {"ts": 8_000, "mid": 101.0},
        {"ts": 3_700_000, "mid": 99.0},
    ]

    picked = EA._session_placebo_entry(rows, 1_000, seed=0, min_gap_ms=30_000)

    assert picked["ts"] == 3_700_000


def test_evidence_audit_walk_forward_uses_temporal_blocks_and_embargo(tmp_path):
    conn = _conn(tmp_path)
    two_hours = 2 * 60 * 60 * 1000
    for ts in (1_000, 2_000, two_hours, two_hours + 1_000, 2 * two_hours, 2 * two_hours + 1_000):
        _entry_with_future(
            conn,
            ts=ts,
            votes={"flow": {"side": "long", "score": 0.9}},
            future_mid=101.0,
        )

    report = build_evidence_audit(conn)
    text = format_evidence_audit_markdown(report)
    rows = [
        r for r in report["walk_forward"]
        if r["source"] == "flow" and r["horizon_ms"] == 5_000
    ]

    assert len(rows) == 2
    assert rows[0]["block"] == 1
    assert rows[0]["embargo_ms"] == 3_600_000
    assert rows[0]["train_observations"] == 2
    assert rows[0]["test_observations"] == 2
    assert rows[0]["train_avg_net_bps"] == 87.0
    assert rows[0]["test_avg_net_bps"] == 87.0
    assert "Walk Forward" in text


def test_evidence_audit_applies_bh_fdr_to_source_forward_tests(tmp_path):
    conn = _conn(tmp_path)
    for i in range(12):
        _entry_with_future(
            conn,
            ts=1_000 + i * 60_000,
            votes={
                "flow": {"side": "long", "score": 0.9},
                "book": {"side": "short", "score": 0.7},
            },
            future_mid=101.0,
        )

    report = build_evidence_audit(conn)
    text = format_evidence_audit_markdown(report)
    tests = {(r["source"], r["horizon_ms"]): r for r in report["multiple_testing"]["tests"]}

    assert report["multiple_testing"]["fdr_method"] == "benjamini_hochberg"
    assert report["multiple_testing"]["fdr_alpha"] == 0.1
    assert report["multiple_testing"]["inference_mode"] == "screening_only_overlapping_entry_labels"
    assert tests[("flow", 5_000)]["p_value"] == 0.0
    assert tests[("flow", 5_000)]["q_value"] == 0.0
    assert tests[("flow", 5_000)]["screening_pass"] is True
    assert tests[("flow", 5_000)]["significant"] is False
    assert tests[("book", 5_000)]["screening_pass"] is False
    assert "Multiple Testing" in text
    assert "screening only" in text


def test_evidence_audit_reports_asset_regime_source_splits(tmp_path):
    conn = _conn(tmp_path)
    _entry_with_future(
        conn,
        asset="BTC",
        ts=1_000,
        votes={"flow": {"side": "long", "score": 0.9}},
        future_mid=101.0,
        spread_bps=4.0,
        bid_depth=30_000,
        ask_depth=28_000,
    )
    _entry_with_future(
        conn,
        asset="ETH",
        ts=1_000,
        votes={"flow": {"side": "long", "score": 0.9}},
        future_mid=99.0,
        spread_bps=24.0,
        bid_depth=2_000,
        ask_depth=1_500,
    )

    report = build_evidence_audit(conn)
    text = format_evidence_audit_markdown(report)
    rows = {
        (r["asset"], r["regime"], r["source"], r["horizon_ms"]): r
        for r in report["asset_regime_source_returns"]
    }

    assert rows[("BTC", "tight_deep", "flow", 5_000)]["avg_net_bps"] == 87.0
    assert rows[("ETH", "wide_thin", "flow", 5_000)]["avg_net_bps"] == -133.0
    assert "Asset/Regime Source Returns" in text


def test_student_t_sf_matches_known_values():
    # Upper-tail survival of Student-t. df=inf -> normal; df small -> heavier tail.
    assert abs(EA._student_t_sf(0.0, 5) - 0.5) < 1e-9
    # df=2 closed form: SF(t) = 0.5 * (1 - t / sqrt(2 + t^2)) for t > 0.
    t = 2.0 * math.sqrt(3.0)
    expected = 0.5 * (1.0 - t / math.sqrt(2.0 + t * t))
    assert abs(EA._student_t_sf(t, 2) - expected) < 1e-6
    # Heavier tail than normal for the same statistic.
    normal = 0.5 * math.erfc(t / math.sqrt(2.0))
    assert EA._student_t_sf(t, 2) > normal


def test_one_sided_mean_p_value_uses_student_t_not_normal():
    values = [0.001, 0.002, 0.003]  # n=3, df=2, positive mean, non-zero variance
    p = EA._one_sided_mean_p_value(values)

    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    se = math.sqrt(var / len(values))
    normal_p = round(0.5 * math.erfc((mean / se) / math.sqrt(2.0)), 6)

    assert p > normal_p          # t has heavier tails than the normal approximation
    assert 0.02 < p < 0.06       # df=2 upper tail at t~3.46 is ~0.037, not ~0.0003


def test_cluster_means_collapses_overlapping_forward_windows():
    rows = [
        {"asset": "BTC", "decision_ts_ms": 1_000, "net_return": 0.01},
        {"asset": "BTC", "decision_ts_ms": 2_000, "net_return": 0.03},
        {"asset": "BTC", "decision_ts_ms": 70_000, "net_return": 0.02},
    ]
    # 30s horizon: ts 1000 and 2000 share a block, 70000 is its own block -> 2 clusters.
    clusters = EA._cluster_means(rows, 30_000)
    assert len(clusters) == 2
    assert sorted(round(c, 6) for c in clusters) == [0.02, 0.02]
    # 1s horizon: every entry is its own block -> no collapse.
    assert len(EA._cluster_means(rows, 1_000)) == 3


def test_multiple_testing_uses_effective_sample_size_for_overlapping_entries(tmp_path):
    conn = _conn(tmp_path)
    # Three entries 1s apart: independent-looking but heavily overlapping forward windows.
    for ts in (1_000, 2_000, 3_000):
        _entry_with_future(
            conn,
            ts=ts,
            votes={"flow": {"side": "long", "score": 0.9}},
            future_mid=101.0,
        )

    report = build_evidence_audit(conn)
    flow_30s = next(
        r for r in report["multiple_testing"]["tests"]
        if r["source"] == "flow" and r["horizon_ms"] == 30_000
    )

    assert flow_30s["observations"] == 3
    assert flow_30s["effective_observations"] == 1   # all three collapse into one block
    assert flow_30s["p_value"] == 1.0                # one effective obs cannot be tested
    assert flow_30s["screening_pass"] is False


def test_evidence_audit_cli_prints_markdown(tmp_path, capsys):
    conn = _conn(tmp_path)
    db = conn.execute("PRAGMA database_list").fetchone()[2]

    assert CLI.main(["--db-path", db]) == 0

    out = capsys.readouterr().out
    assert "# Hyperliquid Microstructure Evidence Audit" in out
    assert "Data Gaps" in out
