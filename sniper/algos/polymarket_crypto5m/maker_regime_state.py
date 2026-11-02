"""Stateful episode helpers for the crypto5m maker regime evaluator.

Pure compute only: no DB, no network, no quoting, no fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Optional


MOMENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RegimeStateConfig:
    transition_debounce_ticks: int = 3
    transition_debounce_ms: int = 3_000
    min_regime_dwell_ms: int = 5_000
    emergency_regimes: tuple[str, ...] = (
        "hard_invalid",
        "late_window",
        "book_quarantined",
        "settlement_conflict",
        "inventory_saturated",
    )


@dataclass(frozen=True)
class Moment:
    schema_version: int
    key: dict[str, str]
    features: dict[str, Any]
    key_hash: str
    source_snapshot_hash: str


@dataclass(frozen=True)
class RegimeEpisodeState:
    episode_id: str
    campaign_id: str
    condition_id: str
    window_start_ts: int
    window_end_ts: int
    current_regime: str
    previous_regime: Optional[str]
    candidate_regime: Optional[str]
    candidate_since_ts: Optional[float]
    candidate_ticks: int
    entered_ts: float
    last_seen_ts: float
    dwell_seconds: float
    transition_reason: str
    transition_confidence: str
    regime_confidence: str
    moment_schema_version: int
    moment_key: dict[str, str]
    moment_features: dict[str, Any]
    source_snapshot_hash: str


@dataclass(frozen=True)
class TransitionEvent:
    episode_id: str
    condition_id: str
    window_start_ts: int
    from_regime: str
    to_regime: str
    ts: float
    reason: str
    confidence: str


@dataclass(frozen=True)
class ProfileDecision:
    profile_decision_id: str
    episode_id: str
    token: str
    outcome: str
    decision_ts: float
    profile_id: str
    profile_mode: str
    base_policy_id: str
    profile_policy_hash: str
    bid_enabled: Optional[bool]
    ask_enabled: Optional[bool]
    exit_only: Optional[bool]
    hard_cut: Optional[bool]
    margin_used: Optional[float]
    clip_used: Optional[float]
    max_shares_used: Optional[float]
    min_edge_used: Optional[float]
    quote_ttl_ms: Optional[int]
    reason: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def stable_hash(payload: Any, *, prefix: str = "") -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{h}" if prefix else h


def build_moment(snapshot: dict[str, Any], *, token_inventory: float, max_shares: float) -> Moment:
    fair = _float_or_none(snapshot.get("fair"))
    bid = _float_or_none(snapshot.get("best_bid"))
    ask = _float_or_none(snapshot.get("best_ask"))
    spread = (ask - bid) if bid is not None and ask is not None else None
    features = {
        "fair": fair,
        "market_bid": bid,
        "market_ask": ask,
        "seconds_remaining": _float_or_none(snapshot.get("seconds_left")),
        "spread_price": round(spread, 8) if spread is not None else None,
        "spread_ticks": round(spread / 0.01, 4) if spread is not None else None,
        "spread_bps_display": round(spread * 10_000, 4) if spread is not None else None,
        "vol_30s": _float_or_none(snapshot.get("vol_30s")),
        "jump_score": _float_or_none(snapshot.get("jump_score")),
        "direction_score": _float_or_none(snapshot.get("direction_score")),
        "inventory_shares": _float_or_none(token_inventory),
        "gross_inventory": _float_or_none(snapshot.get("gross_inventory")),
        "net_directional_inventory": _float_or_none(snapshot.get("net_directional_inventory")),
        "complete_sets": _float_or_none(snapshot.get("complete_sets")),
        "book_age_ms": _float_or_none(snapshot.get("book_age_ms")),
        "depth_at_quote": _float_or_none(snapshot.get("depth_at_quote")),
        "last_fill_age_ms": _float_or_none(snapshot.get("last_fill_age_ms")),
    }
    source_hash = stable_hash({**snapshot, "token_inventory": token_inventory, "max_shares": max_shares})
    features["source_snapshot_hash"] = source_hash
    key = {
        "window_phase": _window_phase(features["seconds_remaining"]),
        "odds_bucket": _odds_bucket(fair),
        "volatility_state": str(snapshot.get("volatility_state") or "unknown"),
        "inventory_state": _inventory_state(token_inventory, max_shares),
        "spread_state": _spread_state(spread),
        "direction_state": str(snapshot.get("direction_state") or "unconfirmed"),
        "book_health": str(snapshot.get("book_health") or "healthy"),
    }
    return Moment(
        schema_version=MOMENT_SCHEMA_VERSION,
        key=key,
        features=features,
        key_hash=stable_hash({"schema": MOMENT_SCHEMA_VERSION, "key": key}),
        source_snapshot_hash=source_hash,
    )


def start_episode(campaign_id: str, condition_id: str, window_start_ts: int, window_end_ts: int,
                  regime_id: str, now_ts: float, moment: Moment,
                  regime_confidence: str = "medium",
                  previous_regime: Optional[str] = None,
                  transition_reason: str = "episode_start") -> RegimeEpisodeState:
    episode_id = stable_hash(
        {
            "campaign_id": campaign_id,
            "condition_id": condition_id,
            "window_start_ts": window_start_ts,
            "regime_id": regime_id,
            "entered_ts": now_ts,
        },
        prefix="ep_",
    )
    return RegimeEpisodeState(
        episode_id=episode_id,
        campaign_id=campaign_id,
        condition_id=condition_id,
        window_start_ts=int(window_start_ts),
        window_end_ts=int(window_end_ts),
        current_regime=regime_id,
        previous_regime=previous_regime,
        candidate_regime=None,
        candidate_since_ts=None,
        candidate_ticks=0,
        entered_ts=float(now_ts),
        last_seen_ts=float(now_ts),
        dwell_seconds=0,
        transition_reason=transition_reason,
        transition_confidence="high" if transition_reason == "emergency_interrupt" else "medium",
        regime_confidence=regime_confidence,
        moment_schema_version=moment.schema_version,
        moment_key=moment.key,
        moment_features=moment.features,
        source_snapshot_hash=moment.source_snapshot_hash,
    )


def advance_episode(current: RegimeEpisodeState, candidate_regime: str, *, now_ts: float,
                    snapshot: dict[str, Any],
                    config: RegimeStateConfig = RegimeStateConfig(),
                    regime_confidence: str = "medium") -> tuple[RegimeEpisodeState, Optional[TransitionEvent]]:
    moment = build_moment(
        snapshot,
        token_inventory=float(snapshot.get("token_inventory") or 0.0),
        max_shares=float(snapshot.get("max_shares") or 1.0),
    )
    if candidate_regime == current.current_regime:
        return (
            _replace_episode(
                current,
                candidate_regime=None,
                candidate_since_ts=None,
                candidate_ticks=0,
                last_seen_ts=float(now_ts),
                dwell_seconds=round(float(now_ts) - current.entered_ts, 6),
                regime_confidence=regime_confidence,
                moment_schema_version=moment.schema_version,
                moment_key=moment.key,
                moment_features=moment.features,
                source_snapshot_hash=moment.source_snapshot_hash,
            ),
            None,
        )

    if candidate_regime in config.emergency_regimes:
        return _transition(current, candidate_regime, now_ts, moment, "emergency_interrupt", "high",
                           regime_confidence)

    same_candidate = candidate_regime == current.candidate_regime
    candidate_since = current.candidate_since_ts if same_candidate else float(now_ts)
    candidate_ticks = current.candidate_ticks + 1 if same_candidate else 1
    elapsed_ms = (float(now_ts) - float(candidate_since)) * 1000
    dwell_ms = (float(now_ts) - current.entered_ts) * 1000
    can_transition = (
        candidate_ticks >= config.transition_debounce_ticks
        or elapsed_ms >= config.transition_debounce_ms
    ) and dwell_ms >= config.min_regime_dwell_ms
    if can_transition:
        return _transition(current, candidate_regime, now_ts, moment, "debounced_transition", "medium",
                           regime_confidence)

    return (
        _replace_episode(
            current,
            candidate_regime=candidate_regime,
            candidate_since_ts=candidate_since,
            candidate_ticks=candidate_ticks,
            last_seen_ts=float(now_ts),
            dwell_seconds=round(float(now_ts) - current.entered_ts, 6),
            regime_confidence=regime_confidence,
            moment_schema_version=moment.schema_version,
            moment_key=moment.key,
            moment_features=moment.features,
            source_snapshot_hash=moment.source_snapshot_hash,
        ),
        None,
    )


def decide_profile(episode: RegimeEpisodeState, *, token: str, token_inventory: float,
                   max_shares: float, active_policy: Any, now_ts: float,
                   profile_mode: str = "log_only") -> ProfileDecision:
    risk_action = _get(active_policy, "risk_action") or "normal"
    profile_id = {
        "pull_all": "pause",
        "reduce_only": "reduce_only",
        "widen": "quote_wide",
        "normal": "quote_base",
    }.get(str(risk_action), "quote_base")
    base_policy_id = _get(active_policy, "policy_id") or "unknown"
    bid_enabled = _get(active_policy, "bid_enabled")
    ask_enabled = _get(active_policy, "ask_enabled")
    exit_only = _get(active_policy, "exit_only")
    hard_cut = _get(active_policy, "can_hard_cut")
    payload = {
        "episode_id": episode.episode_id,
        "token": token,
        "profile_id": profile_id,
        "profile_mode": profile_mode,
        "base_policy_id": base_policy_id,
        "token_inventory": token_inventory,
        "max_shares": max_shares,
    }
    return ProfileDecision(
        profile_decision_id=stable_hash({**payload, "decision_ts": now_ts}, prefix="pd_"),
        episode_id=episode.episode_id,
        token=token,
        outcome=token,
        decision_ts=float(now_ts),
        profile_id=profile_id,
        profile_mode=profile_mode,
        base_policy_id=base_policy_id,
        profile_policy_hash=stable_hash(payload),
        bid_enabled=_bool_or_none(bid_enabled),
        ask_enabled=_bool_or_none(ask_enabled),
        exit_only=_bool_or_none(exit_only),
        hard_cut=_bool_or_none(hard_cut),
        margin_used=_float_or_none(_get(active_policy, "margin")),
        clip_used=_float_or_none(_get(active_policy, "clip")),
        max_shares_used=_float_or_none(_get(active_policy, "max_shares")),
        min_edge_used=_float_or_none(_get(active_policy, "min_edge")),
        quote_ttl_ms=_int_or_none(_get(active_policy, "quote_ttl_ms")),
        reason=str(_get(active_policy, "reason") or ""),
        warnings=tuple(_get(active_policy, "warnings") or ()),
    )


def _transition(current: RegimeEpisodeState, to_regime: str, now_ts: float, moment: Moment,
                reason: str, confidence: str, regime_confidence: str):
    updated = start_episode(
        current.campaign_id,
        current.condition_id,
        current.window_start_ts,
        current.window_end_ts,
        to_regime,
        now_ts,
        moment,
        regime_confidence=regime_confidence,
        previous_regime=current.current_regime,
        transition_reason=reason,
    )
    event = TransitionEvent(
        episode_id=updated.episode_id,
        condition_id=current.condition_id,
        window_start_ts=current.window_start_ts,
        from_regime=current.current_regime,
        to_regime=to_regime,
        ts=float(now_ts),
        reason=reason,
        confidence=confidence,
    )
    return updated, event


def _replace_episode(current: RegimeEpisodeState, **changes: Any) -> RegimeEpisodeState:
    data = current.__dict__.copy()
    data.update(changes)
    return RegimeEpisodeState(**data)


def _window_phase(seconds_left: Optional[float]) -> str:
    if seconds_left is None:
        return "mid_window"
    if seconds_left <= 20:
        return "final_seconds"
    if seconds_left <= 60:
        return "late_window"
    if seconds_left >= 240:
        return "early_window"
    return "mid_window"


def _odds_bucket(fair: Optional[float]) -> str:
    if fair is None:
        return "mid_chop"
    if fair <= 0.20:
        return "extreme_low"
    if fair < 0.40:
        return "low"
    if fair <= 0.60:
        return "mid_chop"
    if fair < 0.80:
        return "high"
    return "extreme_high"


def _inventory_state(inventory: float, max_shares: float) -> str:
    cap = max(1.0, abs(float(max_shares or 1.0)))
    inv = float(inventory or 0.0)
    frac = abs(inv) / cap
    if abs(inv) <= 1e-9:
        return "flat"
    if inv > 0:
        return "long_saturated" if frac >= 0.98 else "long_light"
    return "short_saturated" if frac >= 0.98 else "short_light"


def _spread_state(spread: Optional[float]) -> str:
    if spread is None:
        return "unknown"
    if spread <= 0.03:
        return "healthy"
    if spread <= 0.08:
        return "wide"
    return "untradeable"


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)
