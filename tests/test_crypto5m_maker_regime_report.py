import json

import pytest

from scripts import run_polymarket_crypto5m_maker as MK
from sniper.algos.polymarket_crypto5m import maker_regime_report as RPT


def _insert_fill(conn, *, cid="0xw", ws=1000, fill_id=None, token="up", side="bid",
                 price=0.40, size=10.0, fair=0.45, ts=1010, winner=None,
                 regime_id="mid_chop", profile_id="quote_wide",
                 moment_key=None):
    moment_key = moment_key or {
        "window_phase": "mid_window",
        "odds_bucket": "mid_chop",
        "volatility_state": "calm",
        "inventory_state": "flat",
        "spread_state": "healthy",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
    }
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (cid, ws, size if token == "up" and side == "bid" else 0.0,
                  size if token == "down" and side == "bid" else 0.0,
                  -price * size if side == "bid" else price * size,
                  ((1.0 if winner == "Up" and token == "up" else 0.0) - price) * size
                  if side == "bid" and winner else None,
                  1 if winner else 0, winner, ts, "maker-regime-v1"))
    conn.execute("""INSERT INTO maker_fills(id,campaign_id,condition_id,window_start_ts,token,side,
                  price,size,fair,ts,reason,fill_model,regime_id,profile_id,episode_id,moment_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (fill_id, "maker-regime-v1", cid, ws, token, side, price, size, fair, ts,
                  "entry", "print", regime_id, profile_id, "ep-1", json.dumps(moment_key, sort_keys=True)))


def _insert_future_book(conn, *, cid="0xw", ws=1000, token="up", ts=1013,
                        bid=0.42, ask=0.44):
    conn.execute("""INSERT INTO maker_quotes(campaign_id,condition_id,window_start_ts,token,side,
                  status,price,size,fair,best_bid,best_ask,seconds_left,ts,reason,regime_id,profile_id,
                  episode_id,moment_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", cid, ws, token, "bid", "posted", bid, 1.0, None,
                  bid, ask, 100, ts, "book", "mid_chop", "quote_wide", "ep-1", "{}"))


def test_report_labels_fill_markouts_from_future_books():
    conn = MK._db(":memory:")
    _insert_fill(conn)
    _insert_future_book(conn, ts=1013, bid=0.42, ask=0.44)
    _insert_future_book(conn, ts=1020, bid=0.38, ask=0.40)
    _insert_future_book(conn, ts=1040, bid=0.50, ask=0.52)
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(3, 10, 30),
        min_windows=1, min_episodes=1, min_fills=1,
    )

    assert report["samples"]["n_fills"] == 1
    fill = report["fill_evaluations"][0]
    assert fill["adverse_selection_3s_bps"] == pytest.approx(200.0)
    assert fill["adverse_selection_10s_bps"] == pytest.approx(-200.0)
    assert fill["adverse_selection_30s_bps"] == pytest.approx(1000.0)
    assert fill["mfe_bps"] == pytest.approx(1000.0)
    assert fill["mae_bps"] == pytest.approx(-200.0)
    group = report["by_context_profile"][0]
    assert group["mean_adverse_selection_10s_bps"] == pytest.approx(-200.0)
    assert group["mean_edge_bps"] == pytest.approx(500.0)


def test_report_recommends_disable_for_sampled_negative_profile_context():
    conn = MK._db(":memory:")
    _insert_fill(conn, price=0.60, fair=0.62, winner="Down")
    _insert_future_book(conn, ts=1020, bid=0.50, ask=0.52)
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=1, min_episodes=1, min_fills=1,
    )

    group = report["by_context_profile"][0]
    assert group["sample_ok"] is True
    assert group["mean_adverse_selection_10s_bps"] == pytest.approx(-1000.0)
    assert group["mean_settled_fill_pnl"] == pytest.approx(-6.0)
    assert group["status"] == "disabled_recommended"
    assert group["recommendation"] == "disable_profile_in_context"


def test_report_recommends_promote_for_sampled_positive_profile_context():
    conn = MK._db(":memory:")
    _insert_fill(conn, price=0.40, fair=0.45, winner="Up")
    _insert_future_book(conn, ts=1020, bid=0.47, ask=0.49)
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=1, min_episodes=1, min_fills=1,
    )

    group = report["by_context_profile"][0]
    assert group["sample_ok"] is True
    assert group["mean_adverse_selection_10s_bps"] == pytest.approx(700.0)
    assert group["mean_settled_fill_pnl"] == pytest.approx(6.0)
    assert group["status"] == "promote_recommended"
    assert group["recommendation"] == "promote_profile_in_context"


def test_report_sorts_known_contexts_before_legacy_unknown_rows():
    conn = MK._db(":memory:")
    _insert_fill(conn, cid="0xknown", ws=1000, fill_id=1, price=0.40, fair=0.45)
    _insert_future_book(conn, cid="0xknown", ws=1000, ts=1020, bid=0.47, ask=0.49)
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id)
                  VALUES(?,?,?,?,?,?,0,?,?,?)""",
                 ("0xlegacy", 1300, 0.0, 0.0, 0.0, None, None, 1030, "maker-regime-v1"))
    for fill_id in range(2, 70):
        conn.execute("""INSERT INTO maker_fills(id,campaign_id,condition_id,window_start_ts,token,side,
                      price,size,fair,ts,reason,fill_model)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (fill_id, "maker-regime-v1", "0xlegacy", 1300, "up", "bid", 0.40, 1.0, 0.45,
                      1030 + fill_id, "legacy", "print"))
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=20, min_episodes=30, min_fills=50,
    )

    assert report["by_context_profile"][0]["profile_id"] == "quote_wide"


def test_report_campaign_eval_recommends_kill_when_ci_upper_is_below_zero():
    conn = MK._db(":memory:")
    for i in range(6):
        _insert_fill(conn, cid=f"0xloss{i}", ws=1000 + i * 300, fill_id=i + 1,
                     price=0.50, fair=0.55, ts=1010 + i, winner="Down")
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=1, min_episodes=1, min_fills=1,
        campaign_min_windows=6,
        bootstrap_samples=200,
    )

    campaign_eval = report["campaign_eval"]
    assert campaign_eval["n_settled"] == 6
    assert campaign_eval["eval_ready"] is True
    assert campaign_eval["ci_high"] < 0
    assert campaign_eval["recommendation"] == "kill_campaign"


def test_report_campaign_eval_defaults_to_pre_registered_200_window_gate():
    conn = MK._db(":memory:")
    for i in range(60):
        _insert_fill(conn, cid=f"0xloss{i}", ws=1000 + i * 300, fill_id=i + 1,
                     price=0.50, fair=0.55, ts=1010 + i, winner="Down")
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=1, min_episodes=1, min_fills=1,
        bootstrap_samples=200,
    )

    campaign_eval = report["campaign_eval"]
    assert campaign_eval["n_settled"] == 60
    assert campaign_eval["sample_gate"]["min_windows"] == 200
    assert campaign_eval["eval_ready"] is False
    assert campaign_eval["recommendation"] == "keep_collecting"


def test_report_campaign_eval_uses_fill_ledger_when_window_realized_is_poisoned():
    conn = MK._db(":memory:")
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 ("0xpoison", 1000, 0.0, 0.0, 0.0, 99.0, 1, "Up", 2000, "maker-regime-v1"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,
                  price,size,fair,ts,reason,fill_model,regime_id,profile_id,episode_id,moment_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", "0xpoison", 1000, "up", "bid", 0.40, 10.0, 0.45,
                  1010, "entry", "print", "mid_chop", "quote_wide", "ep-1", "{}"))
    conn.commit()

    report = RPT.build_maker_regime_report(
        conn, "maker-regime-v1", horizons_s=(10,),
        min_windows=1, min_episodes=1, min_fills=1,
        campaign_min_windows=1,
        bootstrap_samples=20,
    )

    campaign_eval = report["campaign_eval"]
    assert campaign_eval["n_settled"] == 1
    assert campaign_eval["total_pnl"] == 6.0
    assert campaign_eval["avg_window_pnl"] == 6.0
