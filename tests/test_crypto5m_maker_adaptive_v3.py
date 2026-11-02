from __future__ import annotations

import random
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import maker_adaptive_v3 as V3


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    V3.ensure_v3_tables(c)
    yield c
    c.close()


def test_logit_blend_uses_probability_geometry():
    blended = V3.logit_blend(0.95, 0.50, weight=0.50)

    assert blended == pytest.approx(0.8134, abs=0.001)
    assert blended != pytest.approx(0.725, abs=0.001)


def test_fair_value_uses_market_fallback_when_structural_missing(conn):
    decision = V3.choose_fair_value(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        q_structural=None,
        q_market_mid=0.61,
        context={
            "seconds_bucket": "mid",
            "odds_bucket": "high",
            "volatility_state": "calm",
            "jump_state": "none",
            "source_health": "structural_missing",
            "spread_state": "healthy",
            "disagreement_bucket": "unknown",
            "book_health": "healthy",
        },
        now_ts=1000,
    )

    assert decision.q_used == pytest.approx(0.61)
    assert decision.fair_source == "fallback_market"
    assert decision.fair_confidence == "low"


def test_fair_value_context_weight_overrides_default(conn):
    context = {
        "seconds_bucket": "mid",
        "odds_bucket": "mid_chop",
        "volatility_state": "calm",
        "jump_state": "none",
        "source_health": "healthy",
        "spread_state": "healthy",
        "disagreement_bucket": "wide",
        "book_health": "healthy",
    }
    key = V3.fair_context_key(context)
    conn.execute(
        """INSERT INTO crypto5m_maker_v3_fair_blend_stats(
             campaign_id,fair_context_key,blend_weight,residual_adjustment,n_observations,reward_sum,last_updated_ts)
           VALUES('maker-adaptive-paper-v3',?,?,?,?,?,?)""",
        (key, 0.20, 0.10, 40, 3.0, 1000),
    )

    decision = V3.choose_fair_value(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        q_structural=0.80,
        q_market_mid=0.50,
        context=context,
        now_ts=1001,
    )

    expected = V3.logit_blend(0.80, 0.50, weight=0.20, residual=0.10)
    assert decision.q_used == pytest.approx(expected)
    assert decision.blend_weight == pytest.approx(0.20)
    assert decision.residual_adjustment == pytest.approx(0.10)
    assert decision.fair_source == "blended"


def test_fair_blend_stats_populated_after_settlement_for_all_candidate_weights(conn):
    conn.executescript("""
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER,
        reason TEXT, fill_model TEXT);
    """)
    campaign = "maker-adaptive-paper-v3.1"
    fair_context = {
        "seconds_bucket": "mid",
        "odds_bucket": "high",
        "volatility_state": "calm",
        "jump_state": "none",
        "source_health": "healthy",
        "spread_state": "healthy",
        "disagreement_bucket": "wide",
        "book_health": "healthy",
    }
    fair_context_key = V3.fair_context_key(fair_context)
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v2", 1000, {})
    V3.record_policy_action(
        conn,
        campaign_id=campaign,
        condition_id="0xfair",
        window_start_ts=1000,
        token="up",
        context_key="mid_window|high|calm|flat|healthy|unconfirmed|healthy",
        fair_context_key=fair_context_key,
        fair_model_id="logit_blend_v1",
        q_structural=0.80,
        q_market_mid=0.55,
        q_used=V3.logit_blend(0.80, 0.55, weight=0.35),
        fair_blend_weight=0.35,
        fair_confidence="medium",
        risk_state="normal",
        tail_risk_score=0.10,
        quote_action_arm="base_small",
        selector_source="fallback",
        exploration_flag=False,
        parent_context_used="global",
        now_ts=1005,
    )
    conn.execute("""INSERT INTO maker_fills(
                    campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 (campaign, "0xfair", 1000, "up", "bid", 0.50, 10.0, 0.62, 1010, "entry", "print"))

    inserted = V3.update_rewards_for_settled_window(
        conn,
        campaign_id=campaign,
        condition_id="0xfair",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=1300,
    )

    row = conn.execute(
        """SELECT blend_weight,n_observations,arm_metrics_json,brier_sum,log_loss_sum
           FROM crypto5m_maker_v3_fair_blend_stats
           WHERE campaign_id=? AND fair_context_key=?""",
        (campaign, fair_context_key),
    ).fetchone()
    metrics = __import__("json").loads(row["arm_metrics_json"])

    assert inserted == 1
    assert row["n_observations"] == 1
    assert {"market_only", "blend_25", "blend_50", "blend_75", "structural_only"} <= set(metrics)
    assert row["blend_weight"] == pytest.approx(1.0)
    assert row["brier_sum"] > 0
    assert row["log_loss_sum"] > 0


def test_tail_risk_hard_states_override_quotes():
    invalid = V3.govern_tail_risk(
        regime_id="hard_invalid",
        moment_key={"book_health": "healthy"},
        token_inventory=0.0,
        seconds_left=120,
        fair_confidence="high",
        transition_state="stable",
    )
    late = V3.govern_tail_risk(
        regime_id="normal",
        moment_key={"book_health": "healthy", "window_phase": "late_window"},
        token_inventory=20.0,
        seconds_left=15,
        fair_confidence="high",
        transition_state="stable",
    )

    assert invalid.risk_state == "pause"
    assert invalid.forced_arm == "pause"
    assert late.risk_state == "reduce_only"
    assert late.forced_arm in {"reduce_only", "exit_fast"}


def test_action_arms_are_complete_and_price_risk_aware():
    arms = V3.ACTION_ARMS

    assert {"pause", "reduce_only", "exit_fast", "tight_small_fast", "base_small",
            "wide_small", "wide_tiny", "normal_small"} <= set(arms)
    assert arms["wide_tiny"].clip_usdc < arms["wide_small"].clip_usdc
    assert arms["tight_small_fast"].quote_ttl_ms < arms["base_small"].quote_ttl_ms
    assert arms["pause"].bid_enabled is False
    assert arms["reduce_only"].exit_only is True


def test_action_selector_uses_parent_context_and_records_fallback(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})
    full = "mid_window|high|calm|long_light|healthy|confirmed_up|healthy"
    parent = "mid_window|high|calm|long_light|healthy|*|healthy"
    V3.upsert_action_arm_stats(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        context_key=parent,
        arm_id="wide_small",
        n_decisions=20,
        n_fills=12,
        reward_sum=6.0,
        reward_sq_sum=4.0,
        adverse_sum=0.0,
        now_ts=1000,
    )

    decision = V3.select_action_arm(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        context_key=full,
        regime_id="normal",
        fair_confidence="medium",
        risk_state="normal",
        token_inventory=0.0,
        rng=random.Random(3),
        epsilon=0.0,
        now_ts=1001,
    )

    assert decision.arm_id == "wide_small"
    assert decision.parent_context_used == parent
    assert decision.fallback_depth == 1
    assert decision.selection_source == "parent_fallback"


def test_safety_override_blocks_exploration(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})

    decision = V3.select_action_arm(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        context_key="late_window|high|calm|long_light|healthy|unconfirmed|healthy",
        regime_id="late_window",
        fair_confidence="low",
        risk_state="reduce_only",
        token_inventory=12.0,
        rng=random.Random(1),
        epsilon=1.0,
        now_ts=1001,
    )

    assert decision.arm_id == "reduce_only"
    assert decision.selection_source == "safety_override"
    assert decision.exploration_flag is False


def test_v31_disables_bad_arm_and_selector_filters_it(conn):
    campaign = "maker-adaptive-paper-v3.1"
    context = "mid_window|low|jump|long_light|healthy|unconfirmed|healthy"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v2", 1000, {})
    V3.upsert_action_arm_stats(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="base_small",
        n_decisions=40,
        n_quotes=40,
        n_fills=12,
        reward_sum=-18.0,
        reward_sq_sum=36.0,
        adverse_sum=-2000.0,
        now_ts=1000,
    )
    V3.upsert_action_arm_stats(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="wide_tiny",
        n_decisions=20,
        n_quotes=20,
        n_fills=8,
        reward_sum=1.6,
        reward_sq_sum=1.0,
        adverse_sum=0.0,
        now_ts=1000,
    )

    status = V3.evaluate_action_arm_status(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="base_small",
        now_ts=1001,
    )
    decision = V3.select_action_arm(
        conn,
        campaign_id=campaign,
        context_key=context,
        regime_id="jump",
        fair_confidence="medium",
        risk_state="caution",
        token_inventory=10.0,
        rng=random.Random(7),
        epsilon=0.0,
        now_ts=1002,
    )
    row = conn.execute(
        """SELECT disabled,status,disable_type
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id=? AND context_key=? AND arm_id='base_small'""",
        (campaign, context),
    ).fetchone()

    assert status == "disabled_active"
    assert row["disabled"] == 1
    assert row["disable_type"] == "catastrophic"
    assert decision.arm_id != "base_small"
    assert decision.profile_disabled_flag is False


def test_v31_promotes_preferred_arm_but_safety_override_still_wins(conn):
    campaign = "maker-adaptive-paper-v3.1"
    context = "mid_window|high|calm|long_light|healthy|unconfirmed|healthy"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v2", 1000, {})
    V3.upsert_action_arm_stats(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="base_small",
        n_decisions=60,
        n_quotes=60,
        n_fills=35,
        reward_sum=1.0,
        reward_sq_sum=10.0,
        adverse_sum=0.0,
        now_ts=1000,
    )
    V3.upsert_action_arm_stats(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="wide_small",
        n_decisions=60,
        n_quotes=60,
        n_fills=35,
        reward_sum=24.5,
        reward_sq_sum=20.0,
        adverse_sum=0.0,
        now_ts=1000,
    )

    status = V3.evaluate_action_arm_status(
        conn,
        campaign_id=campaign,
        context_key=context,
        arm_id="wide_small",
        now_ts=1001,
    )
    decision = V3.select_action_arm(
        conn,
        campaign_id=campaign,
        context_key=context,
        regime_id="normal",
        fair_confidence="high",
        risk_state="normal",
        token_inventory=4.0,
        rng=random.Random(1),
        epsilon=1.0,
        now_ts=1002,
    )
    safety = V3.select_action_arm(
        conn,
        campaign_id=campaign,
        context_key=context,
        regime_id="late_window",
        fair_confidence="high",
        risk_state="reduce_only",
        token_inventory=4.0,
        rng=random.Random(1),
        epsilon=1.0,
        now_ts=1003,
    )

    assert status == "paper_preferred"
    assert decision.arm_id == "wide_small"
    assert decision.selection_source == "paper_preferred"
    assert safety.arm_id == "reduce_only"
    assert safety.selection_source == "safety_override"


def test_cold_start_normal_context_does_not_choose_pause(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})

    decision = V3.select_action_arm(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        context_key="mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy",
        regime_id="normal",
        fair_confidence="medium",
        risk_state="normal",
        token_inventory=0.0,
        rng=random.Random(1),
        epsilon=0.0,
        now_ts=1001,
    )

    assert decision.arm_id == "base_small"
    assert decision.selection_source == "hardcoded_fallback"


def test_v3_schema_and_ablation_rows(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})
    V3.record_policy_action(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        condition_id="0xc",
        window_start_ts=1000,
        token="up",
        context_key="mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy",
        fair_model_id="logit_blend_v1",
        q_structural=0.60,
        q_market_mid=0.52,
        q_used=0.55,
        fair_blend_weight=0.35,
        fair_confidence="medium",
        risk_state="normal",
        tail_risk_score=0.10,
        quote_action_arm="wide_small",
        selector_source="fallback",
        exploration_flag=True,
        parent_context_used="global",
        now_ts=1000,
    )
    V3.record_ablation_rows(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        condition_id="0xc",
        window_start_ts=1000,
        token="up",
        context_key="mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy",
        selected_arm="wide_small",
        q_used=0.55,
        risk_state="normal",
        now_ts=1000,
    )

    action = conn.execute("SELECT * FROM crypto5m_maker_v3_policy_actions").fetchone()
    ablations = conn.execute("SELECT ablation_group FROM crypto5m_maker_v3_ablations ORDER BY ablation_group").fetchall()

    assert action["fair_model_id"] == "logit_blend_v1"
    assert action["quote_action_arm"] == "wide_small"
    assert [r["ablation_group"] for r in ablations] == ["A0", "A1", "A2", "A3", "A4"]


def test_settled_fill_rewards_train_action_arm_stats(conn):
    conn.executescript("""
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER,
        reason TEXT, fill_model TEXT);
    """)
    V3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})
    context_key = "mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy"
    V3.record_policy_action(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        condition_id="0xc",
        window_start_ts=1000,
        token="up",
        context_key=context_key,
        fair_model_id="logit_blend_v1",
        q_structural=0.60,
        q_market_mid=0.52,
        q_used=0.55,
        fair_blend_weight=0.35,
        fair_confidence="medium",
        risk_state="normal",
        tail_risk_score=0.10,
        quote_action_arm="wide_small",
        selector_source="fallback",
        exploration_flag=False,
        parent_context_used="global",
        now_ts=1005,
    )
    conn.execute("""INSERT INTO maker_fills(
                    campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                    VALUES('maker-adaptive-paper-v3','0xc',1000,'up','bid',0.40,10,0.55,1010,
                    'entry','print')""")

    inserted = V3.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=1300,
    )

    reward = conn.execute("SELECT * FROM crypto5m_maker_v3_rewards").fetchone()
    stats = conn.execute(
        """SELECT n_fills,reward_sum
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id='maker-adaptive-paper-v3' AND context_key=? AND arm_id='wide_small'""",
        (context_key,),
    ).fetchone()

    assert inserted == 1
    assert reward["quote_action_arm"] == "wide_small"
    assert reward["reward"] == pytest.approx(6.0)
    assert stats["n_fills"] == 1
    assert stats["reward_sum"] == pytest.approx(6.0)


# ---------- v3.2: markout-penalized reward (single lever over v3.1) --------------------------------

MAKER_FILLS_DDL = """
  CREATE TABLE maker_fills(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
    token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER,
    reason TEXT, fill_model TEXT);
"""

MAKER_QUOTES_DDL = """
  CREATE TABLE maker_quotes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
    token TEXT, side TEXT, status TEXT, price REAL, size REAL, fair REAL, best_bid REAL, best_ask REAL,
    seconds_left REAL, ts INTEGER, reason TEXT);
"""


def _seed_v32_fill(conn, campaign, *, future_best_bid):
    """One bid fill at 0.40x10 on the eventual winner, with a t+10s quote snapshot showing
    where the book went after the fill (future_best_bid < 0.40 = adverse selection)."""
    conn.executescript(MAKER_FILLS_DDL + MAKER_QUOTES_DDL)
    context_key = "mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy"
    V3.record_policy_action(
        conn,
        campaign_id=campaign,
        condition_id="0xc",
        window_start_ts=1000,
        token="up",
        context_key=context_key,
        fair_model_id="logit_blend_v1",
        q_structural=0.60,
        q_market_mid=0.52,
        q_used=0.55,
        fair_blend_weight=0.35,
        fair_confidence="medium",
        risk_state="normal",
        tail_risk_score=0.10,
        quote_action_arm="wide_small",
        selector_source="fallback",
        exploration_flag=False,
        parent_context_used="global",
        now_ts=1005,
    )
    conn.execute("""INSERT INTO maker_fills(
                    campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                    VALUES(?,'0xc',1000,'up','bid',0.40,10,0.55,1010,'entry','print')""", (campaign,))
    conn.execute("""INSERT INTO maker_quotes(
                    campaign_id,condition_id,window_start_ts,token,side,status,price,size,fair,
                    best_bid,best_ask,seconds_left,ts,reason)
                    VALUES(?,'0xc',1000,'up','bid','posted',0.28,10,0.35,?,0.36,200,1021,'requote')""",
                 (campaign, future_best_bid))
    return context_key


def test_v32_reward_subtracts_adverse_selection_markout(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3.2r", "maker-adaptive-paper-v3.1", 1000, {},
                         reward_version="maker-reward-v3.2")
    context_key = _seed_v32_fill(conn, "maker-adaptive-paper-v3.2r", future_best_bid=0.30)

    inserted = V3.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v3.2r",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=1300,
    )

    reward = conn.execute("SELECT * FROM crypto5m_maker_v3_rewards").fetchone()
    stats = conn.execute(
        """SELECT n_fills,reward_sum,adverse_sum
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id='maker-adaptive-paper-v3.2r' AND context_key=? AND arm_id='wide_small'""",
        (context_key,),
    ).fetchone()

    assert inserted == 1
    assert reward["reward_version"] == "maker-reward-v3.2"
    # settled pnl = (1 - 0.40) * 10 = 6.0; markout (bid) = (0.30 - 0.40)*1e4 = -1000 bps
    # adverse penalty = 1000/1e4 * 10 = 1.0 -> reward 5.0
    assert reward["fee_adjusted_pnl"] == pytest.approx(6.0)
    assert reward["adverse_selection_10s_bps"] == pytest.approx(-1000.0)
    assert reward["reward"] == pytest.approx(5.0)
    assert stats["reward_sum"] == pytest.approx(5.0)
    assert stats["adverse_sum"] == pytest.approx(-1000.0)


def test_v32_reward_no_penalty_on_favorable_markout(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3.2r", "maker-adaptive-paper-v3.1", 1000, {},
                         reward_version="maker-reward-v3.2")
    _seed_v32_fill(conn, "maker-adaptive-paper-v3.2r", future_best_bid=0.50)

    V3.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v3.2r",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=1300,
    )

    reward = conn.execute("SELECT * FROM crypto5m_maker_v3_rewards").fetchone()
    assert reward["adverse_selection_10s_bps"] == pytest.approx(1000.0)
    assert reward["reward"] == pytest.approx(6.0)   # favorable markout is never a bonus


def test_v31_reward_path_is_untouched_by_markout_data(conn):
    """A v3.1 campaign must produce byte-identical rewards even when quote snapshots exist."""
    V3.register_campaign(conn, "maker-adaptive-paper-v3.1", "maker-adaptive-paper-v2", 1000, {})
    _seed_v32_fill(conn, "maker-adaptive-paper-v3.1", future_best_bid=0.30)

    V3.update_rewards_for_settled_window(
        conn,
        campaign_id="maker-adaptive-paper-v3.1",
        condition_id="0xc",
        window_start_ts=1000,
        winning_outcome="Up",
        now_ts=1300,
    )

    reward = conn.execute("SELECT * FROM crypto5m_maker_v3_rewards").fetchone()
    assert reward["reward_version"] == "maker-reward-v3.1"
    assert reward["adverse_selection_10s_bps"] is None
    assert reward["reward"] == pytest.approx(6.0)


def test_register_campaign_reward_version_override_persists(conn):
    V3.register_campaign(conn, "maker-adaptive-paper-v3.2r", "maker-adaptive-paper-v3.1", 1000, {},
                         reward_version="maker-reward-v3.2")
    V3.register_campaign(conn, "maker-adaptive-paper-v3.1", "maker-adaptive-paper-v2", 1000, {})

    rows = dict(conn.execute(
        "SELECT campaign_id, reward_version FROM crypto5m_maker_v3_campaigns").fetchall())
    assert rows["maker-adaptive-paper-v3.2r"] == "maker-reward-v3.2"
    assert rows["maker-adaptive-paper-v3.1"] == "maker-reward-v3.1"
