"""Frozen-learner mode (maker-adaptive-paper-v3.2f-frozen ablation).

Pre-registration: docs/superpowers/specs/2026-07-06-crypto5m-learner-tournament-preregistration.md.
Frozen = launch priors held constant: learned stats never read, no exploration, no
learner-state updates. Reward rows are still recorded (telemetry for the paired comparison).
"""
from __future__ import annotations

import random
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import maker_adaptive_v3 as V3

CTX = "mid_window|high|calm|long_light|healthy|confirmed_up|healthy"
FAIR_CTX = {
    "seconds_bucket": "mid",
    "odds_bucket": "high",
    "volatility_state": "calm",
    "jump_state": "none",
    "source_health": "healthy",
    "spread_state": "healthy",
    "disagreement_bucket": "low",
    "book_health": "healthy",
}


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    V3.ensure_v3_tables(c)
    yield c
    c.close()


def _poison_arm_stats(conn, campaign):
    """Learned state that strongly favors wide_tiny — a frozen selector must ignore it."""
    V3.upsert_action_arm_stats(
        conn, campaign_id=campaign, context_key=CTX, arm_id="wide_tiny",
        n_decisions=100, n_fills=80, reward_sum=400.0, reward_sq_sum=10.0,
        adverse_sum=0.0, now_ts=1000,
    )


def test_frozen_selector_ignores_learned_stats_and_is_deterministic(conn):
    campaign = "maker-adaptive-paper-v3.2f-frozen"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v3.2f", 1000, {"frozen": True})
    _poison_arm_stats(conn, campaign)

    for seed in (1, 2, 3):
        decision = V3.select_action_arm(
            conn, campaign_id=campaign, context_key=CTX, regime_id="normal",
            fair_confidence="medium", risk_state="normal", token_inventory=0.0,
            rng=random.Random(seed), epsilon=0.10, now_ts=2000, frozen=True,
        )
        assert decision.arm_id == V3.NORMAL_ACTION_ORDER[0]  # launch prior = base_small
        assert decision.selection_source == "frozen_prior"
        assert decision.exploration_flag is False

    # same inputs without frozen: the learned stats dominate
    learned = V3.select_action_arm(
        conn, campaign_id=campaign, context_key=CTX, regime_id="normal",
        fair_confidence="medium", risk_state="normal", token_inventory=0.0,
        rng=random.Random(1), epsilon=0.0, now_ts=2000,
    )
    assert learned.arm_id == "wide_tiny"


def test_frozen_selector_keeps_safety_overrides(conn):
    campaign = "maker-adaptive-paper-v3.2f-frozen"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v3.2f", 1000, {"frozen": True})

    decision = V3.select_action_arm(
        conn, campaign_id=campaign, context_key=CTX, regime_id="normal",
        fair_confidence="medium", risk_state="pause", token_inventory=0.0,
        rng=random.Random(1), epsilon=0.0, now_ts=2000, frozen=True,
    )
    assert decision.arm_id == "pause"
    assert decision.selection_source == "safety_override"


def test_frozen_fair_value_ignores_learned_blend_stats(conn):
    campaign = "maker-adaptive-paper-v3.2f-frozen"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v3.2f", 1000, {"frozen": True})
    key = V3.fair_context_key(FAIR_CTX)
    conn.execute(
        """INSERT INTO crypto5m_maker_v3_fair_blend_stats(
             campaign_id,fair_context_key,blend_weight,residual_adjustment,n_observations)
           VALUES(?,?,?,?,?)""",
        (campaign, key, 0.05, 0.30, 500),
    )

    frozen = V3.choose_fair_value(
        conn, campaign_id=campaign, q_structural=0.70, q_market_mid=0.50,
        context=FAIR_CTX, now_ts=2000, frozen=True,
    )
    learned = V3.choose_fair_value(
        conn, campaign_id=campaign, q_structural=0.70, q_market_mid=0.50,
        context=FAIR_CTX, now_ts=2000,
    )
    assert learned.blend_weight == pytest.approx(0.05)
    assert learned.residual_adjustment == pytest.approx(0.30)
    assert frozen.residual_adjustment == pytest.approx(0.0)
    assert frozen.blend_weight != pytest.approx(0.05)
    assert frozen.q_used != pytest.approx(learned.q_used)


def test_frozen_settlement_records_rewards_but_never_updates_learner_state(conn):
    campaign = "maker-adaptive-paper-v3.2f-frozen"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v3.2f", 1000, {"frozen": True})
    assert V3.campaign_frozen(conn, campaign) is True

    conn.execute(
        """CREATE TABLE maker_fills(
             id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT,
             window_start_ts INTEGER, token TEXT, side TEXT, price REAL, size REAL,
             fill_model TEXT, quote_id INTEGER, ts INTEGER)"""
    )
    conn.execute(
        """INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fill_model,ts)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (campaign, "0xabc", 5000, "up", "buy", 0.40, 10.0, "print", 5100),
    )
    V3.record_policy_action(
        conn, campaign_id=campaign, condition_id="0xabc", window_start_ts=5000,
        token="up", context_key=CTX, fair_model_id="blended", q_structural=0.60,
        q_market_mid=0.50, q_used=0.55, fair_blend_weight=0.5, fair_confidence="medium",
        risk_state="normal", tail_risk_score=0.0, quote_action_arm="base_small",
        selector_source="frozen_prior", exploration_flag=False,
        parent_context_used="frozen", now_ts=5000, frozen=True,
    )

    inserted = V3.update_rewards_for_settled_window(
        conn, campaign_id=campaign, condition_id="0xabc", window_start_ts=5000,
        winning_outcome="up", now_ts=6000,
    )

    assert inserted == 1
    n_rewards = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_maker_v3_rewards WHERE campaign_id=?", (campaign,)
    ).fetchone()[0]
    assert n_rewards == 1
    n_arm_stats = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_maker_v3_action_arm_stats WHERE campaign_id=?", (campaign,)
    ).fetchone()[0]
    assert n_arm_stats == 0
    n_blend = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_maker_v3_fair_blend_stats WHERE campaign_id=?", (campaign,)
    ).fetchone()[0]
    assert n_blend == 0


def test_unfrozen_campaign_still_updates_learner_state(conn):
    campaign = "maker-adaptive-paper-v3.2f"
    V3.register_campaign(conn, campaign, "maker-adaptive-paper-v3.1", 1000, {"frozen": False})
    assert V3.campaign_frozen(conn, campaign) is False

    conn.execute(
        """CREATE TABLE maker_fills(
             id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT,
             window_start_ts INTEGER, token TEXT, side TEXT, price REAL, size REAL,
             fill_model TEXT, quote_id INTEGER, ts INTEGER)"""
    )
    conn.execute(
        """INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fill_model,ts)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (campaign, "0xabc", 5000, "up", "buy", 0.40, 10.0, "print", 5100),
    )
    V3.record_policy_action(
        conn, campaign_id=campaign, condition_id="0xabc", window_start_ts=5000,
        token="up", context_key=CTX, fair_model_id="blended", q_structural=0.60,
        q_market_mid=0.50, q_used=0.55, fair_blend_weight=0.5, fair_confidence="medium",
        risk_state="normal", tail_risk_score=0.0, quote_action_arm="base_small",
        selector_source="learned_ucb", exploration_flag=False,
        parent_context_used=CTX, now_ts=5000,
    )

    V3.update_rewards_for_settled_window(
        conn, campaign_id=campaign, condition_id="0xabc", window_start_ts=5000,
        winning_outcome="up", now_ts=6000,
    )

    n_arm_stats = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_maker_v3_action_arm_stats WHERE campaign_id=?", (campaign,)
    ).fetchone()[0]
    assert n_arm_stats == 1
