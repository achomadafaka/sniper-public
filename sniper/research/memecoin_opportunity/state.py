"""Point-in-time launch reducer and entity-adjusted demand features."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
import json
from typing import Iterable

from .decoders import DecodedVenueEvent
from .identity import require_utc_iso
from .types import EventKind, LifecycleState, VenueId


STATE_VERSION = "launch-state-v2"
PUMP_SOL_QUOTE_MINT = "11111111111111111111111111111111"
PUMP_USDC_QUOTE_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_QUOTE_MINTS = frozenset((PUMP_SOL_QUOTE_MINT, PUMP_USDC_QUOTE_MINT))
MAYHEM_AGENT_IDS = frozenset(("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",))


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def _progress(initial: int | None, current: int | None) -> int | None:
    if initial is None or current is None or initial <= 0:
        return None
    return max(0, min(10_000, (initial - current) * 10_000 // initial))


def _hhi(balance_items: tuple[tuple[str, int], ...]) -> int | None:
    positive = [amount for _, amount in balance_items if amount > 0]
    total = sum(positive)
    if total <= 0:
        return None
    return sum(amount * amount * 10_000 // (total * total) for amount in positive)


def _eligibility_blockers(
    *,
    venue_id: VenueId,
    token_id: str | None,
    creator_id: str | None,
    lifecycle: LifecycleState,
    pool_id: str | None,
    quote_mint: str | None,
    virtual_token_reserves: int | None,
    virtual_quote_reserves: int | None,
    explicit: Iterable[str],
) -> tuple[str, ...]:
    blockers = {str(code) for code in explicit}
    if not token_id or not creator_id:
        blockers.add("MISSING_TOKEN_OR_CREATOR_IDENTITY")
    if lifecycle is LifecycleState.UNRESOLVED:
        blockers.add("UNRESOLVED_LIFECYCLE")
    elif lifecycle is LifecycleState.MIGRATED:
        blockers.add("MIGRATION_FIRST")
    if venue_id is VenueId.PONS_V1_ROBINHOOD_4663:
        from .pons_4663 import PONS_WETH

        if lifecycle is not LifecycleState.ACTIVE_POOL or not pool_id:
            blockers.add("MISSING_V3_POOL_IDENTITY")
        if str(quote_mint or "").lower() != PONS_WETH:
            blockers.add("UNSUPPORTED_QUOTE_ASSET")
    elif not virtual_token_reserves or not virtual_quote_reserves:
        blockers.add("UNSUPPORTED_CURVE_MATH")
    return tuple(sorted(blockers))


@dataclass(frozen=True)
class LaunchState:
    launch_id: str
    venue_id: VenueId
    token_id: str
    curve_id: str | None
    creator_id: str | None
    launched_at: str
    observed_at: str
    lifecycle: LifecycleState
    event_count: int
    buy_count: int
    sell_count: int
    capital_in_atomic: int
    capital_out_atomic: int
    unique_buyers: tuple[str, ...]
    buyer_token_balances: tuple[tuple[str, int], ...]
    creator_sale_count: int
    virtual_token_reserves: int | None
    virtual_quote_reserves: int | None
    real_token_reserves: int | None
    real_quote_reserves: int | None
    initial_real_token_reserves: int | None
    progress_bps: int | None
    blocker_codes: tuple[str, ...]
    quote_mint: str | None = None
    mayhem_mode: bool = False
    cashback_enabled: bool = False
    effective_fee_bps: int | None = None
    independent_buy_count: int = 0
    independent_sell_count: int = 0
    independent_capital_in_atomic: int = 0
    independent_capital_out_atomic: int = 0
    protocol_agent_flow_atomic: int = 0
    pool_id: str | None = None
    v3_token_is_token0: bool | None = None
    v3_sqrt_price_x96: int | None = None
    v3_liquidity: int | None = None
    v3_tick: int | None = None
    state_version: str = STATE_VERSION

    @property
    def eligible(self) -> bool:
        return not self.blocker_codes

    @property
    def effective_buyer_hhi_bps(self) -> int | None:
        return _hhi(self.buyer_token_balances)

    @property
    def creator_sale_seen(self) -> bool:
        return self.creator_sale_count > 0


def launch_state_from_json(payload: str) -> LaunchState:
    """Restore the immutable launch state fields from a persisted snapshot."""
    values = json.loads(payload)
    allowed = {field.name for field in fields(LaunchState)}
    values = {key: value for key, value in values.items() if key in allowed}
    values.setdefault("quote_mint", None)
    values.setdefault("mayhem_mode", False)
    values.setdefault("cashback_enabled", False)
    values.setdefault("effective_fee_bps", None)
    values.setdefault("independent_buy_count", 0)
    values.setdefault("independent_sell_count", 0)
    values.setdefault("independent_capital_in_atomic", 0)
    values.setdefault("independent_capital_out_atomic", 0)
    values.setdefault("protocol_agent_flow_atomic", 0)
    values.setdefault("pool_id", None)
    values.setdefault("v3_token_is_token0", None)
    values.setdefault("v3_sqrt_price_x96", None)
    values.setdefault("v3_liquidity", None)
    values.setdefault("v3_tick", None)
    values["venue_id"] = VenueId(values["venue_id"])
    values["lifecycle"] = LifecycleState(values["lifecycle"])
    values["unique_buyers"] = tuple(values["unique_buyers"])
    values["buyer_token_balances"] = tuple(
        (str(identity), int(amount)) for identity, amount in values["buyer_token_balances"]
    )
    values["blocker_codes"] = tuple(values["blocker_codes"])
    return LaunchState(**values)


def launch_state(
    launch_id: str,
    venue_id: VenueId,
    event: DecodedVenueEvent,
    observed_at: str,
    *,
    blocker_codes: Iterable[str] = (),
) -> LaunchState:
    if event.event_kind is not EventKind.LAUNCH or not event.token_id:
        raise ValueError("launch_state requires a decoded launch with token identity")
    observed = require_utc_iso(observed_at)
    lifecycle = LifecycleState(event.lifecycle)
    quote_mint = event.facts.get("quote_mint")
    if venue_id is VenueId.PUMPFUN_SOLANA and quote_mint is None:
        quote_mint = PUMP_SOL_QUOTE_MINT
    explicit = set(blocker_codes)
    if (
        venue_id is VenueId.BAGS_ROBINHOOD_4663
        and event.facts.get("state_enrichment_status") == "unavailable"
    ):
        explicit.add("MISSING_BLOCK_SCOPED_STATE")
    if venue_id is VenueId.PUMPFUN_SOLANA and quote_mint not in PUMP_QUOTE_MINTS:
        explicit.add("UNSUPPORTED_QUOTE_ASSET")
    blockers = _eligibility_blockers(
        venue_id=venue_id,
        token_id=event.token_id,
        creator_id=event.creator_id,
        lifecycle=lifecycle,
        pool_id=event.pool_id,
        quote_mint=str(quote_mint) if quote_mint is not None else None,
        virtual_token_reserves=event.virtual_token_reserves,
        virtual_quote_reserves=event.virtual_quote_reserves,
        explicit=explicit,
    )
    return LaunchState(
        launch_id=launch_id,
        venue_id=VenueId(venue_id),
        token_id=event.token_id,
        curve_id=event.curve_id,
        creator_id=event.creator_id,
        launched_at=observed,
        observed_at=observed,
        lifecycle=lifecycle,
        event_count=1,
        buy_count=0,
        sell_count=0,
        capital_in_atomic=0,
        capital_out_atomic=0,
        unique_buyers=(),
        buyer_token_balances=(),
        creator_sale_count=0,
        virtual_token_reserves=event.virtual_token_reserves,
        virtual_quote_reserves=event.virtual_quote_reserves,
        real_token_reserves=event.real_token_reserves,
        real_quote_reserves=event.real_quote_reserves,
        initial_real_token_reserves=event.real_token_reserves,
        progress_bps=(
            event.progress_bps
            if event.progress_bps is not None
            else 0 if event.real_token_reserves else None
        ),
        blocker_codes=blockers,
        quote_mint=str(quote_mint) if quote_mint is not None else None,
        mayhem_mode=bool(event.facts.get("mayhem_mode", False)),
        cashback_enabled=bool(event.facts.get("cashback_enabled", False)),
        effective_fee_bps=None,
        independent_buy_count=0,
        independent_sell_count=0,
        independent_capital_in_atomic=0,
        independent_capital_out_atomic=0,
        protocol_agent_flow_atomic=0,
        pool_id=event.pool_id,
        v3_token_is_token0=(
            bool(event.facts["token_is_token0"])
            if "token_is_token0" in event.facts else None
        ),
        v3_sqrt_price_x96=(
            int(event.facts["sqrt_price_x96"])
            if "sqrt_price_x96" in event.facts else None
        ),
        v3_liquidity=(
            int(event.facts["liquidity"])
            if "liquidity" in event.facts else None
        ),
        v3_tick=(int(event.facts["tick"]) if "tick" in event.facts else None),
    )


def apply_venue_event(
    state: LaunchState,
    event: DecodedVenueEvent,
    observed_at: str,
    *,
    router_ids: Iterable[str] = (),
    blocker_codes: Iterable[str] = (),
) -> LaunchState:
    observed = require_utc_iso(observed_at)
    if _timestamp(observed) < _timestamp(state.observed_at):
        raise ValueError("event observation precedes current state")
    explicit = set(blocker_codes)
    if state.venue_id is VenueId.BAGS_ROBINHOOD_4663:
        enrichment_status = event.facts.get("state_enrichment_status")
        if enrichment_status == "unavailable" or (
            "MISSING_BLOCK_SCOPED_STATE" in state.blocker_codes
            and enrichment_status != "available"
        ):
            explicit.add("MISSING_BLOCK_SCOPED_STATE")
    if event.token_id != state.token_id:
        explicit.add("SOURCE_CONFLICT")
    routers = set(router_ids)
    buyers = list(state.unique_buyers)
    balances = dict(state.buyer_token_balances)
    buy_count, sell_count = state.buy_count, state.sell_count
    capital_in, capital_out = state.capital_in_atomic, state.capital_out_atomic
    independent_buy_count = state.independent_buy_count
    independent_sell_count = state.independent_sell_count
    independent_capital_in = state.independent_capital_in_atomic
    independent_capital_out = state.independent_capital_out_atomic
    protocol_agent_flow = state.protocol_agent_flow_atomic
    creator_sales = state.creator_sale_count
    protocol_agent = event.actor_id in MAYHEM_AGENT_IDS
    independent_actor = bool(
        event.actor_id
        and event.actor_id not in routers
        and event.actor_id != state.creator_id
        and not protocol_agent
    )
    if event.event_kind in (EventKind.BUY, EventKind.SELL) and (
        event.quote_delta_atomic is None or event.token_delta_atomic is None
    ):
        explicit.add("DECODER_FAILURE")
    if event.event_kind is EventKind.BUY:
        buy_count += 1
        capital_in += max(0, int(event.quote_delta_atomic or 0))
        if protocol_agent:
            protocol_agent_flow += max(0, int(event.quote_delta_atomic or 0))
        if independent_actor:
            independent_buy_count += 1
            independent_capital_in += max(0, int(event.quote_delta_atomic or 0))
            if event.actor_id not in buyers:
                buyers.append(event.actor_id)
            balances[event.actor_id] = balances.get(event.actor_id, 0) + max(
                0, -int(event.token_delta_atomic or 0)
            )
    elif event.event_kind is EventKind.SELL:
        sell_count += 1
        capital_out += max(0, -int(event.quote_delta_atomic or 0))
        if protocol_agent:
            protocol_agent_flow += max(0, -int(event.quote_delta_atomic or 0))
        if independent_actor:
            independent_sell_count += 1
            independent_capital_out += max(0, -int(event.quote_delta_atomic or 0))
        if event.actor_id and event.actor_id in balances:
            balances[event.actor_id] = max(
                0, balances[event.actor_id] - max(0, int(event.token_delta_atomic or 0))
            )
        if event.actor_id and state.creator_id and event.actor_id == state.creator_id:
            creator_sales += 1
    lifecycle = LifecycleState(event.lifecycle)
    virtual_token = (
        event.virtual_token_reserves
        if event.virtual_token_reserves is not None
        else state.virtual_token_reserves
    )
    virtual_quote = (
        event.virtual_quote_reserves
        if event.virtual_quote_reserves is not None
        else state.virtual_quote_reserves
    )
    real_token = (
        event.real_token_reserves
        if event.real_token_reserves is not None
        else state.real_token_reserves
    )
    real_quote = (
        event.real_quote_reserves
        if event.real_quote_reserves is not None
        else state.real_quote_reserves
    )
    creator = event.creator_id or state.creator_id
    quote_mint = event.facts.get("quote_mint", state.quote_mint)
    if state.venue_id is VenueId.PUMPFUN_SOLANA and quote_mint not in PUMP_QUOTE_MINTS:
        explicit.add("UNSUPPORTED_QUOTE_ASSET")
    effective_fee_bps = state.effective_fee_bps
    protocol_fee = event.facts.get("fee_basis_points")
    creator_fee = event.facts.get("creator_fee_basis_points")
    is_fee_free_mayhem_agent = (
        state.mayhem_mode
        and event.actor_id in MAYHEM_AGENT_IDS
        and int(protocol_fee or 0) == 0
        and int(creator_fee or 0) == 0
    )
    if (
        not is_fee_free_mayhem_agent
        and protocol_fee is not None
        and creator_fee is not None
    ):
        candidate_fee = int(protocol_fee) + int(creator_fee)
        if 0 <= candidate_fee < 10_000:
            effective_fee_bps = candidate_fee
    blockers = _eligibility_blockers(
        venue_id=state.venue_id,
        token_id=state.token_id,
        creator_id=creator,
        lifecycle=lifecycle,
        pool_id=event.pool_id or state.pool_id,
        quote_mint=str(quote_mint) if quote_mint is not None else None,
        virtual_token_reserves=virtual_token,
        virtual_quote_reserves=virtual_quote,
        explicit=explicit,
    )
    return replace(
        state,
        creator_id=creator,
        observed_at=observed,
        lifecycle=lifecycle,
        event_count=state.event_count + 1,
        buy_count=buy_count,
        sell_count=sell_count,
        capital_in_atomic=capital_in,
        capital_out_atomic=capital_out,
        unique_buyers=tuple(buyers),
        buyer_token_balances=tuple(sorted(balances.items())),
        creator_sale_count=creator_sales,
        virtual_token_reserves=virtual_token,
        virtual_quote_reserves=virtual_quote,
        real_token_reserves=real_token,
        real_quote_reserves=real_quote,
        progress_bps=(
            event.progress_bps
            if event.progress_bps is not None
            else _progress(state.initial_real_token_reserves, real_token)
        ),
        blocker_codes=blockers,
        quote_mint=str(quote_mint) if quote_mint is not None else None,
        effective_fee_bps=effective_fee_bps,
        independent_buy_count=independent_buy_count,
        independent_sell_count=independent_sell_count,
        independent_capital_in_atomic=independent_capital_in,
        independent_capital_out_atomic=independent_capital_out,
        protocol_agent_flow_atomic=protocol_agent_flow,
        pool_id=event.pool_id or state.pool_id,
        v3_token_is_token0=(
            bool(event.facts["token_is_token0"])
            if "token_is_token0" in event.facts else state.v3_token_is_token0
        ),
        v3_sqrt_price_x96=(
            int(event.facts["sqrt_price_x96"])
            if "sqrt_price_x96" in event.facts else state.v3_sqrt_price_x96
        ),
        v3_liquidity=(
            int(event.facts["liquidity"])
            if "liquidity" in event.facts else state.v3_liquidity
        ),
        v3_tick=(
            int(event.facts["tick"])
            if "tick" in event.facts else state.v3_tick
        ),
    )


def state_derived_features(current: LaunchState) -> dict[str, object]:
    """Feature keys that are a pure function of one state, no interval involved."""
    return {
        "eligible": current.eligible,
        "blocker_codes": current.blocker_codes,
        # `effective_*` is the eligible buyer set (no routers, creator, or Mayhem
        # agents). It is not entity-clustered: `op_entity_edges` is empty, so no
        # feature here may be named `independent_*`.
        "effective_buyer_hhi_bps": current.effective_buyer_hhi_bps,
        "effective_buyer_count": len(current.unique_buyers),
        "progress_bps": current.progress_bps,
        "creator_sale_seen": current.creator_sale_seen,
        "lifecycle": current.lifecycle.value,
    }


def _reserve(state: LaunchState) -> int | None:
    if state.real_quote_reserves is not None:
        return state.real_quote_reserves
    return state.v3_liquidity


def liquidity_removed_fraction_bps(
    current: LaunchState, prior: LaunchState,
) -> int | None:
    """Return the reserve reduction for one observation interval."""
    prior_reserve = _reserve(prior)
    current_reserve = _reserve(current)
    if not prior_reserve or current_reserve is None:
        return None
    return max(
        0,
        (prior_reserve - current_reserve) * 10_000 // prior_reserve,
    )


def point_in_time_features(
    current: LaunchState,
    *,
    prior: LaunchState | None,
) -> dict[str, object]:
    features: dict[str, object] = state_derived_features(current)
    hhi_change = (
        current.effective_buyer_hhi_bps - prior.effective_buyer_hhi_bps
        if prior is not None
        and current.effective_buyer_hhi_bps is not None
        and prior.effective_buyer_hhi_bps is not None
        else None
    )
    if prior is None:
        features.update(
            interval_elapsed_ms=None,
            capital_velocity_atomic_per_s=None,
            buyer_velocity_per_s_micros=None,
            sell_pressure_bps=None,
            effective_buyer_hhi_change_bps=None,
            creator_sale_since_prior=False,
            new_eligible_buyers=None,
            eligible_capital_velocity_atomic_per_s=None,
            protocol_agent_flow_interval_atomic=None,
            buyer_retention_bps=None,
            liquidity_removed_fraction_bps=None,
        )
        return features
    elapsed_us = int((_timestamp(current.observed_at) - _timestamp(prior.observed_at)).total_seconds() * 1_000_000)
    if elapsed_us <= 0:
        features.update(
            interval_elapsed_ms=None,
            capital_velocity_atomic_per_s=None,
            buyer_velocity_per_s_micros=None,
            sell_pressure_bps=None,
            effective_buyer_hhi_change_bps=hhi_change,
            creator_sale_since_prior=current.creator_sale_count > prior.creator_sale_count,
            new_eligible_buyers=None,
            eligible_capital_velocity_atomic_per_s=None,
            protocol_agent_flow_interval_atomic=None,
            buyer_retention_bps=None,
            liquidity_removed_fraction_bps=None,
        )
        return features
    capital_delta = max(0, current.capital_in_atomic - prior.capital_in_atomic)
    capital_out_delta = max(0, current.capital_out_atomic - prior.capital_out_atomic)
    total_flow = capital_delta + capital_out_delta
    buyer_delta = max(0, len(current.unique_buyers) - len(prior.unique_buyers))
    independent_capital_delta = max(
        0,
        current.independent_capital_in_atomic - prior.independent_capital_in_atomic,
    )
    protocol_agent_delta = max(
        0,
        current.protocol_agent_flow_atomic - prior.protocol_agent_flow_atomic,
    )
    prior_positive = {identity for identity, amount in prior.buyer_token_balances if amount > 0}
    current_positive = {identity for identity, amount in current.buyer_token_balances if amount > 0}
    features.update(
        interval_elapsed_ms=max(1, elapsed_us // 1_000),
        capital_velocity_atomic_per_s=capital_delta * 1_000_000 // elapsed_us,
        buyer_velocity_per_s_micros=buyer_delta * 1_000_000_000_000 // elapsed_us,
        sell_pressure_bps=(capital_out_delta * 10_000 // total_flow) if total_flow else None,
        effective_buyer_hhi_change_bps=hhi_change,
        creator_sale_since_prior=current.creator_sale_count > prior.creator_sale_count,
        new_eligible_buyers=buyer_delta,
        eligible_capital_velocity_atomic_per_s=(
            independent_capital_delta * 1_000_000 // elapsed_us
        ),
        protocol_agent_flow_interval_atomic=protocol_agent_delta,
        buyer_retention_bps=(
            len(prior_positive & current_positive) * 10_000 // len(prior_positive)
            if prior_positive else None
        ),
        liquidity_removed_fraction_bps=liquidity_removed_fraction_bps(
            current, prior,
        ),
    )
    return features


def add_feature_derivatives(
    features: dict[str, object],
    *,
    prior: dict[str, object] | None,
) -> dict[str, object]:
    """Add point-in-time acceleration without reading future launch state."""
    result = dict(features)
    elapsed_ms = result.get("interval_elapsed_ms")
    for output, velocity in (
        ("capital_acceleration_atomic_per_s2", "capital_velocity_atomic_per_s"),
        ("buyer_acceleration_per_s2_micros", "buyer_velocity_per_s_micros"),
    ):
        current_value = result.get(velocity)
        prior_value = prior.get(velocity) if prior else None
        result[output] = (
            (int(current_value) - int(prior_value)) * 1_000 // int(elapsed_ms)
            if current_value is not None and prior_value is not None and elapsed_ms
            else None
        )
    return result
