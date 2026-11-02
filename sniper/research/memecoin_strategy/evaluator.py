from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .dsl import compile_predicate


@dataclass(frozen=True)
class Evaluation:
    candidate_id: str
    observation_id: int | str
    matched: bool
    reason: str
    features: dict[str, Any]
    source_observation_ids: tuple[int | str, ...]


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"invalid observation time: {value}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"observation time must be timezone aware: {value}")
    return parsed


def _id_key(value):
    return (0, value) if isinstance(value, int) and not isinstance(value, bool) else (1, str(value))


def observation_order_key(observation):
    return (
        _time(observation.get("observed_at")),
        _time(observation.get("source_ts") or observation.get("observed_at")),
        _id_key(observation.get("id")),
    )


def _mint(value):
    value = str(value or "")
    return value.lower() if value.lower().startswith("0x") else value


def _flag(value):
    if value in (0, False):
        return False
    if value in (1, True):
        return True
    return None


def _features(predicate, observation, history):
    current_key = observation_order_key(observation)
    observed_at = current_key[0]
    network = str(observation.get("network_id") or "")
    mint = _mint(observation.get("mint"))
    eligible = []
    for prior in sorted(history, key=observation_order_key):
        if observation_order_key(prior) > current_key:
            break
        if str(prior.get("network_id") or "") != network or _mint(prior.get("mint")) != mint:
            continue
        age = (observed_at - _time(prior.get("observed_at"))).total_seconds()
        if age < 0 or age > predicate.window_s:
            continue
        if (prior.get("normalized_side") == "buy" and prior.get("was_curated") in (1, True)
                and prior.get("source_user_id")):
            eligible.append(prior)
    distinct_buyers = len({row.get("source_user_id") for row in eligible})
    return {
        "network": network or None,
        "market_cap": observation.get("their_market_cap"),
        "size_usd": observation.get("their_size_usd"),
        "was_curated": _flag(observation.get("was_curated")),
        "was_real_approved": _flag(observation.get("was_real_approved")),
        "distinct_buyer_count": distinct_buyers,
        "window_s": predicate.window_s,
        "thesis_stance": observation.get("thesis_stance"),
        "thesis_score": observation.get("thesis_score"),
        "source_lag_s": observation.get("lag_s"),
        "token_age_s": observation.get("token_age_s"),
        "liquidity": observation.get("liquidity"),
        "event_type": observation.get("raw_type"),
    }, tuple(row.get("id") for row in eligible)


def process_observation(candidate, observation, history, mode, now_iso):
    if mode not in {"live", "replay"}:
        raise ValueError("mode must be live or replay")
    predicate = compile_predicate(candidate.predicate)
    features, source_ids = _features(predicate, observation, history)
    matched = predicate.matches(features)
    reason = "matched" if matched else "predicate_no_match"
    if mode == "live":
        age_s = (_time(now_iso) - _time(observation.get("observed_at"))).total_seconds()
        if age_s < -5:
            matched, reason = False, "future_live_observation"
        elif age_s > predicate.freshness_s:
            matched, reason = False, "stale_live_observation"
    return Evaluation(
        candidate_id=candidate.candidate_id, observation_id=observation.get("id"),
        matched=matched, reason=reason, features=features,
        source_observation_ids=source_ids,
    )


def evaluate_stream(candidate, observations, mode, now_iso):
    ordered = sorted(observations, key=observation_order_key)
    return [
        process_observation(candidate, observation, ordered, mode, now_iso)
        for observation in ordered
    ]
