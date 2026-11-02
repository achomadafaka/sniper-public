import pytest

from sniper.hyperlive.altshort_evidence import (
    CostModel,
    block_bootstrap_mean,
    concentration_report,
    evaluate_frozen_exit,
    fixed_slot_portfolio,
    select_exit_rule,
    simulate_exit,
)


def _bars(*points):
    return [{"ts_ms": ts, "open": px, "close": px} for ts, px in points]


def test_exit_simulators_use_next_bar_entry_and_attribute_reason():
    trade = {"coin": "AAA", "side": "short", "decision_ts_ms": 100}
    bars = _bars((200, 100), (300, 95), (400, 85), (500, 90), (600, 92))
    fixed = simulate_exit(
        trade, bars, rule={"id": "fixed", "kind": "fixed", "horizon_ms": 100},
        costs=CostModel(round_trip_bps=10),
    )
    assert fixed["entry_ts_ms"] == 200 and fixed["exit_ts_ms"] == 300
    assert fixed["reason"] == "time_cap"
    assert fixed["net_bps"] == pytest.approx(490.0)

    tp = simulate_exit(
        trade, bars,
        rule={"id": "tp", "kind": "tp_sl_cap", "take_profit_bps": 1000,
              "stop_loss_bps": 1000, "cap_ms": 500},
        costs=CostModel(round_trip_bps=10),
    )
    assert tp["exit_ts_ms"] == 400 and tp["reason"] == "take_profit"

    trail = simulate_exit(
        trade, bars,
        rule={"id": "trail", "kind": "trailing", "arm_bps": 1000,
              "giveback_bps": 400, "cap_ms": 500},
        costs=CostModel(round_trip_bps=10),
    )
    assert trail["exit_ts_ms"] == 500 and trail["reason"] == "trailing_giveback"
    assert trail["net_bps"] == pytest.approx(990.0)


def test_exit_selection_cannot_read_holdout():
    candidates = [
        {"id": "a1", "family": "a"},
        {"id": "a2", "family": "a"},
        {"id": "b1", "family": "b"},
    ]
    train = {"a1": [100, 90], "a2": [80, 70], "b1": [50, 50]}
    validation = {"a1": [-20], "a2": [9999], "b1": [30]}
    selected = select_exit_rule(train, validation, candidates=candidates)
    assert selected["id"] == "b1"  # a2 was eliminated by training within family a
    holdout = {"a1": [1_000_000], "b1": [25, 35]}
    assert evaluate_frozen_exit(holdout, rule=selected)["mean_bps"] == 30


def test_fixed_slot_portfolio_rejects_overlap_and_marks_open_book():
    trades = [
        {"id": 1, "coin": "AAA", "entry_ts_ms": 0, "exit_ts_ms": 100,
         "gross_pnl_bps": 1000, "status": "closed"},
        {"id": 2, "coin": "BBB", "entry_ts_ms": 50, "exit_ts_ms": 70,
         "gross_pnl_bps": 9000, "status": "closed"},
        {"id": 3, "coin": "CCC", "entry_ts_ms": 100, "exit_ts_ms": 150,
         "gross_pnl_bps": -500, "status": "closed"},
        {"id": 4, "coin": "DDD", "entry_ts_ms": 200, "status": "open",
         "unrealized_bps": 200, "mark_ts_ms": 290},
    ]
    result = fixed_slot_portfolio(
        trades, slots=1, initial_equity=1000, costs=CostModel(), now_ms=300,
        max_mark_age_ms=20,
    )
    assert result["valid"] is True
    assert result["accepted_ids"] == [1, 3, 4]
    assert result["rejected_ids"] == [2]
    assert result["closed_pnl"] == pytest.approx(50.0)
    assert result["open_mtm"] == pytest.approx(20.0)
    assert result["total_equity"] == pytest.approx(1070.0)
    assert result["total_return_bps"] == pytest.approx(700.0)
    assert result["max_concurrent"] == 1


def test_fixed_slot_portfolio_blocks_stale_open_mark():
    result = fixed_slot_portfolio(
        [{"id": 1, "coin": "AAA", "entry_ts_ms": 0, "status": "open",
          "unrealized_bps": 100, "mark_ts_ms": 10}],
        slots=1, initial_equity=1000, costs=CostModel(), now_ms=100,
        max_mark_age_ms=20,
    )
    assert result["valid"] is False
    assert result["reason"] == "stale_open_marks"
    assert result["stale_ids"] == [1]


def test_day_block_bootstrap_keeps_clusters_intact_and_is_seeded():
    rows = [
        {"day": "d1", "residual_bps": 100},
        {"day": "d1", "residual_bps": 100},
        {"day": "d2", "residual_bps": -100},
    ]
    a = block_bootstrap_mean(rows, cluster_key="day", replications=100, seed=7)
    b = block_bootstrap_mean(rows, cluster_key="day", replications=100, seed=7)
    assert a == b
    assert a["clusters"] == 2
    expected = (-100.0, 100 / 3, 100.0)
    assert all(any(value == pytest.approx(option) for option in expected) for value in a["samples"])


def test_concentration_reports_top_groups_and_leave_one_out():
    rows = [
        {"coin": "AAA", "week": "w1", "net_bps": 60},
        {"coin": "AAA", "week": "w2", "net_bps": 20},
        {"coin": "BBB", "week": "w2", "net_bps": 20},
    ]
    result = concentration_report(rows)
    assert result["top_coin"] == "AAA" and result["top_coin_share"] == 0.8
    assert result["top_week"] == "w1" and result["top_week_share"] == 0.6
    assert result["leave_top_coin_bps"] == 20
    assert result["leave_top_week_bps"] == 40
