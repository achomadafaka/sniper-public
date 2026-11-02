from __future__ import annotations

import random
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import maker_adaptive as AD
from sniper.algos.polymarket_crypto5m import maker_regime as RG


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    AD.ensure_adaptive_tables(c)
    yield c
    c.close()


def test_context_key_label_is_stable_and_coarse():
    key = {
        "window_phase": "mid_window",
        "odds_bucket": "mid_chop",
        "volatility_state": "calm",
        "inventory_state": "flat",
        "spread_state": "healthy",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
        "ignored_raw_value": "0.512341",
    }

    assert AD.context_key_label(key) == (
        "mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy"
    )


def test_safety_override_pauses_without_exploration(conn):
    decision = AD.select_profile(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        regime_id="hard_invalid",
        moment_key={"book_health": "healthy"},
        token_inventory=0.0,
        now_ts=1000,
        rng=random.Random(7),
    )

    assert decision.profile_id == "pause"
    assert decision.selection_source == "safety_override"
    assert decision.exploration_flag is False


def test_safety_late_window_reduces_inventory_or_pauses(conn):
    flat = AD.select_profile(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        regime_id="late_window",
        moment_key={"book_health": "healthy"},
        token_inventory=0.0,
        now_ts=1000,
        rng=random.Random(7),
    )
    long = AD.select_profile(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        regime_id="late_window",
        moment_key={"book_health": "healthy"},
        token_inventory=12.0,
        now_ts=1000,
        rng=random.Random(7),
    )

    assert flat.profile_id == "pause"
    assert long.profile_id == "reduce_only"


def test_ucb_explores_under_sampled_profiles(conn):
    AD.register_campaign(conn, "maker-adaptive-paper-v1", "maker-regime-v1", 1000, {})
    key = {
        "window_phase": "mid_window",
        "odds_bucket": "high",
        "volatility_state": "calm",
        "inventory_state": "flat",
        "spread_state": "healthy",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
    }
    ctx = AD.context_key_label(key)
    AD.upsert_profile_stats(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        context_key=ctx,
        profile_id="quote_base",
        n_decisions=20,
        n_quotes=20,
        n_fills=20,
        reward_sum=5.0,
        reward_sq_sum=1.25,
        pnl_sum=5.0,
        adverse_sum=0.0,
        hard_cuts=0,
        now_ts=1000,
    )

    decision = AD.select_profile(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        regime_id="normal",
        moment_key=key,
        token_inventory=0.0,
        now_ts=1001,
        rng=random.Random(3),
        epsilon=0.0,
    )

    assert decision.profile_id != "quote_base"
    assert decision.selection_source in {"fallback", "exploration"}
    assert decision.exploration_flag is True


def test_disabled_profile_not_selected(conn):
    key = {
        "window_phase": "mid_window",
        "odds_bucket": "high",
        "volatility_state": "calm",
        "inventory_state": "flat",
        "spread_state": "healthy",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
    }
    ctx = AD.context_key_label(key)
    AD.disable_profile(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        context_key=ctx,
        profile_id="quote_tight",
        now_ts=1000,
        reason="test",
    )

    for seed in range(20):
        decision = AD.select_profile(
            conn,
            campaign_id="maker-adaptive-paper-v1",
            regime_id="normal",
            moment_key=key,
            token_inventory=0.0,
            now_ts=1001,
            rng=random.Random(seed),
            epsilon=1.0,
        )
        assert decision.profile_id != "quote_tight"


def test_apply_profile_reopens_non_safety_mid_chop_for_paper_learning():
    base = RG.MakerRegimePolicy(
        regime_id="mid_chop",
        bid_enabled=False,
        ask_enabled=False,
        risk_action="pull_all",
        reason="static mid-chop control paused",
    )
    policy, knobs = AD.apply_profile(
        base,
        {"margin": 0.02, "clip": 5.0, "max_shares": 10.0, "min_edge": 0.04, "min_seconds_quote": 30},
        "quote_tight",
        token_inventory=0.0,
    )

    assert policy.bid_enabled is True
    assert policy.ask_enabled is True
    assert policy.risk_action == "quote_tight"
    assert knobs["margin"] < 0.02


def test_apply_profile_keeps_safety_regime_closed():
    base = RG.MakerRegimePolicy(
        regime_id="hard_invalid",
        bid_enabled=False,
        ask_enabled=False,
        risk_action="pull_all",
        reason="bad book",
    )
    policy, _ = AD.apply_profile(
        base,
        {"margin": 0.02, "clip": 5.0, "max_shares": 10.0, "min_edge": 0.04, "min_seconds_quote": 30},
        "quote_tight",
        token_inventory=0.0,
    )

    assert policy.bid_enabled is False
    assert policy.ask_enabled is False


def test_reward_update_from_settled_fills_updates_stats_and_disables_bad_profile(conn):
    AD.ensure_adaptive_tables(conn)
    ctx = "mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy"
    for i in range(30):
        conn.execute(
            """INSERT INTO maker_fills(
                 id,campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,fill_model,
                 moment_key,profile_id,fill_profile_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                i + 1,
                "maker-adaptive-paper-v1",
                f"0xc{i}",
                1_000_000 + i * 300,
                "up",
                "bid",
                0.60,
                1.0,
                0.50,
                1_000_100 + i * 300,
                "print",
                '{"window_phase":"mid_window","odds_bucket":"mid_chop","volatility_state":"calm",'
                '"inventory_state":"flat","spread_state":"healthy","direction_state":"unconfirmed",'
                '"book_health":"healthy"}',
                "quote_tight",
                "quote_tight",
            ),
        )

    inserted = AD.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v1",
        condition_id=None,
        window_start_ts=None,
        winning_outcome="Down",
        now_ts=2_000_000,
    )

    assert inserted == 30
    row = conn.execute(
        """SELECT n_fills,reward_sum,pnl_sum,disabled
           FROM crypto5m_maker_profile_stats
           WHERE campaign_id=? AND context_key=? AND profile_id=?""",
        ("maker-adaptive-paper-v1", ctx, "quote_tight"),
    ).fetchone()
    assert row["n_fills"] == 30
    assert row["reward_sum"] == pytest.approx(-18.0)
    assert row["pnl_sum"] == pytest.approx(-18.0)
    assert row["disabled"] == 1


def test_v2_selector_versions_and_fallback_metadata(conn):
    cfg = AD.learner_config("v2")
    assert cfg.selector_version == "adaptive-ucb-v2"
    assert cfg.reward_version == "maker-reward-v2"
    assert cfg.min_profile_fills_per_context == 10

    AD.register_campaign(
        conn,
        "maker-adaptive-paper-v2",
        "maker-regime-v1",
        1000,
        {"learner_version": "v2"},
        learner_version="v2",
    )
    key = {
        "window_phase": "mid_window",
        "odds_bucket": "mid_chop",
        "volatility_state": "calm",
        "inventory_state": "flat",
        "spread_state": "healthy",
        "direction_state": "confirmed_up",
        "book_health": "healthy",
    }
    parent = "mid_window|mid_chop|calm|flat|healthy|*|healthy"
    AD.upsert_profile_stats(
        conn,
        campaign_id="maker-adaptive-paper-v2",
        context_key=parent,
        profile_id="quote_base",
        n_decisions=20,
        n_quotes=20,
        n_fills=12,
        reward_sum=3.0,
        reward_sq_sum=1.0,
        pnl_sum=3.0,
        adverse_sum=0.0,
        now_ts=1000,
    )

    decision = AD.select_profile(
        conn,
        campaign_id="maker-adaptive-paper-v2",
        regime_id="normal",
        moment_key=key,
        token_inventory=0.0,
        now_ts=1001,
        rng=random.Random(4),
        epsilon=0.0,
        learner_version="v2",
    )

    assert decision.selector_version == "adaptive-ucb-v2"
    assert decision.reward_version == "maker-reward-v2"
    assert decision.parent_context_used == parent
    assert decision.fallback_depth == 1
    assert decision.fallback_reason == "drop direction_state"
    assert decision.exploration_budget_remaining >= 0


def test_v2_reward_uses_future_quote_markout_and_reward_components(conn):
    AD.register_campaign(
        conn,
        "maker-adaptive-paper-v2",
        "maker-regime-v1",
        1000,
        {"learner_version": "v2"},
        learner_version="v2",
    )
    moment = (
        '{"window_phase":"mid_window","odds_bucket":"mid_chop","volatility_state":"calm",'
        '"inventory_state":"flat","spread_state":"healthy","direction_state":"unconfirmed",'
        '"book_health":"healthy"}'
    )
    conn.execute(
        """INSERT INTO maker_fills(
             id,campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,fill_model,
             moment_key,profile_id,fill_profile_id)
           VALUES(1,'maker-adaptive-paper-v2','0xc',1000,'up','bid',0.50,10.0,0.52,1010,
                  'print',?,'quote_tight','quote_tight')""",
        (moment,),
    )
    for ts, bid, ask in ((1013, 0.49, 0.51), (1020, 0.47, 0.49), (1040, 0.46, 0.48)):
        conn.execute(
            """INSERT INTO maker_quotes(
                 campaign_id,condition_id,window_start_ts,token,side,status,price,size,fair,
                 best_bid,best_ask,seconds_left,ts,reason)
               VALUES('maker-adaptive-paper-v2','0xc',1000,'up','bid','posted',?,?,?,?,?,?,?,'snap')""",
            (bid, 10.0, 0.52, bid, ask, 100, ts),
        )

    inserted = AD.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v2",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=2000,
    )

    assert inserted == 1
    row = conn.execute(
        """SELECT reward_version,settled_fill_pnl,adverse_selection_3s_bps,
                  adverse_selection_10s_bps,adverse_selection_30s_bps,
                  future_executable_unwind_10s,shadow_quality,reward
           FROM crypto5m_maker_adaptive_rewards
           WHERE campaign_id='maker-adaptive-paper-v2'"""
    ).fetchone()
    assert row["reward_version"] == "maker-reward-v2"
    assert row["settled_fill_pnl"] == pytest.approx(5.0)
    assert row["adverse_selection_3s_bps"] == pytest.approx(-100.0)
    assert row["adverse_selection_10s_bps"] == pytest.approx(-300.0)
    assert row["adverse_selection_30s_bps"] == pytest.approx(-400.0)
    assert row["future_executable_unwind_10s"] == pytest.approx(-0.3)
    assert row["shadow_quality"] == "exact"
    assert row["reward"] == pytest.approx(4.7)


def test_v2_reward_marks_missing_markout_not_evaluable(conn):
    AD.register_campaign(
        conn,
        "maker-adaptive-paper-v2",
        "maker-regime-v1",
        1000,
        {"learner_version": "v2"},
        learner_version="v2",
    )
    conn.execute(
        """INSERT INTO maker_fills(
             id,campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,fill_model,
             moment_key,profile_id,fill_profile_id)
           VALUES(1,'maker-adaptive-paper-v2','0xc',1000,'up','bid',0.50,10.0,0.52,1010,
                  'print','{}','quote_tight','quote_tight')"""
    )

    AD.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v2",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=2000,
    )

    row = conn.execute(
        """SELECT shadow_quality,adverse_selection_10s_bps,reward
           FROM crypto5m_maker_adaptive_rewards
           WHERE campaign_id='maker-adaptive-paper-v2'"""
    ).fetchone()
    assert row["shadow_quality"] == "not_evaluable"
    assert row["adverse_selection_10s_bps"] is None
    assert row["reward"] == pytest.approx(5.0)
