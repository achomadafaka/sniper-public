"""V3.3-learner: window-level paired reward, sufficiency-gated hierarchy, decay,
evidence-gated deviation. Pins the five defect-fixes the tournament pre-registration
promises (spec 2026-07-06-crypto5m-v33-post-gate-maker-design.md, Branch B)."""
from __future__ import annotations

import random
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import maker_adaptive_v33 as V33

CTX = "mid_window|high|calm|long_light|healthy|confirmed_up|healthy"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    V33.ensure_v33_tables(c)
    c.executescript("""
      CREATE TABLE maker_windows(condition_id TEXT, window_start_ts INTEGER, up_inv REAL,
        down_inv REAL, cash REAL, realized_pnl REAL, settled INTEGER, winning_outcome TEXT,
        campaign_id TEXT, ts INTEGER);
      CREATE TABLE crypto5m_maker_v3_policy_actions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, window_start_ts INTEGER, context_key TEXT, quote_action_arm TEXT,
        risk_state TEXT);
      CREATE TABLE maker_regime_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, window_start_ts INTEGER, regime_id TEXT);
    """)
    return c


def _baseline_db(campaign, windows):
    b = sqlite3.connect(":memory:")
    b.row_factory = sqlite3.Row
    b.execute("""CREATE TABLE maker_windows(condition_id TEXT, window_start_ts INTEGER,
                 realized_pnl REAL, settled INTEGER, campaign_id TEXT)""")
    for cid, ws, pnl, settled in windows:
        b.execute("INSERT INTO maker_windows VALUES(?,?,?,?,?)", (cid, ws, pnl, settled, campaign))
    b.commit()
    return b


def _add_decisions(c, campaign, ws, arm_counts):
    for arm, n in arm_counts.items():
        for _ in range(n):
            c.execute("INSERT INTO crypto5m_maker_v3_policy_actions(campaign_id,"
                      "window_start_ts,context_key,quote_action_arm,risk_state) "
                      "VALUES(?,?,?,?,'normal')", (campaign, ws, CTX, arm))
    c.commit()


def _seed_reward(c, campaign, ws, arm, reward, ctx=CTX, regime="normal"):
    c.execute("""INSERT INTO crypto5m_maker_v33_window_rewards(campaign_id,condition_id,
                 window_start_ts,context_key,regime_id,arm_id,decision_share,own_pnl,
                 baseline_pnl,reward,baseline_campaign,reward_version,created_ts)
                 VALUES(?,?,?,?,?,?,1.0,0,0,?,?,?,?)""",
              (campaign, f"cid{ws}", ws, ctx, regime, arm, reward, "base", V33.REWARD_VERSION, ws))


def test_paired_reward_shares_delta_and_skips_unsettled_baseline():
    c = _db()
    base = _baseline_db("frozen", [("cid1", 1000, 3.0, 1), ("cid2", 2000, 1.0, 0)])
    _add_decisions(c, "v33", 1000, {"base_small": 3, "wide_tiny": 1})
    _add_decisions(c, "v33", 2000, {"base_small": 2})

    n = V33.update_window_rewards(c, campaign_id="v33", baseline_conn=base,
                                  baseline_campaign="frozen", condition_id="cid1",
                                  window_start_ts=1000, own_pnl=7.0, now_ts=1500)
    assert n == 2
    rows = {r["arm_id"]: r for r in c.execute(
        "SELECT * FROM crypto5m_maker_v33_window_rewards WHERE window_start_ts=1000")}
    # delta = 7 - 3 = 4, split 0.75 / 0.25
    assert rows["base_small"]["reward"] == pytest.approx(3.0)
    assert rows["wide_tiny"]["reward"] == pytest.approx(1.0)
    assert rows["base_small"]["baseline_pnl"] == pytest.approx(3.0)

    # unsettled baseline window -> NO observation (never a default zero)
    n2 = V33.update_window_rewards(c, campaign_id="v33", baseline_conn=base,
                                   baseline_campaign="frozen", condition_id="cid2",
                                   window_start_ts=2000, own_pnl=5.0, now_ts=2500)
    assert n2 == 0
    # idempotent re-run inserts nothing new
    assert V33.update_window_rewards(c, campaign_id="v33", baseline_conn=base,
                                     baseline_campaign="frozen", condition_id="cid1",
                                     window_start_ts=1000, own_pnl=7.0, now_ts=1500) == 0


def test_sweep_retries_after_baseline_settles_late():
    c = _db()
    c.execute("INSERT INTO maker_windows VALUES('cid1',1000,0,0,0,5.0,1,'up','v33',0)")
    _add_decisions(c, "v33", 1000, {"base_small": 2})
    base = _baseline_db("frozen", [("cid1", 1000, 1.0, 0)])  # not settled yet

    assert V33.sweep_missing_rewards(c, campaign_id="v33", baseline_conn=base,
                                     baseline_campaign="frozen", now_ts=1500) == 0
    base.execute("UPDATE maker_windows SET settled=1")
    base.commit()
    assert V33.sweep_missing_rewards(c, campaign_id="v33", baseline_conn=base,
                                     baseline_campaign="frozen", now_ts=1600) == 1
    r = c.execute("SELECT reward FROM crypto5m_maker_v33_window_rewards").fetchone()
    assert r["reward"] == pytest.approx(4.0)  # (5-1) * share 1.0


def _select(c, rng=None, now_ts=100_000, epsilon=0.0, ctx=CTX):
    return V33.select_action_arm(
        c, campaign_id="v33", context_key=ctx, regime_id="normal",
        fair_confidence="medium", risk_state="normal", token_inventory=0.0,
        rng=rng or random.Random(1), epsilon=epsilon, now_ts=now_ts)


def test_selector_defaults_to_prior_without_sufficient_windows():
    c = _db()
    for ws in range(0, 10):  # 10 windows << V33_MIN_WINDOWS
        _seed_reward(c, "v33", ws * 300, "wide_tiny", 5.0)
    d = _select(c)
    assert d.arm_id == "base_small"
    assert d.selection_source == "prior_default"


def test_evidence_gate_opens_only_on_ci_and_prior_beat():
    c = _db()
    now = 40 * 300 + 100_000
    # 35 recent windows: alt consistently +2 vs baseline; prior mildly negative
    for i in range(35):
        ws = 100_000 + i * 300
        _seed_reward(c, "v33", ws, "wide_small", 2.0 + (0.1 if i % 2 else -0.1))
        _seed_reward(c, "v33", ws, "base_small", -0.5)
    d = _select(c, now_ts=now)
    assert d.selection_source == "evidence_gated"
    assert d.arm_id == "wide_small"
    assert d.ci_low > 0

    # same mean but huge variance -> ci_low <= 0 -> stays on prior
    c2 = _db()
    for i in range(35):
        ws = 100_000 + i * 300
        _seed_reward(c2, "v33", ws, "wide_small", 40.0 if i % 2 else -36.0)
        _seed_reward(c2, "v33", ws, "base_small", -0.5)
    d2 = _select(c2, now_ts=now)
    assert d2.arm_id == "base_small"
    assert d2.selection_source == "prior_confirmed"


def test_recency_decay_downweights_stale_evidence():
    c = _db()
    # 35 windows of great alt evidence, but ALL older than ~5 half-lives
    old = 100_000
    for i in range(35):
        _seed_reward(c, "v33", old + i * 300, "wide_small", 3.0)
        _seed_reward(c, "v33", old + i * 300, "base_small", -0.5)
    now = old + 35 * 300 + 5 * V33.V33_DECAY_HALF_LIFE_S
    d = _select(c, now_ts=now)
    # decayed n < min windows -> walk finds nothing sufficient -> prior
    assert d.arm_id == "base_small"
    assert d.selection_source == "prior_default"


def test_sparse_child_falls_through_to_sufficient_parent_level():
    c = _db()
    now = 200_000
    child = CTX
    sibling = CTX.replace("confirmed_up", "confirmed_down")  # same parent after direction drop
    # child has 2 windows only; sibling contexts supply 40 windows at the parent level
    _seed_reward(c, "v33", now - 300, "base_small", 1.0, ctx=child)
    _seed_reward(c, "v33", now - 600, "base_small", 1.0, ctx=child)
    for i in range(40):
        _seed_reward(c, "v33", now - (i + 3) * 300, "base_small", 1.0, ctx=sibling)
    d = _select(c, now_ts=now)
    assert d.selection_source == "prior_confirmed"
    assert "*" in d.level_used  # a wildcard parent level, not the sparse exact context
    assert d.n_windows >= V33.V33_MIN_WINDOWS


def test_safety_override_and_exploration():
    c = _db()
    d = V33.select_action_arm(
        c, campaign_id="v33", context_key=CTX, regime_id="normal",
        fair_confidence="medium", risk_state="pause", token_inventory=0.0,
        rng=random.Random(1), epsilon=0.0, now_ts=1000)
    assert d.arm_id == "pause"
    assert d.selection_source == "safety_override"

    explored = {V33.select_action_arm(
        c, campaign_id="v33", context_key=CTX, regime_id="normal",
        fair_confidence="medium", risk_state="normal", token_inventory=0.0,
        rng=random.Random(s), epsilon=1.0, now_ts=1000).selection_source
        for s in range(5)}
    assert explored == {"exploration"}
