"""V6 complete post-fill position engine — the only decision authority over a V6 paper position.

Per docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md, this module is a pure
transition: ``step(state, observation) -> V6Decision``. Its private cadence-invariant break helper
lives in ``memecoin_v6_position_policy.py``; no second replay policy exists.

- a None-aware data-sufficiency check that turns any missing or explicitly-null required evidence
  into a deterministic ``HOLD`` with sorted ``data_insufficient:<field>`` reason codes — it never
  lets the seed raise;
- ``state_if_filled`` / ``state_if_unfilled`` successors so an unfilled modeled exit consumes no
  partial-tranche budget and reduces no inventory, while causal observation-history fields
  (``break_level``, ``neg_streak``, ``recovered``, ``vol_hist``) still advance;
- content-fingerprinted, validated observations (``make_observation``) and a frozen, hashed policy
  document so every decision is byte-stable and reconstructable.

This module has no funded authority. It never signs, quotes, reserves, or touches the funded runner.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache

from sniper.services.memecoin_v6_position_policy import (
    CROWDED_SHARE, EARLY_S, FRENZY_RATE, MAX_TRANCHES, NEG_STREAK_CONFIRM,
    POLICY_ID as SEED_POLICY_ID, REQUIRED_POINT_EVIDENCE, VOL_MIN_OBS, VOL_P, V6State,
)
from sniper.services.memecoin_v6_position_policy import missing_evidence as _seed_missing_evidence
from sniper.services.memecoin_v6_position_policy import v6_step
from sniper.analysis.memecoin_lp_removal import (
    LP_CATASTROPHIC, LP_MATERIAL, classify_lp_removal,
)

POLICY_ID = "v6-position-engine-v2"
FRACTION_PPM_SCALE = 1_000_000
MAX_VOLATILITY_SAMPLES = 64
MAX_STATE_BYTES = 16 * 1024
CATASTROPHE_LOSS_PCT = 80.0
TIME_EXPIRY_S = 43_200.0
ROUTE_ABSENT_MIN_CHECKS = 4
ROUTE_ABSENT_MIN_S = 60.0
SUPPORTED_DEPTH_CAPTURE_VERSIONS = ("v6-direct-v1", "v6-incremental-")
TRUSTED_DEPTH_PROVENANCE = ("trusted", "decoded_tape")
LP_MINOR_FRACTION = 0.05
LP_CATASTROPHIC_FRACTION = 0.30
ADVERSE_QUOTE_RATIO_LIMIT = 10.0
CATASTROPHE_CONFIRMATIONS = 2

SUPPORTED_NETWORKS = frozenset({"solana", "4663"})

# The seed's REQUIRED_POINT_EVIDENCE plus the executable sell price the engine itself needs to act.
REQUIRED_ENGINE_EVIDENCE = REQUIRED_POINT_EVIDENCE + ("executable_price",)
SEED_OBSERVATION_FIELDS = (
    "is_break", "held_s", "arrival_rate", "buy_share", "realized_vol",
    "prior_low", "price", "net",
)


class V6Action(str, Enum):
    HOLD = "HOLD"
    DERISK = "DERISK"
    EXIT_FULL = "EXIT_FULL"


class RuleStatus(str, Enum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class V6RuleResult:
    rule_id: str
    status: RuleStatus
    action: V6Action = V6Action.HOLD
    fraction_ppm: int = 0
    priority: int = 0
    reason_codes: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteAbsenceState:
    first_absent_at: str | None = None
    checks: int = 0
    last_quote_evidence_id: str | None = None

    def __post_init__(self):
        if isinstance(self.checks, bool) or not isinstance(self.checks, int) or self.checks < 0:
            raise ValueError("route absence checks must be a non-negative integer")


@dataclass(frozen=True)
class EvidenceIdentity:
    market: str | None = None
    quote: str | None = None
    source: str | None = None

    def __post_init__(self):
        for name, value in (("market", self.market), ("quote", self.quote),
                            ("source", self.source)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} evidence identity must be a non-empty string or null")


@dataclass(frozen=True)
class BreakEpisode:
    episode_id: str
    break_level: float
    first_market_evidence_id: str
    neg_streak: int = 0
    status: str = "active"

    def __post_init__(self):
        if not self.episode_id or not self.first_market_evidence_id:
            raise ValueError("break episode identities must not be empty")
        if not math.isfinite(self.break_level) or self.break_level <= 0:
            raise ValueError("break_level must be finite and positive")
        if isinstance(self.neg_streak, bool) or not isinstance(self.neg_streak, int) \
                or self.neg_streak < 0:
            raise ValueError("neg_streak must be a non-negative integer")
        if self.status not in {"active", "recovered", "confirmed"}:
            raise ValueError("break episode status must be active, recovered, or confirmed")


@dataclass(frozen=True)
class ActionIntent:
    token: str
    action: V6Action
    fraction_ppm: int
    priority: int
    trigger_evidence_id: str

    def __post_init__(self):
        if not self.token or not self.trigger_evidence_id:
            raise ValueError("action intent identities must not be empty")
        if self.action is V6Action.HOLD:
            raise ValueError("HOLD cannot be persisted as an action intent")
        if isinstance(self.fraction_ppm, bool) or not isinstance(self.fraction_ppm, int) \
                or not 0 < self.fraction_ppm <= FRACTION_PPM_SCALE:
            raise ValueError("action intent fraction_ppm must be in 1..1000000")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) \
                or self.priority <= 0:
            raise ValueError("action intent priority must be a positive integer")


SEED_ACTION_MAP = {
    None: V6Action.HOLD,
    "derisk": V6Action.DERISK,
    "cut": V6Action.EXIT_FULL,
}


@dataclass(frozen=True)
class V6PositionState:
    """Bounded per-position engine state.

    ``break_state`` and ``pending_action_token`` remain compatibility fields until the cadence-
    invariant policy and ledger tasks move their consumers to ``break_episode`` and
    ``active_intent``. They are serialized centrally here so that migration has one exact source.
    """
    position_id: str
    remaining_qty_atomic: int
    volatility_samples: tuple[tuple[str, float], ...] = ()
    last_market_evidence_id: str | None = None
    last_quote_evidence_id: str | None = None
    last_source_evidence_id: str | None = None
    break_episode: BreakEpisode | None = None
    filled_tranches: int = 0
    executable_high_water: float | None = None
    last_accepted_executable_price: float | None = None
    catastrophe_candidate_price: float | None = None
    catastrophe_confirmations: int = 0
    entry_unit_price: str | None = None
    entry_quote_asset: str | None = None
    opened_at: str | None = None
    route_absence: RouteAbsenceState = field(default_factory=RouteAbsenceState)
    active_intent: ActionIntent | None = None
    break_state: V6State = field(default_factory=V6State)
    pending_action_token: str | None = None
    last_action_outcome: str | None = None

    def __post_init__(self):
        if not self.position_id:
            raise ValueError("position_id must not be empty")
        if isinstance(self.remaining_qty_atomic, bool) \
                or not isinstance(self.remaining_qty_atomic, int) \
                or self.remaining_qty_atomic < 0:
            raise ValueError("remaining_qty_atomic must be a non-negative integer")
        if len(self.volatility_samples) > MAX_VOLATILITY_SAMPLES:
            raise ValueError(f"volatility_samples cannot exceed {MAX_VOLATILITY_SAMPLES}")
        identities = []
        for sample in self.volatility_samples:
            if not isinstance(sample, tuple) or len(sample) != 2:
                raise ValueError("each volatility sample must be an (evidence_id, value) tuple")
            evidence_id, value = sample
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ValueError("volatility sample evidence_id must not be empty")
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError("volatility sample value must be finite and non-negative")
            identities.append(evidence_id)
        if len(set(identities)) != len(identities):
            raise ValueError("volatility sample evidence identities must be unique")
        if isinstance(self.filled_tranches, bool) or not isinstance(self.filled_tranches, int) \
                or not 0 <= self.filled_tranches <= MAX_TRANCHES:
            raise ValueError(f"filled_tranches must be an integer in 0..{MAX_TRANCHES}")
        if self.executable_high_water is not None and (
                isinstance(self.executable_high_water, bool)
                or not isinstance(self.executable_high_water, (int, float))
                or not math.isfinite(self.executable_high_water)
                or self.executable_high_water <= 0):
            raise ValueError("executable_high_water must be finite and positive")
        for name in ("last_accepted_executable_price", "catastrophe_candidate_price"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool)
                    or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        if (isinstance(self.catastrophe_confirmations, bool)
                or not isinstance(self.catastrophe_confirmations, int)
                or not 0 <= self.catastrophe_confirmations <= CATASTROPHE_CONFIRMATIONS):
            raise ValueError("catastrophe_confirmations is invalid")
        if self.entry_unit_price is not None:
            try:
                entry_price = Decimal(self.entry_unit_price)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("entry_unit_price must be a positive finite decimal") from exc
            if not entry_price.is_finite() or entry_price <= 0:
                raise ValueError("entry_unit_price must be a positive finite decimal")
        if self.entry_quote_asset is not None and not str(self.entry_quote_asset).strip():
            raise ValueError("entry_quote_asset must be non-empty or null")


@dataclass(frozen=True)
class V6Observation:
    """One point-in-time, content-fingerprinted observation. Build via ``make_observation``."""
    observation_id: str
    observed_at: str
    network_id: str
    payload: Mapping[str, object]
    input_fingerprint: str
    evidence: EvidenceIdentity = field(default_factory=EvidenceIdentity)


@dataclass(frozen=True)
class V6Decision:
    action: V6Action
    fraction_ppm: int
    action_token: str
    state_if_filled: V6PositionState
    state_if_unfilled: V6PositionState
    reason_codes: tuple[str, ...]
    thought: str
    input_fingerprint: str
    policy_hash: str
    semantic_transition: bool = True
    action_intent_token: str | None = None
    supersedes_token: str | None = None
    rule_results: tuple[V6RuleResult, ...] = ()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _episode_document(episode: BreakEpisode | None) -> dict | None:
    if episode is None:
        return None
    return {
        "episode_id": episode.episode_id,
        "break_level": episode.break_level,
        "first_market_evidence_id": episode.first_market_evidence_id,
        "neg_streak": episode.neg_streak,
        "status": episode.status,
    }


def _intent_document(intent: ActionIntent | None) -> dict | None:
    if intent is None:
        return None
    return {
        "token": intent.token,
        "action": intent.action.value,
        "fraction_ppm": intent.fraction_ppm,
        "priority": intent.priority,
        "trigger_evidence_id": intent.trigger_evidence_id,
    }


def state_document(state: V6PositionState) -> dict:
    """Return the canonical V1 state document and enforce its serialized size contract."""
    b = state.break_state
    document = {
        "state_version": 2,
        "position_id": state.position_id,
        "remaining_qty_atomic": state.remaining_qty_atomic,
        "volatility_samples": [list(sample) for sample in state.volatility_samples],
        "last_market_evidence_id": state.last_market_evidence_id,
        "last_quote_evidence_id": state.last_quote_evidence_id,
        "last_source_evidence_id": state.last_source_evidence_id,
        "break_episode": _episode_document(state.break_episode),
        "filled_tranches": state.filled_tranches,
        "executable_high_water": state.executable_high_water,
        "last_accepted_executable_price": state.last_accepted_executable_price,
        "catastrophe_candidate_price": state.catastrophe_candidate_price,
        "catastrophe_confirmations": state.catastrophe_confirmations,
        "entry_unit_price": state.entry_unit_price,
        "entry_quote_asset": state.entry_quote_asset,
        "opened_at": state.opened_at,
        "route_absence": {
            "first_absent_at": state.route_absence.first_absent_at,
            "checks": state.route_absence.checks,
            "last_quote_evidence_id": state.route_absence.last_quote_evidence_id,
        },
        "active_intent": _intent_document(state.active_intent),
        "last_action_outcome": state.last_action_outcome,
        "compatibility": {
            "pending_action_token": state.pending_action_token,
            "break_state": {
                "tranches": b.tranches,
                "awaiting": b.awaiting,
                "break_level": b.break_level,
                "neg_streak": b.neg_streak,
                "recovered": b.recovered,
                "vol_hist": list(b.vol_hist),
            },
        },
    }
    if len(canonical_json(document).encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError(f"serialized V6 state exceeds {MAX_STATE_BYTES} bytes")
    return document


def state_from_document(document: Mapping[str, object]) -> V6PositionState:
    """Restore a canonical V1 state document, rejecting unknown versions and malformed shapes."""
    if not isinstance(document, Mapping) or document.get("state_version") not in (1, 2):
        raise ValueError("unsupported V6 state document version")
    episode_doc = document.get("break_episode")
    episode = BreakEpisode(**episode_doc) if isinstance(episode_doc, Mapping) else None
    intent_doc = document.get("active_intent")
    intent = None
    if isinstance(intent_doc, Mapping):
        intent = ActionIntent(
            token=intent_doc["token"], action=V6Action(intent_doc["action"]),
            fraction_ppm=intent_doc["fraction_ppm"], priority=intent_doc["priority"],
            trigger_evidence_id=intent_doc["trigger_evidence_id"],
        )
    compatibility = document.get("compatibility")
    compatibility = compatibility if isinstance(compatibility, Mapping) else {}
    break_doc = compatibility.get("break_state")
    break_doc = break_doc if isinstance(break_doc, Mapping) else {}
    route_doc = document.get("route_absence")
    route_doc = route_doc if isinstance(route_doc, Mapping) else {}
    state = V6PositionState(
        position_id=document["position_id"],
        remaining_qty_atomic=document["remaining_qty_atomic"],
        volatility_samples=tuple(tuple(sample) for sample in document.get(
            "volatility_samples", ())),
        last_market_evidence_id=document.get("last_market_evidence_id"),
        last_quote_evidence_id=document.get("last_quote_evidence_id"),
        last_source_evidence_id=document.get("last_source_evidence_id"),
        break_episode=episode,
        filled_tranches=document.get("filled_tranches", 0),
        executable_high_water=document.get("executable_high_water"),
        last_accepted_executable_price=document.get("last_accepted_executable_price"),
        catastrophe_candidate_price=document.get("catastrophe_candidate_price"),
        catastrophe_confirmations=document.get("catastrophe_confirmations", 0),
        entry_unit_price=document.get("entry_unit_price"),
        entry_quote_asset=document.get("entry_quote_asset"),
        opened_at=document.get("opened_at"),
        route_absence=RouteAbsenceState(
            first_absent_at=route_doc.get("first_absent_at"),
            checks=route_doc.get("checks", 0),
            last_quote_evidence_id=route_doc.get("last_quote_evidence_id"),
        ),
        active_intent=intent,
        break_state=V6State(
            tranches=break_doc.get("tranches", 0),
            awaiting=break_doc.get("awaiting", False),
            break_level=break_doc.get("break_level"),
            neg_streak=break_doc.get("neg_streak", 0),
            recovered=break_doc.get("recovered", False),
            vol_hist=tuple(break_doc.get("vol_hist", ())),
        ),
        pending_action_token=compatibility.get("pending_action_token"),
        last_action_outcome=document.get("last_action_outcome"),
    )
    # Re-run the byte limit after normalization so non-canonical oversized input cannot slip in.
    state_document(state)
    return state


def render_thought(action: V6Action, reasons: tuple[str, ...]) -> str:
    return f"{action.value}: {', '.join(reasons) if reasons else 'no_trigger'}"


def _v2_policy_document() -> dict:
    """Every effective policy parameter, frozen. Changing any value changes ``policy_hash()``."""
    return {
        "engine_policy_id": POLICY_ID,
        "engine_evidence_mode": "per_rule",
        "evidence_semantics": {
            "route_degraded_statuses": ["route_absent"],
            "cancel_active_intent_on_healthy_no_break": True,
            "observation_identity": "semantic_sequence_v1",
            "unknown_quote_units_fail_closed": True,
        },
        "engine_supported_networks": sorted(SUPPORTED_NETWORKS),
        "ordered_rules": [
            "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break"],
        "terminal_parameters": {
            "catastrophe_loss_pct": CATASTROPHE_LOSS_PCT,
            "catastrophe_confirmations": CATASTROPHE_CONFIRMATIONS,
            "adverse_quote_ratio_limit": ADVERSE_QUOTE_RATIO_LIMIT,
            "lp_minor_fraction": LP_MINOR_FRACTION,
            "lp_catastrophic_fraction": LP_CATASTROPHIC_FRACTION,
            "time_expiry_s": TIME_EXPIRY_S,
            "route_absent_min_checks": ROUTE_ABSENT_MIN_CHECKS,
            "route_absent_min_s": ROUTE_ABSENT_MIN_S,
            "supported_depth_capture_versions": list(SUPPORTED_DEPTH_CAPTURE_VERSIONS),
            "trusted_depth_provenance": list(TRUSTED_DEPTH_PROVENANCE),
        },
        "seed_policy_id": SEED_POLICY_ID,
        "seed_required_point_evidence": list(REQUIRED_POINT_EVIDENCE),
        "seed_observation_fields": list(SEED_OBSERVATION_FIELDS),
        "seed_parameters": {
            "early_s": EARLY_S,
            "frenzy_rate": FRENZY_RATE,
            "crowded_share": CROWDED_SHARE,
            "vol_percentile": VOL_P,
            "vol_min_observations": VOL_MIN_OBS,
            "max_tranches": MAX_TRANCHES,
            "negative_streak_confirm": NEG_STREAK_CONFIRM,
            "first_derisk_fraction_by_tier": {"0": 0.25, "1": 0.5},
            "confirmation_derisk_fraction": 0.5,
            "terminal_fraction": 1.0,
        },
    }


@dataclass(frozen=True)
class V6Policy:
    """One frozen decision policy with a byte-stable document."""

    policy_id: str
    document: Mapping[str, object]
    required_evidence: tuple[str, ...]
    ordered_rules: tuple[str, ...]
    params: Mapping[str, object]

    def hash(self) -> str:
        return fingerprint(dict(self.document))


_V2_DOCUMENT = _v2_policy_document()

POLICY_V2 = V6Policy(
    policy_id=POLICY_ID,
    document=_V2_DOCUMENT,
    required_evidence=REQUIRED_ENGINE_EVIDENCE,
    ordered_rules=tuple(_V2_DOCUMENT["ordered_rules"]),
    params={
        "catastrophe_loss_pct": CATASTROPHE_LOSS_PCT,
        "catastrophe_confirmations": CATASTROPHE_CONFIRMATIONS,
        "adverse_quote_ratio_limit": ADVERSE_QUOTE_RATIO_LIMIT,
        "lp_minor_fraction": LP_MINOR_FRACTION,
        "lp_catastrophic_fraction": LP_CATASTROPHIC_FRACTION,
        "time_expiry_s": TIME_EXPIRY_S,
        "route_absent_min_checks": ROUTE_ABSENT_MIN_CHECKS,
        "route_absent_min_s": ROUTE_ABSENT_MIN_S,
        "max_tranches": MAX_TRANCHES,
    },
)


def policy_document() -> dict:
    """Return a copy of the frozen v2 policy document."""
    return dict(POLICY_V2.document)


@lru_cache(maxsize=1)
def policy_hash() -> str:
    return POLICY_V2.hash()


def _validate_payload_values(payload: Mapping[str, object]) -> None:
    for field_name in (
            "is_break", "route_degraded", "source_wallet_exited", "depth_pull_flag",
            "tape_complete"):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{field_name} must be boolean or null")
    tier = payload.get("wallet_tier_adjustment")
    if tier is not None and (isinstance(tier, bool) or not isinstance(tier, int)):
        raise ValueError("wallet_tier_adjustment must be an integer or null")
    quote_status = payload.get("quote_status")
    if quote_status is not None and quote_status not in {
            "healthy", "route_absent", "provider_unavailable", "stale"}:
        raise ValueError("quote_status is invalid")
    source_status = payload.get("source_status")
    if source_status is not None and source_status not in {"holding", "exited"}:
        raise ValueError("source_status is invalid")

    def finite(field_name: str, *, minimum=None, maximum=None, strict_minimum=False):
        value = payload.get(field_name)
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise ValueError(f"{field_name} must be a finite number or null")
        if minimum is not None and (value <= minimum if strict_minimum else value < minimum):
            relation = ">" if strict_minimum else ">="
            raise ValueError(f"{field_name} must be {relation} {minimum} or null")
        if maximum is not None and value > maximum:
            raise ValueError(f"{field_name} must be <= {maximum} or null")

    finite("held_s", minimum=0)
    finite("arrival_rate", minimum=0)
    finite("buy_share", minimum=0, maximum=1)
    finite("realized_vol", minimum=0)
    finite("prior_low", minimum=0, strict_minimum=True)
    finite("price", minimum=0, strict_minimum=True)
    finite("chain_vol_p90", minimum=0)
    finite("executable_price", minimum=0, strict_minimum=True)
    finite("net")


def _validate_observation(observation: V6Observation) -> None:
    """Reject empty ids, unsupported networks, non-aware/non-parseable timestamps, and
    fingerprint mismatch. ``fingerprint()`` itself rejects non-finite payload numbers
    (``allow_nan=False``) before the comparison below can even run. ``None`` values are allowed —
    they are handled as missing evidence, not as a validation error."""
    if not observation.observation_id:
        raise ValueError("observation_id must not be empty")
    if observation.network_id not in SUPPORTED_NETWORKS:
        raise ValueError(f"unsupported network_id: {observation.network_id!r}")
    try:
        parsed = datetime.fromisoformat(observation.observed_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"observed_at is not a parseable timestamp: {observation.observed_at!r}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"observed_at must be timezone-aware: {observation.observed_at!r}")
    _validate_payload_values(observation.payload)
    body = {
        "observation_id": observation.observation_id,
        "observed_at": observation.observed_at,
        "network_id": observation.network_id,
        "payload": dict(observation.payload),
        "evidence": {
            "market": observation.evidence.market,
            "quote": observation.evidence.quote,
            "source": observation.evidence.source,
        },
    }
    if fingerprint(body) != observation.input_fingerprint:
        raise ValueError("observation input_fingerprint mismatch")


def semantic_observation_id(position_id: str, evidence: EvidenceIdentity,
                            sequence: int | None = None) -> str:
    if not position_id:
        raise ValueError("position_id must not be empty")
    if evidence.market is None and evidence.quote is None and evidence.source is None:
        raise ValueError("at least one semantic evidence identity is required")
    if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0):
        raise ValueError("observation sequence must be a positive integer or null")
    return "obs-" + fingerprint({
        "position_id": position_id,
        "evidence": {"market": evidence.market, "quote": evidence.quote,
                     "source": evidence.source},
        "sequence": sequence,
    })[:24]


def make_observation(*, observation_id: str, observed_at: str,
                      network_id: str, payload: Mapping[str, object],
                      evidence: EvidenceIdentity | None = None) -> V6Observation:
    # Transitional compatibility for sealed fixtures/readers. The live builder always supplies
    # explicit semantic lanes; direct historical callers retain their stable observation id.
    evidence = evidence or EvidenceIdentity(market=f"legacy:{observation_id}")
    body = {"observation_id": observation_id, "observed_at": observed_at,
            "network_id": network_id, "payload": dict(payload),
            "evidence": {"market": evidence.market, "quote": evidence.quote,
                         "source": evidence.source}}
    observation = V6Observation(
        observation_id=observation_id, observed_at=observed_at, network_id=network_id,
        payload=dict(payload), input_fingerprint=fingerprint(body), evidence=evidence,
    )
    _validate_observation(observation)
    return observation


def _missing_capture(
    payload: Mapping[str, object], policy: V6Policy = POLICY_V2,
) -> list[str]:
    """Union of the seed's absence-only check and an engine-level None-aware check over the
    5-field required-capture set (REQUIRED_POINT_EVIDENCE + executable_price)."""
    missing = set(_seed_missing_evidence(payload))
    for field_name in policy.required_evidence:
        if field_name not in payload or payload[field_name] is None:
            missing.add(field_name)
    for field_name in SEED_OBSERVATION_FIELDS:
        if field_name not in payload:
            missing.add(field_name)
    if payload.get("is_break") is None:
        missing.add("is_break")
    if payload.get("held_s") is None:
        missing.add("held_s")
    return sorted(missing)


def _action_token(*, position_id: str, observation: V6Observation, p_hash: str,
                   action: V6Action, fraction_ppm: int) -> str:
    return fingerprint({
        "position_id": position_id,
        "observation_id": observation.observation_id,
        "evidence": {
            "market": observation.evidence.market,
            "quote": observation.evidence.quote,
            "source": observation.evidence.source,
        },
        "policy_hash": p_hash,
        "action": action.value,
        "fraction_ppm": fraction_ppm,
    })


def merge_action_intent(state: V6PositionState, candidate: ActionIntent) -> ActionIntent:
    """Keep one active intent; only a stronger action or larger equal-priority fraction replaces it."""
    active = state.active_intent
    if active is None:
        return candidate
    active_strength = (active.priority, active.fraction_ppm)
    candidate_strength = (candidate.priority, candidate.fraction_ppm)
    return candidate if candidate_strength > active_strength else active


def _insufficient(rule_id: str, *fields: str) -> V6RuleResult:
    return V6RuleResult(
        rule_id=rule_id, status=RuleStatus.INSUFFICIENT,
        reason_codes=tuple(f"{rule_id}:insufficient:{name}" for name in fields),
        missing_fields=tuple(fields),
    )


def _advance_route_absence(state: V6PositionState, observation: V6Observation,
                           quote_is_new: bool) -> RouteAbsenceState:
    route = state.route_absence
    if not quote_is_new:
        return route
    status = observation.payload.get("quote_status")
    if status == "healthy":
        return RouteAbsenceState()
    if status != "route_absent":
        return route
    return RouteAbsenceState(
        first_absent_at=route.first_absent_at or observation.observed_at,
        checks=route.checks + 1,
        last_quote_evidence_id=observation.evidence.quote,
    )


def evaluate_lp_loss(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    payload = observation.payload
    missing = []
    if payload.get("depth_pull_flag") is None:
        missing.append("depth_pull_flag")
    if payload.get("tape_complete") is not True:
        missing.append("tape_complete")
    provenance = payload.get("depth_provenance") or payload.get("provenance")
    if provenance not in TRUSTED_DEPTH_PROVENANCE:
        missing.append("depth_provenance")
    capture = str(payload.get("capture_version") or "")
    if not any(capture.startswith(version) for version in SUPPORTED_DEPTH_CAPTURE_VERSIONS):
        missing.append("capture_version")
    if observation.evidence.market is None:
        missing.append("market_evidence_id")
    if missing:
        return _insufficient("lp_loss", *missing)
    if payload.get("depth_pull_flag") is False:
        return V6RuleResult("lp_loss", RuleStatus.NOT_TRIGGERED)
    fraction = payload.get("liquidity_removed_fraction")
    if fraction is None:
        return _insufficient("lp_loss", "liquidity_removed_fraction")
    severity = classify_lp_removal(
        fraction,
        minor_frac=policy.params["lp_minor_fraction"],
        catastrophic_frac=policy.params["lp_catastrophic_fraction"],
    )
    if severity == LP_MATERIAL:
        return V6RuleResult(
            "lp_loss", RuleStatus.TRIGGERED, V6Action.DERISK, 500_000, 60,
            ("lp_loss:material",),
        )
    if severity != LP_CATASTROPHIC:
        return V6RuleResult(
            "lp_loss", RuleStatus.NOT_TRIGGERED,
            reason_codes=("lp_loss:minor",),
        )
    return V6RuleResult(
        "lp_loss", RuleStatus.TRIGGERED, V6Action.EXIT_FULL, FRACTION_PPM_SCALE, 100,
        ("lp_loss",),
    )


def evaluate_catastrophe(state: V6PositionState,
                         observation: V6Observation,
                         policy: V6Policy) -> V6RuleResult:
    payload = observation.payload
    missing = []
    if state.entry_unit_price is None:
        missing.append("entry_unit_price")
    if payload.get("quote_status") not in (None, "healthy"):
        missing.append("healthy_quote")
    if payload.get("executable_price") is None:
        missing.append("executable_price")
    quote_asset = payload.get("quote_out_asset")
    if state.entry_quote_asset and quote_asset != state.entry_quote_asset:
        missing.append("entry_quote_asset")
    if missing:
        return _insufficient("catastrophe", *missing)
    if state.entry_unit_price is None:
        return _insufficient("catastrophe", "entry_unit_price")
    current = Decimal(str(payload["executable_price"]))
    entry = Decimal(state.entry_unit_price)
    reference = (
        state.last_accepted_executable_price or state.executable_high_water
        or float(state.entry_unit_price)
    )
    ratio_limit = float(policy.params["adverse_quote_ratio_limit"])
    loss_level = _loss_level(state, policy)
    if loss_level is None:
        return _insufficient("catastrophe", "entry_unit_price")
    confirmation_count = int(policy.params["catastrophe_confirmations"])
    quarantined = float(current) * ratio_limit < float(reference)
    if current <= loss_level \
            and state.catastrophe_confirmations >= confirmation_count:
        return V6RuleResult(
            "catastrophe", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 90, ("catastrophe",),
        )
    if current <= loss_level:
        reasons = ["catastrophe:confirmation_pending"]
        if quarantined:
            reasons.append("catastrophe:quote_quarantined")
        return V6RuleResult(
            "catastrophe", RuleStatus.NOT_TRIGGERED,
            reason_codes=tuple(reasons))
    return V6RuleResult("catastrophe", RuleStatus.NOT_TRIGGERED)


def _loss_level(state: V6PositionState, policy: V6Policy) -> Decimal | None:
    if state.entry_unit_price is None:
        return None
    entry = Decimal(state.entry_unit_price)
    stop_bps = policy.params.get("exact_stop_bps")
    if stop_bps is None:
        loss_fraction = (
            Decimal("1")
            - Decimal(str(policy.params["catastrophe_loss_pct"])) / Decimal("100")
        )
        return entry * loss_fraction
    return entry * (1 + Decimal(str(stop_bps)) / 10_000)


def _advance_executable_quote(
    state: V6PositionState,
    observation: V6Observation,
    quote_is_new: bool,
    policy: V6Policy = POLICY_V2,
) -> V6PositionState:
    if not quote_is_new or observation.payload.get("quote_status") != "healthy":
        return state
    raw = observation.payload.get("executable_price")
    if raw is None:
        return state
    current = float(raw)
    entry = float(state.entry_unit_price) if state.entry_unit_price is not None else None
    reference = state.last_accepted_executable_price or state.executable_high_water or entry
    loss_level = _loss_level(state, policy)
    catastrophic = loss_level is not None and Decimal(str(current)) <= loss_level
    ratio_limit = float(policy.params["adverse_quote_ratio_limit"])
    implausible = reference is not None and current * ratio_limit < reference
    if catastrophic:
        candidate = state.catastrophe_candidate_price
        consistent = candidate is not None and 0.5 <= current / candidate <= 2.0
        confirmations = min(
            int(policy.params.get(
                "exact_stop_confirmations",
                policy.params.get("catastrophe_confirmations", 2),
            )),
            state.catastrophe_confirmations + 1 if consistent else 1,
        )
        # Do not let an implausible pending value poison accepted high-water state.
        return replace(
            state, catastrophe_candidate_price=current,
            catastrophe_confirmations=confirmations,
            last_accepted_executable_price=(
                state.last_accepted_executable_price if implausible else current),
        )
    return replace(
        state,
        executable_high_water=max(current, state.executable_high_water or current),
        last_accepted_executable_price=current,
        catastrophe_candidate_price=None,
        catastrophe_confirmations=0,
    )


def evaluate_route_loss(state: V6PositionState,
                        observation: V6Observation,
                        policy: V6Policy) -> V6RuleResult:
    route = state.route_absence
    status = observation.payload.get("quote_status")
    if status is None:
        return _insufficient("route_loss", "quote_status")
    if route.checks < int(policy.params["route_absent_min_checks"]) \
            or route.first_absent_at is None:
        return V6RuleResult("route_loss", RuleStatus.NOT_TRIGGERED)
    first = datetime.fromisoformat(route.first_absent_at.replace("Z", "+00:00"))
    current = datetime.fromisoformat(observation.observed_at.replace("Z", "+00:00"))
    if (current - first).total_seconds() < float(policy.params["route_absent_min_s"]):
        return V6RuleResult("route_loss", RuleStatus.NOT_TRIGGERED)
    return V6RuleResult(
        "route_loss", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 80, ("route_loss",),
    )


def evaluate_time_expiry(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    held_s = observation.payload.get("held_s")
    if held_s is None:
        return _insufficient("time_expiry", "held_s")
    if float(held_s) >= float(policy.params["time_expiry_s"]):
        return V6RuleResult(
            "time_expiry", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 70, ("time_expiry",),
        )
    return V6RuleResult("time_expiry", RuleStatus.NOT_TRIGGERED)


def evaluate_exact_stop(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    """Require two distinct healthy quotes for the exact stop."""
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("exact_stop", "healthy_quote")
    current = _executable_return_bps(state, observation)
    if current is None:
        return _insufficient("exact_stop", "executable_price")
    if current > int(policy.params["exact_stop_bps"]):
        return V6RuleResult("exact_stop", RuleStatus.NOT_TRIGGERED)
    if state.catastrophe_confirmations < int(
        policy.params["exact_stop_confirmations"],
    ):
        return V6RuleResult(
            "exact_stop", RuleStatus.NOT_TRIGGERED,
            reason_codes=("exact_stop:confirmation_pending",),
        )
    return V6RuleResult(
        "exact_stop", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 90, ("exact_stop",),
    )


def evaluate_creator_sale(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    del state, policy
    sold = observation.payload.get("creator_sale_since_entry")
    if sold is None:
        return _insufficient("creator_sale", "creator_sale_since_entry")
    if sold is not True:
        return V6RuleResult("creator_sale", RuleStatus.NOT_TRIGGERED)
    return V6RuleResult(
        "creator_sale", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 95, ("creator_sale",),
    )


def evaluate_time_cap(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    """Close at the cap or extend a strong position to the hard cap."""
    held_s = observation.payload.get("held_s")
    if held_s is None:
        return _insufficient("time_cap", "held_s")
    held = float(held_s)
    if held >= float(policy.params["time_extend_s"]):
        return V6RuleResult(
            "time_cap", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 70, ("time_cap:hard",),
        )
    if held < float(policy.params["time_cap_s"]):
        return V6RuleResult("time_cap", RuleStatus.NOT_TRIGGERED)
    net = observation.payload.get("net")
    current = _executable_return_bps(state, observation)
    strong = (
        observation.payload.get("is_break") is False
        and net is not None
        and float(net) > 0
        and current is not None
        and current >= 0
    )
    if strong:
        return V6RuleResult(
            "time_cap", RuleStatus.NOT_TRIGGERED,
            reason_codes=("time_cap:extended",),
        )
    return V6RuleResult(
        "time_cap", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 70, ("time_cap",),
    )


def _executable_return_bps(
    state: V6PositionState, observation: V6Observation,
) -> int | None:
    """Return the exact executable return from the entry price."""
    price = observation.payload.get("executable_price")
    if price is None or state.entry_unit_price is None:
        return None
    entry = Decimal(state.entry_unit_price)
    if entry <= 0:
        return None
    return int((Decimal(str(price)) - entry) * 10_000 / entry)


def evaluate_take_profit(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("take_profit", "healthy_quote")
    current = _executable_return_bps(state, observation)
    if current is None:
        return _insufficient("take_profit", "executable_price")
    if current < int(policy.params["take_profit_bps"]):
        return V6RuleResult("take_profit", RuleStatus.NOT_TRIGGERED)
    if state.filled_tranches >= 1:
        return V6RuleResult(
            "take_profit", RuleStatus.NOT_TRIGGERED,
            reason_codes=("take_profit:already_taken",),
        )
    return V6RuleResult(
        "take_profit", RuleStatus.TRIGGERED, V6Action.DERISK,
        int(policy.params["take_profit_fraction_ppm"]), 30, ("take_profit",),
    )


def evaluate_trail(
    state: V6PositionState, observation: V6Observation, policy: V6Policy,
) -> V6RuleResult:
    """Use the filled take-profit tranche to arm the executable trail."""
    if observation.payload.get("quote_status") not in (None, "healthy"):
        return _insufficient("trail", "healthy_quote")
    price = observation.payload.get("executable_price")
    if price is None or state.entry_unit_price is None \
            or state.executable_high_water is None:
        return _insufficient("trail", "executable_price")
    if state.filled_tranches < 1:
        return V6RuleResult(
            "trail", RuleStatus.NOT_TRIGGERED, reason_codes=("trail:unarmed",),
        )
    entry = Decimal(state.entry_unit_price)
    arm_level = entry * (
        1 + Decimal(str(policy.params["take_profit_bps"])) / 10_000
    )
    if Decimal(str(state.executable_high_water)) < arm_level:
        return V6RuleResult(
            "trail", RuleStatus.NOT_TRIGGERED, reason_codes=("trail:unarmed",),
        )
    giveback = Decimal(str(policy.params["trail_giveback_fraction"]))
    stop_level = Decimal(str(state.executable_high_water)) * (1 - giveback)
    if Decimal(str(price)) > stop_level:
        return V6RuleResult("trail", RuleStatus.NOT_TRIGGERED)
    return V6RuleResult(
        "trail", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
        FRACTION_PPM_SCALE, 40, ("trail",),
    )


def _staged_result(action: V6Action, fraction_ppm: int,
                   reasons: tuple[str, ...], missing: tuple[str, ...] = ()) -> V6RuleResult:
    if missing:
        return V6RuleResult(
            "staged_break", RuleStatus.INSUFFICIENT, reason_codes=reasons,
            missing_fields=missing)
    if action is V6Action.HOLD:
        return V6RuleResult(
            "staged_break", RuleStatus.NOT_TRIGGERED, reason_codes=reasons)
    return V6RuleResult(
        "staged_break", RuleStatus.TRIGGERED, action, fraction_ppm,
        20 if action is V6Action.EXIT_FULL else 10, reasons)


RULE_EVALUATORS = {
    "lp_loss": evaluate_lp_loss,
    "catastrophe": evaluate_catastrophe,
    "route_loss": evaluate_route_loss,
    "time_expiry": evaluate_time_expiry,
    "exact_stop": evaluate_exact_stop,
    "creator_sale": evaluate_creator_sale,
    "time_cap": evaluate_time_cap,
    "trail": evaluate_trail,
    "take_profit": evaluate_take_profit,
    "staged_break": _staged_result,
}


def resolve_rule_results(results: tuple[V6RuleResult, ...]) \
        -> tuple[V6Action, int, tuple[str, ...]]:
    staged = next(result for result in results if result.rule_id == "staged_break")
    triggered = [result for result in results if result.status is RuleStatus.TRIGGERED]
    if triggered:
        chosen = max(
            triggered,
            key=lambda result: (result.action is V6Action.EXIT_FULL,
                                result.priority, result.fraction_ppm),
        )
        diagnostics = tuple(
            code for code in staged.reason_codes if code.endswith(":insufficient"))
        return chosen.action, chosen.fraction_ppm, tuple(dict.fromkeys(
            (*chosen.reason_codes, *diagnostics)))
    diagnostics = tuple(
        code for result in results if result.status is not RuleStatus.INSUFFICIENT
        for code in result.reason_codes)
    return V6Action.HOLD, 0, tuple(dict.fromkeys((*staged.reason_codes, *diagnostics)))


def step(
    state: V6PositionState,
    observation: V6Observation,
    *,
    policy: V6Policy = POLICY_V2,
) -> V6Decision:
    _validate_observation(observation)
    if not state.position_id:
        raise ValueError("position_id must not be empty")
    if state.remaining_qty_atomic <= 0:
        raise ValueError("remaining_qty_atomic must be positive while V6 is deciding")
    p_hash = policy.hash()

    market_is_new = (observation.evidence.market is not None
                     and observation.evidence.market != state.last_market_evidence_id)
    quote_is_new = (observation.evidence.quote is not None
                    and observation.evidence.quote != state.last_quote_evidence_id)
    source_is_new = (observation.evidence.source is not None
                     and observation.evidence.source != state.last_source_evidence_id)
    if not (market_is_new or quote_is_new or source_is_new):
        reason_codes = ("semantic_noop",)
        action_token = _action_token(
            position_id=state.position_id, observation=observation, p_hash=p_hash,
            action=V6Action.HOLD, fraction_ppm=0)
        return V6Decision(
            action=V6Action.HOLD, fraction_ppm=0, action_token=action_token,
            state_if_filled=state, state_if_unfilled=state, reason_codes=reason_codes,
            thought=render_thought(V6Action.HOLD, reason_codes),
            input_fingerprint=observation.input_fingerprint, policy_hash=p_hash,
            semantic_transition=False,
            action_intent_token=(state.active_intent.token if state.active_intent else None),
        )

    event_kind = observation.payload.get("market_event_kind")
    structural_market_is_new = market_is_new and (
        event_kind is None or event_kind in {"v2_swap", "v3_swap", "sol_swap", "sol_curve_swap"}
    )
    staged_should_evaluate = (
        structural_market_is_new or source_is_new
        # Compatibility for explicitly injected observations that predate market identities.
        or (observation.evidence.market is None
            and state.last_market_evidence_id is None and quote_is_new)
    )

    prior_values = tuple(value for _, value in state.volatility_samples)
    seed_tranches = state.break_state.tranches
    if policy.params.get("take_profit_bps") is not None:
        seed_tranches = min(
            int(policy.params["max_tranches"]), state.filled_tranches,
        )
    seed_input = replace(
        state.break_state, tranches=seed_tranches, vol_hist=prior_values,
    )
    staged_missing = ()
    if observation.payload.get("tape_complete") is False:
        staged_missing = ("tape_complete",)
    elif observation.payload.get("is_break") is None:
        staged_missing = ("is_break",)
    elif observation.payload.get("is_break") and observation.payload.get("prior_low") is None:
        staged_missing = ("prior_low",)
    if not staged_should_evaluate:
        seed_action, seed_fraction, seed_new_state = None, 0.0, seed_input
        seed_reasons = ()
    elif staged_missing:
        seed_action, seed_fraction, seed_new_state = None, 0.0, seed_input
        seed_reasons = tuple(f"data_insufficient:{name}" for name in staged_missing)
    else:
        seed_action, seed_fraction, seed_new_state, seed_reasons = v6_step(
            seed_input, observation.payload)
        seed_reasons = tuple(seed_reasons)
    staged_diagnostics = []
    if len(prior_values) < VOL_MIN_OBS and observation.payload.get("chain_vol_p90") is None:
        staged_diagnostics.append("volatility:insufficient")
    if observation.payload.get("wallet_tier_adjustment") is None:
        staged_diagnostics.append("wallet_prior:insufficient")
    seed_reasons = (*seed_reasons, *staged_diagnostics)

    volatility_samples = state.volatility_samples
    realized_vol = observation.payload.get("realized_vol")
    if structural_market_is_new and realized_vol is not None:
        volatility_samples = (
            volatility_samples + ((observation.observation_id, float(realized_vol)),)
        )[-MAX_VOLATILITY_SAMPLES:]
    seed_new_state = replace(
        seed_new_state,
        vol_hist=tuple(value for _, value in volatility_samples),
        neg_streak=(seed_new_state.neg_streak
                    if structural_market_is_new or source_is_new
                    else state.break_state.neg_streak),
    )

    episode = state.break_episode
    if structural_market_is_new and observation.payload.get("is_break"):
        if episode is None or episode.status != "active":
            break_level = observation.payload.get("prior_low")
            if break_level is not None:
                episode = BreakEpisode(
                    episode_id=fingerprint({
                        "position_id": state.position_id,
                        "observation_id": observation.observation_id,
                        "market_evidence_id": observation.evidence.market,
                        "break_level": break_level,
                    }),
                    break_level=float(break_level),
                    first_market_evidence_id=observation.evidence.market,
                )
    if episode is not None and episode.status == "active":
        if "recovery" in seed_reasons:
            episode = replace(episode, status="recovered")
        elif any(reason in {"break_confirmation", "confirmation"}
                 for reason in seed_reasons):
            episode = replace(episode, status="confirmed")

    advanced_state = replace(
        state,
        volatility_samples=volatility_samples,
        last_market_evidence_id=(observation.evidence.market if market_is_new
                                 else state.last_market_evidence_id),
        last_quote_evidence_id=(observation.evidence.quote if quote_is_new
                                else state.last_quote_evidence_id),
        last_source_evidence_id=(observation.evidence.source if source_is_new
                                 else state.last_source_evidence_id),
        break_episode=episode,
        break_state=seed_new_state,
        route_absence=_advance_route_absence(state, observation, quote_is_new),
    )
    advanced_state = _advance_executable_quote(
        advanced_state, observation, quote_is_new, policy,
    )
    staged_action = SEED_ACTION_MAP[seed_action]
    staged_fraction_ppm = int(round(seed_fraction * FRACTION_PPM_SCALE))
    staged = _staged_result(
        staged_action, staged_fraction_ppm, seed_reasons, staged_missing)
    if policy.params.get("take_profit_bps") is not None \
            and state.filled_tranches >= 1 \
            and staged.status is RuleStatus.TRIGGERED:
        staged = V6RuleResult(
            "staged_break", RuleStatus.TRIGGERED, V6Action.EXIT_FULL,
            FRACTION_PPM_SCALE, 20,
            (*staged.reason_codes, "budget_exhausted"),
        )
    rule_results = tuple(
        RULE_EVALUATORS[rule_id](advanced_state, observation, policy)
        for rule_id in policy.ordered_rules
        if rule_id != "staged_break"
    ) + (staged,)
    action, fraction_ppm, reason_codes = resolve_rule_results(rule_results)
    if action is V6Action.HOLD and policy.params.get("take_profit_bps") is not None:
        insufficient_codes = tuple(
            code
            for result in rule_results
            if result.status is RuleStatus.INSUFFICIENT
            for code in result.reason_codes
        )
        reason_codes = tuple(sorted(set((*reason_codes, *insufficient_codes))))
    thought = render_thought(action, reason_codes)

    action_token = _action_token(position_id=state.position_id, observation=observation,
                                  p_hash=p_hash, action=action, fraction_ppm=fraction_ppm)
    action_intent_token = state.active_intent.token if state.active_intent else None
    supersedes_token = None

    candidate_intent = None
    candidate_suppressed = False
    if action is not V6Action.HOLD:
        trigger_evidence_id = (observation.evidence.market or observation.evidence.source
                               or observation.evidence.quote)
        candidate_intent = ActionIntent(
            token=action_token,
            action=action,
            fraction_ppm=fraction_ppm,
            priority=2 if action is V6Action.EXIT_FULL else 1,
            trigger_evidence_id=trigger_evidence_id,
        )
        merged_intent = merge_action_intent(state, candidate_intent)
        if merged_intent is not candidate_intent:
            # The new candidate is equivalent or weaker. Preserve the one already awaiting a fill;
            # this transition still advances causal facts but creates no second executable action.
            action = V6Action.HOLD
            fraction_ppm = 0
            reason_codes = (*reason_codes, "active_intent_retained")
            thought = render_thought(action, reason_codes)
            candidate_intent = None
            candidate_suppressed = True
        else:
            action_intent_token = merged_intent.token
            if state.active_intent is not None:
                supersedes_token = state.active_intent.token

    active_intent_invalidated = (
        action is V6Action.HOLD
        and not candidate_suppressed
        and state.active_intent is not None
        and observation.payload.get("quote_status") == "healthy"
        and observation.payload.get("is_break") is False
    )
    if active_intent_invalidated:
        supersedes_token = state.active_intent.token
        action_intent_token = None
        reason_codes = tuple(dict.fromkeys((*reason_codes, "recovery", "active_intent_cancelled")))
        thought = render_thought(action, reason_codes)

    if action is V6Action.HOLD:
        # Nothing to fill — there is no fork. Observation-driven fields (including the seed's
        # tranche-recovery cancellation) still advance; the pending token is untouched because no
        # new action was taken to await. state_if_filled and state_if_unfilled are the same value.
        hold_state = advanced_state
        if active_intent_invalidated:
            hold_state = replace(
                hold_state, active_intent=None, pending_action_token=None,
                last_action_outcome="cancelled",
            )
        if candidate_suppressed:
            hold_state = replace(
                advanced_state,
                break_state=replace(
                    seed_new_state, tranches=state.break_state.tranches,
                    awaiting=state.break_state.awaiting),
                filled_tranches=state.filled_tranches,
            )
        state_if_filled = hold_state
        state_if_unfilled = hold_state
    else:
        fill_qty = min(
            state.remaining_qty_atomic,
            max(1, (state.remaining_qty_atomic * fraction_ppm) // FRACTION_PPM_SCALE),
        )
        filled_tranches = seed_new_state.tranches
        take_profit_triggered = any(
            result.rule_id == "take_profit"
            and result.status is RuleStatus.TRIGGERED
            for result in rule_results
        )
        if take_profit_triggered:
            filled_tranches = min(
                int(policy.params["max_tranches"]), state.filled_tranches + 1,
            )
        state_if_filled = replace(
            advanced_state,
            remaining_qty_atomic=state.remaining_qty_atomic - fill_qty,
            filled_tranches=filled_tranches,
            active_intent=None,
            pending_action_token=None,
            last_action_outcome="filled",
        )
        # Roll the two action-budget fields (tranches, awaiting) back to their prior values — they
        # only advance when an action actually fills. Causal observation-history fields
        # (break_level, neg_streak, recovered, vol_hist) still advance from the seed's successor.
        state_if_unfilled = replace(
            advanced_state,
            break_state=replace(seed_new_state, tranches=state.break_state.tranches,
                                 awaiting=state.break_state.awaiting),
            filled_tranches=state.filled_tranches,
            active_intent=candidate_intent,
            pending_action_token=action_token,
            last_action_outcome="unfilled",
        )

    return V6Decision(
        action=action, fraction_ppm=fraction_ppm, action_token=action_token,
        state_if_filled=state_if_filled, state_if_unfilled=state_if_unfilled,
        reason_codes=reason_codes, thought=thought,
        input_fingerprint=observation.input_fingerprint, policy_hash=p_hash,
        action_intent_token=action_intent_token,
        supersedes_token=supersedes_token,
        rule_results=rule_results,
    )
