"""Adaptive paper-profile learner for the crypto5m maker.

Paper only. No network, no orders, no private-key imports.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import random
import sqlite3
from typing import Any, Optional

from sniper.algos.polymarket_crypto5m import maker_regime as RG
from sniper.algos.polymarket_crypto5m import maker_regime_state as RS


SELECTOR_VERSION = "adaptive-ucb-v1"
REWARD_VERSION = "maker-reward-v1"
SELECTOR_VERSION_V2 = "adaptive-ucb-v2"
REWARD_VERSION_V2 = "maker-reward-v2"

CONTEXT_FIELDS = (
    "window_phase",
    "odds_bucket",
    "volatility_state",
    "inventory_state",
    "spread_state",
    "direction_state",
    "book_health",
)

PROFILE_IDS = (
    "quote_tight",
    "quote_base",
    "quote_wide",
    "normal_small",
    "reduce_only",
    "pause",
    "exit_fast",
)


@dataclass(frozen=True)
class LearnerConfig:
    learner_version: str
    selector_version: str
    reward_version: str
    min_profile_fills_per_context: int
    epsilon_min: float = 0.0


LEARNER_CONFIGS = {
    "v1": LearnerConfig("v1", SELECTOR_VERSION, REWARD_VERSION, 5, 0.0),
    "v2": LearnerConfig("v2", SELECTOR_VERSION_V2, REWARD_VERSION_V2, 10, 0.02),
}


def learner_config(version: str | None) -> LearnerConfig:
    key = str(version or "v1").strip().lower()
    if key in {"2", "learner-v2", "adaptive-ucb-v2", REWARD_VERSION_V2}:
        key = "v2"
    if key in {"1", "learner-v1", "adaptive-ucb-v1", REWARD_VERSION}:
        key = "v1"
    return LEARNER_CONFIGS.get(key, LEARNER_CONFIGS["v1"])


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    margin_multiplier: float = 1.0
    margin_add: float = 0.0
    clip_multiplier: float = 1.0
    max_shares_multiplier: float = 1.0
    min_edge: float = 0.04
    min_seconds_quote: float = 30.0
    quote_ttl_ms: int = 12_000
    bid_enabled: bool = True
    ask_enabled: bool = True
    exit_only: bool = False
    hard_cut: bool = True


@dataclass(frozen=True)
class AdaptiveDecision:
    profile_id: str
    selector_version: str
    reward_version: str
    selection_source: str
    exploration_flag: bool
    profile_disabled_flag: bool
    parent_context_used: str
    context_key: str
    n_fills: int
    mean_reward: float
    reason: str
    fallback_depth: int = 0
    fallback_reason: str = "full context"
    exploration_budget_remaining: float = 0.0
    confidence_low: Optional[float] = None
    confidence_high: Optional[float] = None
    adaptive_status: str = "collecting"


PROFILES: dict[str, ProfileSpec] = {
    "quote_tight": ProfileSpec("quote_tight", margin_multiplier=0.65, clip_multiplier=1.0,
                               max_shares_multiplier=1.0, min_edge=0.03, quote_ttl_ms=8_000),
    "quote_base": ProfileSpec("quote_base"),
    "quote_wide": ProfileSpec("quote_wide", margin_multiplier=1.75, clip_multiplier=0.55,
                               max_shares_multiplier=0.65, min_edge=0.06, quote_ttl_ms=7_000),
    "normal_small": ProfileSpec("normal_small", margin_multiplier=1.0, clip_multiplier=0.45,
                                max_shares_multiplier=0.55, min_edge=0.04, quote_ttl_ms=10_000),
    "reduce_only": ProfileSpec("reduce_only", margin_multiplier=1.25, clip_multiplier=0.55,
                               max_shares_multiplier=0.50, min_edge=0.04, bid_enabled=False,
                               ask_enabled=True, exit_only=True, quote_ttl_ms=6_000),
    "pause": ProfileSpec("pause", clip_multiplier=0.0, max_shares_multiplier=0.0,
                         bid_enabled=False, ask_enabled=False, exit_only=False, hard_cut=False,
                         quote_ttl_ms=0),
    "exit_fast": ProfileSpec("exit_fast", margin_multiplier=0.75, clip_multiplier=1.0,
                             max_shares_multiplier=0.25, min_edge=0.03, bid_enabled=False,
                             ask_enabled=True, exit_only=True, quote_ttl_ms=3_000),
}


def ensure_adaptive_tables(c: sqlite3.Connection) -> None:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS crypto5m_maker_adaptive_campaigns(
        campaign_id TEXT PRIMARY KEY,
        selector_version TEXT,
        reward_version TEXT,
        control_campaign TEXT,
        started_ts INTEGER,
        config_json TEXT
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_profile_stats(
        campaign_id TEXT,
        context_key TEXT,
        profile_id TEXT,
        n_decisions INTEGER DEFAULT 0,
        n_quotes INTEGER DEFAULT 0,
        n_fills INTEGER DEFAULT 0,
        reward_sum REAL DEFAULT 0,
        reward_sq_sum REAL DEFAULT 0,
        pnl_sum REAL DEFAULT 0,
        adverse_sum REAL DEFAULT 0,
        hard_cuts INTEGER DEFAULT 0,
        disabled INTEGER DEFAULT 0,
        last_updated_ts INTEGER,
        PRIMARY KEY(campaign_id, context_key, profile_id)
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_profile_disables(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        context_key TEXT,
        profile_id TEXT,
        disabled_at INTEGER,
        disabled_reason TEXT,
        selector_version TEXT,
        reward_version TEXT
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_adaptive_rewards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        fill_id INTEGER,
        episode_id TEXT,
        moment_key TEXT,
        context_key TEXT,
        profile_id TEXT,
        reward REAL,
        fee_adjusted_pnl REAL,
        adverse_selection_10s_bps REAL,
        inventory_carry_penalty REAL,
        hard_cut_penalty REAL,
        reward_version TEXT,
        ts INTEGER,
        UNIQUE(campaign_id, fill_id, reward_version)
      );
      CREATE TABLE IF NOT EXISTS maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        token TEXT,
        side TEXT,
        price REAL,
        size REAL,
        fair REAL,
        ts INTEGER,
        reason TEXT
      );
      CREATE TABLE IF NOT EXISTS maker_quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        token TEXT,
        side TEXT,
        status TEXT,
        price REAL,
        size REAL,
        fair REAL,
        best_bid REAL,
        best_ask REAL,
        seconds_left REAL,
        ts INTEGER,
        reason TEXT
      );
    """)
    for table in ("maker_fills", "maker_quotes", "maker_regime_decisions", "maker_profile_decisions"):
        for col, typ in (
            ("selector_version", "TEXT"),
            ("reward_version", "TEXT"),
            ("selection_mode", "TEXT"),
            ("exploration_flag", "INTEGER"),
            ("profile_disabled_flag", "INTEGER"),
            ("parent_context_used", "TEXT"),
            ("adaptive_context_key", "TEXT"),
        ):
            _add_column(c, table, col, typ)
    for col, typ in (
        ("fill_model", "TEXT"),
        ("moment_key", "TEXT"),
        ("profile_id", "TEXT"),
        ("fill_profile_id", "TEXT"),
        ("episode_id", "TEXT"),
    ):
        _add_column(c, "maker_fills", col, typ)
    c.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_fill ON crypto5m_maker_adaptive_rewards(campaign_id,fill_id)")
    for table in ("maker_fills", "maker_quotes", "maker_regime_decisions", "maker_profile_decisions"):
        for col, typ in (
            ("fallback_depth", "INTEGER"),
            ("fallback_reason", "TEXT"),
            ("exploration_budget_remaining", "REAL"),
            ("adaptive_status", "TEXT"),
        ):
            _add_column(c, table, col, typ)
    for col, typ in (
        ("settled_fill_pnl", "REAL"),
        ("adverse_selection_3s_bps", "REAL"),
        ("adverse_selection_30s_bps", "REAL"),
        ("future_executable_unwind_10s", "REAL"),
        ("shadow_quality", "TEXT"),
        ("concentration_penalty", "REAL"),
    ):
        _add_column(c, "crypto5m_maker_adaptive_rewards", col, typ)
    for col, typ in (
        ("status", "TEXT"),
        ("confidence_low", "REAL"),
        ("confidence_high", "REAL"),
        ("max_window_fill_share", "REAL"),
    ):
        _add_column(c, "crypto5m_maker_profile_stats", col, typ)
    for col, typ in (
        ("sample_counts_json", "TEXT"),
        ("confidence_band_json", "TEXT"),
    ):
        _add_column(c, "crypto5m_maker_profile_disables", col, typ)


def register_campaign(c: sqlite3.Connection, campaign_id: str, control_campaign: str,
                      now_ts: int, config: dict[str, Any], *,
                      learner_version: str = "v1") -> None:
    ensure_adaptive_tables(c)
    cfg = learner_config(learner_version)
    config = {**dict(config or {}), "learner_version": cfg.learner_version}
    c.execute(
        """INSERT INTO crypto5m_maker_adaptive_campaigns(
             campaign_id,selector_version,reward_version,control_campaign,started_ts,config_json)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET
             selector_version=excluded.selector_version,
             reward_version=excluded.reward_version,
             control_campaign=excluded.control_campaign,
             config_json=excluded.config_json""",
        (campaign_id, cfg.selector_version, cfg.reward_version, control_campaign, int(now_ts),
         json.dumps(config, sort_keys=True)),
    )
    c.commit()


def context_key_label(moment_key: dict[str, Any] | str | None) -> str:
    if isinstance(moment_key, str):
        try:
            moment_key = json.loads(moment_key)
        except json.JSONDecodeError:
            return moment_key
    moment_key = moment_key or {}
    return "|".join(str(moment_key.get(field) or "unknown") for field in CONTEXT_FIELDS)


def parent_contexts(context_key: str, regime_id: str) -> list[str]:
    parts = context_key.split("|")
    if len(parts) != len(CONTEXT_FIELDS):
        return [context_key, f"regime:{regime_id}", "global"]
    contexts = [context_key]
    for drop in ("direction_state", "inventory_state", "volatility_state"):
        data = dict(zip(CONTEXT_FIELDS, parts))
        data[drop] = "*"
        contexts.append("|".join(data[f] for f in CONTEXT_FIELDS))
    contexts.extend((f"regime:{regime_id}", "global"))
    out: list[str] = []
    for ctx in contexts:
        if ctx not in out:
            out.append(ctx)
    return out


def upsert_profile_stats(c: sqlite3.Connection, *, campaign_id: str, context_key: str, profile_id: str,
                         n_decisions: int = 0, n_quotes: int = 0, n_fills: int = 0,
                         reward_sum: float = 0.0, reward_sq_sum: float = 0.0,
                         pnl_sum: float = 0.0, adverse_sum: float = 0.0,
                         hard_cuts: int = 0, now_ts: int = 0) -> None:
    ensure_adaptive_tables(c)
    c.execute(
        """INSERT INTO crypto5m_maker_profile_stats(
             campaign_id,context_key,profile_id,n_decisions,n_quotes,n_fills,reward_sum,reward_sq_sum,
             pnl_sum,adverse_sum,hard_cuts,disabled,last_updated_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id,context_key,profile_id) DO UPDATE SET
             n_decisions=crypto5m_maker_profile_stats.n_decisions + excluded.n_decisions,
             n_quotes=crypto5m_maker_profile_stats.n_quotes + excluded.n_quotes,
             n_fills=crypto5m_maker_profile_stats.n_fills + excluded.n_fills,
             reward_sum=crypto5m_maker_profile_stats.reward_sum + excluded.reward_sum,
             reward_sq_sum=crypto5m_maker_profile_stats.reward_sq_sum + excluded.reward_sq_sum,
             pnl_sum=crypto5m_maker_profile_stats.pnl_sum + excluded.pnl_sum,
             adverse_sum=crypto5m_maker_profile_stats.adverse_sum + excluded.adverse_sum,
             hard_cuts=crypto5m_maker_profile_stats.hard_cuts + excluded.hard_cuts,
             last_updated_ts=excluded.last_updated_ts""",
        (campaign_id, context_key, profile_id, n_decisions, n_quotes, n_fills, reward_sum,
         reward_sq_sum, pnl_sum, adverse_sum, hard_cuts, 0, int(now_ts or 0)),
    )


def disable_profile(c: sqlite3.Connection, *, campaign_id: str, context_key: str, profile_id: str,
                    now_ts: int, reason: str, learner_version: str = "v1",
                    sample_counts: dict[str, Any] | None = None,
                    confidence_band: tuple[Optional[float], Optional[float]] = (None, None)) -> None:
    ensure_adaptive_tables(c)
    cfg = learner_config(learner_version)
    upsert_profile_stats(c, campaign_id=campaign_id, context_key=context_key, profile_id=profile_id,
                         now_ts=now_ts)
    c.execute(
        """UPDATE crypto5m_maker_profile_stats SET disabled=1,status='disabled_recommended',
             confidence_low=?,confidence_high=?,last_updated_ts=?
           WHERE campaign_id=? AND context_key=? AND profile_id=?""",
        (confidence_band[0], confidence_band[1], int(now_ts), campaign_id, context_key, profile_id),
    )
    c.execute(
        """INSERT INTO crypto5m_maker_profile_disables(
             campaign_id,context_key,profile_id,disabled_at,disabled_reason,selector_version,reward_version,
             sample_counts_json,confidence_band_json)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            campaign_id,
            context_key,
            profile_id,
            int(now_ts),
            reason,
            cfg.selector_version,
            cfg.reward_version,
            json.dumps(sample_counts or {}, sort_keys=True),
            json.dumps({"low": confidence_band[0], "high": confidence_band[1]}, sort_keys=True),
        ),
    )


def select_profile(c: sqlite3.Connection, *, campaign_id: str, regime_id: str,
                   moment_key: dict[str, Any] | str | None, token_inventory: float,
                   now_ts: int, rng: random.Random, epsilon: float = 0.10,
                   learner_version: str = "v1") -> AdaptiveDecision:
    ensure_adaptive_tables(c)
    cfg = learner_config(learner_version)
    context_key = context_key_label(moment_key)
    key_data = _as_dict(moment_key)
    if regime_id == "hard_invalid" or key_data.get("book_health") in {"missing", "stale", "quarantined"}:
        return _decision("pause", "safety_override", False, context_key, context_key, 0, 0.0,
                         "safety state paused quotes", cfg=cfg)
    if regime_id == "late_window":
        profile = "reduce_only" if float(token_inventory or 0.0) > 0 else "pause"
        return _decision(profile, "safety_override", False, context_key, context_key, 0, 0.0,
                         "late window safety override", cfg=cfg)
    if regime_id == "inventory_saturated":
        return _decision("reduce_only", "safety_override", False, context_key, context_key, 0, 0.0,
                         "inventory saturated safety override", cfg=cfg)

    candidates = _candidate_profiles(
        _enabled_profiles(c, campaign_id, context_key),
        token_inventory=float(token_inventory or 0.0),
    )
    if not candidates:
        return _decision("pause", "safety_override", False, context_key, context_key, 0, 0.0,
                         "all profiles disabled", cfg=cfg)

    selected_ctx = "global"
    fallback_depth = 0
    fallback_reason = "full context"
    stats = {p: _empty_stat(p) for p in candidates}
    for depth, ctx in enumerate(parent_contexts(context_key, regime_id)):
        rows = _stats_rows(c, campaign_id, ctx)
        available = {r["profile_id"]: r for r in rows if r["profile_id"] in candidates and not r["disabled"]}
        if available:
            stats.update(available)
            selected_ctx = ctx
            fallback_depth = depth
            fallback_reason = _fallback_reason(depth, ctx)
            break

    min_fill = min(int(_get_stat(stats[p], "n_fills")) for p in candidates)
    thin = [p for p in candidates if int(_get_stat(stats[p], "n_fills")) == min_fill]
    if min_fill < cfg.min_profile_fills_per_context:
        profile = _first_profile(thin)
        row = stats[profile]
        return _decision(profile, "fallback", True, context_key, selected_ctx,
                         int(_get_stat(row, "n_fills")), _mean_reward(row),
                         "minimum exploration sample not met", cfg=cfg,
                         fallback_depth=fallback_depth, fallback_reason=fallback_reason,
                         exploration_budget_remaining=cfg.min_profile_fills_per_context - min_fill,
                         confidence_band=_confidence_band(row))

    effective_epsilon = max(cfg.epsilon_min, max(0.0, float(epsilon)))
    if rng.random() < effective_epsilon:
        profile = rng.choice([p for p in PROFILE_IDS if p in candidates])
        row = stats[profile]
        return _decision(profile, "exploration", True, context_key, selected_ctx,
                         int(_get_stat(row, "n_fills")), _mean_reward(row),
                         "epsilon exploration", cfg=cfg,
                         fallback_depth=fallback_depth, fallback_reason=fallback_reason,
                         confidence_band=_confidence_band(row))

    total = sum(max(1, int(_get_stat(stats[p], "n_fills"))) for p in candidates)
    best = None
    best_score = -1e18
    for profile in candidates:
        row = stats[profile]
        n = max(1, int(_get_stat(row, "n_fills")))
        mean = _mean_reward(row)
        risk = max(0.0, -float(_get_stat(row, "adverse_sum"))) / n / 10_000.0
        score = mean + math.sqrt(2.0 * math.log(total + 1.0) / n) - risk
        if score > best_score:
            best = profile
            best_score = score
    row = stats[str(best)]
    return _decision(str(best), "learned", False, context_key, selected_ctx,
                     int(_get_stat(row, "n_fills")), _mean_reward(row),
                     "ucb best paper reward", cfg=cfg,
                     fallback_depth=fallback_depth, fallback_reason=fallback_reason,
                     confidence_band=_confidence_band(row))


def profile_decision_from_adaptive(episode: RS.RegimeEpisodeState, *, token: str,
                                   active_policy: dict[str, Any],
                                   adaptive: AdaptiveDecision, now_ts: float) -> RS.ProfileDecision:
    profile = PROFILES[adaptive.profile_id]
    base_policy_id = str(active_policy.get("policy_id") or "unknown")
    payload = {
        "episode_id": episode.episode_id,
        "token": token,
        "profile_id": adaptive.profile_id,
        "profile_mode": "active",
        "base_policy_id": base_policy_id,
        "selector_version": adaptive.selector_version,
        "reward_version": adaptive.reward_version,
        "context_key": adaptive.context_key,
    }
    return RS.ProfileDecision(
        profile_decision_id=RS.stable_hash({**payload, "decision_ts": now_ts}, prefix="pd_"),
        episode_id=episode.episode_id,
        token=token,
        outcome=token,
        decision_ts=float(now_ts),
        profile_id=adaptive.profile_id,
        profile_mode="active",
        base_policy_id=base_policy_id,
        profile_policy_hash=RS.stable_hash(payload),
        bid_enabled=profile.bid_enabled,
        ask_enabled=profile.ask_enabled,
        exit_only=profile.exit_only,
        hard_cut=profile.hard_cut,
        margin_used=_float_or_none(active_policy.get("margin")),
        clip_used=_float_or_none(active_policy.get("clip")),
        max_shares_used=_float_or_none(active_policy.get("max_shares")),
        min_edge_used=_float_or_none(active_policy.get("min_edge")),
        quote_ttl_ms=profile.quote_ttl_ms,
        reason=adaptive.reason,
        warnings=(f"selection={adaptive.selection_source}", f"context={adaptive.context_key}"),
    )


def apply_profile(policy: RG.MakerRegimePolicy, knobs: dict[str, float], profile_id: str,
                  *, token_inventory: float) -> tuple[RG.MakerRegimePolicy, dict[str, float]]:
    profile = PROFILES[profile_id]
    next_knobs = {
        "margin": round(max(0.0, knobs["margin"] * profile.margin_multiplier + profile.margin_add), 4),
        "clip": round(max(0.0, knobs["clip"] * profile.clip_multiplier), 2),
        "max_shares": round(max(0.0, knobs["max_shares"] * profile.max_shares_multiplier), 2),
        "min_edge": max(knobs["min_edge"], profile.min_edge),
        "min_seconds_quote": max(knobs["min_seconds_quote"], profile.min_seconds_quote),
    }
    safety_regime = policy.regime_id in {
        "hard_invalid",
        "late_window",
        "book_quarantined",
        "settlement_conflict",
        "inventory_saturated",
    }
    if safety_regime:
        bid_enabled = bool(policy.bid_enabled and profile.bid_enabled)
        ask_enabled = bool(policy.ask_enabled and profile.ask_enabled)
        exit_only = bool(policy.exit_only or profile.exit_only)
    else:
        bid_enabled = bool(profile.bid_enabled)
        ask_enabled = bool(profile.ask_enabled)
        exit_only = bool(profile.exit_only)
    if profile_id in {"reduce_only", "exit_fast"}:
        bid_enabled = False
        ask_enabled = ask_enabled and float(token_inventory or 0.0) > 0.0
    if profile_id == "pause":
        bid_enabled = False
        ask_enabled = False
        exit_only = False
    next_policy = RG.MakerRegimePolicy(
        regime_id=policy.regime_id,
        policy_id=policy.policy_id,
        regime_confidence=policy.regime_confidence,
        margin_multiplier=policy.margin_multiplier,
        margin_add=policy.margin_add,
        clip_multiplier=policy.clip_multiplier,
        max_shares_multiplier=policy.max_shares_multiplier,
        min_edge=next_knobs["min_edge"],
        min_seconds_quote=next_knobs["min_seconds_quote"],
        bid_enabled=bid_enabled,
        ask_enabled=ask_enabled,
        exit_only=exit_only,
        risk_action=profile_id,
        can_hard_cut=bool(policy.can_hard_cut and profile.hard_cut),
        quote_ttl_ms=profile.quote_ttl_ms,
        reason=f"{policy.reason}; adaptive profile {profile_id}".strip("; "),
        warnings=tuple(policy.warnings) + (f"adaptive_profile={profile_id}",),
    )
    return next_policy, next_knobs


def record_decision(c: sqlite3.Connection, *, campaign_id: str, context_key: str, profile_id: str,
                    quote_posted: bool, now_ts: int) -> None:
    upsert_profile_stats(
        c,
        campaign_id=campaign_id,
        context_key=context_key,
        profile_id=profile_id,
        n_decisions=1,
        n_quotes=1 if quote_posted else 0,
        now_ts=now_ts,
    )


def update_rewards_for_settled_window(c: sqlite3.Connection, *, campaign_id: str,
                                      condition_id: Optional[str], window_start_ts: Optional[int],
                                      winning_outcome: str, now_ts: int) -> int:
    ensure_adaptive_tables(c)
    cfg = _campaign_learner_config(c, campaign_id)
    where = ["f.campaign_id=?", "r.id IS NULL"]
    params: list[Any] = [campaign_id]
    if condition_id is not None:
        where.append("f.condition_id=?")
        params.append(condition_id)
    if window_start_ts is not None:
        where.append("f.window_start_ts=?")
        params.append(window_start_ts)
    rows = c.execute(
        f"""SELECT f.*
            FROM maker_fills f
            LEFT JOIN crypto5m_maker_adaptive_rewards r
              ON r.campaign_id=f.campaign_id AND r.fill_id=f.id AND r.reward_version=?
            WHERE {' AND '.join(where)}
            ORDER BY f.id""",
        [cfg.reward_version, *params],
    ).fetchall()
    inserted = 0
    for row in rows:
        profile_id = row["fill_profile_id"] or row["profile_id"] or "unknown"
        if profile_id == "unknown":
            continue
        pnl = settled_fill_pnl(row["side"], row["token"], row["price"], row["size"], winning_outcome)
        hard_cut_penalty = 1.0 if (row["fill_model"] or "") == "hard-cut" else 0.0
        markouts = _reward_markouts(c, row) if cfg.learner_version == "v2" else _empty_markouts()
        adverse_penalty = 0.0
        if cfg.learner_version == "v2" and markouts["adverse_selection_10s_bps"] is not None:
            adverse_penalty = (
                max(0.0, -float(markouts["adverse_selection_10s_bps"])) / 10_000.0 * float(row["size"] or 0.0)
            )
        concentration_penalty = 0.0
        reward = pnl - adverse_penalty - hard_cut_penalty - concentration_penalty
        ctx = context_key_label(row["moment_key"])
        cur = c.execute(
            """INSERT OR IGNORE INTO crypto5m_maker_adaptive_rewards(
                 campaign_id,condition_id,window_start_ts,fill_id,episode_id,moment_key,context_key,profile_id,
                 reward,fee_adjusted_pnl,adverse_selection_10s_bps,inventory_carry_penalty,hard_cut_penalty,
                 reward_version,ts,settled_fill_pnl,adverse_selection_3s_bps,adverse_selection_30s_bps,
                 future_executable_unwind_10s,shadow_quality,concentration_penalty)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, row["condition_id"], row["window_start_ts"], row["id"], row["episode_id"],
             row["moment_key"], ctx, profile_id, reward, pnl, markouts["adverse_selection_10s_bps"], 0.0,
             hard_cut_penalty,
             cfg.reward_version, int(now_ts), pnl, markouts["adverse_selection_3s_bps"],
             markouts["adverse_selection_30s_bps"], markouts["future_executable_unwind_10s"],
             markouts["shadow_quality"], concentration_penalty),
        )
        if cur.rowcount:
            inserted += 1
            upsert_profile_stats(
                c,
                campaign_id=campaign_id,
                context_key=ctx,
                profile_id=profile_id,
                n_fills=1,
                reward_sum=reward,
                reward_sq_sum=reward * reward,
                pnl_sum=pnl,
                adverse_sum=float(markouts["adverse_selection_10s_bps"] or 0.0),
                hard_cuts=1 if hard_cut_penalty else 0,
                now_ts=now_ts,
            )
            _maybe_disable(c, campaign_id, ctx, profile_id, now_ts, learner_version=cfg.learner_version)
            _maybe_promote(c, campaign_id, ctx, profile_id, now_ts, learner_version=cfg.learner_version)
    c.commit()
    return inserted


def settled_fill_pnl(side: str, token: str, price: float, size: float, winning_outcome: str) -> float:
    up_won = str(winning_outcome).lower().startswith("up")
    token_won = (token == "up" and up_won) or (token == "down" and not up_won)
    payoff = 1.0 if token_won else 0.0
    if side == "bid":
        return (payoff - float(price)) * float(size)
    return (float(price) - payoff) * float(size)


def adaptive_meta(adaptive: AdaptiveDecision | None) -> dict[str, Any]:
    if adaptive is None:
        return {
            "selector_version": None,
            "reward_version": None,
            "selection_mode": None,
            "exploration_flag": None,
            "profile_disabled_flag": None,
            "parent_context_used": None,
            "adaptive_context_key": None,
            "fallback_depth": None,
            "fallback_reason": None,
            "exploration_budget_remaining": None,
            "adaptive_status": None,
        }
    return {
        "selector_version": adaptive.selector_version,
        "reward_version": adaptive.reward_version,
        "selection_mode": adaptive.selection_source,
        "exploration_flag": int(adaptive.exploration_flag),
        "profile_disabled_flag": int(adaptive.profile_disabled_flag),
        "parent_context_used": adaptive.parent_context_used,
        "adaptive_context_key": adaptive.context_key,
        "fallback_depth": adaptive.fallback_depth,
        "fallback_reason": adaptive.fallback_reason,
        "exploration_budget_remaining": adaptive.exploration_budget_remaining,
        "adaptive_status": adaptive.adaptive_status,
    }


def _maybe_disable(c: sqlite3.Connection, campaign_id: str, context_key: str, profile_id: str,
                   now_ts: int, *, learner_version: str = "v1") -> None:
    cfg = learner_config(learner_version)
    row = c.execute(
        """SELECT n_fills,reward_sum,reward_sq_sum,pnl_sum,adverse_sum,disabled
           FROM crypto5m_maker_profile_stats
           WHERE campaign_id=? AND context_key=? AND profile_id=?""",
        (campaign_id, context_key, profile_id),
    ).fetchone()
    if row is None or row["disabled"]:
        return
    n = int(row["n_fills"] or 0)
    mean_reward = float(row["reward_sum"] or 0.0) / max(1, n)
    mean_pnl = float(row["pnl_sum"] or 0.0) / max(1, n)
    band = _confidence_band(row)
    if cfg.learner_version == "v1":
        should_disable = n >= 30 and mean_reward < 0 and mean_pnl < 0
        reason = f"n_fills={n} mean_reward={mean_reward:.4f} mean_pnl={mean_pnl:.4f}"
    else:
        mean_adverse = float(row["adverse_sum"] or 0.0) / max(1, n)
        max_share = _max_window_fill_share(c, campaign_id, context_key, profile_id, cfg.reward_version)
        c.execute(
            """UPDATE crypto5m_maker_profile_stats SET confidence_low=?,confidence_high=?,
               max_window_fill_share=?,status=COALESCE(status,'insufficient_sample')
               WHERE campaign_id=? AND context_key=? AND profile_id=?""",
            (band[0], band[1], max_share, campaign_id, context_key, profile_id),
        )
        should_disable = (
            n >= 30
            and mean_reward < 0
            and mean_adverse < -50
            and band[0] is not None
            and band[0] < 0
            and max_share <= 0.50
        )
        ci_low = "none" if band[0] is None else f"{band[0]:.4f}"
        reason = (
            f"n_fills={n} mean_reward={mean_reward:.4f} mean_adverse_10s_bps={mean_adverse:.1f} "
            f"ci_low={ci_low} max_window_fill_share={max_share:.2f}"
        )
    if should_disable:
        disable_profile(
            c,
            campaign_id=campaign_id,
            context_key=context_key,
            profile_id=profile_id,
            now_ts=now_ts,
            reason=reason,
            learner_version=cfg.learner_version,
            sample_counts={"n_fills": n},
            confidence_band=band,
        )


def _maybe_promote(c: sqlite3.Connection, campaign_id: str, context_key: str, profile_id: str,
                   now_ts: int, *, learner_version: str = "v1") -> None:
    cfg = learner_config(learner_version)
    if cfg.learner_version != "v2" or profile_id == "quote_base":
        return
    row = c.execute(
        """SELECT n_fills,reward_sum,adverse_sum,disabled
           FROM crypto5m_maker_profile_stats
           WHERE campaign_id=? AND context_key=? AND profile_id=?""",
        (campaign_id, context_key, profile_id),
    ).fetchone()
    base = c.execute(
        """SELECT n_fills,reward_sum
           FROM crypto5m_maker_profile_stats
           WHERE campaign_id=? AND context_key=? AND profile_id='quote_base'""",
        (campaign_id, context_key),
    ).fetchone()
    if row is None or row["disabled"] or base is None:
        return
    n = int(row["n_fills"] or 0)
    mean = float(row["reward_sum"] or 0.0) / max(1, n)
    base_n = int(base["n_fills"] or 0)
    base_mean = float(base["reward_sum"] or 0.0) / max(1, base_n)
    mean_adverse = float(row["adverse_sum"] or 0.0) / max(1, n)
    max_share = _max_window_fill_share(c, campaign_id, context_key, profile_id, cfg.reward_version)
    if n >= 30 and base_n >= 30 and mean > base_mean and mean_adverse >= -50 and max_share <= 0.50:
        c.execute(
            """UPDATE crypto5m_maker_profile_stats SET status='paper_preferred',last_updated_ts=?
               WHERE campaign_id=? AND context_key=? AND profile_id=?""",
            (int(now_ts), campaign_id, context_key, profile_id),
        )


def _campaign_learner_config(c: sqlite3.Connection, campaign_id: str) -> LearnerConfig:
    try:
        row = c.execute(
            """SELECT selector_version,reward_version,config_json
               FROM crypto5m_maker_adaptive_campaigns
               WHERE campaign_id=?""",
            (campaign_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return LEARNER_CONFIGS["v1"]
    if row is None:
        return LEARNER_CONFIGS["v1"]
    if row["reward_version"] == REWARD_VERSION_V2 or row["selector_version"] == SELECTOR_VERSION_V2:
        return LEARNER_CONFIGS["v2"]
    try:
        cfg_json = json.loads(row["config_json"] or "{}")
    except json.JSONDecodeError:
        cfg_json = {}
    return learner_config(cfg_json.get("learner_version") or row["selector_version"] or "v1")


def _empty_markouts() -> dict[str, Any]:
    return {
        "adverse_selection_3s_bps": None,
        "adverse_selection_10s_bps": None,
        "adverse_selection_30s_bps": None,
        "future_executable_unwind_10s": None,
        "shadow_quality": None,
    }


def _reward_markouts(c: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    values = {}
    for horizon in (3, 10, 30):
        q = _future_quote_snapshot(c, row, horizon)
        values[horizon] = _fill_direction_markout_bps(row, q) if q is not None else None
    known = [v for v in values.values() if v is not None]
    quality = "not_evaluable" if not known else ("exact" if len(known) == 3 else "approximate")
    future_unwind_10s = None
    if values[10] is not None:
        future_unwind_10s = float(values[10]) / 10_000.0 * float(row["size"] or 0.0)
    return {
        "adverse_selection_3s_bps": values[3],
        "adverse_selection_10s_bps": values[10],
        "adverse_selection_30s_bps": values[30],
        "future_executable_unwind_10s": future_unwind_10s,
        "shadow_quality": quality,
    }


def _future_quote_snapshot(c: sqlite3.Connection, row: sqlite3.Row, horizon_s: int) -> sqlite3.Row | None:
    try:
        return c.execute(
            """SELECT best_bid,best_ask,fair,ts
               FROM maker_quotes
               WHERE campaign_id=? AND condition_id=? AND window_start_ts=? AND token=? AND ts>=?
               ORDER BY ts ASC,id ASC LIMIT 1""",
            (
                row["campaign_id"],
                row["condition_id"],
                row["window_start_ts"],
                row["token"],
                int(row["ts"] or 0) + int(horizon_s),
            ),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _fill_direction_markout_bps(fill: sqlite3.Row, quote: sqlite3.Row | None) -> Optional[float]:
    if quote is None:
        return None
    side = str(fill["side"] or "")
    if side == "bid":
        exit_price = quote["best_bid"]
        if exit_price is None:
            return None
        return (float(exit_price) - float(fill["price"])) * 10_000.0
    exit_price = quote["best_ask"]
    if exit_price is None:
        return None
    return (float(fill["price"]) - float(exit_price)) * 10_000.0


def _max_window_fill_share(c: sqlite3.Connection, campaign_id: str, context_key: str, profile_id: str,
                           reward_version: str) -> float:
    rows = c.execute(
        """SELECT window_start_ts,COUNT(*) AS n
           FROM crypto5m_maker_adaptive_rewards
           WHERE campaign_id=? AND context_key=? AND profile_id=? AND reward_version=?
           GROUP BY window_start_ts""",
        (campaign_id, context_key, profile_id, reward_version),
    ).fetchall()
    total = sum(int(r["n"] or 0) for r in rows)
    if total <= 0:
        return 0.0
    return max(int(r["n"] or 0) for r in rows) / total


def _enabled_profiles(c: sqlite3.Connection, campaign_id: str, context_key: str) -> list[str]:
    disabled = {
        r["profile_id"] for r in c.execute(
            """SELECT profile_id FROM crypto5m_maker_profile_stats
               WHERE campaign_id=? AND context_key=? AND disabled=1""",
            (campaign_id, context_key),
        ).fetchall()
    }
    return [p for p in PROFILE_IDS if p not in disabled]


def _candidate_profiles(enabled: list[str], *, token_inventory: float) -> list[str]:
    if token_inventory <= 0:
        allowed = {"quote_tight", "quote_base", "quote_wide", "normal_small"}
    else:
        allowed = {"quote_tight", "quote_base", "quote_wide", "normal_small", "reduce_only", "exit_fast"}
    return [p for p in PROFILE_IDS if p in enabled and p in allowed]


def _first_profile(profiles: list[str]) -> str:
    for profile in PROFILE_IDS:
        if profile in profiles:
            return profile
    return profiles[0]


def _stats_rows(c: sqlite3.Connection, campaign_id: str, context_key: str) -> list[sqlite3.Row]:
    return c.execute(
        """SELECT * FROM crypto5m_maker_profile_stats
           WHERE campaign_id=? AND context_key=?""",
        (campaign_id, context_key),
    ).fetchall()


def _empty_stat(profile_id: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "n_fills": 0,
        "reward_sum": 0.0,
        "adverse_sum": 0.0,
        "disabled": 0,
    }


def _mean_reward(row: Any) -> float:
    n = int(_get_stat(row, "n_fills"))
    if n <= 0:
        return 0.0
    return float(_get_stat(row, "reward_sum")) / n


def _confidence_band(row: Any) -> tuple[Optional[float], Optional[float]]:
    n = int(_get_stat(row, "n_fills"))
    if n < 2:
        return (None, None)
    reward_sum = float(_get_stat(row, "reward_sum"))
    reward_sq = float(_get_stat(row, "reward_sq_sum"))
    mean = reward_sum / n
    variance = max(0.0, (reward_sq - (reward_sum * reward_sum / n)) / (n - 1))
    half = 1.96 * math.sqrt(variance / n)
    return (mean - half, mean + half)


def _fallback_reason(depth: int, context_key: str) -> str:
    if depth == 0:
        return "full context"
    if "|*|" in f"|{context_key}|":
        fields = context_key.split("|")
        dropped = [CONTEXT_FIELDS[i] for i, v in enumerate(fields) if v == "*" and i < len(CONTEXT_FIELDS)]
        if dropped:
            return "drop " + ",".join(dropped)
    if context_key.startswith("regime:"):
        return "regime fallback"
    return "global fallback"


def _get_stat(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key) or 0
    return row[key] if row[key] is not None else 0


def _decision(profile_id: str, source: str, explore: bool, context_key: str, parent_ctx: str,
              n_fills: int, mean_reward: float, reason: str, *, cfg: LearnerConfig | None = None,
              fallback_depth: int = 0, fallback_reason: str = "full context",
              exploration_budget_remaining: float = 0.0,
              confidence_band: tuple[Optional[float], Optional[float]] = (None, None),
              adaptive_status: str = "collecting") -> AdaptiveDecision:
    cfg = cfg or LEARNER_CONFIGS["v1"]
    return AdaptiveDecision(
        profile_id=profile_id,
        selector_version=cfg.selector_version,
        reward_version=cfg.reward_version,
        selection_source=source,
        exploration_flag=explore,
        profile_disabled_flag=False,
        parent_context_used=parent_ctx,
        context_key=context_key,
        n_fills=n_fills,
        mean_reward=mean_reward,
        reason=reason,
        fallback_depth=fallback_depth,
        fallback_reason=fallback_reason,
        exploration_budget_remaining=max(0.0, float(exploration_budget_remaining or 0.0)),
        confidence_low=confidence_band[0],
        confidence_high=confidence_band[1],
        adaptive_status=adaptive_status,
    )


def _as_dict(payload: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return {}


def _add_column(c: sqlite3.Connection, table: str, col: str, typ: str) -> None:
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    except sqlite3.OperationalError:
        pass


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
