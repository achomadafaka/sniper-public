"""Adaptive Maker Brain V3 for the crypto5m paper maker.

Paper only. This module is pure local compute plus SQLite paper telemetry:
no network, no order placement, no private-key imports.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import random
import sqlite3
from typing import Any, Optional

from sniper.algos.polymarket_crypto5m import maker_adaptive as AD
from sniper.algos.polymarket_crypto5m import maker_regime as RG


LEARNER_VERSION = "v3.1"
SELECTOR_VERSION = "adaptive-maker-brain-v3.1"
REWARD_VERSION = "maker-reward-v3.1"
# v3.2: same learner/selector, reward additionally charges the 10s adverse-selection markout
# (max(0, -markout_bps)/1e4 * size — the V2 formula, via maker_adaptive._reward_markouts).
REWARD_VERSION_V32 = "maker-reward-v3.2"
FAIR_MODEL_ID = "logit_blend_v1"

FAIR_BLEND_ARMS: dict[str, float] = {
    "market_only": 0.0,
    "blend_25": 0.25,
    "blend_50": 0.50,
    "blend_75": 0.75,
    "structural_only": 1.0,
}

CATASTROPHIC_DISABLE_MIN_FILLS = 10
CATASTROPHIC_DISABLE_MEAN_REWARD = -0.50
NORMAL_DISABLE_MIN_FILLS = 30
PROMOTE_MIN_FILLS = 30
MAX_WINDOW_CONCENTRATION = 0.50
COOLDOWN_RETEST_SECONDS = 6 * 3600

CONTEXT_FIELDS = (
    "window_phase",
    "odds_bucket",
    "volatility_state",
    "inventory_state",
    "spread_state",
    "direction_state",
    "book_health",
)

FAIR_CONTEXT_FIELDS = (
    "seconds_bucket",
    "odds_bucket",
    "volatility_state",
    "jump_state",
    "source_health",
    "spread_state",
    "disagreement_bucket",
    "book_health",
)


@dataclass(frozen=True)
class FairDecision:
    q_structural: Optional[float]
    q_market_mid: Optional[float]
    q_used: float
    blend_weight: float
    residual_adjustment: float
    fair_confidence: str
    fair_source: str
    fair_model_id: str = FAIR_MODEL_ID
    fair_context_key: str = "unknown"


@dataclass(frozen=True)
class TailRiskDecision:
    risk_state: str
    tail_risk_score: float
    transition_risk_score: float
    forced_arm: Optional[str]
    reason: str


@dataclass(frozen=True)
class ActionArm:
    arm_id: str
    margin_multiplier: float = 1.0
    margin_add_bps: float = 0.0
    clip_usdc: float = 8.0
    clip_shares_cap: float = 18.0
    max_inventory_multiplier: float = 1.0
    quote_ttl_ms: int = 10_000
    min_edge_bps: float = 400.0
    bid_enabled: bool = True
    ask_enabled: bool = True
    exit_only: bool = False
    hard_cut: bool = True


@dataclass(frozen=True)
class ActionDecision:
    arm_id: str
    selector_version: str
    reward_version: str
    context_key: str
    parent_context_used: str
    fallback_depth: int
    selection_source: str
    exploration_flag: bool
    profile_disabled_flag: bool
    n_fills: int
    mean_reward: float
    confidence_low: Optional[float]
    confidence_high: Optional[float]
    fair_confidence: str
    risk_state: str
    reason: str


ACTION_ARMS: dict[str, ActionArm] = {
    "pause": ActionArm(
        "pause",
        clip_usdc=0.0,
        clip_shares_cap=0.0,
        max_inventory_multiplier=0.0,
        quote_ttl_ms=0,
        bid_enabled=False,
        ask_enabled=False,
        exit_only=False,
        hard_cut=False,
    ),
    "reduce_only": ActionArm(
        "reduce_only",
        margin_multiplier=1.20,
        clip_usdc=5.0,
        clip_shares_cap=10.0,
        max_inventory_multiplier=0.50,
        quote_ttl_ms=5_000,
        bid_enabled=False,
        ask_enabled=True,
        exit_only=True,
    ),
    "exit_fast": ActionArm(
        "exit_fast",
        margin_multiplier=0.75,
        clip_usdc=8.0,
        clip_shares_cap=16.0,
        max_inventory_multiplier=0.25,
        quote_ttl_ms=3_000,
        min_edge_bps=300.0,
        bid_enabled=False,
        ask_enabled=True,
        exit_only=True,
    ),
    "tight_small_fast": ActionArm(
        "tight_small_fast",
        margin_multiplier=0.70,
        clip_usdc=6.0,
        clip_shares_cap=12.0,
        max_inventory_multiplier=0.70,
        quote_ttl_ms=4_000,
        min_edge_bps=300.0,
    ),
    "base_small": ActionArm(
        "base_small",
        margin_multiplier=1.0,
        clip_usdc=8.0,
        clip_shares_cap=16.0,
        max_inventory_multiplier=0.80,
        quote_ttl_ms=9_000,
        min_edge_bps=400.0,
    ),
    "wide_small": ActionArm(
        "wide_small",
        margin_multiplier=1.70,
        margin_add_bps=50.0,
        clip_usdc=6.0,
        clip_shares_cap=12.0,
        max_inventory_multiplier=0.65,
        quote_ttl_ms=6_000,
        min_edge_bps=600.0,
    ),
    "wide_tiny": ActionArm(
        "wide_tiny",
        margin_multiplier=2.25,
        margin_add_bps=90.0,
        clip_usdc=3.0,
        clip_shares_cap=6.0,
        max_inventory_multiplier=0.35,
        quote_ttl_ms=4_000,
        min_edge_bps=800.0,
    ),
    "normal_small": ActionArm(
        "normal_small",
        margin_multiplier=1.15,
        clip_usdc=7.0,
        clip_shares_cap=14.0,
        max_inventory_multiplier=0.70,
        quote_ttl_ms=8_000,
        min_edge_bps=450.0,
    ),
}

ACTION_ORDER = (
    "pause",
    "reduce_only",
    "exit_fast",
    "tight_small_fast",
    "base_small",
    "wide_small",
    "wide_tiny",
    "normal_small",
)

NORMAL_ACTION_ORDER = (
    "base_small",
    "wide_small",
    "tight_small_fast",
    "wide_tiny",
    "normal_small",
)


def ensure_v3_tables(c: sqlite3.Connection) -> None:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_campaigns(
        campaign_id TEXT PRIMARY KEY,
        learner_version TEXT,
        selector_version TEXT,
        reward_version TEXT,
        control_campaign TEXT,
        started_ts INTEGER,
        config_json TEXT
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_fair_blend_stats(
        campaign_id TEXT,
        fair_context_key TEXT,
        blend_weight REAL,
        residual_adjustment REAL,
        n_observations INTEGER DEFAULT 0,
        reward_sum REAL DEFAULT 0,
        brier_sum REAL DEFAULT 0,
        log_loss_sum REAL DEFAULT 0,
        calibration_abs_sum REAL DEFAULT 0,
        settled_correct_sum INTEGER DEFAULT 0,
        best_blend_arm TEXT,
        arm_metrics_json TEXT,
        last_updated_ts INTEGER,
        PRIMARY KEY(campaign_id, fair_context_key)
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_action_arm_stats(
        campaign_id TEXT,
        context_key TEXT,
        arm_id TEXT,
        n_decisions INTEGER DEFAULT 0,
        n_quotes INTEGER DEFAULT 0,
        n_fills INTEGER DEFAULT 0,
        reward_sum REAL DEFAULT 0,
        reward_sq_sum REAL DEFAULT 0,
        adverse_sum REAL DEFAULT 0,
        disabled INTEGER DEFAULT 0,
        paper_preferred INTEGER DEFAULT 0,
        status TEXT,
        disable_type TEXT,
        disabled_reason TEXT,
        disabled_at INTEGER,
        paper_preferred_reason TEXT,
        paper_preferred_at INTEGER,
        hard_cuts INTEGER DEFAULT 0,
        max_window_fill_share REAL,
        confidence_low REAL,
        confidence_high REAL,
        last_updated_ts INTEGER,
        PRIMARY KEY(campaign_id, context_key, arm_id)
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_tail_risk_stats(
        campaign_id TEXT,
        context_key TEXT,
        risk_state TEXT,
        n_decisions INTEGER DEFAULT 0,
        forced_count INTEGER DEFAULT 0,
        reward_sum REAL DEFAULT 0,
        last_updated_ts INTEGER,
        PRIMARY KEY(campaign_id, context_key, risk_state)
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_policy_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        learner_version TEXT,
        selector_version TEXT,
        reward_version TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        token TEXT,
        context_key TEXT,
        fair_context_key TEXT,
        parent_context_used TEXT,
        fallback_depth INTEGER,
        fair_model_id TEXT,
        q_structural REAL,
        q_market_mid REAL,
        q_used REAL,
        fair_blend_weight REAL,
        residual_adjustment REAL,
        fair_confidence TEXT,
        risk_state TEXT,
        tail_risk_score REAL,
        quote_action_arm TEXT,
        selector_source TEXT,
        exploration_flag INTEGER,
        policy_action TEXT,
        disabled_profile_flag INTEGER,
        created_ts INTEGER
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_rewards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        fill_id INTEGER,
        token TEXT,
        context_key TEXT,
        quote_action_arm TEXT,
        reward REAL,
        fee_adjusted_pnl REAL,
        adverse_selection_10s_bps REAL,
        inventory_carry_penalty REAL,
        hard_cut_penalty REAL,
        reward_version TEXT,
        created_ts INTEGER
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_fair_blend_observations(
        campaign_id TEXT,
        policy_action_id INTEGER,
        reward_version TEXT,
        created_ts INTEGER,
        PRIMARY KEY(campaign_id, policy_action_id, reward_version)
      );
      CREATE TABLE IF NOT EXISTS crypto5m_maker_v3_ablations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT,
        condition_id TEXT,
        window_start_ts INTEGER,
        token TEXT,
        context_key TEXT,
        ablation_group TEXT,
        selected_arm TEXT,
        q_used REAL,
        risk_state TEXT,
        created_ts INTEGER
      );
    """)
    _add_column(c, "crypto5m_maker_v3_rewards", "fill_id", "INTEGER")
    for col, typ in (
        ("brier_sum", "REAL DEFAULT 0"),
        ("log_loss_sum", "REAL DEFAULT 0"),
        ("calibration_abs_sum", "REAL DEFAULT 0"),
        ("settled_correct_sum", "INTEGER DEFAULT 0"),
        ("best_blend_arm", "TEXT"),
        ("arm_metrics_json", "TEXT"),
    ):
        _add_column(c, "crypto5m_maker_v3_fair_blend_stats", col, typ)
    for col, typ in (
        ("status", "TEXT"),
        ("disable_type", "TEXT"),
        ("disabled_reason", "TEXT"),
        ("disabled_at", "INTEGER"),
        ("paper_preferred_reason", "TEXT"),
        ("paper_preferred_at", "INTEGER"),
        ("hard_cuts", "INTEGER DEFAULT 0"),
        ("max_window_fill_share", "REAL"),
        ("confidence_low", "REAL"),
        ("confidence_high", "REAL"),
    ):
        _add_column(c, "crypto5m_maker_v3_action_arm_stats", col, typ)
    _add_column(c, "crypto5m_maker_v3_policy_actions", "fair_context_key", "TEXT")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_v3_rewards_fill "
        "ON crypto5m_maker_v3_rewards(campaign_id, fill_id, reward_version)"
    )


def register_campaign(c: sqlite3.Connection, campaign_id: str, control_campaign: str,
                      now_ts: int, config: dict[str, Any] | None,
                      reward_version: str | None = None) -> None:
    ensure_v3_tables(c)
    payload = {"learner_version": LEARNER_VERSION, **dict(config or {})}
    c.execute(
        """INSERT INTO crypto5m_maker_v3_campaigns(
             campaign_id,learner_version,selector_version,reward_version,control_campaign,started_ts,config_json)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET
             selector_version=excluded.selector_version,
             reward_version=excluded.reward_version,
             control_campaign=excluded.control_campaign,
             config_json=excluded.config_json""",
        (
            campaign_id,
            LEARNER_VERSION,
            SELECTOR_VERSION,
            reward_version or REWARD_VERSION,
            control_campaign,
            int(now_ts),
            json.dumps(payload, sort_keys=True),
        ),
    )


def _campaign_reward_version(c: sqlite3.Connection, campaign_id: str) -> str:
    row = c.execute(
        "SELECT reward_version FROM crypto5m_maker_v3_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    return str(row["reward_version"]) if row and row["reward_version"] else REWARD_VERSION


def campaign_frozen(c: sqlite3.Connection, campaign_id: str) -> bool:
    """True when the campaign registered with config {"frozen": true} — the DB is the source of
    truth so settlement-time reward processing needs no runner flag threading."""
    row = c.execute(
        "SELECT config_json FROM crypto5m_maker_v3_campaigns WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if not row or not row["config_json"]:
        return False
    try:
        return bool(json.loads(row["config_json"]).get("frozen"))
    except (ValueError, TypeError):
        return False


def fair_context_key(context: dict[str, Any] | str | None) -> str:
    if isinstance(context, str):
        return context
    context = context or {}
    return "|".join(str(context.get(field) or "unknown") for field in FAIR_CONTEXT_FIELDS)


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
        contexts.append("|".join(data[field] for field in CONTEXT_FIELDS))
    contexts.extend((f"regime:{regime_id}", "global"))
    out: list[str] = []
    for ctx in contexts:
        if ctx not in out:
            out.append(ctx)
    return out


def logit_blend(q_structural: float, q_market: float, *, weight: float,
                residual: float = 0.0) -> float:
    w = min(1.0, max(0.0, float(weight)))
    z = w * _logit(q_structural) + (1.0 - w) * _logit(q_market) + float(residual or 0.0)
    return _clamp_prob(_sigmoid(z))


def choose_fair_value(c: sqlite3.Connection, *, campaign_id: str,
                      q_structural: Optional[float], q_market_mid: Optional[float],
                      context: dict[str, Any] | str | None, now_ts: int,
                      frozen: bool = False) -> FairDecision:
    ensure_v3_tables(c)
    key = fair_context_key(context)
    structural = _clean_prob(q_structural)
    market = _clean_prob(q_market_mid)
    if structural is None and market is None:
        return FairDecision(None, None, 0.50, 0.0, 0.0, "low", "unavailable", fair_context_key=key)
    if structural is None:
        return FairDecision(None, market, float(market), 0.0, 0.0, "low", "fallback_market", fair_context_key=key)
    if market is None:
        return FairDecision(structural, None, float(structural), 1.0, 0.0, "low", "structural_only", fair_context_key=key)

    # frozen-learner mode: never read learned blend stats — behave exactly like an
    # empty-DB launch (default blend weight, zero residual) for the campaign's whole life.
    row = None if frozen else c.execute(
        """SELECT blend_weight,residual_adjustment,n_observations
           FROM crypto5m_maker_v3_fair_blend_stats
           WHERE campaign_id=? AND fair_context_key=?""",
        (campaign_id, key),
    ).fetchone()
    if row:
        weight = min(1.0, max(0.0, float(row["blend_weight"])))
        residual = float(row["residual_adjustment"] or 0.0)
        observations = int(row["n_observations"] or 0)
    else:
        weight = _default_blend_weight(context, structural, market)
        residual = 0.0
        observations = 0
    q_used = logit_blend(structural, market, weight=weight, residual=residual)
    disagreement = abs(structural - market)
    confidence = "high" if observations >= 50 and disagreement < 0.05 else "medium"
    if disagreement >= 0.20 or observations < 10:
        confidence = "low" if row else "medium"
    return FairDecision(
        structural,
        market,
        q_used,
        weight,
        residual,
        confidence,
        "blended",
        fair_context_key=key,
    )


def govern_tail_risk(*, regime_id: str, moment_key: dict[str, Any] | str | None,
                     token_inventory: float, seconds_left: float,
                     fair_confidence: str, transition_state: str) -> TailRiskDecision:
    data = _as_dict(moment_key)
    book = str(data.get("book_health") or data.get("book_state") or "healthy")
    phase = str(data.get("window_phase") or "")
    inv = abs(float(token_inventory or 0.0))
    left = float(seconds_left or 0.0)
    transition = str(transition_state or "stable")

    if regime_id == "hard_invalid" or book in {"missing", "stale", "quarantined"}:
        return TailRiskDecision("pause", 1.0, 1.0, "pause", "hard invalid or bad book")
    if regime_id in {"settlement_conflict", "book_quarantined"}:
        return TailRiskDecision("pause", 1.0, 1.0, "pause", f"{regime_id} safety override")
    if regime_id == "late_window" or phase in {"late_window", "final_seconds"} or left <= 20:
        if inv > 0:
            return TailRiskDecision("reduce_only", 0.85, 0.55, "reduce_only", "late window inventory reduction")
        return TailRiskDecision("pause", 0.85, 0.55, "pause", "late window no new inventory")
    if regime_id == "inventory_saturated":
        return TailRiskDecision("reduce_only", 0.80, 0.35, "reduce_only", "inventory saturated")
    if transition in {"fresh_transition", "unstable"} or regime_id == "jump":
        return TailRiskDecision("caution", 0.65, 0.70, None, "jump or transition risk")
    if fair_confidence == "low":
        return TailRiskDecision("caution", 0.45, 0.20, None, "low fair confidence")
    return TailRiskDecision("normal", 0.10, 0.0, None, "normal paper risk")


def upsert_action_arm_stats(c: sqlite3.Connection, *, campaign_id: str, context_key: str,
                            arm_id: str, n_decisions: int = 0, n_quotes: int = 0,
                            n_fills: int = 0, reward_sum: float = 0.0,
                            reward_sq_sum: float = 0.0, adverse_sum: float = 0.0,
                            hard_cuts: int = 0,
                            disabled: int = 0, paper_preferred: int = 0,
                            now_ts: int = 0) -> None:
    ensure_v3_tables(c)
    c.execute(
        """INSERT INTO crypto5m_maker_v3_action_arm_stats(
             campaign_id,context_key,arm_id,n_decisions,n_quotes,n_fills,reward_sum,reward_sq_sum,
             adverse_sum,disabled,paper_preferred,hard_cuts,status,last_updated_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id,context_key,arm_id) DO UPDATE SET
             n_decisions=crypto5m_maker_v3_action_arm_stats.n_decisions + excluded.n_decisions,
             n_quotes=crypto5m_maker_v3_action_arm_stats.n_quotes + excluded.n_quotes,
             n_fills=crypto5m_maker_v3_action_arm_stats.n_fills + excluded.n_fills,
             reward_sum=crypto5m_maker_v3_action_arm_stats.reward_sum + excluded.reward_sum,
             reward_sq_sum=crypto5m_maker_v3_action_arm_stats.reward_sq_sum + excluded.reward_sq_sum,
             adverse_sum=crypto5m_maker_v3_action_arm_stats.adverse_sum + excluded.adverse_sum,
             hard_cuts=crypto5m_maker_v3_action_arm_stats.hard_cuts + excluded.hard_cuts,
             disabled=max(crypto5m_maker_v3_action_arm_stats.disabled, excluded.disabled),
             paper_preferred=max(crypto5m_maker_v3_action_arm_stats.paper_preferred, excluded.paper_preferred),
             status=COALESCE(crypto5m_maker_v3_action_arm_stats.status, excluded.status),
             last_updated_ts=excluded.last_updated_ts""",
        (
            campaign_id,
            context_key,
            arm_id,
            int(n_decisions),
            int(n_quotes),
            int(n_fills),
            float(reward_sum),
            float(reward_sq_sum),
            float(adverse_sum),
            int(disabled),
            int(paper_preferred),
            int(hard_cuts),
            "insufficient_sample",
            int(now_ts or 0),
        ),
    )


def select_action_arm(c: sqlite3.Connection, *, campaign_id: str, context_key: str,
                      regime_id: str, fair_confidence: str, risk_state: str,
                      token_inventory: float, rng: random.Random,
                      epsilon: float = 0.10, now_ts: int = 0,
                      frozen: bool = False) -> ActionDecision:
    ensure_v3_tables(c)
    forced = _forced_arm(regime_id, risk_state, token_inventory)
    if forced:
        return _action_decision(
            forced,
            context_key,
            context_key,
            0,
            "safety_override",
            False,
            False,
            0,
            0.0,
            None,
            None,
            fair_confidence,
            risk_state,
            f"{risk_state} safety override",
        )

    if frozen:
        # frozen-learner mode: the launch-prior policy, held constant. With no learned rows an
        # empty-DB campaign deterministically resolves to the first eligible arm in
        # NORMAL_ACTION_ORDER (all UCB scores tie at n=0) — reproduce that directly, with no
        # exploration, no learned-stats reads, and no disable/prefer gates. Safety overrides
        # above still apply: they are engine risk logic, not learner state.
        arm = _candidate_arms(token_inventory)[0]
        return _action_decision(
            arm,
            context_key,
            "frozen",
            0,
            "frozen_prior",
            False,
            False,
            0,
            0.0,
            None,
            None,
            fair_confidence,
            risk_state,
            "frozen launch-prior policy",
        )

    candidates = _candidate_arms(token_inventory)
    exact_rows = _arm_stats(c, campaign_id, context_key)
    exact_disabled = {
        r["arm_id"]
        for r in exact_rows
        if int(r["disabled"] or 0) and not _cooldown_retest_due(r, now_ts)
    }
    candidates = [arm for arm in candidates if arm not in exact_disabled]
    if not candidates:
        return _action_decision(
            "pause",
            context_key,
            context_key,
            0,
            "disabled_filter",
            False,
            False,
            0,
            0.0,
            None,
            None,
            fair_confidence,
            risk_state,
            "all eligible arms disabled in context",
        )
    selected_ctx = context_key
    fallback_depth = 0
    stats: dict[str, sqlite3.Row | dict[str, Any]] = {arm: _empty_stat(arm) for arm in candidates}
    found = False
    for depth, ctx in enumerate(parent_contexts(context_key, regime_id)):
        rows = _arm_stats(c, campaign_id, ctx)
        available = {
            r["arm_id"]: r
            for r in rows
            if r["arm_id"] in candidates and (not int(r["disabled"] or 0) or _cooldown_retest_due(r, now_ts))
        }
        if available:
            stats.update(available)
            candidates = [arm for arm in ACTION_ORDER if arm in available]
            selected_ctx = ctx
            fallback_depth = depth
            found = True
            break

    if not found:
        selected_ctx = "global"
        fallback_depth = len(parent_contexts(context_key, regime_id)) - 1

    preferred = [
        arm for arm in candidates
        if bool(_stat(stats[arm], "paper_preferred")) and not bool(_stat(stats[arm], "disabled"))
    ]
    if preferred:
        arm = max(preferred, key=lambda a: _mean_reward(stats[a]))
        row = stats[arm]
        return _action_decision(
            arm,
            context_key,
            selected_ctx,
            fallback_depth,
            "paper_preferred",
            False,
            False,
            int(_stat(row, "n_fills")),
            _mean_reward(row),
            *_confidence(row),
            fair_confidence,
            risk_state,
            "paper preferred in context",
        )

    if rng.random() < max(0.0, float(epsilon or 0.0)):
        arm = rng.choice(candidates)
        row = stats[arm]
        return _action_decision(
            arm,
            context_key,
            selected_ctx,
            fallback_depth,
            "exploration",
            True,
            bool(_stat(row, "disabled")),
            int(_stat(row, "n_fills")),
            _mean_reward(row),
            *_confidence(row),
            fair_confidence,
            risk_state,
            "epsilon exploration",
        )

    total = sum(max(1, int(_stat(stats[arm], "n_fills"))) for arm in candidates)
    best_arm = candidates[0]
    best_score = -1e18
    for arm in candidates:
        row = stats[arm]
        n = max(1, int(_stat(row, "n_fills")))
        mean = _mean_reward(row)
        adverse_penalty = max(0.0, -float(_stat(row, "adverse_sum"))) / n / 10_000.0
        score = mean + math.sqrt(2.0 * math.log(total + 1.0) / n) - adverse_penalty
        if score > best_score:
            best_score = score
            best_arm = arm
    row = stats[best_arm]
    source = "learned_ucb" if found and fallback_depth == 0 else "parent_fallback" if found else "hardcoded_fallback"
    return _action_decision(
        best_arm,
        context_key,
        selected_ctx,
        fallback_depth,
        source,
        False,
        bool(_stat(row, "disabled")),
        int(_stat(row, "n_fills")),
        _mean_reward(row),
        *_confidence(row),
        fair_confidence,
        risk_state,
        "ucb best paper reward" if found else "global prior fallback",
    )


def record_policy_action(c: sqlite3.Connection, *, campaign_id: str, condition_id: str,
                         window_start_ts: int, token: str, context_key: str,
                         fair_model_id: str, q_structural: Optional[float],
                         q_market_mid: Optional[float], q_used: float,
                         fair_blend_weight: float, fair_confidence: str,
                         risk_state: str, tail_risk_score: float,
                         quote_action_arm: str, selector_source: str,
                         exploration_flag: bool, parent_context_used: str,
                         now_ts: int, residual_adjustment: float = 0.0,
                         fair_context_key: str | None = None,
                         fallback_depth: int = 0,
                         disabled_profile_flag: bool = False,
                         policy_action: str = "paper_quote",
                         frozen: bool = False) -> int:
    ensure_v3_tables(c)
    cur = c.execute(
        """INSERT INTO crypto5m_maker_v3_policy_actions(
	             campaign_id,learner_version,selector_version,reward_version,condition_id,window_start_ts,
	             token,context_key,fair_context_key,parent_context_used,fallback_depth,fair_model_id,q_structural,q_market_mid,
	             q_used,fair_blend_weight,residual_adjustment,fair_confidence,risk_state,tail_risk_score,
	             quote_action_arm,selector_source,exploration_flag,policy_action,disabled_profile_flag,created_ts)
	           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            campaign_id,
            LEARNER_VERSION,
            SELECTOR_VERSION,
            _campaign_reward_version(c, campaign_id),
            condition_id,
            int(window_start_ts),
            token,
            context_key,
            fair_context_key or "unknown",
            parent_context_used,
            int(fallback_depth),
            fair_model_id,
            q_structural,
            q_market_mid,
            float(q_used),
            float(fair_blend_weight),
            float(residual_adjustment),
            fair_confidence,
            risk_state,
            float(tail_risk_score),
            quote_action_arm,
            selector_source,
            1 if exploration_flag else 0,
            policy_action,
            1 if disabled_profile_flag else 0,
            int(now_ts),
        ),
    )
    if not frozen:
        # frozen campaigns own zero learner state — the policy-action log above is the telemetry.
        upsert_action_arm_stats(
            c,
            campaign_id=campaign_id,
            context_key=context_key,
            arm_id=quote_action_arm,
            n_decisions=1,
            now_ts=now_ts,
        )
    return int(cur.lastrowid)


def record_ablation_rows(c: sqlite3.Connection, *, campaign_id: str, condition_id: str,
                         window_start_ts: int, token: str, context_key: str,
                         selected_arm: str, q_used: float, risk_state: str,
                         now_ts: int) -> None:
    ensure_v3_tables(c)
    rows = [
        ("A0", "base_small", 0.50, "v2_baseline"),
        ("A1", selected_arm, q_used, "fair_only"),
        ("A2", selected_arm, 0.50, "quote_only"),
        ("A3", "pause" if risk_state == "pause" else selected_arm, q_used, risk_state),
        ("A4", selected_arm, q_used, "full_v3"),
    ]
    c.executemany(
        """INSERT INTO crypto5m_maker_v3_ablations(
             campaign_id,condition_id,window_start_ts,token,context_key,ablation_group,selected_arm,q_used,
             risk_state,created_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                campaign_id,
                condition_id,
                int(window_start_ts),
                token,
                context_key,
                group,
                arm,
                float(q),
                state,
                int(now_ts),
            )
            for group, arm, q, state in rows
        ],
    )


def apply_action_arm(policy: RG.MakerRegimePolicy, knobs: dict[str, float], arm_id: str,
                     *, token_inventory: float, token_price: Optional[float]) -> tuple[RG.MakerRegimePolicy, dict[str, float]]:
    arm = ACTION_ARMS[arm_id]
    px = max(0.01, min(0.99, float(token_price if token_price is not None else 0.50)))
    clip_from_usdc = arm.clip_usdc / px if arm.clip_usdc > 0 else 0.0
    clip = min(float(knobs["clip"]), arm.clip_shares_cap, clip_from_usdc)
    next_knobs = {
        "margin": round(max(0.0, float(knobs["margin"]) * arm.margin_multiplier + arm.margin_add_bps / 10_000.0), 4),
        "clip": round(max(0.0, clip), 2),
        "max_shares": round(max(0.0, float(knobs["max_shares"]) * arm.max_inventory_multiplier), 2),
        "min_edge": max(float(knobs["min_edge"]), arm.min_edge_bps / 10_000.0),
        "min_seconds_quote": float(knobs["min_seconds_quote"]),
    }
    bid_enabled = bool(policy.bid_enabled and arm.bid_enabled)
    ask_enabled = bool(policy.ask_enabled and arm.ask_enabled)
    exit_only = bool(policy.exit_only or arm.exit_only)
    if arm.exit_only:
        bid_enabled = False
        ask_enabled = ask_enabled and float(token_inventory or 0.0) > 0.0
    if arm_id == "pause":
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
        risk_action=arm_id,
        can_hard_cut=bool(policy.can_hard_cut and arm.hard_cut),
        quote_ttl_ms=arm.quote_ttl_ms,
        reason=f"{policy.reason}; v3 action {arm_id}".strip("; "),
        warnings=tuple(policy.warnings) + (f"adaptive_v3_action={arm_id}",),
    )
    return next_policy, next_knobs


def update_rewards_for_settled_window(c: sqlite3.Connection, *, campaign_id: str,
                                      condition_id: Optional[str], window_start_ts: Optional[int],
                                      winning_outcome: str, now_ts: int) -> int:
    ensure_v3_tables(c)
    if not _table_exists(c, "maker_fills"):
        return 0
    reward_version = _campaign_reward_version(c, campaign_id)
    markout_reward = reward_version == REWARD_VERSION_V32
    # frozen campaigns record reward rows (needed to compare policies later) but never fold
    # them back into arm stats, disable/prefer gates, or fair-blend stats.
    frozen = campaign_frozen(c, campaign_id)
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
            LEFT JOIN crypto5m_maker_v3_rewards r
              ON r.campaign_id=f.campaign_id AND r.fill_id=f.id AND r.reward_version=?
            WHERE {' AND '.join(where)}
            ORDER BY f.id""",
        [reward_version, *params],
    ).fetchall()
    inserted = 0
    for fill in rows:
        action = _action_for_fill(c, campaign_id, fill)
        if action is None:
            continue
        pnl = settled_fill_pnl(fill["side"], fill["token"], fill["price"], fill["size"], winning_outcome)
        hard_cut_penalty = 1.0 if str(_row_get(fill, "fill_model") or "") == "hard-cut" else 0.0
        adverse_10s_bps = None
        adverse_penalty = 0.0
        if markout_reward:
            # 10s fill-direction markout from the future maker_quotes snapshot — the V2 formula
            # (adverse fills are charged; favorable markout is never a bonus).
            adverse_10s_bps = AD._reward_markouts(c, fill)["adverse_selection_10s_bps"]
            if adverse_10s_bps is not None:
                adverse_penalty = max(0.0, -float(adverse_10s_bps)) / 10_000.0 * float(fill["size"] or 0.0)
        reward = pnl - hard_cut_penalty - adverse_penalty
        cur = c.execute(
            """INSERT OR IGNORE INTO crypto5m_maker_v3_rewards(
                 campaign_id,condition_id,window_start_ts,fill_id,token,context_key,quote_action_arm,
                 reward,fee_adjusted_pnl,adverse_selection_10s_bps,inventory_carry_penalty,
                 hard_cut_penalty,reward_version,created_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                campaign_id,
                fill["condition_id"],
                fill["window_start_ts"],
                fill["id"],
                fill["token"],
                action["context_key"],
                action["quote_action_arm"],
                reward,
                pnl,
                adverse_10s_bps,
                0.0,
                hard_cut_penalty,
                reward_version,
                int(now_ts),
            ),
        )
        if cur.rowcount:
            inserted += 1
            if frozen:
                continue
            upsert_action_arm_stats(
                c,
                campaign_id=campaign_id,
                context_key=action["context_key"],
                arm_id=action["quote_action_arm"],
                n_fills=1,
                reward_sum=reward,
                reward_sq_sum=reward * reward,
                adverse_sum=float(adverse_10s_bps or 0.0),
                hard_cuts=1 if hard_cut_penalty else 0,
                now_ts=now_ts,
            )
            evaluate_action_arm_status(
                c,
                campaign_id=campaign_id,
                context_key=action["context_key"],
                arm_id=action["quote_action_arm"],
                now_ts=now_ts,
                reward_version=reward_version,
            )
    if not frozen:
        update_fair_blend_stats_for_settled_window(
            c,
            campaign_id=campaign_id,
            condition_id=condition_id,
            window_start_ts=window_start_ts,
            winning_outcome=winning_outcome,
            now_ts=now_ts,
        )
    c.commit()
    return inserted


def evaluate_action_arm_status(c: sqlite3.Connection, *, campaign_id: str, context_key: str,
                               arm_id: str, now_ts: int,
                               reward_version: str | None = None) -> str:
    ensure_v3_tables(c)
    reward_version = reward_version or _campaign_reward_version(c, campaign_id)
    row = c.execute(
        """SELECT *
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id=? AND context_key=? AND arm_id=?""",
        (campaign_id, context_key, arm_id),
    ).fetchone()
    if row is None:
        return "insufficient_sample"
    if int(row["disabled"] or 0) and not _cooldown_retest_due(row, now_ts):
        return "disabled_active"
    n = int(row["n_fills"] or 0)
    mean = _mean_reward(row)
    ci_low, ci_high = _confidence(row)
    max_share = _max_window_fill_share(c, campaign_id, context_key, arm_id, reward_version)
    mean_adverse = float(row["adverse_sum"] or 0.0) / max(1, n)
    hard_cuts = int(_stat(row, "hard_cuts") or 0)
    c.execute(
        """UPDATE crypto5m_maker_v3_action_arm_stats
           SET confidence_low=?, confidence_high=?, max_window_fill_share=?, last_updated_ts=?
           WHERE campaign_id=? AND context_key=? AND arm_id=?""",
        (ci_low, ci_high, max_share, int(now_ts), campaign_id, context_key, arm_id),
    )
    if n < CATASTROPHIC_DISABLE_MIN_FILLS:
        c.execute(
            """UPDATE crypto5m_maker_v3_action_arm_stats SET status='insufficient_sample'
               WHERE campaign_id=? AND context_key=? AND arm_id=?""",
            (campaign_id, context_key, arm_id),
        )
        return "insufficient_sample"
    catastrophic = (
        n >= CATASTROPHIC_DISABLE_MIN_FILLS
        and mean <= CATASTROPHIC_DISABLE_MEAN_REWARD
        and (mean_adverse < -50.0 or hard_cuts >= 2 or ci_high is None or ci_high < 0)
        and max_share <= 0.75
    )
    normal_disable = (
        n >= NORMAL_DISABLE_MIN_FILLS
        and mean < 0.0
        and ci_high is not None
        and ci_high < 0.0
        and max_share <= MAX_WINDOW_CONCENTRATION
    )
    if catastrophic or normal_disable:
        disable_type = "catastrophic" if catastrophic else "normal"
        reason = (
            f"n_fills={n} mean_reward={mean:.4f} ci_high={ci_high if ci_high is not None else 'none'} "
            f"mean_adverse_10s_bps={mean_adverse:.1f} hard_cuts={hard_cuts} "
            f"max_window_fill_share={max_share:.2f}"
        )
        c.execute(
            """UPDATE crypto5m_maker_v3_action_arm_stats
               SET disabled=1,paper_preferred=0,status='disabled_active',disable_type=?,
                   disabled_reason=?,disabled_at=?,last_updated_ts=?
               WHERE campaign_id=? AND context_key=? AND arm_id=?""",
            (disable_type, reason, int(now_ts), int(now_ts), campaign_id, context_key, arm_id),
        )
        return "disabled_active"
    if _should_promote(c, campaign_id, context_key, arm_id, row, ci_low, max_share):
        reason = f"n_fills={n} mean_reward={mean:.4f} ci_low={ci_low:.4f} max_window_fill_share={max_share:.2f}"
        c.execute(
            """UPDATE crypto5m_maker_v3_action_arm_stats
               SET paper_preferred=1,status='paper_preferred',paper_preferred_reason=?,
                   paper_preferred_at=?,last_updated_ts=?
               WHERE campaign_id=? AND context_key=? AND arm_id=?""",
            (reason, int(now_ts), int(now_ts), campaign_id, context_key, arm_id),
        )
        return "paper_preferred"
    status = "keep_collecting" if n >= NORMAL_DISABLE_MIN_FILLS else "exploring"
    c.execute(
        """UPDATE crypto5m_maker_v3_action_arm_stats SET status=?,last_updated_ts=?
           WHERE campaign_id=? AND context_key=? AND arm_id=?""",
        (status, int(now_ts), campaign_id, context_key, arm_id),
    )
    return status


def update_fair_blend_stats_for_settled_window(c: sqlite3.Connection, *, campaign_id: str,
                                               condition_id: Optional[str],
                                               window_start_ts: Optional[int],
                                               winning_outcome: str,
                                               now_ts: int) -> int:
    ensure_v3_tables(c)
    reward_version = _campaign_reward_version(c, campaign_id)
    where = ["a.campaign_id=?", "o.policy_action_id IS NULL", "a.q_structural IS NOT NULL",
             "a.q_market_mid IS NOT NULL"]
    params: list[Any] = [campaign_id]
    if condition_id is not None:
        where.append("a.condition_id=?")
        params.append(condition_id)
    if window_start_ts is not None:
        where.append("a.window_start_ts=?")
        params.append(window_start_ts)
    rows = c.execute(
        f"""SELECT a.*
            FROM crypto5m_maker_v3_policy_actions a
            LEFT JOIN crypto5m_maker_v3_fair_blend_observations o
              ON o.campaign_id=a.campaign_id AND o.policy_action_id=a.id AND o.reward_version=?
            WHERE {' AND '.join(where)}
            ORDER BY a.id""",
        [reward_version, *params],
    ).fetchall()
    inserted = 0
    for action in rows:
        fair_key = action["fair_context_key"] or "unknown"
        y = _token_won_label(action["token"], winning_outcome)
        metrics = _fair_candidate_metrics(
            float(action["q_structural"]),
            float(action["q_market_mid"]),
            y,
        )
        current = c.execute(
            """SELECT *
               FROM crypto5m_maker_v3_fair_blend_stats
               WHERE campaign_id=? AND fair_context_key=?""",
            (campaign_id, fair_key),
        ).fetchone()
        merged = _merge_fair_metrics(current["arm_metrics_json"] if current else None, metrics)
        best_arm, best_weight = _best_fair_arm(merged)
        best = merged[best_arm]
        n_obs = int(current["n_observations"] or 0) + 1 if current else 1
        c.execute(
            """INSERT INTO crypto5m_maker_v3_fair_blend_stats(
                 campaign_id,fair_context_key,blend_weight,residual_adjustment,n_observations,reward_sum,
                 brier_sum,log_loss_sum,calibration_abs_sum,settled_correct_sum,best_blend_arm,
                 arm_metrics_json,last_updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(campaign_id,fair_context_key) DO UPDATE SET
                 blend_weight=excluded.blend_weight,
                 residual_adjustment=excluded.residual_adjustment,
                 n_observations=excluded.n_observations,
                 brier_sum=excluded.brier_sum,
                 log_loss_sum=excluded.log_loss_sum,
                 calibration_abs_sum=excluded.calibration_abs_sum,
                 settled_correct_sum=excluded.settled_correct_sum,
                 best_blend_arm=excluded.best_blend_arm,
                 arm_metrics_json=excluded.arm_metrics_json,
                 last_updated_ts=excluded.last_updated_ts""",
            (
                campaign_id,
                fair_key,
                best_weight,
                0.0,
                n_obs,
                float(current["reward_sum"] or 0.0) if current else 0.0,
                float(best["brier_sum"]),
                float(best["log_loss_sum"]),
                float(best["calibration_abs_sum"]),
                int(best["correct_sum"]),
                best_arm,
                json.dumps(merged, sort_keys=True),
                int(now_ts),
            ),
        )
        cur = c.execute(
            """INSERT OR IGNORE INTO crypto5m_maker_v3_fair_blend_observations(
                 campaign_id,policy_action_id,reward_version,created_ts)
               VALUES(?,?,?,?)""",
            (campaign_id, int(action["id"]), reward_version, int(now_ts)),
        )
        inserted += int(cur.rowcount or 0)
    return inserted


def settled_fill_pnl(side: str, token: str, price: float, size: float, winning_outcome: str) -> float:
    up_won = str(winning_outcome).lower().startswith("up")
    token_won = (str(token).lower() == "up" and up_won) or (str(token).lower() == "down" and not up_won)
    payoff = 1.0 if token_won else 0.0
    if str(side).lower() == "bid":
        return (payoff - float(price or 0.0)) * float(size or 0.0)
    return (float(price or 0.0) - payoff) * float(size or 0.0)


def _clean_prob(q: Optional[float]) -> Optional[float]:
    if q is None:
        return None
    try:
        x = float(q)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return _clamp_prob(x)


def _clamp_prob(q: float) -> float:
    return min(0.99, max(0.01, float(q)))


def _logit(q: float) -> float:
    x = _clamp_prob(q)
    return math.log(x / (1.0 - x))


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _default_blend_weight(context: dict[str, Any] | str | None, q_structural: float, q_market: float) -> float:
    data = _as_dict(context)
    if data.get("source_health") not in {None, "", "healthy"}:
        return 0.10
    if data.get("jump_state") in {"jump", "spike"}:
        return 0.15
    disagreement = abs(q_structural - q_market)
    if disagreement >= 0.20:
        return 0.20
    if disagreement >= 0.10:
        return 0.35
    return 0.45


def _as_dict(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _forced_arm(regime_id: str, risk_state: str, token_inventory: float) -> Optional[str]:
    if risk_state in {"pause", "hard_stop"} or regime_id in {"hard_invalid", "book_quarantined", "settlement_conflict"}:
        return "pause"
    if risk_state == "reduce_only" or regime_id in {"late_window", "inventory_saturated"}:
        return "reduce_only" if float(token_inventory or 0.0) > 0.0 else "pause"
    return None


def _candidate_arms(token_inventory: float) -> list[str]:
    if float(token_inventory or 0.0) <= 0.0:
        return list(NORMAL_ACTION_ORDER)
    return list(NORMAL_ACTION_ORDER) + ["reduce_only", "exit_fast"]


def _arm_stats(c: sqlite3.Connection, campaign_id: str, context_key: str) -> list[sqlite3.Row]:
    return c.execute(
        """SELECT *
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id=? AND context_key=?""",
        (campaign_id, context_key),
    ).fetchall()


def _empty_stat(arm_id: str) -> dict[str, Any]:
    return {
        "arm_id": arm_id,
        "n_decisions": 0,
        "n_quotes": 0,
        "n_fills": 0,
        "reward_sum": 0.0,
        "reward_sq_sum": 0.0,
        "adverse_sum": 0.0,
        "disabled": 0,
        "paper_preferred": 0,
    }


def _stat(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else 0
    return row.get(key, 0)


def _mean_reward(row: sqlite3.Row | dict[str, Any]) -> float:
    n = int(_stat(row, "n_fills") or 0)
    if n <= 0:
        return 0.0
    return float(_stat(row, "reward_sum") or 0.0) / n


def _confidence(row: sqlite3.Row | dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    n = int(_stat(row, "n_fills") or 0)
    if n <= 1:
        return None, None
    mean = _mean_reward(row)
    sq = float(_stat(row, "reward_sq_sum") or 0.0)
    var = max(0.0, (sq / n) - mean * mean)
    half = 1.96 * math.sqrt(var / n)
    return mean - half, mean + half


def _cooldown_retest_due(row: sqlite3.Row | dict[str, Any], now_ts: int) -> bool:
    if not int(_stat(row, "disabled") or 0):
        return False
    disabled_at = _stat(row, "disabled_at")
    if disabled_at in (None, ""):
        return False
    try:
        return int(now_ts or 0) >= int(disabled_at) + COOLDOWN_RETEST_SECONDS
    except (TypeError, ValueError):
        return False


def _max_window_fill_share(c: sqlite3.Connection, campaign_id: str, context_key: str,
                           arm_id: str, reward_version: str) -> float:
    try:
        rows = c.execute(
            """SELECT window_start_ts, COUNT(*) n
               FROM crypto5m_maker_v3_rewards
               WHERE campaign_id=? AND context_key=? AND quote_action_arm=? AND reward_version=?
               GROUP BY window_start_ts""",
            (campaign_id, context_key, arm_id, reward_version),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    total = sum(int(r["n"] or 0) for r in rows)
    if total <= 0:
        return 0.0
    return max(int(r["n"] or 0) for r in rows) / total


def _should_promote(c: sqlite3.Connection, campaign_id: str, context_key: str, arm_id: str,
                    row: sqlite3.Row, ci_low: Optional[float], max_share: float) -> bool:
    n = int(row["n_fills"] or 0)
    if n < PROMOTE_MIN_FILLS or int(row["disabled"] or 0) or ci_low is None or ci_low <= 0.0:
        return False
    if max_share > MAX_WINDOW_CONCENTRATION:
        return False
    mean = _mean_reward(row)
    if arm_id == "base_small":
        return mean > 0.0
    base = c.execute(
        """SELECT n_fills,reward_sum
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id=? AND context_key=? AND arm_id='base_small'""",
        (campaign_id, context_key),
    ).fetchone()
    if base is None or int(base["n_fills"] or 0) < PROMOTE_MIN_FILLS:
        return mean > 0.0
    return mean > _mean_reward(base)


def _token_won_label(token: str, winning_outcome: str) -> float:
    up_won = str(winning_outcome).lower().startswith("up")
    token_won = (str(token).lower() == "up" and up_won) or (str(token).lower() == "down" and not up_won)
    return 1.0 if token_won else 0.0


def _fair_candidate_metrics(q_structural: float, q_market: float, y: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for arm, weight in FAIR_BLEND_ARMS.items():
        q = logit_blend(q_structural, q_market, weight=weight)
        brier = (q - y) ** 2
        log_loss = -(y * math.log(q) + (1.0 - y) * math.log(1.0 - q))
        out[arm] = {
            "weight": float(weight),
            "n": 1.0,
            "brier_sum": float(brier),
            "log_loss_sum": float(log_loss),
            "calibration_abs_sum": abs(q - y),
            "correct_sum": 1.0 if ((q >= 0.5) == bool(y)) else 0.0,
        }
    return out


def _merge_fair_metrics(existing_json: str | None,
                        latest: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    try:
        merged = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        merged = {}
    if not isinstance(merged, dict):
        merged = {}
    for arm, metrics in latest.items():
        current = merged.get(arm) if isinstance(merged.get(arm), dict) else {}
        merged[arm] = {
            "weight": float(metrics["weight"]),
            "n": float(current.get("n", 0.0)) + float(metrics["n"]),
            "brier_sum": float(current.get("brier_sum", 0.0)) + float(metrics["brier_sum"]),
            "log_loss_sum": float(current.get("log_loss_sum", 0.0)) + float(metrics["log_loss_sum"]),
            "calibration_abs_sum": (
                float(current.get("calibration_abs_sum", 0.0)) + float(metrics["calibration_abs_sum"])
            ),
            "correct_sum": float(current.get("correct_sum", 0.0)) + float(metrics["correct_sum"]),
        }
    return merged


def _best_fair_arm(metrics: dict[str, dict[str, float]]) -> tuple[str, float]:
    best_arm = "market_only"
    best_score = float("inf")
    for arm, data in metrics.items():
        n = max(1.0, float(data.get("n", 0.0)))
        score = float(data.get("brier_sum", 0.0)) / n
        if score < best_score:
            best_arm = arm
            best_score = score
    return best_arm, float(metrics[best_arm]["weight"])


def _action_decision(arm_id: str, context_key: str, parent_context_used: str,
                     fallback_depth: int, selection_source: str, exploration_flag: bool,
                     profile_disabled_flag: bool, n_fills: int, mean_reward: float,
                     confidence_low: Optional[float], confidence_high: Optional[float],
                     fair_confidence: str, risk_state: str, reason: str) -> ActionDecision:
    return ActionDecision(
        arm_id=arm_id,
        selector_version=SELECTOR_VERSION,
        reward_version=REWARD_VERSION,
        context_key=context_key,
        parent_context_used=parent_context_used,
        fallback_depth=fallback_depth,
        selection_source=selection_source,
        exploration_flag=exploration_flag,
        profile_disabled_flag=profile_disabled_flag,
        n_fills=n_fills,
        mean_reward=mean_reward,
        confidence_low=confidence_low,
        confidence_high=confidence_high,
        fair_confidence=fair_confidence,
        risk_state=risk_state,
        reason=reason,
    )


def _action_for_fill(c: sqlite3.Connection, campaign_id: str, fill: sqlite3.Row) -> sqlite3.Row | None:
    row = c.execute(
        """SELECT *
           FROM crypto5m_maker_v3_policy_actions
           WHERE campaign_id=? AND condition_id=? AND window_start_ts=? AND token=? AND created_ts<=?
           ORDER BY created_ts DESC, id DESC LIMIT 1""",
        (
            campaign_id,
            fill["condition_id"],
            fill["window_start_ts"],
            fill["token"],
            fill["ts"] or 0,
        ),
    ).fetchone()
    if row is not None:
        return row
    return c.execute(
        """SELECT *
           FROM crypto5m_maker_v3_policy_actions
           WHERE campaign_id=? AND condition_id=? AND window_start_ts=? AND token=?
           ORDER BY created_ts DESC, id DESC LIMIT 1""",
        (campaign_id, fill["condition_id"], fill["window_start_ts"], fill["token"]),
    ).fetchone()


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _add_column(c: sqlite3.Connection, table: str, col: str, typ: str) -> None:
    if not _table_exists(c, table):
        return
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    except sqlite3.OperationalError:
        pass


def _row_get(row: sqlite3.Row, key: str, default=None):
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        return default
    return default
