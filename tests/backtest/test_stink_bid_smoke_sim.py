import pytest

from data.backtest.schema import open_backtest_db
from scripts.simulate_stink_bid_smoke import SimConfig, _summarize_trades, run_sim


def test_smoke_sim_price_cross_fill_winning_favorite(tmp_path):
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

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(dip_pct=0.30, min_favorite=0.55, cycle_seconds=300, position_size_usdc=25),
    )
    assert out["eligible_priced_events"] == 1
    assert out["fills"] == 1
    assert out["wins"] == 1
    assert out["total_pnl_usdc"] > 0
    assert out["by_year"]["2025"]["fills"] == 1
    assert out["sample_trades"][0]["sack_year"] == "2025"


def test_smoke_sim_audit_explains_price_cross_fill(tmp_path):
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

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(dip_pct=0.30, min_favorite=0.55, cycle_seconds=300),
        include_audit=True,
    )

    assert out["fills"] == 1
    assert len(out["audit_events"]) == 1
    row = out["audit_events"][0]
    assert row["status"] == "filled"
    assert row["reason"] == "price_cross_fill"
    assert row["favorite_side"] == "yes"
    assert row["winner_side"] == "yes"
    assert row["initial_bid"] == pytest.approx(0.49)
    assert row["fill_price"] == pytest.approx(0.485)
    assert row["ticks_seen"] == 2


def test_smoke_sim_audit_explains_no_fill(tmp_path):
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
            ('tok_yes', 1735689900, 0.60, 1),
            ('tok_no', 1735689900, 0.40, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(dip_pct=0.30, min_favorite=0.55, cycle_seconds=300),
        include_audit=True,
    )

    assert out["fills"] == 0
    row = out["audit_events"][0]
    assert row["status"] == "no_fill"
    assert row["reason"] == "no_fill_before_cutoff"
    assert row["favorite_ref"] == pytest.approx(0.70)
    assert row["min_observed_price"] == pytest.approx(0.60)


def test_smoke_sim_trade_confirmed_requires_sell_trade(tmp_path):
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
        VALUES ('cid', 'atp_main', '20250101', 'Bob Jones', 'Alice Smith', 0, 1.0)
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

    no_trade = run_sim(tmp_path / "bt.db", SimConfig(fill_model="trade_confirmed"))
    assert no_trade["fills"] == 0

    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('h', 'cid', 'tx', 'tok_yes', 1735689900, 'YES', 0.48, 10, 'SELL')
        """
    )
    con.close()

    with_trade = run_sim(tmp_path / "bt.db", SimConfig(fill_model="trade_confirmed"))
    assert with_trade["fills"] == 1
    assert with_trade["losses"] == 1
    assert with_trade["sample_trades"][0]["fill_price"] == pytest.approx(0.49)


def test_smoke_sim_trade_confirmed_ignores_late_trade(tmp_path):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev', 'Cup: Alice Smith vs Bob Jones', 'atp-smith-jones', 'atp',
                'tok_yes', 'tok_no', '2025-01-01T00:00:00Z', '2025-01-01T08:00:00Z')
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
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('h', 'cid', 'tx', 'tok_yes', 1735707601, 'YES', 0.48, 10, 'SELL')
        """
    )
    con.close()

    out = run_sim(tmp_path / "bt.db", SimConfig(fill_model="trade_confirmed", max_position_minutes=240))
    assert out["fills"] == 0


def _seed_runner_parity_market(tmp_path):
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
            ('tok_yes', 1735689600, 0.55, 1),
            ('tok_no', 1735689600, 0.45, 1),
            ('tok_yes', 1735689900, 0.54, 1),
            ('tok_no', 1735689900, 0.46, 1),
            ('tok_yes', 1735690200, 0.53, 1),
            ('tok_no', 1735690200, 0.47, 1)
        """
    )
    return con


def test_runner_parity_trade_uses_rounded_bid_strict_size_and_trade_price(tmp_path):
    con = _seed_runner_parity_market(tmp_path)
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('h', 'cid', 'tx', 'tok_yes', 1735689720, 'YES', 0.38, 75, 'BUY')
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(fill_model="runner_parity_trade", dip_pct=0.30, min_favorite=0.55),
        include_audit=True,
    )

    assert out["fills"] == 1
    assert out["sample_trades"][0]["fill_price"] == pytest.approx(0.38)
    row = out["audit_events"][0]
    assert row["reason"] == "runner_parity_strict_fill"
    assert row["initial_bid"] == pytest.approx(0.39)
    assert row["fill_trade_size"] == pytest.approx(75)
    assert row["fill_trade_side"] == "BUY"
    assert row["fill_transaction_hash"] == "tx"


def test_runner_parity_trade_rejects_loose_and_wrong_token_trades(tmp_path):
    con = _seed_runner_parity_market(tmp_path)
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES
            ('loose', 'cid', 'tx-loose', 'tok_yes', 1735689720, 'YES', 0.38, 10, 'SELL'),
            ('wrong-token', 'cid', 'tx-wrong', 'tok_no', 1735689780, 'NO', 0.38, 100, 'SELL')
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(fill_model="runner_parity_trade", dip_pct=0.30, min_favorite=0.55),
        include_audit=True,
    )

    assert out["fills"] == 0
    row = out["audit_events"][0]
    assert row["status"] == "no_fill"
    assert row["reason"] == "only_loose_fills"
    assert row["loose_fills_seen"] == 1


def test_runner_parity_trade_cancels_when_favorite_flips(tmp_path):
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
            ('tok_yes', 1735689900, 0.49, 1),
            ('tok_no', 1735689900, 0.51, 1)
        """
    )
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('late', 'cid', 'tx-late', 'tok_yes', 1735690200, 'YES', 0.30, 100, 'SELL')
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        SimConfig(fill_model="runner_parity_trade", dip_pct=0.30, min_favorite=0.55),
        include_audit=True,
    )

    assert out["fills"] == 0
    assert out["audit_events"][0]["reason"] == "favorite_flipped"


def test_runner_parity_trade_can_require_prior_trade_activity(tmp_path):
    con = _seed_runner_parity_market(tmp_path)
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

    rejected = run_sim(
        tmp_path / "bt.db",
        SimConfig(
            fill_model="runner_parity_trade",
            dip_pct=0.30,
            min_favorite=0.55,
            min_prior_trades=2,
        ),
        include_audit=True,
    )
    accepted = run_sim(
        tmp_path / "bt.db",
        SimConfig(
            fill_model="runner_parity_trade",
            dip_pct=0.30,
            min_favorite=0.55,
            min_prior_trades=1,
        ),
        include_audit=True,
    )

    assert rejected["fills"] == 0
    assert rejected["audit_events"][0]["reason"] == "prior_trade_count_below_min"
    assert rejected["audit_events"][0]["prior_trade_count"] == 1
    assert accepted["fills"] == 1


def test_runner_parity_trade_can_require_prior_price_range(tmp_path):
    con = _seed_runner_parity_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735687800, 0.53, 1),
            ('tok_yes', 1735688400, 0.56, 1)
        """
    )
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('fill', 'cid', 'tx-fill', 'tok_yes', 1735689720, 'YES', 0.38, 75, 'BUY')
        """
    )
    con.close()

    rejected = run_sim(
        tmp_path / "bt.db",
        SimConfig(
            fill_model="runner_parity_trade",
            dip_pct=0.30,
            min_favorite=0.55,
            min_prior_price_range=0.05,
            prior_window_minutes=60,
        ),
        include_audit=True,
    )
    accepted = run_sim(
        tmp_path / "bt.db",
        SimConfig(
            fill_model="runner_parity_trade",
            dip_pct=0.30,
            min_favorite=0.55,
            min_prior_price_range=0.02,
            prior_window_minutes=60,
        ),
        include_audit=True,
    )

    assert rejected["fills"] == 0
    assert rejected["audit_events"][0]["reason"] == "prior_price_range_below_min"
    assert rejected["audit_events"][0]["prior_price_range"] == pytest.approx(0.03)
    assert accepted["fills"] == 1


def test_smoke_sim_filters_by_sack_year(tmp_path):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    for idx, year in enumerate((2025, 2026), start=1):
        cid = f"cid{idx}"
        yes = f"tok_yes_{idx}"
        no = f"tok_no_{idx}"
        ts = 1735689600 if year == 2025 else 1767225600
        con.execute(
            """
            INSERT INTO gamma_events
            (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
            VALUES (?, ?, 'Cup: Alice Smith vs Bob Jones', 'atp-smith-jones', 'atp',
                    ?, ?, ?, ?)
            """,
            [cid, f"ev{idx}", yes, no, f"{year}-01-01T00:00:00Z", f"{year}-01-01T03:00:00Z"],
        )
        con.execute(
            """
            INSERT INTO event_alignment
            (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
            VALUES (?, 'atp_main', ?, 'Alice Smith', 'Bob Jones', 0, 1.0)
            """,
            [cid, f"{year}0101"],
        )
        con.execute(
            """
            INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
            VALUES
                (?, ?, 0.70, 1),
                (?, ?, 0.30, 1),
                (?, ?, 0.48, 1),
                (?, ?, 0.52, 1)
            """,
            [yes, ts, no, ts, yes, ts + 300, no, ts + 300],
        )
    con.close()

    out = run_sim(tmp_path / "bt.db", SimConfig(), since_year=2026, until_year=2026)
    assert out["eligible_priced_events"] == 1
    assert out["fills"] == 1
    assert set(out["by_year"]) == {"2026"}
    assert out["sample_trades"][0]["sack_year"] == "2026"


def test_smoke_sim_t_stat_handles_near_identical_returns():
    class Trade:
        pnl_usdc = -25.0
        return_pct = -1.0
        entry_ts = 1
        condition_id = "cid"

    out = _summarize_trades([Trade(), Trade(), Trade(), Trade()])
    assert out["return_t_stat"] is None
