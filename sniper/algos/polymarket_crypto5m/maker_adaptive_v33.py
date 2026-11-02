"""Adaptive Maker Brain V3.3-learner — rebuilt on window-level PAIRED reward.

Pre-registered entrant `maker-v33-learner-paper` in the learner tournament
(docs/superpowers/specs/2026-07-06-crypto5m-learner-tournament-preregistration.md,
design: docs/superpowers/specs/2026-07-06-crypto5m-v33-post-gate-maker-design.md
Branch B, launched in parallel instead of waiting for the gate).

The five v3 learner defects this fixes, by construction:
1. Censored reward -> reward = (own window PnL - BASELINE same-window PnL) x decision
   share, one observation per (window, context, arm). Not participating through a window
   the baseline monetized is a negative reward.
2. Brier fair objective -> the fair is FROZEN (default blend, honest vol floor); no
   blend learning of any kind.
3. Pseudo-replication -> one observation per window, never per fill.
4. Hierarchy trust -> a level is trusted only at n >= V33_MIN_WINDOWS decayed windows;
   otherwise the walk continues (a sparse child never blocks a sufficient parent).
5. No decay -> exponential recency decay (36h half-life) applied at scoring time;
   history is never rewritten.

Deviation is evidence-gated: the selector quotes the prior arm (first eligible in
NORMAL_ACTION_ORDER) unless an alternative has n >= V33_MIN_WINDOWS, decayed CI low > 0
(i.e. beats the baseline), and a decayed mean above the prior's. Epsilon exploration
still gathers the data that can ever justify deviating.

Paper-only. The baseline DB is opened read-only; this module never writes to it.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import maker_adaptive_v3 as V3

V33_MIN_WINDOWS = 30
V33_DECAY_HALF_LIFE_S = 36 * 3600
V33_EPSILON = 0.10
LEARNER_VERSION = "v3.3-window-paired"
REWARD_VERSION = "maker-reward-v3.3-window-paired"


@dataclass
class V33ActionDecision:
    arm_id: str
    context_key: str
    level_used: str
    selection_source: str
    exploration_flag: bool
    n_windows: int
    decayed_mean: Optional[float]
    ci_low: Optional[float]
    reason: str


def ensure_v33_tables(c: sqlite3.Connection) -> None:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v33_campaigns(
        campaign_id TEXT PRIMARY KEY, learner_version TEXT, reward_version TEXT,
        baseline_campaign TEXT, baseline_db TEXT, started_ts INTEGER, config_json TEXT);
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v33_window_rewards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        context_key TEXT, regime_id TEXT, arm_id TEXT,
        decision_share REAL, own_pnl REAL, baseline_pnl REAL, reward REAL,
        baseline_campaign TEXT, reward_version TEXT, created_ts INTEGER,
        UNIQUE(campaign_id, window_start_ts, context_key, arm_id));
      CREATE INDEX IF NOT EXISTS idx_v33_rewards_campaign
        ON crypto5m_maker_v33_window_rewards(campaign_id, window_start_ts);
    """)


def register_campaign(c: sqlite3.Connection, campaign_id: str, *, baseline_campaign: str,
                      baseline_db: str, now_ts: int, config: dict[str, Any] | None = None) -> None:
    ensure_v33_tables(c)
    payload = {"learner_version": LEARNER_VERSION, "reward_version": REWARD_VERSION,
               "min_windows": V33_MIN_WINDOWS, "decay_half_life_s": V33_DECAY_HALF_LIFE_S,
               "epsilon": V33_EPSILON, **dict(config or {})}
    c.execute(
        """INSERT INTO crypto5m_maker_v33_campaigns(
             campaign_id,learner_version,reward_version,baseline_campaign,baseline_db,
             started_ts,config_json)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET
             baseline_campaign=excluded.baseline_campaign,
             baseline_db=excluded.baseline_db,
             config_json=excluded.config_json""",
        (campaign_id, LEARNER_VERSION, REWARD_VERSION, baseline_campaign, baseline_db,
         int(now_ts), json.dumps(payload, sort_keys=True)),
    )


def is_v33_campaign(c: sqlite3.Connection, campaign_id: str) -> bool:
    try:
        return c.execute(
            "SELECT 1 FROM crypto5m_maker_v33_campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# Reward: one observation per (window, context, arm), paired vs the baseline.
# ---------------------------------------------------------------------------

def update_window_rewards(c: sqlite3.Connection, *, campaign_id: str, baseline_conn,
                          baseline_campaign: str, condition_id: str, window_start_ts: int,
                          own_pnl: float, now_ts: int) -> int:
    """Record paired window rewards for one settled window. Returns rows inserted.
    Idempotent (UNIQUE + INSERT OR IGNORE). If the baseline window is not settled yet,
    records NOTHING — callers re-attempt via sweep_missing_rewards()."""
    ensure_v33_tables(c)
    base = baseline_conn.execute(
        """SELECT realized_pnl FROM maker_windows
           WHERE campaign_id=? AND condition_id=? AND settled=1 AND realized_pnl IS NOT NULL""",
        (baseline_campaign, condition_id),
    ).fetchone()
    if base is None:
        return 0
    baseline_pnl = float(base[0])
    delta = float(own_pnl) - baseline_pnl

    decisions = c.execute(
        """SELECT context_key, quote_action_arm arm, COUNT(*) n
           FROM crypto5m_maker_v3_policy_actions
           WHERE campaign_id=? AND window_start_ts=?
           GROUP BY context_key, quote_action_arm""",
        (campaign_id, window_start_ts),
    ).fetchall()
    total = sum(int(r["n"]) for r in decisions)
    if not total:
        return 0
    regime = _window_regime(c, campaign_id, window_start_ts)
    inserted = 0
    for r in decisions:
        share = int(r["n"]) / total
        cur = c.execute(
            """INSERT OR IGNORE INTO crypto5m_maker_v33_window_rewards(
                 campaign_id,condition_id,window_start_ts,context_key,regime_id,arm_id,
                 decision_share,own_pnl,baseline_pnl,reward,baseline_campaign,
                 reward_version,created_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, condition_id, int(window_start_ts), r["context_key"], regime,
             r["arm"], round(share, 6), float(own_pnl), baseline_pnl,
             round(delta * share, 6), baseline_campaign, REWARD_VERSION, int(now_ts)),
        )
        inserted += int(cur.rowcount or 0)
    return inserted


def sweep_missing_rewards(c: sqlite3.Connection, *, campaign_id: str, baseline_conn,
                          baseline_campaign: str, now_ts: int, limit: int = 20) -> int:
    """Re-attempt reward recording for settled windows with no reward rows yet (the
    baseline may settle later than we do). Cheap; run once per settle pass."""
    ensure_v33_tables(c)
    rows = c.execute(
        """SELECT w.condition_id, w.window_start_ts, w.realized_pnl
           FROM maker_windows w
           WHERE w.campaign_id=? AND w.settled=1 AND w.realized_pnl IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM crypto5m_maker_v33_window_rewards r
                             WHERE r.campaign_id=w.campaign_id
                               AND r.window_start_ts=w.window_start_ts)
           ORDER BY w.window_start_ts DESC LIMIT ?""",
        (campaign_id, int(limit)),
    ).fetchall()
    done = 0
    for r in rows:
        done += update_window_rewards(
            c, campaign_id=campaign_id, baseline_conn=baseline_conn,
            baseline_campaign=baseline_campaign, condition_id=r["condition_id"],
            window_start_ts=int(r["window_start_ts"]), own_pnl=float(r["realized_pnl"]),
            now_ts=now_ts,
        )
    return done


def sweep_for_campaign(c: sqlite3.Connection, campaign_id: str, now_ts: int) -> int:
    """Settlement-path entry point: baseline location is read from the campaign row
    (self-describing, like the v3 frozen flag), baseline DB opened read-only per sweep."""
    row = c.execute(
        "SELECT baseline_campaign, baseline_db FROM crypto5m_maker_v33_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if not row:
        return 0
    try:
        baseline = sqlite3.connect(f"file:{row['baseline_db']}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError:
        return 0
    baseline.row_factory = sqlite3.Row
    try:
        return sweep_missing_rewards(
            c, campaign_id=campaign_id, baseline_conn=baseline,
            baseline_campaign=row["baseline_campaign"], now_ts=now_ts,
        )
    finally:
        baseline.close()


def _window_regime(c: sqlite3.Connection, campaign_id: str, ws: int) -> str:
    try:
        r = c.execute(
            """SELECT regime_id, COUNT(*) n FROM maker_regime_decisions
               WHERE campaign_id=? AND window_start_ts=? GROUP BY regime_id
               ORDER BY n DESC LIMIT 1""",
            (campaign_id, ws),
        ).fetchone()
        return str(r["regime_id"]) if r else "unknown"
    except sqlite3.OperationalError:
        return "unknown"


# ---------------------------------------------------------------------------
# Selection: prior arm unless the evidence gate opens; sufficiency-gated hierarchy;
# recency decay at scoring time.
# ---------------------------------------------------------------------------

def _decay_weight(age_s: float, half_life_s: float = V33_DECAY_HALF_LIFE_S) -> float:
    return 0.5 ** (max(0.0, age_s) / float(half_life_s))


def _level_matches(level: str, context_key: str, regime_id: str) -> bool:
    if level == "global":
        return True
    if level.startswith("regime:"):
        return level == f"regime:{regime_id}"
    lp = level.split("|")
    cp = context_key.split("|")
    if len(lp) != len(cp):
        return False
    return all(a == "*" or a == b for a, b in zip(lp, cp))


def _aggregate(rows, level: str, now_ts: int) -> dict[str, dict[str, float]]:
    """Decayed per-arm stats over reward rows matching a hierarchy level.
    n is the DECAYED window count; mean/CI use decay weights."""
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        if not _level_matches(level, row["context_key"], row["regime_id"] or "unknown"):
            continue
        w = _decay_weight(now_ts - int(row["window_start_ts"]))
        s = out.setdefault(row["arm_id"], {"n": 0.0, "wsum": 0.0, "wr": 0.0, "wr2": 0.0})
        s["n"] += w
        s["wsum"] += w
        s["wr"] += w * float(row["reward"])
        s["wr2"] += w * float(row["reward"]) ** 2
    for s in out.values():
        mean = s["wr"] / s["wsum"] if s["wsum"] else 0.0
        var = max(0.0, s["wr2"] / s["wsum"] - mean * mean) if s["wsum"] else 0.0
        se = math.sqrt(var / s["n"]) if s["n"] > 0 else float("inf")
        s["mean"] = mean
        s["ci_low"] = mean - 1.96 * se
    return out


def select_action_arm(c: sqlite3.Connection, *, campaign_id: str, context_key: str,
                      regime_id: str, fair_confidence: str, risk_state: str,
                      token_inventory: float, rng: random.Random,
                      epsilon: float = V33_EPSILON, now_ts: int = 0) -> V33ActionDecision:
    forced = V3._forced_arm(regime_id, risk_state, token_inventory)
    if forced:
        return V33ActionDecision(forced, context_key, "safety", "safety_override", False,
                                 0, None, None, f"{risk_state} safety override")
    candidates = V3._candidate_arms(token_inventory)
    prior = candidates[0]

    if rng.random() < max(0.0, float(epsilon or 0.0)):
        arm = rng.choice(candidates)
        return V33ActionDecision(arm, context_key, "exploration", "exploration", True,
                                 0, None, None, "epsilon exploration")

    ensure_v33_tables(c)
    rows = c.execute(
        """SELECT context_key, regime_id, arm_id, reward, window_start_ts
           FROM crypto5m_maker_v33_window_rewards WHERE campaign_id=?""",
        (campaign_id,),
    ).fetchall()
    for level in V3.parent_contexts(context_key, regime_id):
        stats = _aggregate(rows, level, now_ts)
        eligible = {a: s for a, s in stats.items()
                    if a in candidates and s["n"] >= V33_MIN_WINDOWS}
        if not eligible:
            continue  # sufficiency gate: a sparse level never blocks the walk
        prior_mean = eligible.get(prior, {}).get("mean", 0.0)
        best_alt = None
        for arm, s in eligible.items():
            if arm == prior:
                continue
            # evidence gate: deviate only when the alternative both beats the baseline
            # (ci_low > 0 on PAIRED reward) and beats the prior's decayed mean
            if s["ci_low"] > 0.0 and s["mean"] > prior_mean:
                if best_alt is None or s["mean"] > eligible[best_alt]["mean"]:
                    best_alt = arm
        if best_alt is not None:
            s = eligible[best_alt]
            return V33ActionDecision(best_alt, context_key, level, "evidence_gated",
                                     False, int(s["n"]), round(s["mean"], 4),
                                     round(s["ci_low"], 4),
                                     "alt beats baseline (ci_low>0) and prior mean")
        s = eligible.get(prior)
        return V33ActionDecision(prior, context_key, level, "prior_confirmed", False,
                                 int(s["n"]) if s else 0,
                                 round(s["mean"], 4) if s else None,
                                 round(s["ci_low"], 4) if s else None,
                                 "no alternative clears the evidence gate")
    return V33ActionDecision(prior, context_key, "insufficient", "prior_default", False,
                             0, None, None, "no level has sufficient decayed windows")
