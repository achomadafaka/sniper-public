from scripts.passive_strategy_scoreboard import (
    ScoreboardConfig,
    ScoreboardRow,
    classify_status,
    rank_rows,
    run_scoreboard,
)


def test_classify_status_rejects_negative_or_tiny_samples():
    assert classify_status(entries=8, total_pnl=100, current_entries=2, min_entries=10) == "reject"
    assert classify_status(entries=40, total_pnl=-1, current_entries=10, min_entries=10) == "reject"


def test_classify_status_marks_watch_and_paper_candidates():
    assert classify_status(entries=20, total_pnl=5, current_entries=2, min_entries=10) == "watch"
    assert classify_status(entries=40, total_pnl=50, current_entries=8, min_entries=10) == "paper"


def test_rank_rows_prefers_paper_status_then_robust_pnl():
    rows = [
        ScoreboardRow("a", "watch", 20, 10, 5, 10.0, 1.1, 2, 0.0, "cmd a"),
        ScoreboardRow("b", "paper", 40, 28, 12, 20.0, 1.2, 8, 15.0, "cmd b"),
        ScoreboardRow("c", "paper", 35, 24, 11, 30.0, 1.3, 5, 5.0, "cmd c"),
    ]

    ranked = rank_rows(rows)

    assert [row.strategy_id for row in ranked] == ["b", "c", "a"]


def test_scoreboard_runs_known_simulated_candidates(tmp_path, monkeypatch):
    def fake_run_grid(db_path, **kwargs):
        mode = kwargs["modes"][0]
        if mode == "favorite_flip_reclaim":
            return {
                "best": {
                    "entries": 20,
                    "wins": 13,
                    "losses": 7,
                    "total_pnl_usdc": 12.0,
                    "profit_factor": 1.1,
                    "config": {"mode": mode},
                }
            }
        return {
            "best": {
                "entries": 45,
                "wins": 25,
                "losses": 20,
                "total_pnl_usdc": -68.0,
                "profit_factor": 0.86,
                "config": {"mode": mode},
            }
        }

    def fake_run_sim(db_path, cfg, since_year=None, **kwargs):
        return {"entries": 3 if cfg.mode == "favorite_flip_reclaim" else 8}

    monkeypatch.setattr("scripts.passive_strategy_scoreboard.run_grid", fake_run_grid)
    monkeypatch.setattr("scripts.passive_strategy_scoreboard.run_sim", fake_run_sim)

    out = run_scoreboard(tmp_path / "bt.db", ScoreboardConfig(min_entries=10, current_since_year=2026))

    assert out["rows"][0]["strategy_id"] == "favorite_flip_reclaim_atp"
    assert out["rows"][0]["status"] == "watch"
    assert {row["strategy_id"] for row in out["rows"]} >= {
        "favorite_drop_fade_all",
        "favorite_flip_fade_all",
    }
    assert out["rows"][-1]["status"] == "reject"
