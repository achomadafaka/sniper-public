"""Event-driven opportunity state, landmark, quote, and Level-1 evaluation."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from .curve_math import (
    CONSTANT_PRODUCT_QUOTE_VERSION,
    ClipQuote,
    constant_product_clip_quote,
    quote_atomic_for_usd,
)
from .decoders import DecodedVenueEvent
from .directional import (
    ADAPTIVE_V3_RULES,
    CONFIRMATION_MICROSCOPE_RULES,
    CONFIRMATION_V3_RULES,
    CONFIRMATION_V4_RULES,
    CONFIRMATION_V5_RULES,
    CONFIRMATION_V6_LAUNCH_MODE,
    CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS,
    CONFIRMATION_V6_RULES,
    FORWARD_CANDIDATE_RULES,
    VENUE_CONFIRMATION_RULES,
    VENUE_CONTROL_RULES,
    DirectionalVerdict,
    RadarZone,
    confirmation_v3_labels,
    evaluate_forward_arm,
    evaluate_confirmation_v3,
    evaluate_confirmation_v6,
    evaluate_adaptive_v3,
    evaluate_level1,
    evaluate_venue_confirmation,
)
from .frenzy import FrenzyCeiling
from .identity import content_fingerprint, require_utc_iso
from .paper_adapter import (
    ADAPTER_VERSION as PAPER_ADAPTER_VERSION,
    FILL_MODEL_VERSION as PAPER_FILL_MODEL_VERSION,
    PAPER_CONTROL_SAMPLE_DENOMINATOR,
    PAPER_CONTROL_BY_VENUE,
    PAPER_RULE_BY_VENUE,
    PaperFillRequest,
    append_paper_fill_request,
    paper_request_id,
)
from .landmarks import (
    LaunchSnapshot,
    OutOfOrderObservation,
    append_landmark,
    materialize_landmarks,
)
from .outcomes import (
    CounterfactualEntry,
    ExecutableObservation,
    ExitObservationKind,
    append_competing_outcome,
    append_counterfactual_entry,
    append_executable_observation,
    evaluate_competing_outcome,
    load_executable_observations,
)
from .outcome_tracker import (
    active_forward_launch_ids,
    backfill_finalized_outcomes,
    record_launch_outcomes,
)
from .breadth_label import record_breadth_label
from .projection import (
    append_forward_alert_receipt,
    append_forward_alert_entry,
    forward_alert_id,
    persist_state_evaluation,
)
from .store import (
    append_forward_alert_features,
    network_for_venue,
    read_creator_prior,
    transaction,
)
from .state import (
    LaunchState,
    PUMP_SOL_QUOTE_MINT,
    PUMP_USDC_QUOTE_MINT,
    add_feature_derivatives,
    apply_venue_event,
    launch_state,
    liquidity_removed_fraction_bps,
    point_in_time_features,
    state_derived_features,
)
from .types import ConfirmationState, EventKind, VenueId
from .venue_policy import scoreable_venues


PRIMARY_CLIP_USD_MICROS = 25_000_000
ALERT_EVALUATOR_VERSION = "alert-outcome-v3"
ALERT_V4_EVALUATOR_VERSION = "alert-outcome-v4"
CLIP_LADDER_USD_MICROS = (5_000_000, 10_000_000, 25_000_000, 50_000_000)
ZONE_DISTANCE_BPS = {
    RadarZone.OUTER: 10_000,
    RadarZone.TRACKING: 7_500,
    RadarZone.APPROACHING: 5_000,
    RadarZone.INNER: 2_500,
    RadarZone.CENTER: 0,
}
ALERT_FEATURE_VERSION = "confirmation-features-v1"
CONFIRMATION_V3_FEATURE_VERSION = "confirmation-features-v2"
CONFIRMATION_V4_FEATURE_VERSION = "confirmation-features-v3"
REMOTE_SNAPSHOT_HISTORY_MAX = 256


def _launch_mode(state) -> str:
    """One derivation of the fee tier. The decision and the alert must not disagree."""
    if state.cashback_enabled:
        return "cashback"
    return "mayhem" if state.mayhem_mode else "standard"


def _paper_cohort_eligible(rule_version: str, features: Mapping[str, object], alert_id: str) -> bool:
    """Return whether this alert joins the realistic paper cohort.

    The arm always joins. The control joins only inside v6's own observable band, and
    then only on a deterministic 1-in-N sample, because a full control at 57 rows a day
    would exhaust the shared 2-second quote pace that the open positions need.
    """
    venue = str(features.get("venue_id") or "")
    if rule_version in ADAPTIVE_V3_RULES:
        return True
    if not venue and rule_version in {
        PAPER_RULE_BY_VENUE[VenueId.PUMPFUN_SOLANA.value],
        PAPER_CONTROL_BY_VENUE[VenueId.PUMPFUN_SOLANA.value],
    }:
        venue = VenueId.PUMPFUN_SOLANA.value
    if rule_version == PAPER_RULE_BY_VENUE.get(venue):
        return True
    if rule_version != PAPER_CONTROL_BY_VENUE.get(venue):
        return False
    if (
        venue == VenueId.PUMPFUN_SOLANA.value
        and features.get("launch_mode") != CONFIRMATION_V6_LAUNCH_MODE
    ):
        return False
    observed = features.get("observed_pair_return_bps")
    if observed is None or int(observed) < CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS:
        return False
    return int(alert_id, 16) % PAPER_CONTROL_SAMPLE_DENOMINATOR == 0


@dataclass(frozen=True)
class RemoteRoundTripProbe:
    entry_quote_ok: bool
    acquired_quantity_sell_ok: bool
    provider: str
    latency_ms: int
    blocker_code: str | None = None
    quote_in_atomic: int | None = None
    quote_mint: str | None = None
    tokens_out_atomic: int | None = None
    immediate_exit_quote_atomic: int | None = None
    entry_min_out_atomic: int | None = None
    immediate_exit_min_quote_atomic: int | None = None
    round_trip_return_bps: int | None = None
    minimum_round_trip_return_bps: int | None = None
    launch_id: str | None = None
    venue_id: str | None = None
    token_id: str | None = None
    state_fingerprint: str | None = None
    requested_state_fingerprint: str | None = None
    requested_block_identity: str | None = None
    queued_at: str | None = None
    entry_quoted_at: str | None = None
    exit_quoted_at: str | None = None
    completed_at: str | None = None
    consumed_at: str | None = None
    queue_delay_ms: int | None = None
    pair_age_ms: int | None = None
    result_age_ms: int | None = None
    block_identity: str | None = None
    route_binding: Mapping[str, object] | None = None


@dataclass(frozen=True)
class EngineObservation:
    snapshot_id: str
    launch_id: str
    zone: RadarZone
    would_fire: bool
    landmark_id: str | None
    blocker_codes: tuple[str, ...]


RemoteProbe = Callable[[LaunchState, ClipQuote], RemoteRoundTripProbe]


@dataclass(frozen=True)
class RemoteQuoteInput:
    quote_in_atomic: int
    quote_mint: str | None = None
    launch_id: str | None = None
    state_fingerprint: str | None = None
    requested_block_identity: str | None = None
    priority_distance_bps: int = 10_000
    inward_velocity_bps_per_s: int = 0


def _remote_curve_candidate(state: LaunchState) -> bool:
    return (
        (
            state.venue_id is VenueId.BAGS_SOLANA
            and set(state.blocker_codes) == {"UNSUPPORTED_CURVE_MATH"}
        )
        or (
            state.venue_id is VenueId.PONS_V1_ROBINHOOD_4663
            and state.eligible
        )
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def _post_request_evm_risk(current: LaunchState, requested: LaunchState) -> bool:
    removed = liquidity_removed_fraction_bps(current, requested)
    return (
        current.creator_sale_count > requested.creator_sale_count
        or current.pool_id != requested.pool_id
        or removed is None
        or int(removed) >= 500
    )


def _completion_time_is_valid(
    *,
    completed_at: str,
    requested_at: str,
    observed_at: str,
    max_age_ms: int,
) -> bool:
    requested = _parse(requested_at)
    completed = _parse(completed_at)
    observed = _parse(observed_at)
    return (
        requested <= completed <= observed
        and observed - completed <= timedelta(milliseconds=int(max_age_ms))
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _risk(verdict: DirectionalVerdict) -> str:
    reasons = set(verdict.reason_codes)
    if any("MIGRATION" in reason for reason in reasons):
        return "migration"
    if any("EXIT" in reason or "ROUTE" in reason for reason in reasons):
        return "exit_failure"
    if any("GAP" in reason or "MISSING" in reason or "UNSUPPORTED" in reason for reason in reasons):
        return "incomplete"
    if any("CREATOR" in reason or "CONCENTRATION" in reason or "LOSS" in reason for reason in reasons):
        return "dump"
    return "profit"


class OpportunityEngine:
    def __init__(
        self,
        conn,
        *,
        quote_usd_price_micros: Mapping[VenueId, int],
        quote_decimals: Mapping[VenueId, int],
        fee_bps: Mapping[VenueId, int],
        entry_latency_ms: Mapping[VenueId, int] | None = None,
        network_cost_quote_atomic: Mapping[VenueId, int] | None = None,
        quote_input_as_of: Mapping[VenueId, str] | None = None,
        remote_probe: RemoteProbe | None = None,
        clip_ladder_usd_micros: tuple[int, ...] = CLIP_LADDER_USD_MICROS,
        remote_completion_max_age_ms: int = 1_000,
        forward_arm_min_launched_at: str | None = None,
        paper_adapter_enabled: bool = False,
        adaptive_v3_enabled: bool = False,
        paper_adapter_expiry_s: float = 30.0,
    ):
        self.conn = conn
        # Default OFF: the adapter mints paper fills a separate consumer must admit.
        self.paper_adapter_enabled = bool(paper_adapter_enabled)
        self.adaptive_v3_enabled = bool(adaptive_v3_enabled)
        if paper_adapter_expiry_s <= 0:
            raise ValueError("paper_adapter_expiry_s must be > 0")
        self.paper_adapter_expiry_s = float(paper_adapter_expiry_s)
        self.quote_usd_price_micros = dict(quote_usd_price_micros)
        self.quote_decimals = dict(quote_decimals)
        self.fee_bps = dict(fee_bps)
        self.entry_latency_ms = dict(entry_latency_ms or {})
        self.network_cost_quote_atomic = dict(network_cost_quote_atomic or {})
        self.quote_input_as_of = dict(quote_input_as_of or {})
        self.remote_probe = remote_probe
        if remote_completion_max_age_ms <= 0:
            raise ValueError("remote_completion_max_age_ms must be > 0")
        self.remote_completion_max_age_ms = int(remote_completion_max_age_ms)
        self.forward_arm_min_launched_at = (
            require_utc_iso(forward_arm_min_launched_at)
            if forward_arm_min_launched_at is not None
            else None
        )
        self.clip_ladder = tuple(clip_ladder_usd_micros)
        self.states: dict[str, LaunchState] = {}
        self.last_observations: dict[str, LaunchSnapshot] = {}
        self.observation_by_fingerprint: OrderedDict[str, LaunchSnapshot] = OrderedDict()
        self.state_by_fingerprint: OrderedDict[str, LaunchState] = OrderedDict()
        self.features_by_fingerprint: OrderedDict[str, dict[str, object]] = OrderedDict()
        self.prior_features_by_fingerprint: OrderedDict[
            str, dict[str, object] | None
        ] = OrderedDict()
        self.last_landmark_states: dict[str, LaunchState] = {}
        self.prior_interval_features: dict[str, dict[str, object]] = {}
        self.last_interval_features: dict[str, dict[str, object]] = {}
        self.last_verdicts: dict[str, DirectionalVerdict] = {}
        self.frenzy_ceiling = FrenzyCeiling()

    def _remember_snapshot(self, point: LaunchSnapshot, state: LaunchState) -> None:
        key = point.state_fingerprint
        self.observation_by_fingerprint[key] = point
        self.state_by_fingerprint[key] = state
        self.observation_by_fingerprint.move_to_end(key)
        self.state_by_fingerprint.move_to_end(key)
        while len(self.observation_by_fingerprint) > REMOTE_SNAPSHOT_HISTORY_MAX:
            expired, _ = self.observation_by_fingerprint.popitem(last=False)
            self.state_by_fingerprint.pop(expired, None)
            self.features_by_fingerprint.pop(expired, None)
            self.prior_features_by_fingerprint.pop(expired, None)

    def _remember_features(
        self,
        state_fingerprint: str,
        current: Mapping[str, object],
        prior: Mapping[str, object] | None,
    ) -> None:
        self.features_by_fingerprint[state_fingerprint] = dict(current)
        self.prior_features_by_fingerprint[state_fingerprint] = (
            None if prior is None else dict(prior)
        )
        self.features_by_fingerprint.move_to_end(state_fingerprint)
        self.prior_features_by_fingerprint.move_to_end(state_fingerprint)

    def _forward_launch_eligible(self, state: LaunchState) -> bool:
        return state.venue_id in scoreable_venues() and (
            self.forward_arm_min_launched_at is None
            or _parse(state.launched_at) >= _parse(self.forward_arm_min_launched_at)
        )

    def _alert_features(
        self,
        state: LaunchState,
        current: Mapping[str, object],
        result: RemoteRoundTripProbe,
        *,
        quote_age_ms: int,
    ) -> dict[str, object]:
        market = self._market_input(state)
        keys = (
            "launch_age_ms",
            "decision_age_ms",
            "chain_state_age_ms",
            "progress_bps",
            "creator_prior_samples",
            "creator_prior_mean_bps",
            "new_eligible_buyers",
            "effective_buyer_hhi_change_bps",
            "buyer_retention_bps",
            "eligible_capital_velocity_atomic_per_s",
            "capital_acceleration_atomic_per_s2",
            "frenzy_ceiling_atomic_per_s2",
            "liquidity_removed_fraction_bps",
            "sell_pressure_bps",
            "creator_sale_since_prior",
            "protocol_agent_flow_interval_atomic",
            "confirmation_independent_pass",
            "confirmation_clean_pass",
            "confirmation_low_friction_pass",
            "modeled_round_trip_cost_bps",
            "observed_pair_return_bps",
            "observed_pair_floor_return_bps",
            "observed_pair_move_bps",
            "route_receipt_state_match",
        )
        features = {key: current.get(key) for key in keys}
        # The v4 spec names the persisted snapshot fields `eligible_*`; the state
        # reducer calls the same eligible-actor set `effective_*`. One value, one
        # source — no `independent_*` alias claiming entity clustering.
        features["eligible_buyer_count"] = current.get("effective_buyer_count")
        features["eligible_buyer_hhi_bps"] = current.get("effective_buyer_hhi_bps")
        features.update(
            venue_id=state.venue_id.value,
            quote_mint=state.quote_mint,
            effective_fee_bps=market[2] if market is not None else None,
            mayhem_mode=state.mayhem_mode,
            cashback_enabled=state.cashback_enabled,
            launch_mode=_launch_mode(state),
            quote_drag_bps=result.round_trip_return_bps,
            quote_age_ms=int(quote_age_ms),
            quote_latency_ms=int(result.latency_ms),
            quote_completed_at=result.completed_at,
        )
        return features

    def replace_market_inputs(self, inputs) -> None:
        """Atomically replace startup inputs so stale prices stop quote production."""
        self.quote_usd_price_micros = {
            venue: item.usd_price_micros for venue, item in inputs.items()
        }
        self.quote_decimals = {venue: item.quote_decimals for venue, item in inputs.items()}
        self.fee_bps = {venue: item.fee_bps for venue, item in inputs.items()}
        self.network_cost_quote_atomic = {
            venue: item.network_cost_quote_atomic for venue, item in inputs.items()
        }
        self.quote_input_as_of = {venue: item.price_as_of for venue, item in inputs.items()}

    def finalize_due_outcomes(
        self,
        observed_at: str,
        *,
        source_healthy_by_venue: Mapping[VenueId, bool] | None = None,
    ) -> tuple[str, ...]:
        """Finalize quiet-launch timeouts without waiting for another chain event."""
        now = require_utc_iso(observed_at)
        health = dict(source_healthy_by_venue or {})
        completed: list[str] = []
        forward_launches = set(active_forward_launch_ids(self.conn))
        for launch_id, state in tuple(self.states.items()):
            venue = state.venue_id
            market = self._market_input(state)
            price, decimals, fee, network_cost = market or (None, None, None, 0)
            if launch_id in forward_launches:
                completed.extend(
                    backfill_finalized_outcomes(
                        self.conn,
                        launch_id=launch_id,
                        quote_usd_price_micros=price,
                        quote_decimals=decimals,
                        fee_bps=fee,
                        network_cost_quote_atomic=network_cost,
                        source_healthy=health.get(venue, True),
                    )
                )
            self._materialize_due_entries(
                state,
                now,
                self._quotes(state) if state.eligible and health.get(venue, True) else {},
            )
            state_id = content_fingerprint(
                {"launch_id": launch_id, "timeout_sweep_at": now, "state_version": state.state_version}
            )
            completed.extend(
                record_launch_outcomes(
                    self.conn,
                    state=state,
                    canonical_state_id=state_id,
                    observed_at=now,
                    quote_usd_price_micros=price,
                    quote_decimals=decimals,
                    fee_bps=fee,
                    network_cost_quote_atomic=network_cost,
                    source_healthy=health.get(venue, True),
                    only_due=True,
                )
            )
        return tuple(completed)

    def censor_gapped_outcomes(
        self,
        observed_at: str,
        *,
        source_healthy_by_venue: Mapping[VenueId, bool],
    ) -> tuple[str, ...]:
        """Censor open paths at the first observed unhealthy source sweep."""
        now = require_utc_iso(observed_at)
        completed: list[str] = []
        for launch_id, state in tuple(self.states.items()):
            venue = state.venue_id
            if source_healthy_by_venue.get(venue, True):
                continue
            market = self._market_input(state)
            price, decimals, fee, network_cost = market or (None, None, None, 0)
            self._materialize_due_entries(
                state,
                now,
                self._quotes(state) if state.eligible else {},
            )
            state_id = content_fingerprint(
                {"launch_id": launch_id, "source_gap_at": now, "state_version": state.state_version}
            )
            completed.extend(
                record_launch_outcomes(
                    self.conn,
                    state=state,
                    canonical_state_id=state_id,
                    observed_at=now,
                    quote_usd_price_micros=price,
                    quote_decimals=decimals,
                    fee_bps=fee,
                    network_cost_quote_atomic=network_cost,
                    source_healthy=False,
                )
            )
        return tuple(completed)

    def quarantine_venue(
        self,
        venue_id: VenueId,
        *,
        observed_at: str,
    ) -> tuple[str, ...]:
        """Censor and forget launch state that crossed an unrecovered span."""
        venue = VenueId(venue_id)
        launch_ids = tuple(sorted(
            launch_id
            for launch_id, state in self.states.items()
            if state.venue_id is venue
        ))
        try:
            self.censor_gapped_outcomes(
                observed_at,
                source_healthy_by_venue={venue: False},
            )
        except Exception:
            # Reconstructed hot state can be incomplete after prior retention.
            # The immutable entry ledger below is the final quarantine truth.
            pass
        now = require_utc_iso(observed_at)
        if launch_ids:
            placeholders = ",".join("?" for _ in launch_ids)
            pending = self.conn.execute(
                "SELECT entry.* FROM op_counterfactual_entries entry "
                "JOIN op_landmark_states landmark ON landmark.landmark_id=entry.landmark_id "
                f"WHERE landmark.launch_id IN ({placeholders}) AND NOT EXISTS ("
                "SELECT 1 FROM op_competing_outcomes outcome "
                "WHERE outcome.entry_id=entry.entry_id) AND ("
                "landmark.status='canonical' OR EXISTS ("
                "SELECT 1 FROM op_landmark_states successor "
                "WHERE successor.supersedes_landmark_id=landmark.landmark_id "
                "AND successor.status IN ('canonical','orphaned')))",
                launch_ids,
            ).fetchall()
            for row in pending:
                entry = CounterfactualEntry(
                    entry_id=row["entry_id"],
                    landmark_id=row["landmark_id"],
                    clip_usd_atomic=int(row["clip_usd_atomic"]),
                    entry_latency_ms=row["entry_latency_ms"],
                    entry_observed_at=row["entry_observed_at"],
                    entry_effective_at=row["entry_effective_at"],
                    acquired_token_atomic=int(row["acquired_token_atomic"]),
                    entry_cost_usd_atomic=int(row["entry_cost_usd_atomic"]),
                    entry_price_impact_bps=row["entry_price_impact_bps"],
                    quote_provider=row["quote_provider"],
                    quote_version=row["quote_version"],
                )
                elapsed_ms = max(
                    0,
                    int(
                        (
                            _parse(now) - _parse(entry.entry_effective_at)
                        ).total_seconds()
                        * 1_000
                    ),
                )
                state_id = content_fingerprint({
                    "entry_id": entry.entry_id,
                    "source_gap_at": now,
                    "kind": "quarantine",
                })
                observation = ExecutableObservation(
                    observation_id=content_fingerprint({
                        "entry_id": entry.entry_id,
                        "canonical_state_id": state_id,
                    }),
                    entry_id=entry.entry_id,
                    elapsed_ms=elapsed_ms,
                    canonical_state_id=state_id,
                    observed_at=now,
                    exit_kind=ExitObservationKind.TRANSIENT_FAILURE,
                    net_return_bps=None,
                    net_proceeds_usd_atomic=None,
                    source_healthy=False,
                )
                append_executable_observation(
                    self.conn,
                    observation,
                    provider="opportunity-gap-quarantine-v1",
                    blocker_code="EVENT_GAP",
                )
                append_competing_outcome(
                    self.conn,
                    evaluate_competing_outcome(
                        entry,
                        load_executable_observations(self.conn, entry.entry_id),
                    ),
                )
        for launch_id in launch_ids:
            for mapping in (
                self.states,
                self.last_observations,
                self.last_landmark_states,
                self.last_interval_features,
                self.last_verdicts,
            ):
                mapping.pop(launch_id, None)
        return launch_ids

    def _quotes(self, state: LaunchState) -> dict[int, ClipQuote]:
        market = self._market_input(state)
        if market is None or not state.virtual_token_reserves or not state.virtual_quote_reserves:
            return {}
        usd_price_micros, quote_decimals, fee_bps, network_cost = market
        quotes = {}
        for clip in self.clip_ladder:
            quote_in = quote_atomic_for_usd(
                clip,
                usd_price_micros,
                quote_decimals,
            )
            quotes[clip] = constant_product_clip_quote(
                virtual_token_reserves=state.virtual_token_reserves,
                virtual_quote_reserves=state.virtual_quote_reserves,
                quote_in_atomic=quote_in,
                fee_bps=fee_bps,
                network_cost_quote_atomic=network_cost,
            )
        return quotes

    def _market_input(self, state: LaunchState) -> tuple[int, int, int, int] | None:
        """Resolve quote economics without treating every Pump curve as SOL-paired."""
        venue = state.venue_id
        if venue not in self.fee_bps:
            return None
        fee_bps = state.effective_fee_bps if state.effective_fee_bps is not None else self.fee_bps[venue]
        if venue is VenueId.PUMPFUN_SOLANA:
            if state.quote_mint == PUMP_USDC_QUOTE_MINT:
                sol_usd = self.quote_usd_price_micros.get(venue)
                if sol_usd is None:
                    return None
                sol_cost = self.network_cost_quote_atomic.get(venue, 0)
                usdc_cost = sol_cost * sol_usd // 1_000_000_000
                return 1_000_000, 6, fee_bps, usdc_cost
            if state.quote_mint != PUMP_SOL_QUOTE_MINT:
                return None
        if venue not in self.quote_usd_price_micros or venue not in self.quote_decimals:
            return None
        return (
            self.quote_usd_price_micros[venue],
            self.quote_decimals[venue],
            fee_bps,
            self.network_cost_quote_atomic.get(venue, 0),
        )

    def _materialize_due_entries(
        self,
        state: LaunchState,
        observed_at: str,
        quotes: Mapping[int, ClipQuote],
    ) -> None:
        """Use the first known venue state at or after modeled inclusion latency."""
        if not state.eligible:
            return
        primary = quotes.get(PRIMARY_CLIP_USD_MICROS)
        if primary is None or not primary.executable:
            return
        latency = self.entry_latency_ms.get(state.venue_id, 0)
        now = _parse(observed_at)
        rows = self.conn.execute(
            "SELECT lm.landmark_id,lm.observed_at FROM op_landmark_states lm "
            "WHERE lm.launch_id=? AND lm.status IN ('tentative','canonical') AND NOT EXISTS ("
            "SELECT 1 FROM op_counterfactual_entries ce WHERE ce.landmark_id=lm.landmark_id "
            "AND ce.clip_usd_atomic=?) ORDER BY lm.observed_at,lm.landmark_id",
            (state.launch_id, str(PRIMARY_CLIP_USD_MICROS)),
        ).fetchall()
        for row in rows:
            due = _parse(row["observed_at"]) + timedelta(milliseconds=latency)
            if now < due:
                continue
            effective_at = _iso(due)
            entry_id = content_fingerprint(
                {
                    "landmark_id": row["landmark_id"],
                    "clip": PRIMARY_CLIP_USD_MICROS,
                    "latency": latency,
                }
            )
            append_counterfactual_entry(
                self.conn,
                CounterfactualEntry(
                    entry_id=entry_id,
                    landmark_id=row["landmark_id"],
                    clip_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                    entry_latency_ms=latency,
                    entry_observed_at=row["observed_at"],
                    entry_effective_at=effective_at,
                    acquired_token_atomic=primary.tokens_out_atomic,
                    entry_cost_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                    entry_price_impact_bps=primary.entry_price_impact_bps,
                    quote_provider="local_curve_math_at_inclusion",
                    quote_version=CONSTANT_PRODUCT_QUOTE_VERSION,
                ),
            )

    def observe_remote_completion(
        self,
        result: RemoteRoundTripProbe,
        *,
        observed_at: str,
    ) -> EngineObservation | None:
        """Evaluate one fresh quote completion against the latest eligible state."""
        observed = require_utc_iso(observed_at)
        launch_id = str(result.launch_id or "")
        state = self.states.get(launch_id)
        point = self.last_observations.get(launch_id)
        prior_verdict = self.last_verdicts.get(launch_id)
        evm_exact_receipt = result.provider in {
            "bags_curve_eth_call_v2",
            "pons_v3_quoter_v1",
        }
        requested_fingerprint = (
            result.requested_state_fingerprint or result.state_fingerprint
        )
        requested_point = (
            self.observation_by_fingerprint.get(str(requested_fingerprint))
            if requested_fingerprint is not None else None
        )
        requested_state = (
            self.state_by_fingerprint.get(str(requested_fingerprint))
            if requested_fingerprint is not None else None
        )
        requested_features = (
            self.features_by_fingerprint.get(str(requested_fingerprint))
            if requested_fingerprint is not None else None
        )
        requested_prior_features = (
            self.prior_features_by_fingerprint.get(str(requested_fingerprint))
            if requested_fingerprint is not None else None
        )
        exact_route_receipt = result.provider in {
            "jupiter_quote_v3",
            "bags_curve_eth_call_v2",
            "pons_v3_quoter_v1",
        }
        jupiter_floor_receipt = result.provider == "jupiter_quote_v3"
        truth = self.conn.execute(
            "SELECT a.alert_id,link.quote_completed_at,ce.landmark_id,"
            "s.observed_event_id FROM op_forward_alerts a "
            "JOIN op_forward_alert_entries link USING(alert_id) "
            "JOIN op_counterfactual_entries ce ON ce.entry_id=link.entry_id "
            "JOIN op_state_snapshots s ON s.snapshot_id=a.snapshot_id "
            "WHERE a.launch_id=? AND a.rule_version='pump-confirmation-truth-v3' "
            "LIMIT 1",
            (launch_id,),
        ).fetchone()
        delayed_exists = self.conn.execute(
            "SELECT 1 FROM op_forward_alerts WHERE launch_id=? "
            "AND rule_version='pump-confirmation-delayed-v2'",
            (launch_id,),
        ).fetchone()
        delayed_measurement = bool(
            truth is not None
            and delayed_exists is None
            and result.completed_at is not None
            and point is not None
            and point.observed_event_id != truth["observed_event_id"]
            and _parse(require_utc_iso(result.completed_at))
            > _parse(truth["quote_completed_at"])
        )
        if evm_exact_receipt:
            if requested_point is None or requested_state is None \
                    or requested_features is None or state is None:
                return None
            if _post_request_evm_risk(state, requested_state):
                return None
        receipt_point = requested_point if evm_exact_receipt else point
        receipt_state = requested_state if evm_exact_receipt else state
        decision_prior_features = (
            requested_prior_features
            if evm_exact_receipt else self.prior_interval_features.get(launch_id)
        )
        if (
            state is None
            or point is None
            or prior_verdict is None
            or (
                prior_verdict.zone is not RadarZone.INNER
                and not delayed_measurement
            )
            or result.venue_id != state.venue_id.value
            or result.token_id != state.token_id
            or (evm_exact_receipt and result.quote_mint != state.quote_mint)
            or (
                result.provider in {"jupiter_quote_v2", "jupiter_quote_v3"}
                and result.quote_mint != state.quote_mint
            )
            or (
                jupiter_floor_receipt
                and (
                    result.entry_min_out_atomic is None
                    or result.immediate_exit_min_quote_atomic is None
                    or result.minimum_round_trip_return_bps is None
                )
            )
            or not result.entry_quote_ok
            or not result.acquired_quantity_sell_ok
            or result.quote_in_atomic is None
            or result.tokens_out_atomic is None
            or result.immediate_exit_quote_atomic is None
            or result.completed_at is None
        ):
            return None
        if not self._forward_launch_eligible(state):
            return None
        completed = require_utc_iso(result.completed_at)
        if receipt_point is None or receipt_state is None:
            return None
        if not _completion_time_is_valid(
            completed_at=completed,
            requested_at=receipt_point.observed_at,
            observed_at=observed,
            max_age_ms=self.remote_completion_max_age_ms,
        ):
            return None
        replayed_control = self.conn.execute(
            "SELECT 1 FROM op_forward_alerts a "
            "JOIN op_forward_alert_entries link USING(alert_id) "
            "WHERE a.launch_id=? AND a.rule_version='route-control-v1' "
            "AND link.quote_completed_at=? AND link.provider=?",
            (launch_id, completed, result.provider),
        ).fetchone()
        if replayed_control is not None:
            return None
        age_ms = int(round(
            (_parse(observed) - _parse(completed)).total_seconds() * 1_000
        ))
        candidate_rules = (
            *FORWARD_CANDIDATE_RULES,
            *CONFIRMATION_MICROSCOPE_RULES,
            *CONFIRMATION_V3_RULES,
            *CONFIRMATION_V4_RULES,
            *CONFIRMATION_V5_RULES,
            *CONFIRMATION_V6_RULES,
            *ADAPTIVE_V3_RULES,
            *VENUE_CONFIRMATION_RULES.values(),
            *VENUE_CONTROL_RULES.values(),
        )
        rule_placeholders = ",".join("?" for _ in candidate_rules)
        if not delayed_measurement and self.conn.execute(
            "SELECT 1 FROM op_forward_alerts WHERE launch_id=? "
            f"AND rule_version IN ({rule_placeholders})",
            (launch_id, *candidate_rules),
        ).fetchone() is not None:
            return None
        landmark = (
            {"landmark_id": truth["landmark_id"]}
            if delayed_measurement
            else self.conn.execute(
                "SELECT landmark_id FROM op_landmark_states "
                "WHERE launch_id=? AND observed_event_id=? "
                "AND status IN ('tentative','canonical') "
                "ORDER BY observed_at DESC,landmark_id DESC LIMIT 1",
                (launch_id, receipt_point.observed_event_id),
            ).fetchone()
        )
        if landmark is None:
            return None
        current = dict(
            requested_features
            if evm_exact_receipt else self.last_interval_features.get(launch_id) or {}
        )
        if not current or not current.get("eligible") or state.blocker_codes:
            return None
        quotes = self._quotes(receipt_state) if receipt_state.eligible else {}
        primary = quotes.get(PRIMARY_CLIP_USD_MICROS)
        if jupiter_floor_receipt and (
            primary is None
            or not primary.executable
            or primary.round_trip_return_bps is None
        ):
            return None
        if jupiter_floor_receipt and primary is not None:
            market = self._market_input(receipt_state)
            if market is None:
                return None
            price_micros, quote_decimals, _fee_bps, _network_cost = market
            modeled_proceeds_quote = max(
                0,
                primary.immediate_exit_quote_atomic - primary.network_cost_quote_atomic,
            )
            modeled_proceeds_usd = (
                modeled_proceeds_quote * price_micros // (10**quote_decimals)
            )
            modeled_cost_bps = (
                (modeled_proceeds_usd - PRIMARY_CLIP_USD_MICROS)
                * 10_000
                // PRIMARY_CLIP_USD_MICROS
            )
        else:
            modeled_cost_bps = result.round_trip_return_bps
            market = self._market_input(receipt_state)
            if market is None or result.immediate_exit_quote_atomic is None:
                return None
            price_micros, quote_decimals, _fee_bps, network_cost = market
            modeled_proceeds_quote = max(
                0,
                int(result.immediate_exit_quote_atomic) - int(network_cost),
            )
            modeled_proceeds_usd = (
                modeled_proceeds_quote * price_micros // (10**quote_decimals)
            )
        decision_age_ms = max(
            0,
            int(
                (_parse(observed) - _parse(receipt_state.launched_at)).total_seconds()
                * 1_000
            ),
        )
        chain_state_age_ms = max(
            0,
            int(
                (_parse(observed) - _parse(receipt_point.observed_at)).total_seconds()
                * 1_000
            ),
        )
        # The creator's record BEFORE this launch. Structurally causal: outcomes only
        # credit a creator after they finalize, which is after the decision that made
        # them, so this launch can never be in its own prior.
        creator_prior = read_creator_prior(
            self.conn,
            network_id=network_for_venue(receipt_state.venue_id),
            venue_id=receipt_state.venue_id.value,
            creator_id=receipt_state.creator_id,
        )
        current.update(
            {
                "venue_id": receipt_state.venue_id.value,
                # v6 gates on the fee tier, so the decision needs it, not only the alert.
                "launch_mode": _launch_mode(receipt_state),
                "creator_prior_samples": creator_prior["samples"],
                "creator_prior_mean_bps": creator_prior["mean_return_bps"],
                "launch_age_ms": decision_age_ms,
                "decision_age_ms": decision_age_ms,
                "chain_state_age_ms": chain_state_age_ms,
                "quote_age_ms": age_ms,
                "remote_entry_quote_ok": True,
                "remote_sell_simulation_ok": True,
                "remote_quote_blocker_code": None,
                "local_exit_exists": True,
                "immediate_round_trip_return_bps": modeled_cost_bps,
                "modeled_round_trip_cost_bps": (
                    modeled_cost_bps if exact_route_receipt else None
                ),
                "observed_pair_return_bps": (
                    result.round_trip_return_bps if exact_route_receipt else None
                ),
                "observed_pair_floor_return_bps": (
                    result.minimum_round_trip_return_bps if jupiter_floor_receipt else None
                ),
                "observed_pair_move_bps": (
                    int(result.round_trip_return_bps) - int(modeled_cost_bps)
                    if exact_route_receipt
                    and result.round_trip_return_bps is not None
                    and modeled_cost_bps is not None
                    else None
                ),
                "route_receipt_state_match": (
                    requested_fingerprint == receipt_point.state_fingerprint
                    if exact_route_receipt else None
                ),
                "effective_fee_bps": (
                    self._market_input(receipt_state)[2]
                    if self._market_input(receipt_state) is not None
                    else None
                ),
            }
        )
        if delayed_measurement:
            confirmation_verdict = None
        elif receipt_state.venue_id in {
            VenueId.BAGS_ROBINHOOD_4663,
            VenueId.PONS_V1_ROBINHOOD_4663,
        }:
            confirmation_verdict = evaluate_venue_confirmation(
                current,
                prior=decision_prior_features,
            )
        else:
            confirmation_verdict = evaluate_confirmation_v3(
                current, prior=decision_prior_features
            )
        if confirmation_verdict is not None \
                and receipt_state.venue_id is VenueId.PUMPFUN_SOLANA:
            current.update(confirmation_v3_labels(
                current,
                prior=decision_prior_features,
            ))
            if jupiter_floor_receipt:
                # v6 replaces v5 as the firing confirmation arm, exactly as v5 replaced
                # v4. A predecessor is never mutated -- raising its bar in place would
                # redefine what its accrued evidence meant -- so it keeps its history and
                # stops firing.
                confirmation_verdict = evaluate_confirmation_v6(
                    current, prior=decision_prior_features
                )
        adaptive_verdict = (
            evaluate_adaptive_v3(
                current,
                prior=decision_prior_features,
            )
            if self.adaptive_v3_enabled
            else None
        )
        # `pump-creator-prior-v1` RETIRED 2026-08-07 (operator approved). Its evidence
        # basis was retracted for target leakage the day it shipped (+392 bps at t=0.70
        # after resolution-ordering); the clean-universe rescan confirms the null
        # (creator_prior_mean t=0.5, n=196). Meanwhile the arm consumed the CONTROL's
        # slot on ~half of control-eligible launches AND biased the control cohort by
        # structurally excluding positive-prior creators from it. The slot returns to
        # route-control-v2; history keeps its rows and `evaluate_creator_prior_arm`
        # stays for them. The creator_prior_* FEATURES keep accruing -- they are
        # decision-time facts, not an arm.
        creator_prior_verdict = None
        if delayed_measurement:
            verdict = DirectionalVerdict(
                RadarZone.INNER,
                False,
                ("PUMP_CONFIRMATION_DELAYED_MEASUREMENT",),
                rule_version="pump-confirmation-delayed-v2",
                truth_label="DELAYED CONTROL — RESEARCH ONLY",
            )
        else:
            verdict = adaptive_verdict or confirmation_verdict or evaluate_forward_arm(
                current, prior=decision_prior_features
            ) or DirectionalVerdict(
                RadarZone.INNER,
                False,
                ("NO_FORWARD_ARM",),
                rule_version="radar-forward-v3",
            )
        current["zone_transition"] = (
            "INNER->CENTER" if verdict.zone is RadarZone.CENTER else None
        )
        elapsed_ms = max(
            1,
            int(
                (_parse(observed) - _parse(receipt_point.observed_at)).total_seconds()
                * 1_000
            ),
        )
        current["inward_velocity_bps_per_s"] = (
            (ZONE_DISTANCE_BPS[RadarZone.INNER] - ZONE_DISTANCE_BPS[verdict.zone])
            * 1_000
            // elapsed_ms
        )
        persisted_features = {
            key: value
            for key, value in current.items()
            if key not in {"eligible", "blocker_codes", "effective_fee_bps"}
        }
        # `current` describes the landmark this quote confirms; the snapshot below
        # stores the newest state, which a non-landmark event can have moved past.
        # Re-derive the state-owned keys so one snapshot never carries two answers
        # for the same field.
        persisted_features.update(
            (key, value)
            for key, value in state_derived_features(receipt_state).items()
            if key in persisted_features
        )
        persisted_features["radar_eligible"] = True
        latency_ms = (
            0
            if exact_route_receipt
            else self.entry_latency_ms.get(receipt_state.venue_id, 0)
        )
        effective_at = observed if exact_route_receipt else _iso(
            _parse(completed) + timedelta(milliseconds=latency_ms)
        )
        with transaction(self.conn):
            snapshot_id = persist_state_evaluation(
                self.conn,
                state=receipt_state,
                observed_event_id=receipt_point.observed_event_id,
                clip_quotes=quotes,
                verdict=verdict,
                observed_at=observed,
                dominant_risk=_risk(verdict),
                features=persisted_features,
                remote_probe=result,
                market_input_as_of=self.quote_input_as_of.get(receipt_state.venue_id),
                network_cost_quote_atomic=self.network_cost_quote_atomic.get(
                    receipt_state.venue_id, 0
                ),
                snapshot_state_version=(
                    f"{state.state_version}:remote:"
                    f"{content_fingerprint({'completed_at': completed, 'provider': result.provider})}"
                ),
                alert_round_trip_return_bps=modeled_cost_bps,
            )
            # `radar-buyer-breadth-v1` freezes this launch's breadth class HERE, before
            # arm selection. The branches below build no control arm when a confirmation
            # or an adaptive arm fires, and those arms need a high buyer count. A label
            # bound to the control receipt would therefore drop most `pass` rows and
            # bias the very cohort it measures. The label is not an arm: it appends
            # nothing to `arms` and takes no slot.
            record_breadth_label(
                self.conn,
                launch_id=launch_id,
                network_id=network_for_venue(receipt_state.venue_id),
                venue_id=receipt_state.venue_id.value,
                decision_snapshot_id=snapshot_id,
                features=self._alert_features(
                    receipt_state, current, result, quote_age_ms=age_ms
                ),
                observed_at=observed,
            )
            if delayed_measurement:
                arms = [(verdict.rule_version, verdict.truth_label)]
            elif confirmation_verdict is not None or adaptive_verdict is not None:
                arms = []
                if confirmation_verdict is not None:
                    arms.append((
                        confirmation_verdict.rule_version,
                        confirmation_verdict.truth_label,
                    ))
                if adaptive_verdict is not None:
                    arms.append((
                        adaptive_verdict.rule_version,
                        adaptive_verdict.truth_label,
                    ))
            else:
                # The creator arm takes the CONTROL's slot, never a slot beside it: one
                # decision must not produce two receipts with identical outcomes. It is
                # therefore disjoint from route-control-v2 exactly as truth-v4 is, and
                # the matched-control machinery pairs it against control rows from other
                # launches. Confirmation keeps precedence so its evidence is untouched,
                # and candidate arms are unaffected.
                arms = [(
                    (
                        creator_prior_verdict.rule_version,
                        creator_prior_verdict.truth_label,
                    )
                    if creator_prior_verdict is not None
                    else (
                        VENUE_CONTROL_RULES.get(
                            receipt_state.venue_id.value,
                            "route-control-v2" if jupiter_floor_receipt else "route-control-v1",
                        ),
                        "ROUTE CONTROL — DIAGNOSTIC PAPER" if exact_route_receipt else "ROUTE CONTROL — PAPER ONLY",
                    )
                )]
                if verdict.would_fire:
                    arms.append((verdict.rule_version, verdict.truth_label))
            for rule_version, truth_label in arms:
                alert_id = forward_alert_id(
                    launch_id,
                    rule_version,
                    PRIMARY_CLIP_USD_MICROS,
                )
                existing_alert = self.conn.execute(
                    "SELECT snapshot_id FROM op_forward_alerts WHERE alert_id=?",
                    (alert_id,),
                ).fetchone()
                if existing_alert is not None and existing_alert["snapshot_id"] != snapshot_id:
                    continue
                if existing_alert is None:
                    alert_id = append_forward_alert_receipt(
                        self.conn,
                        state=receipt_state,
                        snapshot_id=snapshot_id,
                        observed_at=observed,
                        clip_usd_micros=PRIMARY_CLIP_USD_MICROS,
                        rule_version=rule_version,
                        quote_provider=result.provider,
                        round_trip_return_bps=(
                            modeled_cost_bps if exact_route_receipt else result.round_trip_return_bps
                        ),
                        truth_label=truth_label,
                    )
                alert_features = self._alert_features(
                    receipt_state, current, result, quote_age_ms=age_ms
                )
                if delayed_measurement:
                    alert_features["paired_truth_alert_id"] = truth["alert_id"]
                append_forward_alert_features(
                    self.conn,
                    alert_id,
                    alert_features,
                    (
                        CONFIRMATION_V4_FEATURE_VERSION
                        if rule_version
                        in (
                            CONFIRMATION_V4_RULES
                            + CONFIRMATION_V5_RULES
                            + CONFIRMATION_V6_RULES
                            + tuple(VENUE_CONFIRMATION_RULES.values())
                            + tuple(VENUE_CONTROL_RULES.values())
                            + ADAPTIVE_V3_RULES
                        )
                        else CONFIRMATION_V3_FEATURE_VERSION
                        if rule_version in CONFIRMATION_V3_RULES
                        else ALERT_FEATURE_VERSION
                    ),
                )
                # The paper-adapter request rides this transaction on purpose. Written
                # afterwards it could be lost to a crash while the alert survived, and
                # a qualifying signal with no request is indistinguishable from one the
                # adapter chose not to quote. Written here, a crash leaves a request
                # with no receipt -- visible as `pending`, then swept to `expired`.
                if (
                    self.paper_adapter_enabled
                    and (primary is not None or result.tokens_out_atomic is not None)
                    and _paper_cohort_eligible(rule_version, alert_features, alert_id)
                ):
                    request_id = paper_request_id(
                        alert_id=alert_id,
                        adapter_version=PAPER_ADAPTER_VERSION,
                        fill_model_version=PAPER_FILL_MODEL_VERSION,
                        clip_usd_micros=PRIMARY_CLIP_USD_MICROS,
                    )
                    append_paper_fill_request(
                        self.conn,
                        PaperFillRequest(
                            request_id=request_id,
                            alert_id=alert_id,
                            launch_id=launch_id,
                            network_id=str(network_for_venue(receipt_state.venue_id)),
                            venue_id=receipt_state.venue_id.value,
                            token_id=receipt_state.token_id,
                            creator_id=receipt_state.creator_id,
                            launch_mode=str(alert_features["launch_mode"]),
                            state_fingerprint=receipt_point.state_fingerprint,
                            rule_version=rule_version,
                            evaluator_version=(
                                ALERT_V4_EVALUATOR_VERSION
                                if exact_route_receipt
                                else ALERT_EVALUATOR_VERSION
                            ),
                            adapter_version=PAPER_ADAPTER_VERSION,
                            fill_model_version=PAPER_FILL_MODEL_VERSION,
                            clip_usd_micros=PRIMARY_CLIP_USD_MICROS,
                            decision_at=observed,
                            decision_age_ms=decision_age_ms,
                            intent_at=observed,
                            expires_at=_iso(
                                _parse(observed)
                                + timedelta(seconds=self.paper_adapter_expiry_s)
                            ),
                            local_qty_atomic=int(
                                primary.tokens_out_atomic
                                if primary is not None else result.tokens_out_atomic
                            ),
                        ),
                    )
                if self.conn.execute(
                    "SELECT 1 FROM op_forward_alert_entries WHERE alert_id=?",
                    (alert_id,),
                ).fetchone() is not None:
                    continue
                entry_id = content_fingerprint(
                    {
                        "alert_id": alert_id,
                        "evaluator_version": (
                            ALERT_V4_EVALUATOR_VERSION
                            if exact_route_receipt
                            else ALERT_EVALUATOR_VERSION
                        ),
                        "entry_effective_at": effective_at,
                    }
                )
                append_counterfactual_entry(
                    self.conn,
                    CounterfactualEntry(
                        entry_id=entry_id,
                        landmark_id=landmark["landmark_id"],
                        clip_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                        entry_latency_ms=latency_ms,
                        entry_observed_at=(observed if exact_route_receipt else completed),
                        entry_effective_at=effective_at,
                        acquired_token_atomic=int(
                            primary.tokens_out_atomic
                            if jupiter_floor_receipt and primary else result.tokens_out_atomic
                        ),
                        entry_cost_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                        entry_price_impact_bps=(
                            primary.entry_price_impact_bps if primary else 0
                        ),
                        quote_provider=(
                            "local_curve_math_at_decision" if jupiter_floor_receipt else result.provider
                        ),
                        quote_version=(
                            CONSTANT_PRODUCT_QUOTE_VERSION
                            if jupiter_floor_receipt
                            else "remote-acquired-quantity-probe-v4"
                            if exact_route_receipt
                            else "remote-acquired-quantity-probe-v2"
                        ),
                    ),
                )
                append_forward_alert_entry(
                    self.conn,
                    alert_id=alert_id,
                    entry_id=entry_id,
                    snapshot_id=snapshot_id,
                    quote_observed_at=(result.entry_quoted_at or completed),
                    quote_completed_at=completed,
                    quote_age_ms=age_ms,
                    entry_effective_at=effective_at,
                    provider=result.provider,
                    evaluator_version=(
                        ALERT_V4_EVALUATOR_VERSION if exact_route_receipt else ALERT_EVALUATOR_VERSION
                    ),
                )
                if exact_route_receipt:
                    # The decision snapshot IS the elapsed-zero state. Keying the
                    # baseline on the snapshot lets `record_launch_outcomes` see it
                    # as already recorded; a synthetic id produced a second
                    # elapsed-zero row that only hash order could break ties on.
                    baseline_state_id = snapshot_id
                    append_executable_observation(
                        self.conn,
                        ExecutableObservation(
                            observation_id=content_fingerprint(
                                {
                                    "entry_id": entry_id,
                                    "canonical_state_id": baseline_state_id,
                                }
                            ),
                            entry_id=entry_id,
                            elapsed_ms=0,
                            canonical_state_id=baseline_state_id,
                            observed_at=observed,
                            exit_kind=ExitObservationKind.EXECUTABLE,
                            net_return_bps=int(modeled_cost_bps),
                            net_proceeds_usd_atomic=int(modeled_proceeds_usd),
                            source_healthy=True,
                        ),
                        provider="opportunity-path-v4",
                        blocker_code=None,
                    )
        if receipt_point.state_fingerprint == point.state_fingerprint:
            self.last_verdicts[launch_id] = verdict
            self.last_interval_features[launch_id] = current
        return EngineObservation(
            snapshot_id=snapshot_id,
            launch_id=launch_id,
            zone=verdict.zone,
            would_fire=verdict.would_fire,
            landmark_id=landmark["landmark_id"],
            blocker_codes=state.blocker_codes,
        )

    def observe_decoded(
        self,
        *,
        decoded: DecodedVenueEvent,
        canonical_event_id: str,
        confirmation: ConfirmationState,
        observed_at: str,
        source_gap_open: bool = False,
        router_ids: set[str] | None = None,
    ) -> EngineObservation:
        row = self.conn.execute(
            "SELECT launch_id,venue_id,chain_timestamp,block_or_slot FROM op_canonical_events "
            "WHERE canonical_event_id=?",
            (canonical_event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("canonical event must be persisted before state evaluation")
        launch_id, venue = row["launch_id"], VenueId(row["venue_id"])
        event_time = row["chain_timestamp"]
        explicit_blockers = {"EVENT_GAP"} if source_gap_open else set()
        if decoded.event_kind is EventKind.LAUNCH:
            state = launch_state(
                launch_id, venue, decoded, event_time, blocker_codes=explicit_blockers
            )
            self.last_landmark_states.setdefault(launch_id, state)
        else:
            prior_state = self.states.get(launch_id)
            if prior_state is None:
                raise ValueError("launch state is unavailable; restart reconstruction required")
            prior_point = self.last_observations.get(launch_id)
            if _parse(event_time) < _parse(prior_state.observed_at):
                prior_block = None
                if prior_point is not None:
                    prior_row = self.conn.execute(
                        "SELECT block_or_slot FROM op_canonical_events "
                        "WHERE canonical_event_id=?",
                        (prior_point.observed_event_id,),
                    ).fetchone()
                    prior_block = prior_row["block_or_slot"] if prior_row is not None else None
                if prior_block is not None and row["block_or_slot"] < prior_block:
                    raise OutOfOrderObservation("native chain order regressed")
                # Corrected receive/chain timestamps can move backward by a small
                # amount even while native slot order advances. Preserve event
                # order and make the interval zero instead of opening a fake gap.
                event_time = prior_state.observed_at
            prior_quotes = self._quotes(prior_state) if prior_state.eligible and not source_gap_open else {}
            self._materialize_due_entries(prior_state, observed_at, prior_quotes)
            state = apply_venue_event(
                prior_state,
                decoded,
                event_time,
                router_ids=router_ids or set(),
                blocker_codes=explicit_blockers,
            )
        current_quotes = self._quotes(state) if state.eligible and not source_gap_open else {}
        age_ms = max(0, int((_parse(event_time) - _parse(state.launched_at)).total_seconds() * 1000))
        state_fingerprint = content_fingerprint(
            {
                "state": state,
                "observed_event_id": canonical_event_id,
                "observed_at": observed_at,
            }
        )
        point = LaunchSnapshot(
            launch_id=launch_id,
            observed_event_id=canonical_event_id,
            observed_at=observed_at,
            age_ms=age_ms,
            progress_bps=state.progress_bps or 0,
            confirmation=confirmation,
            state_fingerprint=state_fingerprint,
            source_gap_open=source_gap_open,
            ineligible_reason=(
                None
                if _remote_curve_candidate(state)
                else state.blocker_codes[0] if state.blocker_codes else None
            ),
        )
        prior_point = self.last_observations.get(launch_id)
        landmark = materialize_landmarks(prior_point, point)
        self.states[launch_id] = state
        self.last_observations[launch_id] = point
        self._remember_snapshot(point, state)
        quotes: dict[int, ClipQuote] = {}
        remote_result = None
        persisted_features: dict[str, object] = {}
        if landmark is None:
            immediate_block = (
                bool(state.blocker_codes) and not _remote_curve_candidate(state)
            ) or decoded.event_kind is EventKind.MIGRATION or (
                decoded.event_kind is EventKind.SELL
                and decoded.actor_id is not None
                and state.creator_id is not None
                and decoded.actor_id == state.creator_id
            )
            if immediate_block:
                verdict = DirectionalVerdict(
                    RadarZone.OUTER,
                    False,
                    tuple(state.blocker_codes) or ("IMMEDIATE_RISK_INTERRUPT",),
                )
            else:
                verdict = self.last_verdicts.get(
                    launch_id,
                    DirectionalVerdict(RadarZone.OUTER, False, ("AWAITING_FIRST_LANDMARK",)),
                )
        else:
            append_landmark(self.conn, landmark)
            quotes = current_quotes
            primary = quotes.get(PRIMARY_CLIP_USD_MICROS)
            prior_landmark_state = self.last_landmark_states.get(launch_id)
            prior_interval_features = self.last_interval_features.get(launch_id)
            features = add_feature_derivatives(
                point_in_time_features(state, prior=prior_landmark_state),
                prior=prior_interval_features,
            )
            acceleration = features.get("capital_acceleration_atomic_per_s2")
            features["frenzy_ceiling_atomic_per_s2"] = self.frenzy_ceiling.ceiling(
                venue.value, age_ms,
            )
            self.frenzy_ceiling.observe(venue.value, age_ms, acceleration)
            current = {
                **features,
                "venue_id": venue.value,
                "quote_mint": state.quote_mint,
                "launch_age_ms": age_ms,
                "eligible": state.eligible or _remote_curve_candidate(state),
                "blocker_codes": (
                    () if _remote_curve_candidate(state) else features.get("blocker_codes", ())
                ),
                "local_exit_exists": bool(primary and primary.executable)
                or _remote_curve_candidate(state),
                "immediate_round_trip_return_bps": primary.round_trip_return_bps if primary else None,
                "remote_entry_quote_ok": False,
                "remote_sell_simulation_ok": False,
                "data_quality_bps": 10_000 if state.eligible else 0,
            }
            pre_current = current
            if _remote_curve_candidate(state) and current["immediate_round_trip_return_bps"] is None:
                pre_current = {**current, "immediate_round_trip_return_bps": 0}
            pre_verdict = evaluate_level1(
                pre_current,
                prior=prior_interval_features,
            )
            prior_verdict = self.last_verdicts.get(launch_id)
            elapsed_ms = current.get("interval_elapsed_ms")
            pre_inward_velocity = (
                (
                    ZONE_DISTANCE_BPS[prior_verdict.zone]
                    - ZONE_DISTANCE_BPS[pre_verdict.zone]
                )
                * 1_000
                // int(elapsed_ms)
                if prior_verdict is not None and elapsed_ms
                else 0
            )
            remote_input = (
                RemoteQuoteInput(
                    primary.quote_in_atomic,
                    quote_mint=state.quote_mint,
                    launch_id=launch_id,
                    state_fingerprint=state_fingerprint,
                    requested_block_identity=str(row["block_or_slot"]),
                    priority_distance_bps=ZONE_DISTANCE_BPS[pre_verdict.zone],
                    inward_velocity_bps_per_s=pre_inward_velocity,
                )
                if primary is not None
                else None
            )
            if remote_input is None and _remote_curve_candidate(state):
                if venue in self.quote_usd_price_micros and venue in self.quote_decimals:
                    remote_input = RemoteQuoteInput(
                        quote_atomic_for_usd(
                            PRIMARY_CLIP_USD_MICROS,
                            self.quote_usd_price_micros[venue],
                            self.quote_decimals[venue],
                        ),
                        quote_mint=state.quote_mint,
                        launch_id=launch_id,
                        state_fingerprint=state_fingerprint,
                        requested_block_identity=str(row["block_or_slot"]),
                        priority_distance_bps=ZONE_DISTANCE_BPS[pre_verdict.zone],
                        inward_velocity_bps_per_s=pre_inward_velocity,
                    )
            if (
                pre_verdict.zone is RadarZone.INNER
                and remote_input is not None
                and self.remote_probe is not None
            ):
                remote_result = self.remote_probe(state, remote_input)
                current["remote_entry_quote_ok"] = remote_result.entry_quote_ok
                current["remote_sell_simulation_ok"] = remote_result.acquired_quantity_sell_ok
                current["remote_quote_blocker_code"] = remote_result.blocker_code
                if remote_result.acquired_quantity_sell_ok:
                    current["local_exit_exists"] = True
                if remote_result.round_trip_return_bps is not None:
                    current["immediate_round_trip_return_bps"] = remote_result.round_trip_return_bps
            level1_input = (
                pre_current
                if _remote_curve_candidate(state)
                and current["immediate_round_trip_return_bps"] is None
                else current
            )
            level1_verdict = evaluate_level1(
                level1_input,
                prior=prior_interval_features,
            )
            verdict = (
                evaluate_forward_arm(current, prior=prior_interval_features)
                if level1_verdict.zone is RadarZone.CENTER
                and self._forward_launch_eligible(state)
                else None
            ) or (
                DirectionalVerdict(
                    RadarZone.INNER,
                    False,
                    ("NO_FORWARD_ARM",),
                    rule_version="radar-forward-v3",
                )
                if level1_verdict.zone is RadarZone.CENTER
                else level1_verdict
            )
            current["zone_transition"] = (
                f"{prior_verdict.zone.value}->{verdict.zone.value}"
                if prior_verdict is not None and prior_verdict.zone is not verdict.zone
                else None
            )
            current["inward_velocity_bps_per_s"] = (
                (
                    ZONE_DISTANCE_BPS[prior_verdict.zone]
                    - ZONE_DISTANCE_BPS[verdict.zone]
                )
                * 1_000
                // int(elapsed_ms)
                if prior_verdict is not None and elapsed_ms
                else None
            )
            if prior_interval_features is not None:
                self.prior_interval_features[launch_id] = prior_interval_features
            self.last_interval_features[launch_id] = current
            self._remember_features(
                state_fingerprint, current, prior_interval_features,
            )
            persisted_features = {
                key: value
                for key, value in current.items()
                if key not in {"eligible", "blocker_codes"}
            }
            persisted_features["radar_eligible"] = bool(current["eligible"])
            self.last_landmark_states[launch_id] = state
            latency = self.entry_latency_ms.get(venue, 0)
            if (
                latency == 0
                and primary
                and primary.executable
                and landmark.status.value in ("tentative", "canonical")
            ):
                entry_id = content_fingerprint(
                    {"landmark_id": landmark.landmark_id, "clip": PRIMARY_CLIP_USD_MICROS, "latency": latency}
                )
                append_counterfactual_entry(
                    self.conn,
                    CounterfactualEntry(
                        entry_id=entry_id,
                        landmark_id=landmark.landmark_id,
                        clip_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                        entry_latency_ms=latency,
                        entry_observed_at=observed_at,
                        entry_effective_at=observed_at,
                        acquired_token_atomic=primary.tokens_out_atomic,
                        entry_cost_usd_atomic=PRIMARY_CLIP_USD_MICROS,
                        entry_price_impact_bps=primary.entry_price_impact_bps,
                        quote_provider="local_curve_math",
                        quote_version=CONSTANT_PRODUCT_QUOTE_VERSION,
                    ),
                )
        self.last_verdicts[launch_id] = verdict
        snapshot_id = persist_state_evaluation(
            self.conn,
            state=state,
            observed_event_id=canonical_event_id,
            clip_quotes=quotes,
            verdict=verdict,
            observed_at=observed_at,
            dominant_risk=_risk(verdict),
            features=persisted_features,
            remote_probe=remote_result,
            market_input_as_of=self.quote_input_as_of.get(venue),
            network_cost_quote_atomic=self.network_cost_quote_atomic.get(venue, 0),
            alert_round_trip_return_bps=persisted_features.get(
                "immediate_round_trip_return_bps"
            ),
        )
        outcome_market = self._market_input(state)
        outcome_price, outcome_decimals, outcome_fee, outcome_network_cost = (
            outcome_market or (None, None, None, 0)
        )
        record_launch_outcomes(
            self.conn,
            state=state,
            canonical_state_id=snapshot_id,
            observed_at=observed_at,
            quote_usd_price_micros=outcome_price,
            quote_decimals=outcome_decimals,
            fee_bps=outcome_fee,
            network_cost_quote_atomic=outcome_network_cost,
            source_healthy=not source_gap_open,
        )
        return EngineObservation(
            snapshot_id=snapshot_id,
            launch_id=launch_id,
            zone=verdict.zone,
            would_fire=verdict.would_fire,
            landmark_id=landmark.landmark_id if landmark else None,
            blocker_codes=state.blocker_codes,
        )
