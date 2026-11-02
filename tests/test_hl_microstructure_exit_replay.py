import os

from sniper.algos.hl_microstructure import exit_replay as ER
from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision


def test_reconstruct_excursions_long_tracks_best_and_worst_move():
    out = ER.reconstruct_excursions(100.0, "long", [101.0, 99.0, 102.0])
    assert round(out["mfe_pct"], 6) == 0.02   # best favorable: 102 vs 100
    assert round(out["mae_pct"], 6) == -0.01  # worst adverse: 99 vs 100
    assert out["points"] == 3


def test_reconstruct_excursions_short_is_side_aware():
    out = ER.reconstruct_excursions(100.0, "short", [101.0, 98.0])
    assert round(out["mfe_pct"], 6) == 0.02   # 98 is favorable for a short
    assert round(out["mae_pct"], 6) == -0.01  # 101 is adverse for a short
    assert out["points"] == 2


def test_reconstruct_excursions_empty_path_is_none():
    out = ER.reconstruct_excursions(100.0, "long", [])
    assert out["mfe_pct"] is None
    assert out["mae_pct"] is None
    assert out["points"] == 0


def test_replay_exit_take_profit_fills_at_threshold_not_overshoot():
    path = [(1_000, 100.5), (2_000, 102.5)]  # +0.5%, then +2.5% overshoot
    out = ER.replay_exit(100.0, 0, "long", path,
                         stop_loss_pct=0.01, take_profit_pct=0.02, time_stop_ms=60_000)
    assert out["exit_reason"] == "take_profit"
    assert round(out["exit_move_pct"], 6) == 0.02   # capped at the limit, not 0.025
    assert out["elapsed_ms"] == 2_000


def test_replay_exit_stop_loss_fills_at_stop_level():
    path = [(1_000, 99.0)]  # -1.0%
    out = ER.replay_exit(100.0, 0, "long", path,
                         stop_loss_pct=0.005, take_profit_pct=0.02, time_stop_ms=60_000)
    assert out["exit_reason"] == "stop_loss"
    assert round(out["exit_move_pct"], 6) == -0.005


def test_replay_exit_time_stop_uses_actual_move():
    path = [(1_000, 100.3), (90_000, 100.7)]  # never hits stop/take
    out = ER.replay_exit(100.0, 0, "long", path,
                         stop_loss_pct=0.05, take_profit_pct=0.05, time_stop_ms=60_000)
    assert out["exit_reason"] == "time_stop"
    assert round(out["exit_move_pct"], 6) == 0.007
    assert out["elapsed_ms"] == 90_000


def test_replay_exit_path_end_is_right_censored():
    path = [(1_000, 100.3)]  # path exhausts before any trigger
    out = ER.replay_exit(100.0, 0, "long", path,
                         stop_loss_pct=0.05, take_profit_pct=0.05, time_stop_ms=60_000)
    assert out["exit_reason"] == "path_end"
    assert round(out["exit_move_pct"], 6) == 0.003


def test_replay_exit_empty_path_is_none():
    assert ER.replay_exit(100.0, 0, "long", [],
                          stop_loss_pct=0.01, take_profit_pct=0.02, time_stop_ms=60_000) is None


def _conn(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_replay.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    return conn


def _closed_long_with_path(conn):
    pos_id = P.open_position(
        conn, policy_id="fusion_radar", asset="BTC", side="long",
        entry_price=100.0, quantity=0.25, notional_usdc=25.0, leverage=1.0,
        fee_usdc=0.01, opened_ts_ms=1_000, source_votes={"flow": {"side": "long", "score": 0.9}},
    )
    P.close_position(conn, position_id=pos_id, exit_fee_usdc=0.01, realized_pnl=0.10,
                     closed_ts_ms=5_000, reason="take_profit")
    snap = FeatureSnapshot("BTC", 1_000, "hlm_fv1",
                           {"mid": 100.0, "spread_bps": 4.0, "bid_depth_5": 30_000, "ask_depth_5": 28_000},
                           "ok", [])
    snap_id = P.record_feature_snapshot(conn, snap)
    P.record_decision(conn, PolicyDecision("fusion_radar", "enter", "long", "fusion_2_source_long", 0.8),
                      snap, feature_snapshot_id=snap_id, status="entered", position_id=pos_id, fee_rate=0.00045)
    for ts, mid in ((2_000, 100.5), (3_000, 100.9), (200_000, 101.0)):
        P.record_feature_snapshot(conn, FeatureSnapshot("BTC", ts, "hlm_fv1", {"mid": mid, "spread_bps": 4.0}, "ok", []))
    return pos_id


def test_build_exit_replay_reports_mfe_mae_over_forward_path(tmp_path):
    conn = _conn(tmp_path)
    _closed_long_with_path(conn)

    report = ER.build_exit_replay(conn)

    assert report["summary"]["positions"] == 1
    assert report["summary"]["with_path"] == 1
    assert report["mfe_mae"]["avg_mfe_bps"] == 100.0   # best forward mid 101.0 == +1%
    assert report["mfe_mae"]["avg_mae_bps"] == 50.0    # least-favorable point 100.5 == +0.5%


def test_build_exit_replay_grid_uses_threshold_exits_and_spread_cost(tmp_path):
    conn = _conn(tmp_path)
    _closed_long_with_path(conn)

    report = ER.build_exit_replay(conn)
    cell = next(
        c for c in report["exit_grid"]
        if c["take_profit_pct"] == 0.008 and c["stop_loss_pct"] == 0.003 and c["time_stop_ms"] == 300_000
    )

    # +0.9% touches the 0.008 take-profit -> fills at 0.008; net = 0.008 - (2*0.00045 + 4bps) = 67 bps.
    assert cell["positions"] == 1
    assert cell["avg_net_bps"] == 67.0
    assert cell["net_win_rate"] == 1.0


def test_build_exit_replay_cost_sensitivity_is_hypothetical_and_cheaper(tmp_path):
    conn = _conn(tmp_path)
    _closed_long_with_path(conn)

    report = ER.build_exit_replay(conn)
    models = {m["name"]: m for m in report["cost_sensitivity"]}

    # Best cell gross = 0.008 (TP touched). taker = -13bps, maker = -2bps, frictionless = 0.
    assert models["taker_actual"]["best_avg_net_bps"] == 67.0
    assert models["maker_hypothetical"]["best_avg_net_bps"] == 78.0
    assert models["frictionless_bound"]["best_avg_net_bps"] == 80.0
    # Honesty guards: taker is real, maker/frictionless are flagged hypothetical.
    assert models["taker_actual"]["hypothetical"] is False
    assert models["maker_hypothetical"]["hypothetical"] is True
    assert models["frictionless_bound"]["hypothetical"] is True


def test_build_exit_replay_cli_prints_markdown(tmp_path, capsys):
    import scripts.hl_microstructure_exit_replay as CLI
    conn = _conn(tmp_path)
    _closed_long_with_path(conn)
    db = conn.execute("PRAGMA database_list").fetchone()[2]

    assert CLI.main(["--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "# Hyperliquid Microstructure Exit Replay" in out
    assert "in-sample" in out.lower()
