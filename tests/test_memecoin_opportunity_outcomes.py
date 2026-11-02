from __future__ import annotations

import hashlib

import pytest

from sniper.research.memecoin_opportunity.landmarks import (
    LandmarkState,
    LandmarkStatus,
    append_landmark,
)
from sniper.research.memecoin_opportunity.outcomes import (
    CompetingOutcome,
    CounterfactualEntry,
    ExecutableObservation,
    ExitObservationKind,
    OutcomeKind,
    append_competing_outcome,
    append_counterfactual_entry,
    evaluate_competing_outcome,
)
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    connect,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)


NOW = "2026-07-26T14:00:00Z"


def _entry(**overrides) -> CounterfactualEntry:
    values = {
        "entry_id": "entry-1",
        "landmark_id": "landmark-1",
        "clip_usd_atomic": 25_000_000,
        "entry_latency_ms": 150,
        "entry_observed_at": NOW,
        "entry_effective_at": "2026-07-26T14:00:00.150000Z",
        "acquired_token_atomic": 9_000_000_000_000_000_000_000,
        "entry_cost_usd_atomic": 25_000_000,
        "entry_price_impact_bps": 125,
        "quote_provider": "local-curve",
        "quote_version": "curve-v1",
    }
    values.update(overrides)
    return CounterfactualEntry(**values)


def _observation(
    elapsed_ms: int,
    net_return_bps: int | None,
    **overrides,
) -> ExecutableObservation:
    exit_kind = overrides.pop("exit_kind", ExitObservationKind.EXECUTABLE)
    values = {
        "observation_id": f"obs-{elapsed_ms}-{overrides.get('canonical_state_id', elapsed_ms)}",
        "entry_id": "entry-1",
        "elapsed_ms": elapsed_ms,
        "canonical_state_id": str(elapsed_ms),
        "observed_at": NOW,
        "exit_kind": exit_kind,
        "net_return_bps": net_return_bps,
        "net_proceeds_usd_atomic": (
            None if net_return_bps is None else 25_000_000 + net_return_bps * 2_500
        ),
        "source_healthy": True,
    }
    values.update(overrides)
    return ExecutableObservation(**values)


def _failure(elapsed_ms: int, kind: ExitObservationKind, **overrides):
    return _observation(
        elapsed_ms,
        None,
        exit_kind=kind,
        net_proceeds_usd_atomic=None,
        **overrides,
    )


def test_profit_crossed_before_loss_is_profit_first():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(5_000, 1_250), _observation(8_000, -900)],
    )

    assert outcome.outcome_kind == OutcomeKind.PROFIT_FIRST
    assert outcome.elapsed_ms == 5_000
    assert outcome.terminal_net_return_bps == 1_250


def test_loss_crossed_before_profit_is_loss_first():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(5_000, -850), _observation(8_000, 1_500)],
    )

    assert outcome.outcome_kind == OutcomeKind.LOSS_FIRST
    assert outcome.elapsed_ms == 5_000


def test_input_order_does_not_change_chronological_result():
    chronological = [_observation(1_000, 100), _observation(5_000, 1_250)]

    left = evaluate_competing_outcome(_entry(), chronological)
    right = evaluate_competing_outcome(_entry(), list(reversed(chronological)))

    assert right == left


def test_distinct_states_at_same_elapsed_time_are_deterministic():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _observation(1_000, 100, canonical_state_id="state-a"),
            _observation(1_000, 200, canonical_state_id="state-b"),
        ],
    )
    assert outcome.outcome_kind is OutcomeKind.CENSORED_DATA_GAP
    assert outcome.terminal_observation_id == "obs-1000-state-b"


def test_same_elapsed_time_uses_observation_time_before_content_identity():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _observation(
                1_000,
                1_300,
                canonical_state_id="state-a",
                observed_at="2026-07-26T14:00:01.900000Z",
            ),
            _observation(
                1_000,
                -900,
                canonical_state_id="state-z",
                observed_at="2026-07-26T14:00:01.100000Z",
            ),
        ],
    )

    assert outcome.outcome_kind is OutcomeKind.LOSS_FIRST
    assert outcome.terminal_observation_id == "obs-1000-state-z"


def test_deterministic_route_failure_is_terminal_immediately():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _failure(2_000, ExitObservationKind.DETERMINISTIC_FAILURE)],
    )

    assert outcome.outcome_kind == OutcomeKind.PERSISTENT_EXIT_FAILURE_FIRST
    assert outcome.elapsed_ms == 2_000
    assert outcome.last_executable_return_bps == 100


def test_one_transient_failure_then_recovery_is_not_exit_failure():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _observation(1_000, 100),
            _failure(2_000, ExitObservationKind.TRANSIENT_FAILURE),
            _observation(3_000, 200),
            _observation(60_000, 300),
        ],
    )

    assert outcome.outcome_kind == OutcomeKind.TIMEOUT
    assert outcome.last_executable_return_bps == 300


def test_two_distinct_healthy_transient_failures_are_persistent_exit_failure():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _failure(2_000, ExitObservationKind.TRANSIENT_FAILURE),
            _failure(3_000, ExitObservationKind.TRANSIENT_FAILURE),
        ],
    )

    assert outcome.outcome_kind == OutcomeKind.PERSISTENT_EXIT_FAILURE_FIRST
    assert outcome.elapsed_ms == 3_000


def test_unhealthy_source_censors_instead_of_calling_exit_failure():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _failure(
                2_000,
                ExitObservationKind.TRANSIENT_FAILURE,
                source_healthy=False,
            )
        ],
    )

    assert outcome.outcome_kind == OutcomeKind.CENSORED_DATA_GAP


def test_gap_between_executable_observations_censors_first_passage():
    outcome = evaluate_competing_outcome(
        _entry(),
        [
            _observation(1_000, 100),
            _failure(
                2_000,
                ExitObservationKind.TRANSIENT_FAILURE,
                source_healthy=False,
            ),
            _observation(3_000, 1_500),
        ],
    )

    assert outcome.outcome_kind == OutcomeKind.CENSORED_DATA_GAP
    assert outcome.elapsed_ms == 2_000


def test_migration_preserves_current_executable_return():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(2_000, 700, migration=True)],
    )

    assert outcome.outcome_kind == OutcomeKind.MIGRATION_FIRST
    assert outcome.terminal_net_return_bps == 700
    assert outcome.last_executable_return_bps == 700


def test_migration_without_prior_executable_quote_has_none_return():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_failure(2_000, ExitObservationKind.TRANSIENT_FAILURE, migration=True)],
    )

    assert outcome.outcome_kind == OutcomeKind.MIGRATION_FIRST
    assert outcome.terminal_net_return_bps is None
    assert outcome.last_executable_return_bps is None


def test_orphaned_state_returns_chain_reorg():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_failure(2_000, ExitObservationKind.TRANSIENT_FAILURE, orphaned=True)],
    )

    assert outcome.outcome_kind == OutcomeKind.CHAIN_REORG


def test_no_barrier_by_horizon_is_timeout():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(60_000, 300)],
    )

    assert outcome.outcome_kind == OutcomeKind.TIMEOUT
    assert outcome.elapsed_ms == 60_000


def test_barrier_reached_exactly_at_horizon_counts():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(60_000, 1_200)],
    )

    assert outcome.outcome_kind == OutcomeKind.PROFIT_FIRST


def test_observations_after_horizon_do_not_change_timeout():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(1_000, 100), _observation(61_000, 5_000)],
    )

    assert outcome.outcome_kind == OutcomeKind.TIMEOUT
    assert outcome.elapsed_ms == 60_000
    assert outcome.last_executable_return_bps == 100


def test_stopped_coverage_before_horizon_is_censored():
    outcome = evaluate_competing_outcome(_entry(), [_observation(1_000, 100)])

    assert outcome.outcome_kind == OutcomeKind.CENSORED_DATA_GAP


def test_default_barriers_are_plus_1200_minus_800_and_60_seconds():
    outcome = evaluate_competing_outcome(
        _entry(),
        [_observation(60_000, 100)],
    )

    assert (outcome.profit_barrier_bps, outcome.loss_barrier_bps, outcome.horizon_ms) == (
        1_200,
        -800,
        60_000,
    )


def test_alternative_barriers_produce_distinct_outcome_identity():
    observations = [_observation(30_000, 900)]

    primary = evaluate_competing_outcome(_entry(), observations)
    secondary = evaluate_competing_outcome(
        _entry(), observations, profit_barrier_bps=800, horizon_ms=30_000
    )

    assert primary.outcome_id != secondary.outcome_id
    assert secondary.outcome_kind == OutcomeKind.PROFIT_FIRST


def _raw_event(event_id: str, confirmation: ConfirmationState, order: int) -> RawEvent:
    payload = f'{{"event":"{event_id}"}}'
    return RawEvent(
        raw_event_id=f"raw-{event_id}",
        network_id=NetworkId.ROBINHOOD_4663,
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        source_key="4663:bags:provider-a",
        source_provider="provider-a",
        source_cursor=f"101:0:{order}",
        token_id="0xabc",
        transaction_id=f"0xtx{order}",
        chain_order=ChainOrder(101, 0, order, "0xblock"),
        chain_timestamp=NOW,
        received_at=NOW,
        confirmation=confirmation,
        raw_payload_json=payload,
        raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )


def _canonical_event(
    event_id: str,
    confirmation: ConfirmationState,
    order: int,
    event_kind: EventKind,
) -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=event_id,
        raw_event_id=f"raw-{event_id}",
        launch_id="launch-1",
        network_id=NetworkId.ROBINHOOD_4663,
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        token_id="0xabc",
        lifecycle=LifecycleState.PRE_MIGRATION,
        event_kind=event_kind,
        transaction_id=f"0xtx{order}",
        chain_order=ChainOrder(101, 0, order, "0xblock"),
        chain_timestamp=NOW,
        received_at=NOW,
        decoded_at=NOW,
        state_applied_at=NOW,
        confirmation=confirmation,
        actor_id="0xbuyer",
        quote_delta_atomic=1,
        token_delta_atomic=1,
        progress_bps=1_000,
        creator_linked=False,
        decoder_version="bags-4663-v1",
    )


def _append_event(conn, event_id, confirmation, order, event_kind=EventKind.BUY):
    append_raw_event(conn, _raw_event(event_id, confirmation, order))
    append_canonical_event(
        conn,
        _canonical_event(event_id, confirmation, order, event_kind),
    )


def _seed_landmark(conn, status: LandmarkStatus) -> LandmarkState:
    _append_event(conn, "event-launch", ConfirmationState.CONFIRMED, 1, EventKind.LAUNCH)
    upsert_launch_identity(
        conn,
        LaunchIdentity(
            "launch-1",
            NetworkId.ROBINHOOD_4663,
            VenueId.BAGS_ROBINHOOD_4663,
            "0xabc",
            "0xcreator",
            "event-launch",
            NOW,
        ),
    )
    confirmation = {
        LandmarkStatus.TENTATIVE: ConfirmationState.TENTATIVE,
        LandmarkStatus.CANONICAL: ConfirmationState.CONFIRMED,
        LandmarkStatus.INELIGIBLE: ConfirmationState.CONFIRMED,
    }[status]
    _append_event(conn, "event-observed", confirmation, 2)
    landmark = LandmarkState(
        landmark_id="landmark-1",
        launch_id="launch-1",
        observed_event_id="event-observed",
        observed_at=NOW,
        age_ms=1_000,
        progress_bps=200,
        crossed_time_ms=(1_000,),
        crossed_progress_bps=(200,),
        status=status,
        state_fingerprint="state-1",
        ineligible_reason="source_gap_open" if status == LandmarkStatus.INELIGIBLE else None,
    )
    append_landmark(conn, landmark)
    return landmark


def _append_successor(conn, prior: LandmarkState, status: LandmarkStatus) -> LandmarkState:
    confirmation = (
        ConfirmationState.CONFIRMED
        if status == LandmarkStatus.CANONICAL
        else ConfirmationState.ORPHANED
    )
    _append_event(conn, f"event-{status.value}", confirmation, 3)
    successor = LandmarkState(
        landmark_id=f"landmark-{status.value}",
        launch_id="launch-1",
        observed_event_id=f"event-{status.value}",
        observed_at=NOW,
        age_ms=1_000,
        progress_bps=200,
        crossed_time_ms=(),
        crossed_progress_bps=(),
        status=status,
        state_fingerprint=f"state-{status.value}",
        supersedes_landmark_id=prior.landmark_id,
    )
    append_landmark(conn, successor)
    return successor


def test_counterfactual_entry_persists_unbounded_atomics_as_text(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_landmark(conn, LandmarkStatus.CANONICAL)

    assert append_counterfactual_entry(conn, _entry()) is True
    assert append_counterfactual_entry(conn, _entry()) is False
    row = conn.execute(
        "SELECT acquired_token_atomic, typeof(acquired_token_atomic) FROM op_counterfactual_entries"
    ).fetchone()
    assert tuple(row) == ("9000000000000000000000", "text")


def test_ineligible_landmark_cannot_receive_counterfactual_entry(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_landmark(conn, LandmarkStatus.INELIGIBLE)

    with pytest.raises(ValueError, match="entry-eligible"):
        append_counterfactual_entry(conn, _entry())


def test_tentative_entry_cannot_receive_outcome_before_reconciliation(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_landmark(conn, LandmarkStatus.TENTATIVE)
    append_counterfactual_entry(conn, _entry())
    outcome = evaluate_competing_outcome(_entry(), [_observation(5_000, 1_200)])

    with pytest.raises(ValueError, match="reconciliation"):
        append_competing_outcome(conn, outcome)


def test_tentative_entry_accepts_economic_outcome_after_canonical_successor(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    prior = _seed_landmark(conn, LandmarkStatus.TENTATIVE)
    _append_successor(conn, prior, LandmarkStatus.CANONICAL)
    append_counterfactual_entry(conn, _entry())
    outcome = evaluate_competing_outcome(_entry(), [_observation(5_000, 1_200)])

    assert append_competing_outcome(conn, outcome) is True
    assert append_competing_outcome(conn, outcome) is False
    assert conn.execute(
        "SELECT evaluator_version FROM op_competing_outcomes"
    ).fetchone()[0] == "opportunity-outcome-v3"


def test_orphaned_tentative_entry_accepts_only_chain_reorg_outcome(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    prior = _seed_landmark(conn, LandmarkStatus.TENTATIVE)
    _append_successor(conn, prior, LandmarkStatus.ORPHANED)
    append_counterfactual_entry(conn, _entry())
    profit = evaluate_competing_outcome(_entry(), [_observation(5_000, 1_200)])
    reorg = evaluate_competing_outcome(
        _entry(),
        [_failure(2_000, ExitObservationKind.TRANSIENT_FAILURE, orphaned=True)],
    )

    with pytest.raises(ValueError, match="CHAIN_REORG"):
        append_competing_outcome(conn, profit)
    assert append_competing_outcome(conn, reorg) is True


def test_finalized_outcome_credits_the_launch_creator(tmp_path):
    """The prior is only worth anything if outcomes actually accumulate into it."""
    from sniper.research.memecoin_opportunity.store import read_creator_prior

    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_landmark(conn, LandmarkStatus.CANONICAL)
    append_counterfactual_entry(conn, _entry())

    assert append_competing_outcome(
        conn, evaluate_competing_outcome(_entry(), [_observation(5_000, 1_500)])
    ) is True

    prior = read_creator_prior(
        conn,
        network_id="4663",
        venue_id="bags_robinhood_4663",
        creator_id="0xcreator",
    )
    assert prior["samples"] == 1
    assert prior["mean_return_bps"] == 1_500


def test_censored_outcome_does_not_pollute_the_creator_prior(tmp_path):
    """A censored path says something about our capture, not about the creator."""
    from sniper.research.memecoin_opportunity.store import read_creator_prior

    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_landmark(conn, LandmarkStatus.CANONICAL)
    append_counterfactual_entry(conn, _entry())

    append_competing_outcome(
        conn,
        evaluate_competing_outcome(
            _entry(), [_observation(5_000, -300, source_healthy=False)]
        ),
    )

    assert read_creator_prior(
        conn,
        network_id="4663",
        venue_id="bags_robinhood_4663",
        creator_id="0xcreator",
    ) == {"samples": 0, "mean_return_bps": None}
