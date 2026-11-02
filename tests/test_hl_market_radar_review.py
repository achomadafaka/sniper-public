import os

from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision
from sniper.algos.hl_microstructure.paper import PaperConfig, PaperEngine
from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.radar import RadarCandidate
from sniper.algos.hl_microstructure.experts import build_expert_signals
from sniper.algos.hl_microstructure.review import build_performance_report, format_markdown_report


def test_performance_report_calls_out_loss_and_selected_markets(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    P.record_radar_snapshot(
        conn,
        decision_ts_ms=10_000,
        candidates=[RadarCandidate("HOT", 15.0, 1, ["volume", "funding_extreme"], 6_000_000, 90_000_000, -0.0002, -0.004)],
    )
    book = BookState.from_l2("HOT", [[{"px": "99", "sz": "10"}], [{"px": "100", "sz": "10"}]], exchange_ts_ms=10_000, receive_ts_ms=10_000)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=10.0, fee_rate=0.001))
    snap = FeatureSnapshot("HOT", 10_000, "hlm_fv0", {"mid": 99.5, "spread_bps": 10.0, "realized_vol_60s": 0.002}, "ok", [])
    result = engine.on_decision(PolicyDecision("fusion_radar", "enter", "long", "fusion_3_source_long", 0.8), snap, book)
    P.close_position(conn, position_id=result.position_id, exit_fee_usdc=0.01, realized_pnl=-0.25, closed_ts_ms=12_000, reason="stop_loss")

    report = build_performance_report(conn)
    text = format_markdown_report(report)

    assert report["summary"]["fusion_realized_pnl"] < 0
    assert report["radar_assets"][0]["asset"] == "HOT"
    assert any("negative" in x.lower() for x in report["bad"])
    assert "HOT" in text
    assert "What to improve" in text


def test_performance_report_includes_expert_lab(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    snap = FeatureSnapshot(
        "HOT",
        10_000,
        "hlm_fv1",
        {
            "mid": 100.0,
            "spread_bps": 5.0,
            "flow_imbalance_5s": 0.7,
            "flow_imbalance_10s": 0.7,
            "trade_count_5s": 5,
            "trade_count_10s": 8,
            "return_15s": 0.002,
        },
        "ok",
        [],
    )
    P.record_expert_predictions(conn, snap, build_expert_signals(snap))
    P.settle_due_expert_predictions(conn, asset="HOT", decision_ts_ms=80_000, mid=101.0)

    report = build_performance_report(conn)
    text = format_markdown_report(report)

    assert report["expert_health"]
    assert "Expert lab" in text
