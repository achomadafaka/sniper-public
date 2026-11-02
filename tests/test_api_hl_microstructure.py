from __future__ import annotations

import os

from flask import Flask

from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.features import RollingMarketState
from sniper.algos.hl_microstructure.experts import build_expert_signals
from sniper.algos.hl_microstructure.models import PolicyDecision
from sniper.algos.hl_microstructure.paper import PaperConfig, PaperEngine
from sniper.algos.hl_microstructure.policies import PolicyConfig, evaluate_policies
from sniper.algos.hl_microstructure.radar import RadarCandidate
from sniper.routes.api_hl_microstructure import create_api_hl_microstructure_blueprint


def _client(db_path: str):
    app = Flask(__name__)
    app.register_blueprint(create_api_hl_microstructure_blueprint({"db_path": db_path}))
    return app.test_client()


def test_status_reports_missing_hlm_db_without_creating_it(tmp_path):
    db_path = tmp_path / "missing.sqlite"

    body = _client(str(db_path)).get("/api/hl-microstructure/status").get_json()

    assert body["available"] is False
    assert body["summary"]["decisions"] == 0
    assert body["message"] == "paper DB not found"
    assert not os.path.exists(db_path)


def test_status_reads_paper_lane_summary(tmp_path):
    db_path = tmp_path / "hlm.sqlite"
    conn = P.connect(str(db_path))
    P.ensure_schema(conn)
    state = RollingMarketState()
    engine = PaperEngine(
        conn,
        PaperConfig(notional_usdc=5.0, fee_rate=0.001, take_profit_pct=1.0, stop_loss_pct=1.0),
    )
    ts = 1_800_000_000_000
    for i in range(8):
        now = ts + i * 1000
        state.update_book(BookState.from_l2(
            "BTC",
            [[{"px": str(100 + i), "sz": "10"}], [{"px": str(100.1 + i), "sz": "1"}]],
            exchange_ts_ms=now,
            receive_ts_ms=now,
        ))
        state.ingest_trade("BTC", side="B", price=100.1 + i, size=1, exchange_ts_ms=now, receive_ts_ms=now)
        snap = state.snapshot("BTC", decision_ts_ms=now)
        for decision in evaluate_policies(snap, PolicyConfig()):
            engine.on_decision(decision, snap, state.books["BTC"])
    conn.close()

    body = _client(str(db_path)).get("/api/hl-microstructure/status").get_json()

    assert body["available"] is True
    assert body["summary"]["decisions"] > 0
    assert body["summary"]["positions_open"] >= 1
    assert any(r["policy_id"] == "flow_momentum" for r in body["lanes"])
    assert body["positions"]
    assert "Paper-only" in body["disclaimer"]


def test_status_includes_market_radar_cockpit_payload(tmp_path):
    db_path = tmp_path / "hlm.sqlite"
    conn = P.connect(str(db_path))
    P.ensure_schema(conn)
    P.record_radar_snapshot(
        conn,
        decision_ts_ms=1_800_000_010_000,
        candidates=[
            RadarCandidate("ZEC", 14.5, 1, ["volume", "funding_extreme"], 83_000_000, 448_000, 0.000012, -0.0002),
            RadarCandidate("HYPE", 12.0, 2, ["volume"], 423_000_000, 20_000_000, 0.0000125, 0.0),
        ],
    )
    state = RollingMarketState()
    book = BookState.from_l2(
        "ZEC",
        [[{"px": "100", "sz": "10"}], [{"px": "100.1", "sz": "10"}]],
        exchange_ts_ms=1_800_000_010_000,
        receive_ts_ms=1_800_000_010_000,
    )
    state.update_book(book)
    snap = state.snapshot("ZEC", decision_ts_ms=1_800_000_010_000)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=5.0, fee_rate=0.001))
    engine.on_decision(
        decision=PolicyDecision("fusion_radar", "enter", "long", "fusion_3_source_long", 0.72),
        snapshot=snap,
        book=book,
    )
    conn.close()

    body = _client(str(db_path)).get("/api/hl-microstructure/status").get_json()

    assert body["paper_status"]["paper_trading_live"] is True
    assert body["paper_status"]["real_money"] is False
    assert body["radar"][0]["asset"] == "ZEC"
    assert body["radar"][0]["reasons"] == ["volume", "funding_extreme"]
    assert body["open_theses"][0]["policy_id"] == "fusion_radar"
    assert body["open_theses"][0]["thesis"]
    assert body["open_theses"][0]["exit_plan"]
    assert "flow" in body["open_theses"][0]["source_votes"]


def test_status_market_radar_tables_are_scoped_to_fusion_lane(tmp_path):
    db_path = tmp_path / "hlm.sqlite"
    conn = P.connect(str(db_path))
    P.ensure_schema(conn)
    book = BookState.from_l2(
        "HYPE",
        [[{"px": "100", "sz": "10"}], [{"px": "100.1", "sz": "10"}]],
        exchange_ts_ms=1_800_000_020_000,
        receive_ts_ms=1_800_000_020_000,
    )
    state = RollingMarketState()
    state.update_book(book)
    snap = state.snapshot("HYPE", decision_ts_ms=1_800_000_020_000)
    snapshot_id = P.record_feature_snapshot(conn, snap)
    P.record_decision(
        conn,
        PolicyDecision("fusion_radar", "skip", None, "needs_two_source_families", 0.0),
        snap,
        feature_snapshot_id=snapshot_id,
        status="skipped",
        fee_rate=0.001,
    )
    P.record_decision(
        conn,
        PolicyDecision("legacy_control", "skip", None, "control_lane", 0.0),
        snap,
        feature_snapshot_id=snapshot_id,
        status="skipped",
        fee_rate=0.001,
    )
    conn.close()

    body = _client(str(db_path)).get("/api/hl-microstructure/status").get_json()

    assert [r["reason"] for r in body["skip_reasons"]] == ["needs_two_source_families"]
    assert {r["policy_id"] for r in body["decisions"]} == {"fusion_radar"}


def test_status_exposes_expert_and_source_health(tmp_path):
    db_path = tmp_path / "hlm.sqlite"
    conn = P.connect(str(db_path))
    P.ensure_schema(conn)
    state = RollingMarketState()
    for ts, bid in ((1_000, 100.0), (6_000, 100.4), (11_000, 100.8)):
        book = BookState.from_l2(
            "HYPE",
            [[{"px": str(bid), "sz": "10"}], [{"px": str(bid + 0.1), "sz": "10"}]],
            exchange_ts_ms=ts,
            receive_ts_ms=ts,
        )
        P.record_raw_event(conn, channel="l2Book", asset="HYPE", exchange_ts_ms=ts, receive_ts_ms=ts, payload={"coin": "HYPE"})
        P.record_raw_event(conn, channel="trades", asset="HYPE", exchange_ts_ms=ts, receive_ts_ms=ts, payload={"coin": "HYPE", "time": ts})
        state.update_book(book)
        state.ingest_trade("HYPE", side="B", price=bid + 0.1, size=2, exchange_ts_ms=ts, receive_ts_ms=ts)
    snap = state.snapshot("HYPE", decision_ts_ms=11_000)
    signals = build_expert_signals(snap)
    P.record_expert_predictions(conn, snap, signals)
    P.settle_due_expert_predictions(conn, asset="HYPE", decision_ts_ms=80_000, mid=102.0)
    conn.close()

    body = _client(str(db_path)).get("/api/hl-microstructure/status").get_json()

    assert body["expert_health"]
    assert body["source_health"]["book_flow"]["active"] is True
    assert body["source_health"]["expert_lab"]["active"] is True
