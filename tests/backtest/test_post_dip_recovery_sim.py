import pytest

from data.backtest.schema import open_backtest_db
from scripts.simulate_post_dip_recovery import PostDipConfig, run_grid, run_sim


def _seed_market(tmp_path, *, winner="Alice Smith", tour="atp_main"):
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
        VALUES ('cid', ?, '20250101', ?, ?, 0, 1.0)
        """,
        [tour, winner, loser],
    )
    return con


def test_post_dip_skips_non_moneyline_props(tmp_path):
    con = open_backtest_db(str(tmp_path / "bt.db"))
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev', 'Djokovic vs. Cobolli: Will Djokovic win by 8+ games?', 'prop', 'atp',
                'tok_yes', 'tok_no', '2025-01-01T00:00:00Z', '2025-01-01T03:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid', 'atp_main', '20250101', 'Novak Djokovic', 'Flavio Cobolli', 0, 1.0)
        """
    )
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.49, 1),
            ('tok_yes', 1735689720, 0.52, 1),
            ('tok_yes', 1735689780, 0.55, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(mode="favorite_flip_reclaim", reclaim_price=0.52),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "non_moneyline_title"


def test_post_dip_enters_next_bar_after_observable_drop(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689660, 0.70, 1),
            ('tok_yes', 1735689720, 0.63, 1),
            ('tok_yes', 1735689780, 0.66, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(drop_pct=0.20, max_trigger_minutes=60, slippage=0.02),
        include_audit=True,
    )

    assert out["entries"] == 1
    trade = out["sample_trades"][0]
    assert trade["trigger_ts"] == 1735689720
    assert trade["entry_ts"] == 1735689780
    assert trade["entry_price"] == pytest.approx(0.68)
    assert trade["pnl_usdc"] > 0
    assert out["audit_events"][0]["reason"] == "post_dip_entry"


def test_post_dip_requires_next_bar_and_skips_if_drop_is_last_point(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689720, 0.63, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(drop_pct=0.20, max_trigger_minutes=60, slippage=0.02),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "no_next_price_after_trigger"


def test_post_dip_losing_favorite_loses_notional_plus_fee(tmp_path):
    con = _seed_market(tmp_path, winner="Bob Jones")
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689720, 0.63, 1),
            ('tok_yes', 1735689780, 0.66, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(drop_pct=0.20, max_trigger_minutes=60, slippage=0.02),
    )

    assert out["entries"] == 1
    assert out["losses"] == 1
    assert out["total_pnl_usdc"] < -25.0


def test_post_dip_filters_reference_band(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.95, 1),
            ('tok_no', 1735689600, 0.05, 1),
            ('tok_yes', 1735689720, 0.70, 1),
            ('tok_yes', 1735689780, 0.72, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(drop_pct=0.20, min_ref=0.60, max_ref=0.90),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "favorite_ref_outside_band"


def test_post_dip_reports_year_and_tour_splits(tmp_path):
    con = _seed_market(tmp_path, tour="wta_main")
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689720, 0.63, 1),
            ('tok_yes', 1735689780, 0.66, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(drop_pct=0.20, max_trigger_minutes=60, slippage=0.02),
    )

    assert out["by_year"]["2025"]["entries"] == 1
    assert out["by_tour"]["wta"]["entries"] == 1


def test_post_dip_grid_uses_cached_path(tmp_path, monkeypatch):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689720, 0.63, 1),
            ('tok_yes', 1735689780, 0.66, 1)
        """
    )
    con.close()

    def fail_if_run_sim_is_used(*args, **kwargs):
        raise AssertionError("grid should use cached dataset path")

    monkeypatch.setattr("scripts.simulate_post_dip_recovery.run_sim", fail_if_run_sim_is_used)
    out = run_grid(
        tmp_path / "bt.db",
        drop_pcts=[0.20, 0.25],
        max_trigger_minutes=[60],
        slippages=[0.02],
        min_ref=0.60,
        max_ref=0.90,
        min_entries=1,
    )

    assert out["runs_evaluated"] == 2
    assert out["runs_after_min_entries"] == 1
    assert out["best"]["entries"] == 1


def test_dip_reclaim_enters_after_reclaim_from_local_low(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689660, 0.63, 1),
            ('tok_yes', 1735689720, 0.60, 1),
            ('tok_yes', 1735689780, 0.62, 1),
            ('tok_yes', 1735689840, 0.66, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="dip_reclaim",
            drop_pct=0.20,
            reclaim_pct=0.05,
            reclaim_window_minutes=30,
            max_trigger_minutes=60,
            slippage=0.01,
        ),
        include_audit=True,
    )

    trade = out["sample_trades"][0]
    assert out["entries"] == 1
    assert trade["trigger_ts"] == 1735689660
    assert trade["local_low_price"] == pytest.approx(0.60)
    assert trade["entry_ts"] == 1735689840
    assert trade["entry_price"] == pytest.approx(0.67)
    assert out["audit_events"][0]["reason"] == "dip_reclaim_entry"


def test_favorite_drop_fade_buys_original_underdog_after_favorite_drop(tmp_path):
    con = _seed_market(tmp_path, winner="Bob Jones")
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689720, 0.63, 1),
            ('tok_no', 1735689780, 0.38, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(mode="favorite_drop_fade", drop_pct=0.20, max_trigger_minutes=60, slippage=0.02),
        include_audit=True,
    )

    assert out["entries"] == 1
    trade = out["sample_trades"][0]
    assert trade["favorite_side"] == "yes"
    assert trade["entry_side"] == "no"
    assert trade["trigger_ts"] == 1735689720
    assert trade["entry_ts"] == 1735689780
    assert trade["entry_price"] == pytest.approx(0.40)
    assert trade["pnl_usdc"] > 0
    assert out["audit_events"][0]["reason"] == "favorite_drop_fade_entry"


def test_favorite_flip_fade_buys_original_underdog_after_favorite_flips(tmp_path):
    con = _seed_market(tmp_path, winner="Bob Jones")
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.49, 1),
            ('tok_no', 1735689720, 0.53, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(mode="favorite_flip_fade", max_trigger_minutes=60, slippage=0.01),
        include_audit=True,
    )

    assert out["entries"] == 1
    trade = out["sample_trades"][0]
    assert trade["entry_side"] == "no"
    assert trade["trigger_ts"] == 1735689660
    assert trade["entry_ts"] == 1735689720
    assert trade["entry_price"] == pytest.approx(0.54)
    assert out["audit_events"][0]["reason"] == "favorite_flip_fade_entry"


def test_dip_reclaim_skips_when_reclaim_window_expires(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.80, 1),
            ('tok_no', 1735689600, 0.20, 1),
            ('tok_yes', 1735689660, 0.63, 1),
            ('tok_yes', 1735689720, 0.60, 1),
            ('tok_yes', 1735695000, 0.66, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="dip_reclaim",
            drop_pct=0.20,
            reclaim_pct=0.05,
            reclaim_window_minutes=30,
            max_trigger_minutes=60,
            slippage=0.01,
        ),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "no_reclaim_after_drop"


def test_favorite_flip_reclaim_enters_next_bar_after_reclaim(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.49, 1),
            ('tok_yes', 1735689720, 0.52, 1),
            ('tok_yes', 1735689780, 0.55, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="favorite_flip_reclaim",
            min_ref=0.65,
            reclaim_price=0.52,
            reclaim_window_minutes=30,
            slippage=0.01,
        ),
        include_audit=True,
    )

    trade = out["sample_trades"][0]
    assert out["entries"] == 1
    assert trade["trigger_ts"] == 1735689660
    assert trade["entry_ts"] == 1735689780
    assert trade["entry_price"] == pytest.approx(0.56)
    assert out["audit_events"][0]["reason"] == "favorite_flip_reclaim_entry"


def test_favorite_flip_reclaim_skips_without_flip(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.51, 1),
            ('tok_yes', 1735689720, 0.54, 1),
            ('tok_yes', 1735689780, 0.56, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="favorite_flip_reclaim",
            min_ref=0.65,
            reclaim_price=0.52,
            reclaim_window_minutes=30,
            slippage=0.01,
        ),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "no_favorite_flip"


def test_favorite_flip_reclaim_skips_without_reclaim(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.49, 1),
            ('tok_yes', 1735689720, 0.50, 1),
            ('tok_yes', 1735689780, 0.51, 1)
        """
    )
    con.close()

    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="favorite_flip_reclaim",
            min_ref=0.65,
            reclaim_price=0.52,
            reclaim_window_minutes=30,
            slippage=0.01,
        ),
        include_audit=True,
    )

    assert out["entries"] == 0
    assert out["audit_events"][0]["reason"] == "no_reclaim_after_flip"


def test_run_sim_can_write_trade_export(tmp_path):
    con = _seed_market(tmp_path)
    con.execute(
        """
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES
            ('tok_yes', 1735689600, 0.72, 1),
            ('tok_no', 1735689600, 0.28, 1),
            ('tok_yes', 1735689660, 0.49, 1),
            ('tok_yes', 1735689720, 0.52, 1),
            ('tok_yes', 1735689780, 0.55, 1)
        """
    )
    con.close()

    export_path = tmp_path / "trades.jsonl"
    out = run_sim(
        tmp_path / "bt.db",
        PostDipConfig(
            mode="favorite_flip_reclaim",
            min_ref=0.65,
            reclaim_price=0.52,
            reclaim_window_minutes=30,
            slippage=0.01,
        ),
        trades_output=export_path,
    )

    assert out["entries"] == 1
    assert out["trades_output"] == str(export_path)
    assert '"condition_id": "cid"' in export_path.read_text()
