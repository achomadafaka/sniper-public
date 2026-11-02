import pytest

from data.backtest.schema import open_backtest_db
from scripts.optimize_stink_bid_params import parse_number_grid, run_grid


def test_parse_number_grid_supports_values_and_ranges():
    assert parse_number_grid("0.2,0.3", float) == [0.2, 0.3]
    assert parse_number_grid("1:3:1", int) == [1, 2, 3]
    with pytest.raises(ValueError):
        parse_number_grid("1:3", int)


def test_optimizer_ranks_grid_results(tmp_path):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev', 'Cup: Alice Smith vs Bob Jones', 'atp-smith-jones', 'atp',
                'tok_yes', 'tok_no', '2025-01-01T00:00:00Z', '2025-01-01T03:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid', 'atp_main', '20250101', 'Alice Smith', 'Bob Jones', 0, 1.0)
        """
    )
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.70, 1),
            ('tok_no', 1735689600, 0.30, 1),
            ('tok_yes', 1735689900, 0.48, 1),
            ('tok_no', 1735689900, 0.52, 1)
        """
    )
    con.close()

    out = run_grid(
        tmp_path / "bt.db",
        dip_pcts=[0.2, 0.3],
        min_favorites=[0.55],
        cycle_seconds=[300],
        max_position_minutes=[240],
        fill_model="price_cross",
        objective="total_pnl_usdc",
        min_fills=1,
    )
    assert out["runs_evaluated"] == 2
    assert out["best"]["fills"] == 1
    assert out["best"]["total_pnl_usdc"] > 0


def test_optimizer_passes_filter_grid_to_runner_parity(tmp_path, monkeypatch):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev', 'Cup: Alice Smith vs Bob Jones', 'atp-smith-jones', 'atp',
                'tok_yes', 'tok_no', '2025-01-01T00:00:00Z', '2025-01-01T03:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid', 'atp_main', '20250101', 'Alice Smith', 'Bob Jones', 0, 1.0)
        """
    )
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735687800, 0.53, 1),
            ('tok_yes', 1735688400, 0.56, 1),
            ('tok_yes', 1735689600, 0.55, 1),
            ('tok_no', 1735689600, 0.45, 1),
            ('tok_yes', 1735689900, 0.54, 1),
            ('tok_no', 1735689900, 0.46, 1)
        """
    )
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES
            ('pre', 'cid', 'tx-pre', 'tok_yes', 1735689540, 'YES', 0.54, 75, 'BUY'),
            ('fill', 'cid', 'tx-fill', 'tok_yes', 1735689720, 'YES', 0.38, 75, 'BUY')
        """
    )
    con.close()

    def fail_if_per_config_simulator_is_used(*args, **kwargs):
        raise AssertionError("runner parity grids should use cached optimizer path")

    monkeypatch.setattr("scripts.optimize_stink_bid_params.run_sim", fail_if_per_config_simulator_is_used)
    out = run_grid(
        tmp_path / "bt.db",
        dip_pcts=[0.30],
        min_favorites=[0.55],
        cycle_seconds=[300],
        max_position_minutes=[240],
        fill_model="runner_parity_trade",
        objective="total_pnl_usdc",
        min_fills=1,
        min_prior_trades=[0, 2],
        min_prior_price_ranges=[None, 0.05],
        prior_window_minutes=[60],
    )

    assert out["runs_evaluated"] == 4
    assert out["runs_after_min_fills"] == 1
    assert out["best"]["config"]["min_prior_trades"] == 0
    assert out["best"]["config"]["min_prior_price_range"] is None
