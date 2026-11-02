import pytest

from data.backtest.schema import open_backtest_db
from scripts.simulate_two_sided_stink_bid import TwoSidedConfig, run_sim


def _seed_market(tmp_path, *, winner="Alice Smith", ref_yes=0.60, ref_no=0.40):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev', 'Cup: Alice Smith vs Bob Jones', 'atp-smith-jones', 'atp',
                'tok_yes', 'tok_no', '2025-01-01T00:00:00Z', '2025-01-01T03:00:00Z')
        """
    )
    loser = "Bob Jones" if winner == "Alice Smith" else "Alice Smith"
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid', 'atp_main', '20250101', ?, ?, 0, 1.0)
        """,
        [winner, loser],
    )
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, ?, 1),
            ('tok_no', 1735689600, ?, 1)
        """,
        [ref_yes, ref_no],
    )
    return con


def test_two_sided_static_tracks_paired_fill_and_actual_prices(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES
            ('yes', 'cid', 'tx-yes', 'tok_yes', 1735689660, 'YES', 0.50, 100, 'BUY'),
            ('no', 'cid', 'tx-no', 'tok_no', 1735689720, 'NO', 0.30, 100, 'BUY')
        """
    )
    con.close()

    out = run_sim(tmp_path / "bt.db", TwoSidedConfig(dip_pct=0.15), include_audit=True)

    assert out["eligible_priced_events"] == 1
    assert out["paired_fills"] == 1
    assert out["unpaired_fills"] == 0
    assert out["total_pnl_usdc"] == pytest.approx(0.0)
    row = out["audit_events"][0]
    assert row["status"] == "paired"
    assert row["combined_fill_price"] == pytest.approx(0.80)
    assert row["yes_fill"]["fill_price"] == pytest.approx(0.50)
    assert row["no_fill"]["fill_price"] == pytest.approx(0.30)


def test_two_sided_static_reports_unpaired_directional_risk(tmp_path):
    con = _seed_market(tmp_path, winner="Bob Jones")
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('yes', 'cid', 'tx-yes', 'tok_yes', 1735689660, 'YES', 0.50, 100, 'BUY')
        """
    )
    con.close()

    out = run_sim(tmp_path / "bt.db", TwoSidedConfig(dip_pct=0.15), include_audit=True)

    assert out["paired_fills"] == 0
    assert out["unpaired_fills"] == 1
    assert out["total_pnl_usdc"] == pytest.approx(-25.0)
    row = out["audit_events"][0]
    assert row["status"] == "unpaired"
    assert row["filled_legs"] == 1
    assert row["payout_usdc"] == pytest.approx(0.0)


def test_two_sided_static_can_skip_expensive_pair_bid(tmp_path):
    con = _seed_market(tmp_path, ref_yes=0.60, ref_no=0.40)
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        TwoSidedConfig(dip_pct=0.05, max_pair_bid_cost=0.90),
        include_audit=True,
    )

    assert out["active_markets"] == 0
    row = out["audit_events"][0]
    assert row["status"] == "skipped"
    assert row["reason"] == "pair_bid_cost_above_max"
    assert row["bid_yes"] + row["bid_no"] > 0.90


def test_after_first_fill_hedges_with_matched_share_size(tmp_path):
    con = _seed_market(tmp_path, winner="Alice Smith", ref_yes=0.60, ref_no=0.40)
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES
            ('yes', 'cid', 'tx-yes', 'tok_yes', 1735689660, 'YES', 0.50, 100, 'BUY'),
            ('no', 'cid', 'tx-no', 'tok_no', 1735689720, 'NO', 0.34, 60, 'BUY')
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        TwoSidedConfig(mode="after_first_fill", dip_pct=0.15, max_pair_bid_cost=0.90),
        include_audit=True,
    )

    assert out["paired_fills"] == 1
    row = out["audit_events"][0]
    assert row["status"] == "paired"
    assert row["reason"] == "hedged_pair_filled"
    assert row["combined_fill_price"] == pytest.approx(0.84)
    assert row["total_cost_usdc"] == pytest.approx(42.0)
    assert row["payout_usdc"] == pytest.approx(50.0)
    assert row["pnl_usdc"] == pytest.approx(8.0)


def test_after_first_fill_reports_unpaired_when_hedge_does_not_fill(tmp_path):
    con = _seed_market(tmp_path, winner="Bob Jones", ref_yes=0.60, ref_no=0.40)
    con.execute(
        """
        INSERT INTO trades
        (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES
            ('yes', 'cid', 'tx-yes', 'tok_yes', 1735689660, 'YES', 0.50, 100, 'BUY'),
            ('no-too-small', 'cid', 'tx-no', 'tok_no', 1735689720, 'NO', 0.34, 10, 'BUY')
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        TwoSidedConfig(mode="after_first_fill", dip_pct=0.15, max_pair_bid_cost=0.90),
        include_audit=True,
    )

    assert out["paired_fills"] == 0
    assert out["unpaired_fills"] == 1
    row = out["audit_events"][0]
    assert row["status"] == "unpaired"
    assert row["reason"] == "hedge_not_filled"
    assert row["loose_fills_seen"] == 1
    assert row["pnl_usdc"] == pytest.approx(-25.0)
